"""测试样本与假上游（供各用例 import；**全部本地生成、零网络**）。

样本的真实性分两档，用例按需选择：

- **格式真实**（PNG/JPEG）：真的能被解码器读出像素 —— `image_size()` 有断言价值；
- **帧真实**（SSE）：帧结构与上游实证一致（`data:` 行 + 三处组件名），
  但**不是**真实抓包 —— 端到端真跑请用 `scripts/probe.py --live`。
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs

import httpx

from app.config import Settings

__all__ = [
    "arun",
    "settings",
    "PNG_SMALL",
    "JPEG_SMALL",
    "data_uri",
    "b64",
    "homepage_html",
    "frame",
    "kunlun_frame",
    "hint_frame",
    "sse_text",
    "sse_response",
    "json_response",
    "STS_OK",
    "register_ok",
    "make_upstream",
    "UpstreamSpy",
]


def arun(coro: Any) -> Any:
    """同步用例里跑协程（避免为几个单测引入 pytest-asyncio）。"""
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    """测试用配置：显式给值、**不读 .env 也不受环境变量影响**。

    `_env_file=None` 只关掉 .env 文件；环境变量仍会生效 ⇒ 把关键字段逐个钉死，
    避免本机/CI 的 BAIDU_* 环境变量把用例带偏。
    """
    base: dict[str, Any] = {
        "API_KEYS": "",
        "COOKIE": "BAIDUID=test-cookie",        # 已就绪；测「未配」的用例显式清空
        "SESSION_TOKEN": "03abcdef",            # 显式凭据 ⇒ 不触网取首页
        "LID": "9159525798387571915",
        "ORI_LID": "",
        "UPLOAD_MODE": "bos",
        "PROMPT_MODE": "ignore",
        "STRIP_WATERMARK": True,
        "NORMALIZE": False,                      # 测试默认关（避免 PIL 改写字节）
        "MAX_SIDE": 2048,
        "MAX_BYTES": 2 * 1024 * 1024,
        "MAX_DOWNLOAD_MB": 30,
        "TIMEOUT": 10.0,
        "MIN_INTERVAL": 0.0,
        "PER_MINUTE": 60,
        "MAX_WAIT": 5.0,
        "COOLDOWN": 0.0,
        "ALLOW_UNVERIFIED": False,
        "PROXY_POOL": "",
        "LEGACY": "off",
        "LEGACY_BASE": "https://image.baidu.com",
        "LEGACY_POLL_INTERVAL": 0.01,
        "LEGACY_POLL_TRIES": 5,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------


def _png(w: int = 24, h: int = 16, rgb: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    """手写 PNG（zlib+struct，无第三方依赖）：`image_size` 能解析出真实宽高。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (struct.pack(">I", len(data)) + payload
                + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG_SMALL = _png()
#: 24x16 真 JPEG（636B，PIL 生成一次后固化 —— 测试不依赖 PIL）
JPEG_SMALL = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZG"
    "NywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09P"
    "T09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAQABgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAA"
    "AAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAk"
    "M2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKT"
    "lJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QA"
    "HwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdh"
    "cRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hp"
    "anN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDEooornPrwooooA//Z"
)


def data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------------------
# SSE 帧 / 假上游
# ---------------------------------------------------------------------------


def homepage_html(token: str = "03abcdef", lid: str = "9159525798387571915") -> str:
    return ("<html><body><script type=\"application/json\" id=\"x\" "
            "name=\"aiTabFrameBaseData\">"
            + json.dumps({"token": token, "lid": lid, "userInfo": {}})
            + "</script></body></html>")


def frame(component: str, data: Any) -> dict[str, Any]:
    return {"status": 0, "data": {"message": {"content": {
        "generator": {"component": component, "data": data}}}}}


def kunlun_frame(value: str = "kunlun_popup") -> dict[str, Any]:
    return {"status": 0, "query": "变清晰", "chatHitKunlun": value}


def hint_frame(htype: str, text: str = "😩抱歉，出了点小问题") -> dict[str, Any]:
    return {"status": 1001, "data": {"message": {"content": {
        "generator": {"component": "markdown-yiyan", "data": {"value": "出了点小问题"}},
        "hints": {"parts": [{"type": htype, "text": text}]}}}}}


IMG_ITEM = {"originUrl": "https://cdn.bce.test/out.png?x-bce-process=image/watermark,1",
            "previewUrl": "https://cdn.bce.test/out_p.png", "width": 24, "height": 16}


