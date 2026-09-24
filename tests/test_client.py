"""出站请求形状 / 凭据现取 / BOS 上传链 / 错误映射 —— 全部走 MockTransport（零出网）。

断言的是「**真正发出去的东西**」（URL、请求头、body 字节、每个字段的取值），
而不是「内层函数被调用了」—— 后者会让整条路径从未被真实执行。
"""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

import app.upstream.baidu.client as baidu_client_mod
from app.errors import (
    ApiError,
    AuthError,
    RiskControlError,
    UpstreamParamError,
    UpstreamUnavailableError,
)
from app.models import lookup
from app.upstream.baidu import BaiduClient
from tests.helpers import (
    JPEG_SMALL,
    PNG_SMALL,
    UpstreamSpy,
    arun,
    default_frames,
    frame,
    hint_frame,
    homepage_html,
    kunlun_frame,
    settings,
    sse_response,
)


def _client(spy: UpstreamSpy, **overrides):
    conf = settings(**overrides)
    return conf, BaiduClient(conf, transport=spy.transport())


def _converse(client: BaiduClient, cap_key: str = "wenxin:clarity", *, rank: int = 7,
              image: str = "https://img.test/in.png", strip: bool = False):
    cap = lookup(cap_key)
    return arun(client.converse(cap, query_text=cap.title, image_url=image, rank=rank,
                                strip_watermark=strip))


# ---------------------------------------------------------------- 会话请求形状


def test_conversation_request_shape():
    spy = UpstreamSpy()
    conf, client = _client(spy)
    out = _converse(client, rank=42)
    arun(client.aclose())

    assert out.text and len(out.images) == 1
    assert out.cred_source == "explicit"
    assert len(spy.seen) == 1
    req = spy.seen[0]
    assert req.method == "POST" and req.url.path == "/aichat/api/conversation"
    h = req.headers
    assert h["accept"] == "text/event-stream"
    assert h["origin"] == "https://wenxin.baidu.com"
    assert h["cookie"] == conf.COOKIE
    assert h["isdeepseek"] == "1" and h["source"] == "pc_csaitab"
    assert h["x-chat-message"] == "enter_type:unknown,re_rank:42,modelName:smartMode"

    body = json.loads(req.content)
    si = body["message"]["searchInfo"]
    assert json.loads(si["mcpInfo"]["ext"])["type"] == "3"
    assert si["sa"] == "workspace_piccreate_3"
    assert si["re_rank"] == "42" and body["rank"] == 42
    assert body["message"]["query"][1]["data"]["text"]["query"] == "变清晰"


@pytest.mark.parametrize("rank", [3, 42, 90])
def test_rank_reaches_x_chat_message(rank):
    """`rank` 必须真的到达请求头（历史回归：曾被硬编码成 3，随机值形同废弃）。"""
    spy = UpstreamSpy()
    _, client = _client(spy)
    _converse(client, rank=rank)
    arun(client.aclose())
    msg = spy.seen[0].headers["x-chat-message"]
    assert int(msg.split("re_rank:")[1].split(",")[0]) == rank


def test_strip_watermark_flag_reaches_the_parse():
    spy = UpstreamSpy()
    _, client = _client(spy)
    out = _converse(client, strip=True)
    arun(client.aclose())
    assert "x-bce-process" not in out.images[0].url
    assert out.images[0].note == "已剥离水印处理参数"


# ---------------------------------------------------------------- 错误映射


def test_items_null_is_explicit_failure_with_editor_hint():
    """上游会把 `items` 显式给成 null（实测）—— 不 TypeError，且说清是编辑器深链。"""
    spy = UpstreamSpy(frames=[
        frame("markdown-yiyan", {"value": "好的，已经完成了 去水印 操作。"}),
        frame("image-generate", {"ratio": "", "items": None,
                                 "picEditBaseUrl": "baiduboxapp://v1/browser/open?url=x"}),
    ])
    _, client = _client(spy)
    with pytest.raises(UpstreamUnavailableError) as ei:
        _converse(client, "wenxin:dewatermark")
    arun(client.aclose())
    msg = str(ei.value)
    assert "交互式编辑链接" in msg and "picEditBaseUrl" in msg
    assert ei.value.detail.get("editor_url"), "editor_url 必须随错误带出，便于排障"


