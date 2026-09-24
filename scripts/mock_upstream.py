#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假上游（stdlib、零依赖）：复刻 chat.baidu.com + BOS + 结果 CDN 的最小形态。

用途有三个，都是**零真实上游请求**：

1. `scripts/smoke.sh`：真起服务 + 真 HTTP + 假上游 ⇒ 端到端冒烟；
2. `scripts/probe.py --phases loop`：把每条能力从 HTTP 端点打到"上游"再回装配；
3. 本地联调：`BAIDU_BASE_URL=http://127.0.0.1:<port>`、`BAIDU_BOS_HOST=http://127.0.0.1:<port>/bos`、
   `BAIDU_CDN_BASE=http://127.0.0.1:<port>/img` 指向本服务即可。

它还记录**服务真正发过来的东西**（路径 / 方法 / cookie 是否存在 / tk / content-type /
body 头 8 字节 / conversation 的关键字段），经 `GET /mock/log` 读回 —— 冒烟能把
"链路两端都验证了"，而不只是"服务没报错"。

场景开关（可在运行期切换，供冒烟分阶段断言）：
  · `GET  /mock/scenario`        读当前场景
  · `POST /mock/scenario`        `{"mode": "ok|kunlun|tokenfail|items_null|http500"}`
  · 环境变量 `MOCK_SCENARIO=<mode>` 设初始值（默认 ok）

帧形态与真上游**逐类同形**（`data:` 行 + 三处组件名），产物图为一张 24x16 的真 PNG。

用法：`python scripts/mock_upstream.py [port]`（默认 8800）
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- 样本

#: 24x16 的真 PNG（78 字节，**固化** —— 冒烟用例按同一串字节断言，
#: 故假上游必须原样返回；自己现生成会因压缩差异对不上）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000018000000100802000000aa17c1a0"
    "0000001a49444154789c63fcffff3f0326c8280a6360148500a3280c0083b80a1d"
    "0000000049454e44ae426082")
#: 老接口的返回形态：`picArr[0].src` 是 data URI（2026-09-24 实测）
LEGACY_SRC = "data:image/png;base64," + base64.b64encode(PNG).decode()
HOMEPAGE = (
    '<html><body><script type="application/json" id="x" '
    'name="aiTabFrameBaseData">{"token":"03abcdef","lid":"9159525798387571915",'
    '"userInfo":{}}</script></body></html>'
)
STS = {"status": 0, "data": {"ak": "ak-mock", "sk": "sk-mock", "token": "st-mock",
                             "preFixPath": "pic_create/mock/", "bucketName": "aisearch",
                             "bceUrl": "https://aisearch.bj.bcebos.com"}}


def _frame(component: str, data: object) -> dict:
    return {"status": 0, "data": {"message": {"content": {
        "generator": {"component": component, "data": data}}}}}


def _sse(mode: str, cdn_base: str) -> str:
    if mode == "kunlun":
        frames = [{"status": 0, "query": "变清晰", "chatHitKunlun": "kunlun_popup"}]
    elif mode == "tokenfail":
        frames = [{"status": 1001, "data": {"message": {"content": {
            "generator": {"component": "markdown-yiyan", "data": {"value": "出了点小问题"}},
            "hints": {"parts": [{"type": "tokenFail", "text": "😩抱歉，出了点小问题"}]}}}}}]
    elif mode == "items_null":
        frames = [_frame("markdown-yiyan", {"value": "好的，已经完成了 变清晰 操作。"}),
                  _frame("image-generate", {"ratio": "", "items": None,
                                            "picEditBaseUrl": "baiduboxapp://mock/edit"})]
    else:  # ok
        frames = [_frame("markdown-yiyan", {"value": "好的，根据你的需求，已经完成了 变清晰 操作。"}),
                  _frame("image-generate", {"ratio": "3-2", "items": [
                      {"originUrl": f"{cdn_base}/out.png?x-bce-process=image/watermark,mock",
                       "previewUrl": f"{cdn_base}/out_p.png",
                       "width": 24, "height": 16}]})]
    return "".join("data: " + json.dumps(f, ensure_ascii=False) + "\n\n" for f in frames)


# --------------------------------------------------------------------------- 记录