def default_frames() -> list[dict[str, Any]]:
    return [
        frame("markdown-yiyan", {"value": "好的，根据你的需求，已经完成了 变清晰 操作。"}),
        frame("image-generate", {"ratio": "3-2", "items": [IMG_ITEM]}),
    ]


def sse_text(frames: Iterable[dict[str, Any]]) -> str:
    return "".join("data: " + json.dumps(f, ensure_ascii=False) + "\n\n" for f in frames)


def sse_response(frames: Iterable[dict[str, Any]], status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=sse_text(frames),
                          headers={"content-type": "text/event-stream"})


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


STS_OK: dict[str, Any] = {"status": 0, "data": {
    "ak": "ak-1", "sk": "sk-1", "token": "st-1", "preFixPath": "pic_create/test/",
    "bucketName": "aisearch", "bceUrl": "https://aisearch.bj.bcebos.com"}}


def register_ok(file_url: str = "ok") -> dict[str, Any]:
    return {"status": 0, "data": {"file_url": file_url}}


class UpstreamSpy:
    """假上游：按路径路由并把每条请求记录下来（断言用）。"""

    def __init__(
        self,
        *,
        frames: list[dict[str, Any]] | None = None,
        result: bytes = PNG_SMALL,
        result_status: int = 200,
        homepage: str | None = None,
        sts: dict[str, Any] | None = None,
        routes: dict[str, Callable[[httpx.Request], httpx.Response]] | None = None,
        legacy_create_status: int = 0,
        legacy_ready_after: int = 1,
    ) -> None:
        self.frames = frames if frames is not None else default_frames()
        self.result = result
        self.result_status = result_status
        self.homepage = homepage if homepage is not None else homepage_html()
        self.sts = sts if sts is not None else STS_OK
        self.routes = routes or {}
        self.legacy_create_status = legacy_create_status
        self.legacy_ready_after = legacy_ready_after
        self.seen: list[httpx.Request] = []
        self.conversations = 0
        self.homepages = 0
        self.legacy_creates = 0
        self.legacy_polls = 0
        #: 老接口 create 的**表单字段原文**（断言用：type / picInfo / picInfo2 / create_level / text …）
        self.legacy_forms: list[dict[str, str]] = []
        #: 老接口 poll 的**查询参数原文**
        self.legacy_query_params: list[dict[str, str]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        url, host, path = str(req.url), req.url.host, req.url.path
        self.seen.append(req)
        if path in self.routes:
            return self.routes[path](req)
        if path == "/aigc/pccreate":                        # 老接口（image.baidu.com/aigc）
            self.legacy_creates += 1
            self.legacy_forms.append(
                {k: v[0] for k, v in parse_qs(req.content.decode("utf-8")).items()})
            if self.legacy_create_status != 0:
                return json_response({"status": self.legacy_create_status,
                                      "message": "mock 拒绝"})
            return json_response({"status": 0, "pcEditTaskid": "lg-1", "resType": 0})
        if path == "/aigc/pcquery":
            self.legacy_polls += 1
            self.legacy_query_params.append(
                {k: v[0] for k, v in parse_qs(req.url.query.decode("utf-8")).items()})
            if self.legacy_polls < self.legacy_ready_after:
                return json_response({"isGenerate": False, "progress": 30})
            return json_response({"isGenerate": True, "progress": 100,
                                  "picArr": [{"src": "data:image/png;base64,"
                                              + b64(self.result)}]})
        if host == "aisearch.bj.bcebos.com":               # BOS 四段式
            if req.method == "POST" and "uploads=" in url:
                return json_response({"uploadId": "u-1"})
            if req.method == "PUT":
                return httpx.Response(200, text="")
            return httpx.Response(200, text="<CompleteMultipartUploadResult/>")
        if path == "/aichat/api/file/sts":
            return json_response(self.sts)
        if path == "/aichat/api/file/upload":
            return json_response(register_ok())
        if path == "/aichat/api/conversation":
            self.conversations += 1
            return sse_response(self.frames)
        if path == "/":
            self.homepages += 1
            return httpx.Response(200, text=self.homepage)
        if host == "cdn.bce.test":                          # 结果图下载
            return httpx.Response(self.result_status, content=self.result)
        return httpx.Response(404, json={"unexpected": url})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.seen]


def make_upstream(**kwargs: Any) -> UpstreamSpy:
    return UpstreamSpy(**kwargs)
