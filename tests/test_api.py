"""对外接口用例：端点 / dry_run / 鉴权 / 门禁 / 错误信封 / 冷却窗 / 取件。

注入方式与 textin 一致：`with TestClient(app) as c:` **起完生命周期后**把上游客户端
换成走 MockTransport 的实例 —— 这样连"服务自己 new 的真 client"这条假绿路径也被堵住。
"""

from __future__ import annotations

import base64
import json
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.upstream.baidu import BaiduClient
from tests.helpers import (
    PNG_SMALL,
    UpstreamSpy,
    data_uri,
    frame,
    hint_frame,
    kunlun_frame,
    settings,
)

URL = "/v1/images/generations"


def _app(tmp_path, **overrides: Any):
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    conf = settings(MEDIA_DIR=str(media), **overrides)
    return create_app(conf), media


def _inject(app, spy: UpstreamSpy, **overrides: Any) -> None:
    conf = (app.state.settings.model_copy(update=overrides)
            if overrides else app.state.settings)
    app.state.client = BaiduClient(conf, transport=spy.transport())


def _post(c: TestClient, payload: dict[str, Any], **kw):
    return c.post(URL, json=payload, **kw)


# --------------------------------------------------------------------- 运维面


def test_healthz(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/healthz").json()
    assert body["status"] == "ok" and body["version"]


def test_readyz_reports_cookie_and_counts(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/readyz").json()
    assert body["status"] == "ok"
    checks = body["checks"]
    assert checks["cookie_configured"] is True
    assert checks["credentials"]["source"] == "explicit"
    assert checks["capabilities"] == {"total": 20, "available": 12}
    assert checks["legacy"] == {"mode": "off", "base": None}
    assert checks["risk_window"]["cooling"] is False
    assert checks["proxy_pool"] == []


def test_readyz_degraded_without_cookie(tmp_path):
    app, _ = _app(tmp_path, COOKIE="")
    with TestClient(app) as c:
        body = c.get("/readyz").json()
    assert body["status"] == "degraded"
    assert body["checks"]["cookie_configured"] is False


# --------------------------------------------------------------------- 鉴权


def test_auth_enforced_but_models_is_public(tmp_path):
    app, _ = _app(tmp_path, API_KEYS="sk-a,sk-b")
    with TestClient(app) as c:
        assert c.get("/v1/models").status_code == 200          # 发现端点免鉴权
        r1 = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
        assert r1.status_code == 401 and r1.json()["error"]["code"] == "unauthorized"
        r2 = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)},
                   headers={"authorization": "Bearer wrong"})
        assert r2.status_code == 401
        assert c.get("/capabilities").status_code == 401
        assert c.get("/capabilities",
                     headers={"authorization": "Bearer sk-b"}).status_code == 200
        spy = UpstreamSpy()
        _inject(app, spy)
        r3 = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)},
                   headers={"authorization": "Bearer sk-b"})
        assert r3.status_code == 200


# --------------------------------------------------------------------- 发现面


def test_models_lists_only_available(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    assert "wenxin:dewatermark" not in ids
    assert "wenxin:clarity" in ids and "wenxin:expand" in ids
    assert "wenxin:restyle" not in ids and "wenxin:bgreplace" not in ids
    assert len(ids) == 12


def test_models_includes_unverified_when_gate_open(tmp_path):
    app, _ = _app(tmp_path, ALLOW_UNVERIFIED=True)
    with TestClient(app) as c:
        ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    assert "wenxin:dewatermark" in ids and len(ids) == 18


def test_capabilities_explains_absences(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/capabilities").json()
    top = [m for m in body["models"] if m["id"] == "wenxin:clarity"][0]
    assert top["tool_type"] == "3" and top["verified"] is True
    gated = [m for m in body["not_available"] if m["id"] == "wenxin:dewatermark"]
    assert gated and "BAIDU_ALLOW_UNVERIFIED" in gated[0]["reason"]
    assert any("toolType 1 " in k for k in body["not_registered"])
    assert "生成文案" in " ".join(body["deliberately_absent"])


# --------------------------------------------------------------------- 主流程


def test_happy_path_bos_upload(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
    assert r.status_code == 200, r.text
    body = r.json()
    item = body["data"][0]
    assert base64.b64decode(item["b64_json"]) == PNG_SMALL
    assert item["mime"] == "image/png" and item["size"] == "24x16"
    assert body["model"] == "wenxin:clarity"
    assert body["usage"] == {"generated_images": 1}
    assert body["upstream"]["tool_type"] == "3" and body["upstream"]["frames"] == 2
    assert body["effective"]["upload"]["path"] == "bos"
    # 转存链真的发生了；结果图取回也真的发生了
    assert "/aichat/api/file/sts" in spy.paths()
    assert spy.conversations == 1
    assert any(r_.url.host == "cdn.bce.test" for r_ in spy.seen)


def test_direct_mode_passes_url_through_without_upload(tmp_path):
    app, _ = _app(tmp_path, UPLOAD_MODE="direct")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:clarity", "image": "https://img.test/in.png"})
    assert r.status_code == 200
    assert r.json()["effective"]["upload"]["path"] == "direct"
    assert "/aichat/api/file/sts" not in spy.paths()


def test_response_format_url_mirrors_and_serves(tmp_path):
    app, media = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL),
                      "response_format": "url"})
        url = r.json()["data"][0]["url"]
        served = c.get(url)
    assert url.startswith("/files/")
    assert served.status_code == 200 and served.content == PNG_SMALL
    assert (media / url.rsplit("/", 1)[1]).read_bytes() == PNG_SMALL