def test_kunlun_first_frame_raises_immediately_with_frames_count():
    spy = UpstreamSpy(frames=[kunlun_frame("kunlun_popup"), *default_frames()])
    _, client = _client(spy)
    with pytest.raises(RiskControlError) as ei:
        _converse(client)
    arun(client.aclose())
    assert ei.value.detail["frames"] == 1, "首帧熔断：只能消费到第 1 帧"


def test_token_fail_maps_to_auth_with_actionable_message_and_no_retry():
    """显式配置的凭据 tokenFail ⇒ 不重试（配置问题，不替调用方改写意图）。"""
    spy = UpstreamSpy(frames=[hint_frame("tokenFail")])
    _, client = _client(spy)
    with pytest.raises(AuthError) as ei:
        _converse(client)
    arun(client.aclose())
    assert "searchframeLid" in str(ei.value)
    assert spy.conversations == 1, "显式凭据失败不得重试"


def test_other_hint_is_upstream_error():
    spy = UpstreamSpy(frames=[hint_frame("badRequest", "服务繁忙，请稍后再试")])
    _, client = _client(spy)
    with pytest.raises(UpstreamUnavailableError) as ei:
        _converse(client)
    arun(client.aclose())
    assert "服务繁忙" in str(ei.value)


def test_http_400_maps_to_param_error():
    spy = UpstreamSpy(routes={"/aichat/api/conversation":
                              lambda req: httpx.Response(400, text="bad request")})
    _, client = _client(spy)
    with pytest.raises(UpstreamParamError):
        _converse(client)
    arun(client.aclose())


def test_http_500_maps_to_upstream_unavailable():
    spy = UpstreamSpy(routes={"/aichat/api/conversation":
                              lambda req: httpx.Response(503, text="edge busy")})
    _, client = _client(spy)
    with pytest.raises(UpstreamUnavailableError):
        _converse(client)
    arun(client.aclose())


# ---------------------------------------------------------------- 凭据现取 / 缓存


def test_auto_credentials_are_fetched_from_homepage_and_cached():
    spy = UpstreamSpy()
    conf = settings(SESSION_TOKEN="", LID="")
    client = BaiduClient(conf, transport=spy.transport())

    assert client.creds.cached() == ("", "")
    assert spy.homepages == 0, "cached() 不得触网"

    _converse(client)
    tok, lid = client.creds.cached()
    assert tok == "03abcdef" and lid == "9159525798387571915"
    _converse(client)
    arun(client.aclose())
    assert spy.homepages == 1, "TTL 内不得重复取首页"
    assert client.creds.diagnostics()["source"] == "fetched"


