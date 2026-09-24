"""翻译层纯函数用例：chat_token 实证向量 / 请求体三处一致 / SSE 各形态 / BCE 签名。

断言的是**真正会发出去的东西**（query 与 md5 的绑定、三处能力字段的一致性、
kunlun 首帧熔断的提前中断），而不是「内层函数被调用了」。
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from app.errors import AuthError, UpstreamUnavailableError
from app.models import lookup
from app.upstream.baidu.capabilities import (
    apply_frame,
    bce_sign,
    build_body,
    build_chat_token,
    canon_query,
    extract_creds_from_homepage,
    extract_legacy_result,
    legacy_form,
    parse_sse,
    redact_payload,
    uri_encode,
)
from tests.helpers import (
    IMG_ITEM,
    PNG_SMALL,
    frame,
    hint_frame,
    homepage_html,
    kunlun_frame,
)

#: 契约 §1.3 的实证向量：md5("扩图") == e397abf2beb38bec0bf0c76a9a8f1f46
_KNOWN_MD5_KUOTU = "e397abf2beb38bec0bf0c76a9a8f1f46"
_KNOWN_LID = "8467947656174719911"


def _lines(frames: list[dict]) -> list[str]:
    return [f"data: {json.dumps(f, ensure_ascii=False)}" for f in frames]


# ---------------------------------------------------------------- chat_token


def test_chat_token_matches_the_captured_vector():
    """b64decode 后必须是 `<token>|<md5(query)>|<ts_ms>|<lid>`，尾部 `-<lid>-3`。"""
    tok = build_chat_token("34bf619b", "扩图", _KNOWN_LID)
    assert tok.endswith(f"-{_KNOWN_LID}-3")
    b64part = tok[: -(len(_KNOWN_LID) + 3)]
    raw = base64.b64decode(b64part).decode()
    session, md5, ts, lid = raw.split("|")
    assert session == "34bf619b"
    assert md5 == hashlib.md5("扩图".encode()).hexdigest() == _KNOWN_MD5_KUOTU
    assert ts.isdigit() and lid == _KNOWN_LID


def test_chat_token_md5_tracks_the_query_text():
    a = build_chat_token("t", "变清晰", "1")
    b = build_chat_token("t", "换风格", "1")
    md5a = base64.b64decode(a[: -len("-1-3")]).decode().split("|")[1]
    md5b = base64.b64decode(b[: -len("-1-3")]).decode().split("|")[1]
    assert md5a != md5b


# ---------------------------------------------------------------- 首页凭据


def test_extract_creds_from_homepage_ok():
    tok, lid = extract_creds_from_homepage(homepage_html("abc12345", "999"))
    assert (tok, lid) == ("abc12345", "999")


def test_extract_creds_missing_script_is_actionable_auth_error():
    with pytest.raises(AuthError) as ei:
        extract_creds_from_homepage("<html>验证中</html>", http_status=403)
    msg = str(ei.value)
    assert "aiTabFrameBaseData" in msg and "BAIDU_SESSION_TOKEN" in msg and "403" in msg


def test_extract_creds_bad_json_is_upstream_error():
    broken = ('<script type="application/json" name="aiTabFrameBaseData">'
              "{not json}</script>")
    with pytest.raises(UpstreamUnavailableError):
        extract_creds_from_homepage(broken)


def test_extract_creds_missing_fields_is_auth_error():
    html = ('<script type="application/json" name="aiTabFrameBaseData">'
            '{"token":"","lid":"x"}</script>')
    with pytest.raises(AuthError):
        extract_creds_from_homepage(html)


# ---------------------------------------------------------------- 请求体


def test_build_body_three_places_stay_consistent():
    """能力由三处同时决定：`mcpInfo.ext.type` / `sa` / `query[TEXT].query`（+extData）。"""
    cap = lookup("wenxin:expand")
    b = build_body(cap, query_text=cap.title, image_url="https://x/y.png", rank=7,
                   token="tk", lid="L1", expand="4:3")
    si = b["message"]["searchInfo"]
    ext = json.loads(si["mcpInfo"]["ext"])
    assert ext["type"] == cap.tool_type == "4"
    assert ext["image_expand"] == "4:3"
    assert ext["image"] == "https://x/y.png" and ext["channel"] == "edit"
    assert si["sa"] == b["sa"] == "workspace_piccreate_4"
    text = b["message"]["query"][1]["data"]["text"]
    assert text["query"] == cap.title == "扩图"
    assert json.loads(text["extData"]) == {"toolType": "4", "disableReply": True}
    assert b["message"]["query"][0]["data"]["image"]["image_url"] == "https://x/y.png"
    assert si["re_rank"] == "7" and b["rank"] == 7
    # ori_lid 缺省时复用 lid；chat_token 的 md5 与 query 完全绑定
    assert si["ori_lid"] == "L1"
    b64part = si["chatParams"]["chat_token"][: -len("-L1-3")]
    assert base64.b64decode(b64part).decode().split("|")[1] == hashlib.md5(
        cap.title.encode()).hexdigest()


def test_build_body_no_expand_key_when_not_given():
    cap = lookup("wenxin:clarity")
    b = build_body(cap, query_text=cap.title, image_url="u", rank=3, token="t", lid="l")
    assert "image_expand" not in json.loads(b["message"]["searchInfo"]["mcpInfo"]["ext"])
    assert b["message"]["searchInfo"]["sa"] == "workspace_piccreate_3"


def test_redact_payload_keeps_structure_but_blanks_chat_token():
    cap = lookup("wenxin:clarity")
    b = build_body(cap, query_text=cap.title, image_url="u", rank=3, token="t", lid="l")
    out = redact_payload(b)
    assert out["message"]["searchInfo"]["chatParams"]["chat_token"] == "***"
    # 原对象不受影响（深拷贝）
    assert b["message"]["searchInfo"]["chatParams"]["chat_token"] != "***"
    assert out["message"]["query"][1]["data"]["text"]["extData"] == \
        b["message"]["query"][1]["data"]["text"]["extData"]


# ---------------------------------------------------------------- SSE


def test_parse_sse_normal_flow_and_strip():
    out = parse_sse(_lines([frame("markdown-yiyan", {"value": "已完成 变清晰"}),
                            frame("image-generate", {"ratio": "3-2", "items": [IMG_ITEM]})]),
                    strip_watermark=True)
    assert out.frames == 2
    assert out.text == "已完成 变清晰" and out.ratio == "3-2"
    assert len(out.images) == 1
    img = out.images[0]
    assert img.url == "https://cdn.bce.test/out.png"     # 水印参数已剥离
    assert img.note == "已剥离水印处理参数"
    assert (img.width, img.height) == (24, 16)


def test_parse_sse_without_strip_keeps_watermark_param():
    out = parse_sse(_lines([frame("image-generate", {"items": [IMG_ITEM]})]),
                    strip_watermark=False)
    assert "x-bce-process" in out.images[0].url and out.images[0].note == ""


def test_parse_sse_items_null_with_editor_url():
    """🔴 上游会把 items 显式给成 null —— 必须不 TypeError，且记下编辑器深链。"""
    out = parse_sse(_lines([frame("image-generate",
                                  {"ratio": "", "items": None,
                                   "picEditBaseUrl": "baiduboxapp://v1/browser/open?url=x"})]))
    assert out.images == []
    assert out.editor_url and out.editor_url.startswith("baiduboxapp://")


def test_parse_sse_hint_captured():
    out = parse_sse(_lines([hint_frame("tokenFail")]))
    assert out.hint and out.hint["type"] == "tokenFail"


def test_parse_sse_skips_junk_lines():
    out = parse_sse(["event:basedata", "data: {not json}", "", "data: ",
                     *_lines([frame("markdown-yiyan", {"value": "x"})])])
    assert out.frames == 1 and out.text == "x"


def test_kunlun_first_frame_stops_iteration_immediately():
    """首帧熔断：kunlun 命中后**不再消费后续行**（调用方随即可断连）。"""
    out = parse_sse(_lines([kunlun_frame("kunlun_popup"),
                            frame("image-generate", {"items": [IMG_ITEM]})]))
    assert out.kunlun == "kunlun_popup"
    assert out.frames == 1 and out.images == []


def test_apply_frame_contract():
    out = parse_sse([])
    assert apply_frame(out, 'data: {"chatHitKunlun": "kunlun_popup"}') is False
    assert apply_frame(out, 'data: {"chatHitKunlun": ""}') is True
    assert apply_frame(out, "not a data line") is True


# ---------------------------------------------------------------- BCE 签名


def test_bce_sign_shape_and_determinism():
    headers = {"content-type": "image/png", "host": "aisearch.bj.bcebos.com",
               "x-bce-security-token": "st"}
    auth, ts = bce_sign("POST", "/pic/a.png", "uploads=", headers, "ak1", "sk1",
                        ["content-type", "host", "x-bce-date", "x-bce-security-token"],
                        ts="2026-09-24T00:00:00Z")
    assert ts == "2026-09-24T00:00:00Z" and headers["x-bce-date"] == ts
    prefix, signed_headers, sig = auth.rsplit("/", 2)
    assert prefix == "bce-auth-v1/ak1/2026-09-24T00:00:00Z/1800"
    assert signed_headers == "content-type;host;x-bce-date;x-bce-security-token"
    assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)

    # 同输入同签名；换 sk 则变
    h2 = {"content-type": "image/png", "host": "aisearch.bj.bcebos.com",
          "x-bce-security-token": "st"}
    auth2, _ = bce_sign("POST", "/pic/a.png", "uploads=", h2, "ak1", "sk1",
                        ["content-type", "host", "x-bce-date", "x-bce-security-token"],
                        ts="2026-09-24T00:00:00Z")
    assert auth2 == auth
    auth3, _ = bce_sign("POST", "/pic/a.png", "uploads=", h2, "ak1", "sk2",
                        ["content-type", "host", "x-bce-date", "x-bce-security-token"],
                        ts="2026-09-24T00:00:00Z")
    assert auth3 != auth


def test_uri_encode_and_canon_query():
    assert uri_encode("a b/c") == "a%20b%2Fc"
    assert uri_encode("a/b", encode_slash=False) == "a/b"
    assert canon_query("b=2&a=1") == "a=1&b=2"
    assert canon_query("") == ""
    assert canon_query("partNumber=1&uploadId=x%2Fy") == "partNumber=1&uploadId=x%252Fy"


# ---------------------------------------------------------------- 老接口（兜底通路）


def test_legacy_form_shape_and_defaults():
    form = legacy_form("3", "QUJD")
    assert form["type"] == "3" and form["picInfo"] == "QUJD"
    assert form["query"] == "bdaitpzs百度AI图片助手bdaitpzs"
    assert form["front_display"] == "2" and form["image_source"] == "1"
    assert form["is_first"] == "true" and form["ext_ratio"] == ""
    assert legacy_form("1", "x", ext_ratio="4:3")["ext_ratio"] == "4:3"


def test_extract_legacy_result_accepts_data_uri_and_bare_b64():
    data = base64.b64encode(PNG_SMALL).decode()
    assert extract_legacy_result(
        {"picArr": [{"src": f"data:image/png;base64,{data}"}]}) == PNG_SMALL
    assert extract_legacy_result({"picArr": [{"src": data}]}) == PNG_SMALL
    assert extract_legacy_result({"picArr": []}) is None
    assert extract_legacy_result({}) is None
    assert extract_legacy_result({"picArr": [{"src": "%%%%"}]}) is None