# --------------------------------------------------------------------- dry_run


def test_dry_run_touches_nothing_and_returns_preview(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:expand", "image": data_uri(PNG_SMALL),
                      "size": "4:3", "dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True and body["data"] == [] and body["usage"] is None
    assert body["preview"]["would_post_to"].endswith("/aichat/api/conversation")
    ext = json.loads(body["preview"]["body"]["message"]["searchInfo"]["mcpInfo"]["ext"])
    assert ext["type"] == "4" and ext["image_expand"] == "4:3"
    tok = body["preview"]["body"]["message"]["searchInfo"]["chatParams"]["chat_token"]
    assert tok == "***", "预览里的凭据必须打码"
    assert spy.seen == [], "dry_run 必须零上游请求"


def test_dry_run_via_header(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)},
                  headers={"x-avm-dry-run": "1"})
    assert r.json()["dry_run"] is True and spy.seen == []


# --------------------------------------------------------------------- 门禁


def test_no_cookie_blocks_real_calls_but_not_dry_run(tmp_path):
    app, _ = _app(tmp_path, COOKIE="")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        real = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
        dry = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL),
                        "dry_run": True})
    assert real.status_code == 503
    assert real.json()["error"]["code"] == "upstream_not_configured"
    assert "mint_identity" in real.json()["error"]["message"]
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    assert spy.seen == []


def test_unverified_gate_503_but_dry_run_passes(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        real = _post(c, {"model": "wenxin:dewatermark", "image": data_uri(PNG_SMALL)})
        dry = _post(c, {"model": "wenxin:dewatermark", "image": data_uri(PNG_SMALL),
                        "dry_run": True})
    assert real.status_code == 503
    assert real.json()["error"]["code"] == "capability_not_verified"
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    assert spy.conversations == 0, "闸门必须生效在调用上游之前"


def test_unverified_gate_can_be_opened(tmp_path):
    app, _ = _app(tmp_path, ALLOW_UNVERIFIED=True)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:dewatermark", "image": data_uri(PNG_SMALL)})
    assert r.status_code == 200


# --------------------------------------------------------------------- 冷却窗 / 限速


def test_kunlun_trips_window_and_short_circuits(tmp_path):
    app, _ = _app(tmp_path, COOLDOWN=600)
    with TestClient(app) as c:
        spy = UpstreamSpy(frames=[kunlun_frame("kunlun_popup")])
        _inject(app, spy)
        first = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
        second = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
    assert first.status_code == 429
    assert first.json()["error"]["kind"] == "risk_control"
    assert first.headers.get("retry-after") == "600"
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "risk_control_cooldown"
    assert spy.conversations == 1, "冷却窗内不许再打上游"


def test_rate_gate_rejects_when_wait_exceeds_budget(tmp_path):
    app, _ = _app(tmp_path, MIN_INTERVAL=5.0, MAX_WAIT=0.05)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        first = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
        second = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert int(second.headers.get("retry-after", "0")) >= 1
    assert spy.conversations == 1


# --------------------------------------------------------------------- 上游失败形态


def test_silent_upstream_failure_is_502(tmp_path):
    """既没图也没 hint ⇒ 显式失败（静默型失败最致命）。"""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy(frames=[frame("markdown-yiyan", {"value": "已完成 变清晰"})])
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
    assert r.status_code == 502
    err = r.json()["error"]
    assert "未产出图片" in err["message"] and err["kind"] == "upstream"


def test_token_fail_is_503_auth_deployment_problem(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy(frames=[hint_frame("tokenFail")])
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["kind"] == "auth" and "searchframeLid" in err["message"]


# --------------------------------------------------------------------- 校验


def test_request_validation_errors(tmp_path):
    app, _ = _app(tmp_path)
    cases = [
        ({"model": "wenxin:nope", "image": "x"}, "unknown_model"),
        ({"image": data_uri(PNG_SMALL)}, "missing_model"),
        ({"model": "wenxin:clarity"}, "missing_input"),
        ({"model": "wenxin:clarity", "image": data_uri(PNG_SMALL), "typo_field": 1},
         "unknown_field"),
        ({"model": "wenxin:clarity", "image": data_uri(PNG_SMALL),
          "response_format": "nope"}, "invalid_response_format"),
        ({"model": "wenxin:clarity", "image": data_uri(PNG_SMALL), "size": 42},
         "invalid_size"),
    ]
    with TestClient(app) as c:
        for payload, code in cases:
            r = _post(c, payload)
            assert r.status_code == 400, (payload, r.text)
            assert r.json()["error"]["code"] == code, (payload, r.text)


def test_known_unsupported_fields_are_reported_not_swallowed(tmp_path):
    """语义边界：`n` 是真"不支持"（进 unsupported[]）；
    `prompt` / `size` 是**已登记的入口字段**，走 warnings[]（"被忽略/未透传"）——不算 unsupported。"""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL),
                      "prompt": "随便写", "n": 2, "size": "1024x1024"})
    body = r.json()
    assert r.status_code == 200
    assert set(body["unsupported"]) == {"n"}
    assert any("prompt 未透传" in w for w in body["warnings"])
    assert any("不接受 size" in w for w in body["warnings"])