def test_token_fail_with_auto_creds_refreshes_once_and_retries():
    conf = settings(SESSION_TOKEN="", LID="")
    state = {"conv": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/":
            return httpx.Response(200, text=homepage_html("tokA", "lidA"))
        if req.url.path == "/aichat/api/conversation":
            state["conv"] += 1
            if state["conv"] == 1:
                return sse_response([hint_frame("tokenFail")])
            return sse_response(default_frames())
        return httpx.Response(404)

    client = BaiduClient(conf, transport=httpx.MockTransport(handler))
    out = _converse(client)
    arun(client.aclose())
    assert state["conv"] == 2, "应恰好重试一次"
    assert out.refreshed is True
    assert len(out.images) == 1


def test_homepage_redirects_are_followed():
    """🔴 真跑教训（2026-09-24）：`chat.baidu.com/` 会 302 到 `wenxin.baidu.com/?…`，
    凭据在**跳转后的页面**上；httpx 默认**不**跟随重定向（requests 系探针默认跟随，
    所以这个坑只在服务侧暴露：拿到 302 空页 → 误判成「上游改版」）。"""
    state = {"home": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/":
            state["home"] += 1
            return httpx.Response(302, headers={"location": "/landing"})
        if req.url.path == "/landing":
            return httpx.Response(200, text=homepage_html("tokR", "lidR"))
        if req.url.path == "/aichat/api/conversation":
            return sse_response(default_frames())
        return httpx.Response(404)

    conf = settings(SESSION_TOKEN="", LID="")
    client = BaiduClient(conf, transport=httpx.MockTransport(handler))
    out = _converse(client)
    arun(client.aclose())
    assert state["home"] == 1
    assert client.creds.cached() == ("tokR", "lidR")
    assert out.images, "跟随重定向后应能取到凭据并正常出图"


def test_explicit_credentials_never_touch_homepage():
    spy = UpstreamSpy()
    _, client = _client(spy)          # 默认显式 SESSION_TOKEN/LID
    _converse(client)
    arun(client.aclose())
    assert spy.homepages == 0
    assert client.creds.explicit() is True


# ---------------------------------------------------------------- BOS 上传链


def test_upload_image_chain_shapes():
    spy = UpstreamSpy()
    conf, client = _client(spy)
    url, reg = arun(client.upload_image(JPEG_SMALL, "jpeg"))
    arun(client.aclose())

    assert url.startswith(f"{conf.CDN_BASE}/pic_create/test/")
    assert url.endswith(".jpg")
    assert reg.get("status") == 0

    assert spy.paths()[0] == "/aichat/api/file/sts"
    assert "?tk=" in str(spy.seen[0].url), "STS 要带 tk（chat_token）"

    bos = [r for r in spy.seen if r.url.host == "aisearch.bj.bcebos.com"]
    assert len(bos) == 3, "初始化 / 分片 / 合并 三步"
    assert bos[0].method == "POST" and "uploads=" in str(bos[0].url)
    assert bos[0].headers["authorization"].startswith("bce-auth-v1/ak-1/")
    assert bos[0].headers["x-bce-security-token"] == "st-1"
    assert bos[1].method == "PUT" and "partNumber=1" in str(bos[1].url)
    assert bos[1].content == JPEG_SMALL
    # 🔴 jpg 必须映射为 image/jpeg（写成 image/jpg 是历史上的真实失败诱因）
    assert bos[1].headers["content-type"] == "image/jpeg"
    parts = json.loads(bos[2].content)["parts"]
    assert parts == [{"partNumber": 1, "partSize": len(JPEG_SMALL),
                      "eTag": hashlib.md5(JPEG_SMALL).hexdigest()}]

    reg_reqs = [r for r in spy.seen if r.url.path == "/aichat/api/file/upload"]
    assert len(reg_reqs) == 1
    body = json.loads(reg_reqs[0].content)
    assert body["type"] == "image" and body["size"] == len(JPEG_SMALL)
    assert body["path"].startswith("pic_create/test/") and body["name"].endswith(".jpg")
    assert body["id"], "id（base64 时间戳）不能为空"


def test_upload_png_keeps_standard_png_mime():
    spy = UpstreamSpy()
    _, client = _client(spy)
    url, _ = arun(client.upload_image(PNG_SMALL, "png"))
    arun(client.aclose())
    bos_put = [r for r in spy.seen
               if r.url.host == "aisearch.bj.bcebos.com" and r.method == "PUT"][0]
    assert bos_put.headers["content-type"] == "image/png"
    assert url.endswith(".png")


def test_sts_business_error_is_auth_error():
    spy = UpstreamSpy(sts={"status": 4007, "msg": "无权限"})
    _, client = _client(spy)
    with pytest.raises(AuthError):
        arun(client.upload_image(PNG_SMALL, "png"))
    arun(client.aclose())


def test_sts_non_json_is_upstream_error():
    spy = UpstreamSpy(routes={"/aichat/api/file/sts":
                              lambda req: httpx.Response(200, text="<html>challenge</html>")})
    _, client = _client(spy)
    with pytest.raises(UpstreamUnavailableError):
        arun(client.upload_image(PNG_SMALL, "png"))
    arun(client.aclose())


# ---------------------------------------------------------------- 取回上限


def test_fetch_url_ok_and_bounds():
    payload = PNG_SMALL * 3

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "image/png"})

    client = BaiduClient(settings(), transport=httpx.MockTransport(handler))
    data, ct = arun(client.fetch_url("https://img.test/x.png", 1 << 20))
    assert data == payload and ct == "image/png"
    arun(client.aclose())


def test_fetch_url_rejects_by_content_length_and_by_stream():
    def big(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 10,
                              headers={"content-length": str(1 << 21)})

    client = BaiduClient(settings(), transport=httpx.MockTransport(big))
    with pytest.raises(ApiError) as ei:
        arun(client.fetch_url("https://img.test/big.bin", 1 << 20))
    arun(client.aclose())
    assert ei.value.code == "content_too_large"

    # 没有 content-length ⇒ 读流封顶兜住
    client2 = BaiduClient(settings(), transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"x" * (1 << 20 + 5))))
    with pytest.raises(ApiError):
        arun(client2.fetch_url("https://img.test/big2.bin", 1 << 20))
    arun(client2.aclose())


def test_fetch_url_http_error():
    client = BaiduClient(settings(),
                         transport=httpx.MockTransport(lambda req: httpx.Response(404)))
    with pytest.raises(UpstreamUnavailableError):
        arun(client.fetch_url("https://img.test/miss.png", 1 << 20))
    arun(client.aclose())


