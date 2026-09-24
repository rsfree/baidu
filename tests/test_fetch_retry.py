"""结果取回的重试策略（`fetch_url`）—— 只重试瞬时错误。

2026-09-24 实测：上游结果图在境外 CDN，跨境取回有 38~47s 的偶发慢（表现为 504）⇒
本服务加了"重试 + 可配超时"。这里用 `httpx.MockTransport` 把情形钉死：

  ① 首次超时、随后成功 ⇒ 重试后成功
  ② 连续 503 ⇒ 用尽重试后抛（且**真的重试了**）
  ③ 404 ⇒ **不重试**（立刻抛）
  ④ 超过字节上限 ⇒ **不重试**（重试只会重复失败）
  ⑤ 默认 `retries=0` ⇒ 输入取回保持旧行为（不被静默加时）
  ⑥ `timeout` 参数真的传下去了

仓内约定：不引入 pytest-asyncio，用 `tests.helpers.arun` 在同步用例里跑协程。
"""

from __future__ import annotations

import base64

import httpx
import pytest

from app.config import Settings
from app.errors import ApiError, UpstreamTimeout, UpstreamUnavailableError
from app.upstream.baidu.client import BaiduClient
from tests.helpers import arun


def _client(handler) -> BaiduClient:
    return BaiduClient(Settings(_env_file=None, COOKIE="BAIDUID=x"),
                       transport=httpx.MockTransport(handler))


def test_retries_on_timeout_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, content=b"ok-bytes", headers={"content-type": "image/jpeg"})

    c = _client(handler)
    raw, ct = arun(c.fetch_url("https://cdn.example/x.jpg", 1024, retries=2))
    arun(c.aclose())
    assert raw == b"ok-bytes" and ct == "image/jpeg"
    assert calls["n"] == 2, "应当重试一次后成功"


def test_retries_on_5xx_then_raises_when_exhausted():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, content=b"busy")

    c = _client(handler)
    with pytest.raises(UpstreamUnavailableError):
        arun(c.fetch_url("https://cdn.example/x.jpg", 1024, retries=2))
    arun(c.aclose())
    assert calls["n"] == 3, "1 次 + 2 次重试"


def test_does_not_retry_on_404():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, content=b"nope")

    c = _client(handler)
    with pytest.raises(UpstreamUnavailableError):
        arun(c.fetch_url("https://cdn.example/x.jpg", 1024, retries=3))
    arun(c.aclose())
    assert calls["n"] == 1, "4xx 不重试（重试只会重复失败）"


def test_does_not_retry_when_too_large():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"x" * 5000,
                              headers={"content-type": "image/jpeg", "content-length": "5000"})

    c = _client(handler)
    with pytest.raises(ApiError) as ei:
        arun(c.fetch_url("https://cdn.example/x.jpg", 1024, retries=2))
    arun(c.aclose())
    assert calls["n"] == 1, "超限不重试"
    assert "超过上限" in str(ei.value)


def test_no_retries_by_default():
    """默认 retries=0：输入取回保持旧行为，不被静默加时。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    c = _client(handler)
    with pytest.raises(UpstreamUnavailableError):
        arun(c.fetch_url("https://cdn.example/x.jpg", 1024))
    arun(c.aclose())
    assert calls["n"] == 1


def test_timeout_param_is_honoured():
    """`timeout` 参数要真的传下去（默认走模块常量 30s）。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, content=b"z", headers={"content-type": "image/png"})

    c = _client(handler)
    arun(c.fetch_url("https://cdn.example/x.png", 1024, timeout=7.5))
    arun(c.aclose())
    assert seen["timeout"] is not None, "httpx 未暴露 timeout 扩展 ⇒ 需要换测法"
    assert float(seen["timeout"]["read"]) == 7.5, seen["timeout"]


def test_retryable_classification():
    """可重试性判据：超时 / 无状态码的网络错误 / 5xx / 429 ⇒ 是；4xx 与超限 ⇒ 否。"""
    assert BaiduClient._fetch_retryable(UpstreamTimeout("t"))
    assert BaiduClient._fetch_retryable(UpstreamUnavailableError("网络错误"))
    assert BaiduClient._fetch_retryable(UpstreamUnavailableError("busy", http_status=503))
    assert BaiduClient._fetch_retryable(UpstreamUnavailableError("rate", http_status=429))
    assert not BaiduClient._fetch_retryable(UpstreamUnavailableError("nope", http_status=404))
    assert not BaiduClient._fetch_retryable(ApiError(400, "content_too_large", "太大"))


