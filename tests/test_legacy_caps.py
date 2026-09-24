"""新能力通路用例：遮罩类（消除/局部替换）与 legacy-only（AI重绘/相似图）。

全 mock、零网络。覆盖 2026-09-24 矩阵实测确立的四条通路 + 路径选择语义：

- 消除(8) / 局部替换(5)：`requires_mask` ⇒ **直达老接口**（主链遮罩参数未逆向），`picInfo2` 直传；
- AI重绘(6)/相似图(7)：legacy-only ⇒ `create_level` 档位透传；
- 「新接口为主、老接口兜底」：requires_mask / legacy-only 不进主链，其余能力在 fallback 下先主链。
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.upstream.baidu import BaiduClient
from tests.helpers import PNG_SMALL, UpstreamSpy, data_uri, settings

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


def _mask_uri() -> str:
    """黑底白框遮罩（内容无关紧要：本层只做透传；语义已由判决实验坐实）。"""
    return data_uri(PNG_SMALL)


# --------------------------------------------------------------------- 遮罩类


def test_erase_via_legacy_sends_mask_as_picinfo2(tmp_path):
    app, _ = _app(tmp_path, LEGACY="fallback")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:erase", "image": data_uri(PNG_SMALL),
                      "mask": _mask_uri()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["effective"]["path"] == "legacy"
    assert body["effective"]["legacy"] == {"mode": "fallback", "type": "8", "enabled": True}
    assert body["upstream"]["legacy"]["has_mask"] is True
    assert any("需要遮罩" in w for w in body["warnings"])
    form = spy.legacy_forms[0]
    assert form["type"] == "8"
    assert base64.b64decode(form["picInfo2"]) == PNG_SMALL, "mask 以 picInfo2 直传（纯 base64）"
    assert base64.b64decode(form["picInfo"]) == PNG_SMALL
    assert spy.conversations == 0, "requires_mask ⇒ 直达老接口，不打主链"
    assert "/aichat/api/file/sts" not in spy.paths(), "老接口直通免 BOS 转存"


def test_replace_forwards_prompt_as_text(tmp_path):
    app, _ = _app(tmp_path, LEGACY="fallback")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:replace", "image": data_uri(PNG_SMALL),
                      "mask": _mask_uri(), "prompt": "一只橘猫"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"]["mask"], "mask 进 requested 诊断（只有摘要，无内容）"
    assert body["requested"]["prompt"] == "一只橘猫"
    form = spy.legacy_forms[0]
    assert form["type"] == "5" and form["text"] == "一只橘猫"
    assert form["picInfo2"], "局部替换同样需要遮罩"


def test_erase_missing_mask_is_400_regardless_of_legacy(tmp_path):
    app, _ = _app(tmp_path, LEGACY="off")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:erase", "image": data_uri(PNG_SMALL)})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "missing_mask" and "白" in err["message"], "必须讲清遮罩语义"
    assert spy.seen == [], "缺字段在触网之前就被拒"


def test_erase_with_mask_but_legacy_off_is_503(tmp_path):
    """有 mask 但没开兜底 ⇒ 未取证闸门 503（主链遮罩参数未逆向，不制造假能力）。"""
    app, _ = _app(tmp_path, LEGACY="off")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:erase", "image": data_uri(PNG_SMALL),
                      "mask": _mask_uri()})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "capability_not_verified"
    assert spy.seen == []


# ------------------------------------------------------------------- legacy-only


def test_redraw_and_similar_forward_create_level(tmp_path):
    app, _ = _app(tmp_path, LEGACY="fallback")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r1 = _post(c, {"model": "wenxin:redraw", "image": data_uri(PNG_SMALL)})
        r2 = _post(c, {"model": "wenxin:similar", "image": data_uri(PNG_SMALL)})
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert any("legacy-only" in w for w in r1.json()["warnings"])
    f1, f2 = spy.legacy_forms
    assert (f1["type"], f1["create_level"]) == ("6", "2"), "AI重绘 = type 6 + level 2"
    assert (f2["type"], f2["create_level"]) == ("7", "5"), "相似图 = type 7 + level 5"
    assert spy.conversations == 0


def test_legacy_only_blocked_without_legacy(tmp_path):
    app, _ = _app(tmp_path, LEGACY="off")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:redraw", "image": data_uri(PNG_SMALL)})
        caps = c.get("/capabilities").json()
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "capability_not_verified"
    entry = [m for m in caps["not_available"] if m["id"] == "wenxin:redraw"][0]
    assert entry["legacy_only"] is True and "legacy-only" in entry["reason"]
    assert spy.seen == []


def test_dry_run_previews_legacy_plan_without_network(tmp_path):
    app, _ = _app(tmp_path, LEGACY="fallback")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:redraw", "image": data_uri(PNG_SMALL),
                      "dry_run": True})
    body = r.json()
    assert body["dry_run"] is True
    prev = body["preview"]
    assert prev["would_post_to"].endswith("/aigc/pccreate")
    assert prev["legacy"]["type"] == "6" and prev["legacy"]["create_level"] == "2"
    assert prev["body"] is None, "legacy-only 没有主链 body"
    assert spy.seen == [], "干跑零网络（含遮罩解析）"


# --------------------------------------------------------------- 兜底路径选择


def test_expand_via_legacy_forwards_ext_ratio(tmp_path):
    """扩图（有映射、非遮罩类）在 prefer 下走老接口，`size` → `ext_ratio`。"""
    app, _ = _app(tmp_path, LEGACY="prefer")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:expand", "image": data_uri(PNG_SMALL),
                      "size": "4:3"})
    assert r.status_code == 200, r.text
    assert r.json()["effective"]["path"] == "legacy"
    form = spy.legacy_forms[0]
    assert form["type"] == "4" and form["ext_ratio"] == "4:3"
    assert spy.conversations == 0


def test_mask_ignored_with_warning_on_non_mask_capability(tmp_path):
    app, _ = _app(tmp_path, LEGACY="off")
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject(app, spy)
        r = _post(c, {"model": "wenxin:clarity", "image": data_uri(PNG_SMALL),
                      "mask": _mask_uri()})
    body = r.json()
    assert body["effective"]["path"] == "conversation", "非遮罩能力仍走主链"
    assert any("不使用 mask" in w for w in body["warnings"])
    assert spy.legacy_forms == [], "非遮罩能力不会去碰老接口"