# ---------------------------------------------------------------- 出口池


def test_pool_rotates_a_fresh_client_per_request(monkeypatch):
    """有池 ⇒ 每请求新建 client 且**只传 proxy**；无池 ⇒ 只传 transport。

    🔴 回归点：httpx 同时给 `transport` 与 `proxy` 时会走代理、把假传输整个绕开
    （textin 轮实测）⇒ 池路径的单测只能走「替换构造器」。
    """
    recorded: list[dict] = []
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        recorded.append(dict(kwargs))
        safe = {k: v for k, v in kwargs.items() if k not in ("proxy", "transport")}
        return real_client(**safe, transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text="{}")))

    monkeypatch.setattr(baidu_client_mod.httpx, "AsyncClient", factory)

    # 无池 + transport：构造函数只带 transport，不带 proxy
    BaiduClient(settings(), transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    assert "transport" in recorded[0] and "proxy" not in recorded[0]

    # 有池：初始 client 直连；每个请求各建一个绑定代理的新 client
    conf = settings(PROXY_POOL="http://p-one:1, http://p-two:2")
    client = BaiduClient(conf)

    async def flow():
        async with client._session():  # noqa: SLF001
            pass
        async with client._session():  # noqa: SLF001
            pass

    arun(flow())
    arun(client.aclose())
    pool_creates = [c for c in recorded if c.get("proxy")]
    assert [c["proxy"] for c in pool_creates] == ["http://p-one:1", "http://p-two:2"]
    assert all("transport" not in c for c in pool_creates)
    assert client.masked_proxies() == ["http://p-one:1", "http://p-two:2"]
    assert client.pool_size == 2


def test_masked_proxies_handles_junk_without_crashing():
    conf = settings(PROXY_POOL="http://u:p@h1:1, :::junk, socks5h://u:p@h2:1080")
    client = BaiduClient(conf)
    masked = client.masked_proxies()
    arun(client.aclose())
    assert masked[0] == "http://h1:1"
    assert masked[1] == "<unparsable>"
    assert masked[2] == "socks5h://h2:1080"


# ---------------------------------------------------------------- 老接口（兜底通路）


def test_legacy_process_create_poll_and_return_bytes():
    spy = UpstreamSpy()
    conf, client = _client(spy)
    raw, info = arun(client.legacy_process("3", JPEG_SMALL))
    arun(client.aclose())
    assert raw == PNG_SMALL                      # mock 的返回值（与输入无关）
    assert info["task_id"] == "lg-1" and info["polls"] == 1
    assert info["create_status"] == 0

    create_req = spy.seen[0]
    assert create_req.url.host == "image.baidu.com"
    assert create_req.url.path == "/aigc/pccreate"
    assert create_req.headers["origin"] == "https://image.baidu.com"
    assert create_req.headers["referer"] == "https://image.baidu.com/"
    assert create_req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = {k: v[0] for k, v in parse_qs(create_req.content.decode()).items()}
    assert form["type"] == "3"
    assert form["picInfo"] == base64.b64encode(JPEG_SMALL).decode()

    q = spy.seen[1]
    assert q.url.path == "/aigc/pcquery" and "taskId=lg-1" in str(q.url)


def test_legacy_process_retries_until_ready():
    spy = UpstreamSpy(legacy_ready_after=3)
    _, client = _client(spy)
    raw, info = arun(client.legacy_process("1", PNG_SMALL))
    arun(client.aclose())
    assert raw == PNG_SMALL and info["polls"] == 3 and spy.legacy_polls == 3


def test_legacy_process_timeout_is_explicit():
    spy = UpstreamSpy(legacy_ready_after=99)
    _, client = _client(spy)                      # LEGACY_POLL_TRIES=5
    with pytest.raises(UpstreamUnavailableError) as ei:
        arun(client.legacy_process("3", PNG_SMALL))
    arun(client.aclose())
    assert "超时未出图" in str(ei.value)
    assert ei.value.detail.get("task_id") == "lg-1"


def test_legacy_process_rejected_create_is_explicit():
    spy = UpstreamSpy(legacy_create_status=5)     # 「网络异常」形态
    _, client = _client(spy)
    with pytest.raises(UpstreamUnavailableError) as ei:
        arun(client.legacy_process("3", PNG_SMALL))
    arun(client.aclose())
    assert "拒绝创建" in str(ei.value) and "status=5" in str(ei.value)