def test_expand_size_warning_for_unusual_ratio(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:expand", "image": data_uri(PNG_SMALL),
                      "size": "5:3", "dry_run": True})
    body = r.json()
    assert body["preview"]  # 干跑穿了
    assert any("5:3" in w and "实测支持集合" in w for w in body["warnings"])


# --------------------------------------------------------------------- 老接口兜底


def test_legacy_unlocks_dewatermark_in_models(tmp_path):
    app, _ = _app(tmp_path, LEGACY="fallback")
    with TestClient(app) as c:
        ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
        caps = c.get("/capabilities").json()
    assert "wenxin:dewatermark" in ids and len(ids) == 17
    dewater = [m for m in caps["models"] if m["id"] == "wenxin:dewatermark"][0]
    assert dewater["legacy_type"] == "1"
    assert caps["legacy"]["mode"] == "fallback"
    # 无映射的未取证能力仍在 not_available（不制造假能力）
    assert "wenxin:reimagine" in [m["id"] for m in caps["not_available"]]
    # 换风格老接口 14 已死 ⇒ 不因开兜底变可用
    assert "wenxin:restyle" in [m["id"] for m in caps["not_available"]]


def test_fallback_on_primary_silent_failure(tmp_path):
    """主链 items:null（去水印的历史形态）⇒ 自动改走老接口（type=1）。"""
    app, _ = _app(tmp_path, LEGACY="fallback")
    with TestClient(app) as c:
        spy = UpstreamSpy(frames=[frame("markdown-yiyan",
                                        {"value": "已经完成了 去水印 操作。"})])
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:dewatermark", "image": data_uri(PNG_SMALL)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["effective"]["path"] == "legacy"
    assert body["upstream"]["path"] == "legacy"
    assert body["upstream"]["legacy"]["type"] == "1"
    assert any("改走老接口" in w for w in body["warnings"])
    assert spy.conversations == 1 and spy.legacy_creates == 1 and spy.legacy_polls == 1
    # fallback 模式先跑主链 ⇒ BOS 转存已发生（"先主链后兜底"的固有成本，文档已注明；
    # 要免掉转存请用 prefer 模式 —— 见 test_prefer_skips_primary_and_bos）
    assert "/aichat/api/file/sts" in spy.paths()


def test_fallback_on_kunlun_and_cooldown_serves_legacy(tmp_path):
    app, _ = _app(tmp_path, LEGACY="fallback", COOLDOWN=600)
    with TestClient(app) as c:
        spy = UpstreamSpy(frames=[kunlun_frame("kunlun_popup")])
        _inject(app, spy)
        r1 = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
        r2 = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
        r3 = _post(c, {"model": "wenxin:filter", "image": data_uri(PNG_SMALL)})
    assert r1.status_code == 200 and r1.json()["effective"]["path"] == "legacy"
    assert any("改走老接口" in w for w in r1.json()["warnings"])
    # 冷却窗内：有映射 ⇒ 直接走老接口（不再打主链、也不再 429）
    assert r2.status_code == 200 and r2.json()["effective"]["path"] == "legacy"
    assert any("冷却窗" in w for w in r2.json()["warnings"])
    assert spy.conversations == 1, "冷却窗内不得再打主链"
    # 无映射 ⇒ 维持 429（不制造假能力）
    assert r3.status_code == 429
    assert r3.json()["error"]["code"] == "risk_control_cooldown"