_LOG: list[dict] = []
_LOCK = threading.Lock()
_MODE = {"mode": os.environ.get("MOCK_SCENARIO", "ok").strip() or "ok"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_base = ""                     # main() 里写入（http://127.0.0.1:<port>）

    def log_message(self, *_args: object) -> None:  # 静默（冒烟输出要干净）
        return

    # ---- 工具 ----

    def _send(self, status: int, payload: object, content_type: str = "application/json") -> None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode()
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = str(payload).encode()
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def _record(self, method: str, path: str, body: bytes, extra: dict | None = None) -> None:
        entry: dict = {
            "method": method,
            "path": path,
            "host": self.headers.get("host", ""),
            "has_cookie": bool(self.headers.get("cookie")),
            "tk": (parse_qs(urlparse(self.path).query).get("tk") or [""])[0][:12],
            "content_type": self.headers.get("content-type") or "",
            "auth_bce": (self.headers.get("authorization") or "").startswith("bce-auth-v1/"),
            "body_head": body[:8].hex(),
            "body_len": len(body),
        }
        if extra:
            entry.update(extra)
        with _LOCK:
            _LOG.append(entry)

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/mock/log"):
            with _LOCK:
                self._send(200, {"entries": list(_LOG)})
            return
        if path.startswith("/mock/health"):
            self._send(200, {"ok": True, "mode": _MODE["mode"]})
            return
        if path.startswith("/mock/scenario"):
            self._send(200, {"mode": _MODE["mode"]})
            return
        if path == "/":
            self._record("GET", path, b"")
            self._send(200, HOMEPAGE, "text/html; charset=utf-8")
            return
        if path == "/aichat/api/file/sts":
            self._record("GET", path, b"")
            self._send(200, STS)
            return
        if path == "/aigc/pcquery":                      # 老接口：轮询
            self._record("GET", path, b"")
            if _MODE["mode"] == "legacy_notready":
                self._send(200, {"isGenerate": False, "progress": 30})
                return
            self._send(200, {"isGenerate": True, "progress": 100,
                             "picArr": [{"src": LEGACY_SRC}]})
            return
        if path.startswith("/img/"):                     # 结果 CDN
            self._record("GET", path, b"")
            self._send(200, PNG, "image/png")
            return
        self._record("GET", path, b"")
        self._send(404, {"error": "unknown path"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        body = self._read_body()

        if path.startswith("/mock/scenario"):
            try:
                mode = str(json.loads(body or b"{}").get("mode", "ok"))
            except ValueError:
                mode = "ok"
            _MODE["mode"] = mode
            self._send(200, {"mode": mode})
            return

        if path == "/aichat/api/conversation":
            extra = {}
            try:
                d = json.loads(body)
                si = d["message"]["searchInfo"]
                ext = json.loads(si["mcpInfo"]["ext"])
                extra = {"tool_type": ext.get("type"), "sa": si.get("sa"),
                         "query": d["message"]["query"][1]["data"]["text"]["query"],
                         "has_chat_token": bool(si["chatParams"].get("chat_token"))}
            except Exception:  # noqa: BLE001 - 解析失败也要留档
                extra = {"parse": "failed"}
            mode = _MODE["mode"]
            if mode == "http500":
                self._record("POST", path, body, extra)
                self._send(500, {"error": "mock 500"})
                return
            self._record("POST", path, body, extra)
            self._send(200, _sse(mode, self.server_base + "/img"),
                       "text/event-stream; charset=utf-8")
            return

        if path == "/aichat/api/file/upload":
            self._record("POST", path, body)
            self._send(200, {"status": 0, "data": {"file_url": "mock://registered"}})
            return

        if path == "/aigc/pccreate":                     # 老接口：建任务
            form = {k: v[0] for k, v in
                    parse_qs(body.decode("utf-8", "replace")).items()}
            self._record("POST", path, body, {
                "type": form.get("type"),
                "picInfo_len": len(form.get("picInfo") or ""),
                # 新能力通路的透传证据（消除/局部替换的遮罩、AI重绘/相似图的档位、替换文本）
                "picInfo2_len": len(form.get("picInfo2") or ""),
                "create_level": form.get("create_level"),
                "text": form.get("text"),
                "ext_ratio": form.get("ext_ratio"),
            })
            if _MODE["mode"] == "legacy_reject":
                self._send(200, {"status": 5, "message": "网络异常，请稍候再试~"})
                return
            self._send(200, {"status": 0, "pcEditTaskid": "mock-legacy-1", "resType": 0})
            return

        if path.startswith("/bos/"):                     # BOS 四段式（假）
            if "uploads=" in (parsed.query or ""):
                self._record("POST", path, body)
                self._send(200, {"uploadId": "u-mock"})
                return
            if query.get("uploadId"):                    # complete
                self._record("POST", path, body)
                self._send(200, "<CompleteMultipartUploadResult/>".encode(),
                           "application/xml")
                return
            self._record("POST", path, body)
            self._send(404, {"error": "unknown bos post"})
            return

        self._record("POST", path, body)
        self._send(404, {"error": "unknown path"})

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_body()
        self._record("PUT", parsed.path, body)
        if parsed.path.startswith("/bos/"):
            self._send(200, b"", "application/octet-stream")
            return
        self._send(404, {"error": "unknown path"})


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8800
    Handler.server_base = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock upstream on http://127.0.0.1:{port}（mode={_MODE['mode']}）")
    server.serve_forever()


if __name__ == "__main__":
    main()