# ------------------------------------------------- 老接口拒单要「报错友好」（2026-09-24）

def test_legacy_refusal_messages_are_actionable():
    """老接口拒单按 `resType` 分类给可操作提示 —— 别只丢一个 status=0。

    两类实测拒法：`resType=2`（入参体积/内容）与「无任务号且无 resType」（疑似限流）。
    """

    def handler_of(handler_body):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/aigc/pccreate"):
                return httpx.Response(200, json=handler_body)
            return httpx.Response(200, json={})          # pcquery 用不到（create 就拒了）

        return handler

    for body, expect in (({"status": 0, "resType": 2}, "入参体积"),
                         ({"status": 0, "resType": None}, "自动铸新匿名身份"),
                         ({"antiFlag": 1, "message": "Forbid spider access"}, "反爬标记"),
                         ({"status": 0, "resType": 9}, "未知拒法")):
        c = BaiduClient(Settings(_env_file=None, COOKIE="BAIDUID=x",
                                 ROTATE_COOKIE_ON_BURN=False),   # 关轮换：单测文案本身
                        transport=httpx.MockTransport(handler_of(body)))
        with pytest.raises(UpstreamUnavailableError) as ei:
            arun(c.legacy_process("1", b"\x89PNG\r\n\x1a\n" + b"0" * 32))
        arun(c.aclose())
        assert expect in str(ei.value), f"{body} ⇒ {ei.value}"


def test_burned_cookie_triggers_identity_rotation():
    """cookie 被老接口拉黑（无任务号 + 无 resType）⇒ **自动铸新匿名身份并重试一次**。

    实测依据（2026-09-24 对照实验）：同一 cookie 冷却 15 分钟仍被拒；**换新匿名 cookie、同 IP 立刻收单**。
    这里把三次交互钉死：① 旧 cookie 被拒 → ② GET 首页铸到新 cookie → ③ 新 cookie 成功受理。
    """
    seen: list[str] = []
    state = {"creates": 0, "minted": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":                                   # 铸身份
            state["minted"] = True
            return httpx.Response(200, headers={
                "set-cookie": "BAIDUID=FRESH:FG=1; Path=/; Domain=baidu.com",
            }, text="<html></html>")
        if request.url.path.endswith("/aigc/pccreate"):
            seen.append(request.headers.get("cookie", ""))
            state["creates"] += 1
            if state["creates"] == 1:
                # 实测的反爬形态：**没有 status 键**（旧判据要求 status==0 ⇒ 漏判过）
                return httpx.Response(200, json={"antiFlag": 1, "message": "Forbid spider access"})
            return httpx.Response(200, json={"status": 0, "pcEditTaskid": "T1", "resType": 0})
        if request.url.path.endswith("/aigc/pcquery"):
            return httpx.Response(200, json={"progress": 100, "picArr": [
                {"src": "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"x" * 64).decode()}]})
        return httpx.Response(404)

    c = _client(handler)
    raw, info = arun(c.legacy_process("1", b"\x89PNG\r\n\x1a\n" + b"0" * 32))
    arun(c.aclose())
    assert state["minted"], "被拉黑后必须去铸新身份"
    assert state["creates"] == 2, "应当重试一次"
    assert seen[0] != seen[1] and "FRESH" in seen[1], f"重试必须换 cookie：{seen}"
    assert info.get("identity_rotated"), "过程记录里要有轮换痕迹（不许静默）"


def test_rotation_can_be_disabled_by_knob():
    """`ROTATE_COOKIE_ON_BURN=0` ⇒ 不铸身份、原样报错（给排障留后门）。"""
    state = {"minted": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            state["minted"] = True
            return httpx.Response(200, headers={"set-cookie": "BAIDUID=X; Path=/"})
        if request.url.path.endswith("/aigc/pccreate"):
            return httpx.Response(200, json={"status": 0})
        return httpx.Response(404)

    c = BaiduClient(Settings(_env_file=None, COOKIE="BAIDUID=old",
                             ROTATE_COOKIE_ON_BURN=False),
                    transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamUnavailableError):
        arun(c.legacy_process("1", b"\x89PNG\r\n\x1a\n" + b"0" * 32))
    arun(c.aclose())
    assert not state["minted"], "关掉旋钮后不得偷偷铸身份"