def test_prefer_skips_primary_and_bos(tmp_path):
    app, _ = _app(tmp_path, LEGACY="prefer")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
    body = r.json()
    assert r.status_code == 200 and body["effective"]["path"] == "legacy"
    assert spy.conversations == 0, "prefer 不得打主链"
    assert "/aichat/api/file/sts" not in spy.paths(), "老接口直通不做 BOS 转存"
    assert any("prefer" in w for w in body["warnings"])
    assert body["data"][0]["size"] == "24x16"


def test_prefer_without_mapping_warns_and_uses_primary(tmp_path):
    app, _ = _app(tmp_path, LEGACY="prefer")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:filter", "image": data_uri(PNG_SMALL)})
    body = r.json()
    assert body["effective"]["path"] == "conversation"
    assert any("无老接口映射" in w for w in body["warnings"])
    assert spy.conversations == 1


def test_fallback_legacy_failure_keeps_primary_cause(tmp_path):
    """兜底也失败时：**保留主因**（风控 429），把兜底失败挂进 detail —— 两段信息都不丢。"""
    app, _ = _app(tmp_path, LEGACY="fallback", COOLDOWN=600)
    with TestClient(app) as c:
        spy = UpstreamSpy(frames=[kunlun_frame("kunlun_popup")], legacy_create_status=5)
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL)})
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["kind"] == "risk_control"                      # 主因保留
    assert "legacy_failed" in err and "status=5" in err["legacy_failed"]  # 兜底失败可见
    assert r.headers.get("retry-after") == "600"


# --------------------------------------------------- 工具入口形状（2026-09-24）


def _preview_body(body: dict[str, Any]) -> dict[str, Any]:
    return body["preview"]["body"]["message"]


def test_entry_type_shape_uses_searchbox_and_enter_type(tmp_path):
    """换风格/背景替换走**工具入口形状**：sa=searchbox_image + enter_type + 无 mcpInfo。

    形状来源：站点 UI 真实报文（2026-09-24 抓取）。真实调用需 ALLOW_UNVERIFIED（已门禁）。
    """
    app, _ = _app(tmp_path, ALLOW_UNVERIFIED=True)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:restyle", "image": data_uri(PNG_SMALL),
                      "prompt": "宫崎骏风格"})
    body = r.json()
    assert body["effective"]["entry_type"] == "pic_picfunc_14"
    assert body["effective"]["query"] == "宫崎骏风格", "prompt 就是指令文本"
    assert any("工具入口形状" in w for w in body["warnings"])


def test_entry_type_dry_run_preview_shape(tmp_path):
    app, _ = _app(tmp_path, ALLOW_UNVERIFIED=True)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:bgreplace", "image": data_uri(PNG_SMALL),
                      "prompt": "大雪纷飞的背景", "dry_run": True})
    msg = _preview_body(r.json())
    search = msg["searchInfo"]
    assert search["sa"] == "searchbox_image"
    assert search["enter_type"] == "pic_picfunc_11"
    assert "mcpInfo" not in search, "入口形状**不带** mcpInfo（实测报文如此）"
    assert msg["query"][-1]["data"]["text"]["query"] == "大雪纷飞的背景"
    assert spy.seen == [], "干跑零网络"


def test_entry_type_requires_instruction_but_dry_run_passes(tmp_path):
    app, _ = _app(tmp_path, ALLOW_UNVERIFIED=True)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        # 真跑缺指令 ⇒ 400（触网前拒）
        r = _post(c, {"model": "wenxin:restyle", "image": data_uri(PNG_SMALL)})
        # 干跑缺指令 ⇒ 200（闸门语义：干跑穿透）+ 占位警告
        r2 = _post(c, {"model": "wenxin:restyle", "image": data_uri(PNG_SMALL),
                       "dry_run": True})
    assert r.status_code == 400 and r.json()["error"]["code"] == "missing_instruction"
    assert r2.status_code == 200 and r2.json()["dry_run"] is True
    assert any("干跑未提供" in w for w in r2.json()["warnings"])
    assert spy.seen == []


def test_entry_type_caps_are_gated_by_default(tmp_path):
    """换风格/背景替换默认门禁（2026-09-24 复测已变编辑器/agent 形态）。"""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:restyle", "image": data_uri(PNG_SMALL),
                      "prompt": "宫崎骏风格"})
        caps = c.get("/capabilities").json()
    assert r.status_code == 503 and r.json()["error"]["code"] == "capability_not_verified"
    entry = [m for m in caps["not_available"] if m["id"] == "wenxin:restyle"][0]
    assert entry["entry_type"] == "pic_picfunc_14" and entry["needs_instruction"] is True
    assert spy.seen == []
