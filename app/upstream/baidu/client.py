#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度文心上游 HTTP 客户端。

设计要点（每条都有理由，不是风格）：

1. **`trust_env=False`**：环境里的 `HTTP(S)_PROXY` 会把对外请求也代理走，
   对带鉴权的 POST 常表现为静默挂起或 broken pipe，且两侧都没有日志。
   走不走代理由部署方用 `BAIDU_PROXY_POOL` 显式决定，不由宿主环境偶然决定。
2. **出口池**：文心的门禁是**出口（IP）维度的风险评分**（docs/UPSTREAM.md §7）——
   换到未滥用出口立即恢复；身份/cookie 维度已证否。有池时**每个请求新建 client
   并绑定下一个代理**：轮换的保证来自「每请求一个新连接」，不是代理凭据
   （池通常按 TCP 连接轮换出口）。
   🔴 httpx 同时给 `transport` 与 `proxy` 时会走代理、把假传输整个绕开
   ⇒ 池路径的单测走**替换构造器**（见 tests/test_client.py）。
3. **判成败靠 SSE 帧内容**（`chatHitKunlun` / `hints` / `items`），不是 HTTP 状态 ——
   判读集中在 `capabilities.apply_frame` + 本文件的 `_converse_once` 尾部。
4. **`dry_run` 绝不触网**（由服务层保证：干跑只调用 `creds.cached()` 与纯函数）。
5. **tokenFail 自动重试一次**：`token` 可现取（TTL 540s），刷新后重试一次；
   但**显式配置**的凭据失败不重试 —— 那是配置问题，不替调用方改写意图。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import itertools
import json
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from loguru import logger
from PIL import Image

from ...config import Settings, get_settings
from ...errors import (
    ApiError,
    AuthError,
    RiskControlError,
    UpstreamParamError,
    UpstreamTimeout,
    UpstreamUnavailableError,
)
from ...media import mime_ext, upload_ext
from ...models import Capability
from .capabilities import (
    SseOutcome,
    apply_frame,
    bce_sign,
    build_body,
    build_chat_token,
    extract_creds_from_homepage,
    extract_legacy_result,
    legacy_form,
)

__all__ = ["BaiduClient", "Credentials"]

#: 与实证探针逐字一致的浏览器 UA（改 UA 必须整仓一起改）。
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

#: 代取/结果下载的独立超时（与上游处理超时无关）。
_FETCH_TIMEOUT = 30.0


def fit_legacy_image(data: bytes, *, max_side: int, max_bytes: int
                     ) -> tuple[bytes, dict[str, Any] | None]:
    """把**老接口入参**缩到实测可接受的范围（只在超限时动；非图片或缩不动则原样返回）。

    依据（2026-09-24 实测，`type=1` 去水印，全部 base64 直传、同一条链路）：
    **600×400 / 39.9KB ⇒ 收单；800×533 / 63.2KB、900×600 / 73.3KB ⇒ `resType=2` 拒**。
    同一时刻 `type=3` 用同一张大图收单 ⇒ 不是链路/cookie 问题，是**入参体积**。
    ⇒ 服务端自动兼容：超限先等比压缩，保证"大图不再直接失败"。
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            long_side = max(w, h)
            need = long_side > max_side or len(data) > max_bytes
            if not need:
                return data, None
            scale = min(1.0, max_side / long_side) if long_side else 1.0
            cur = im.convert("RGB")
            if scale < 1.0:
                cur = cur.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                 Image.LANCZOS)
            for quality in (85, 75, 65, 55, 45):
                buf = io.BytesIO()
                cur.save(buf, "JPEG", quality=quality, optimize=True)
                out = buf.getvalue()
                if len(out) <= max_bytes:
                    break
            else:                                # 压到最低质量仍超限 ⇒ 继续缩尺寸（有界）
                for _ in range(3):
                    cur = cur.resize((max(1, cur.width * 4 // 5), max(1, cur.height * 4 // 5)),
                                     Image.LANCZOS)
                    buf = io.BytesIO()
                    cur.save(buf, "JPEG", quality=65, optimize=True)
                    out = buf.getvalue()
                    if len(out) <= max_bytes:
                        break
            meta = {"from_size": [w, h], "to_size": [cur.width, cur.height],
                    "bytes_in": len(data), "bytes_out": len(out)}
            return out, meta
    except (OSError, ValueError):                # 不是图片 / 解不开 ⇒ 原样交给上游去报错
        return data, None


class Credentials:
    """`chat_token` 需要的 (token, lid) 从哪来、怎么缓存。

    2026-09-12 用 A/B 对照把三件事分开了（同参数、间隔数秒）：

      · `BAIDU_COOKIE` —— **必需**。去掉 cookie 立刻变「抱歉，服务繁忙，请稍后再试」；
        它也是三者里唯一长效的。
      · `token` / `lid` —— **可现取**（内嵌在首页 HTML 的 `aiTabFrameBaseData` 里）。
      · `BAIDU_SESSION_TOKEN` / `BAIDU_LID` —— 手工填的值实测只活 ≥10 分钟，
        本就不该当配置项。显式配了就用配置（排障 / 防上游改版时钉住），否则自动现取。
    """

    #: 与实测存活（≥10 分钟）对齐，留一点余量
    TTL = 540.0

    def __init__(self, settings: Settings,
                 fetch: Callable[[], Awaitable[tuple[str, str]]]) -> None:
        self._s = settings
        self._fetch = fetch
        self._token = ""
        self._lid = ""
        self._at = 0.0
        self._lock = asyncio.Lock()
        self.fetches = 0

    def explicit(self) -> bool:
        """调用方是否显式配置了 token + lid。配了就**不再**自动现取。"""
        return bool(self._s.SESSION_TOKEN and self._s.LID)

    def cached(self) -> tuple[str, str]:
        """已可用的 (token, lid)，没有则空串。**不触网** —— 干跑用它。"""
        if self.explicit():
            return self._s.SESSION_TOKEN, self._s.LID
        if self._token and (time.time() - self._at) < self.TTL:
            return self._token, self._lid
        return "", ""

    async def get(self, *, force: bool = False) -> tuple[str, str]:
        """取 (token, lid)。`force=True` 忽略**缓存**重新现取（tokenFail 重试用）。

        ⚠️ 显式配置的凭据**永远优先**，`force` 也不能越过它 —— 否则「钉住一组值排障」
        会在 tokenFail 之后被悄悄改写成现取值，排障前提就没了。
        并发去重：拿锁后再查一次缓存（冷启动时多个请求只取一次首页）。
        """
        if self.explicit():
            return self._s.SESSION_TOKEN, self._s.LID
        if not force:
            got = self.cached()
            if got[0]:
                return got
        async with self._lock:
            if not force:
                got = self.cached()
                if got[0]:
                    return got
            token, lid = await self._fetch()
            self._token, self._lid, self._at = token, lid, time.time()
            self.fetches += 1
            return token, lid

    def diagnostics(self) -> dict[str, Any]:
        tok, _ = self.cached()
        return {
            "source": ("explicit" if self.explicit()
                       else ("fetched" if tok else "not-yet-fetched")),
            "fetches": self.fetches,
            "ttl_s": self.TTL,
            "cookie_configured": bool(self._s.COOKIE),
        }


class BaiduClient:
    """一次构造 = 一个直连连接池（无池时）/ 一个代理轮换器（有池时）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._s = settings or get_settings()
        #: 测试注入的假传输：**只在无代理路径使用**（见类文档 🔴）。
        self._transport = transport
        self._proxies = [p.strip() for p in self._s.PROXY_POOL.split(",") if p.strip()]
        self._rr = itertools.count()
        self._client = self._build_client(None)
        self.creds = Credentials(self._s, self._fetch_homepage_creds)

    # ------------------------------------------------------------------ 会话

    def _build_client(self, proxy: str | None) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self._s.BASE_URL.rstrip("/"),
            "timeout": self._s.TIMEOUT,
            "headers": {"accept": "application/json"},
            "trust_env": False,
        }
        if proxy:
            kwargs["proxy"] = proxy
        elif self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def next_proxy(self) -> str | None:
        """轮询取下一个代理；池为空返回 None（直连）。"""
        if not self._proxies:
            return None
        return self._proxies[next(self._rr) % len(self._proxies)]

    def masked_proxies(self) -> list[str]:
        """池的**脱敏**视图（只留 scheme://host:port），供 `/readyz` 与日志。"""
        out: list[str] = []
        for raw in self._proxies:
            try:
                u = httpx.URL(raw)
                host = u.host or ""
                port = f":{u.port}" if u.port else ""
                out.append(f"{u.scheme}://{host}{port}")
            except Exception:  # noqa: BLE001 - 坏配置不该让健康面炸掉
                out.append("<unparsable>")
        return out

    @property
    def pool_size(self) -> int:
        return len(self._proxies)

    @property
    def endpoint(self) -> str:
        return f"{self._s.BASE_URL.rstrip('/')}/aichat/api/conversation"

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        """取一个会话。

        - **无池**：复用长命 client（连接池有效）；
        - **有池**：**每个请求新建一个 client 并绑定该代理**。轮换的保证来自
          「每请求一个新连接」—— 复用连接会拿回同一个出口 IP（实测）。
        """
        proxy = self.next_proxy()
        if proxy is None:
            yield self._client
            return
        client = self._build_client(proxy)
        try:
            yield client
        finally:
            await client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BaiduClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ 出站头

    def _browser_headers(self, *, content_type: str | None = None,
                         accept: str | None = None) -> dict[str, str]:
        """百度系请求的公共头（与实证探针逐字一致；`Cookie` 是唯一凭据）。"""
        h: dict[str, str] = {
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": _UA,
            "Origin": "https://wenxin.baidu.com",
            "Referer": "https://wenxin.baidu.com/",
            "Cookie": self._s.COOKIE,
        }
        if content_type:
            h["Content-Type"] = content_type
        if accept:
            h["accept"] = accept
        return h

    async def _tk(self, query: str = "") -> str:
        """上传链路用的 `tk`（= 同款 chat_token，query 传空串，与实证脚本一致）。"""
        tok, lid = await self.creds.get()
        return build_chat_token(tok, query, lid)

    # ------------------------------------------------------------------ 凭据现取

    async def _fetch_homepage_creds(self) -> tuple[str, str]:
        """GET `/` 取 `aiTabFrameBaseData`（契约 §8.2：token/lid 内嵌在首页，可现取）。

        🔴 **必须 follow_redirects**：真跑实测（2026-09-24）`chat.baidu.com/` 会
        `302 → https://wenxin.baidu.com/?enter_type=chat_site`，凭据在**跳转后的页面**上。
        httpx 默认不跟随重定向（requests 系探针默认跟随，所以这个坑只在服务侧暴露）；
        不跟随会拿到 302 空页 → 误判成「上游改版」。
        """
        async with self._session() as client:
            try:
                r = await client.get("/", headers=self._browser_headers(),
                                     timeout=min(30.0, self._s.TIMEOUT),
                                     follow_redirects=True)
            except httpx.HTTPError as exc:
                raise UpstreamUnavailableError(
                    f"文心首页请求失败（取 chat_token 用）：{exc}") from exc
        return extract_creds_from_homepage(r.text or "", http_status=r.status_code)

    # ------------------------------------------------------------------ 外链/结果取回

    @staticmethod
    def _fetch_retryable(exc: Exception) -> bool:
        """该错误值不值得重试 —— **只重试瞬时类**：

        · 超时（跨境取上游结果 CDN 偶发慢，2026-09-24 实测一天 2 次）
        · 网络错误（无 http_status）
        · HTTP 5xx / 429
        不重试：4xx、`content_too_large`（重试只会重复失败且更慢）。
        """
        if isinstance(exc, UpstreamTimeout):
            return True
        if isinstance(exc, UpstreamUnavailableError):
            s = exc.http_status
            return s is None or s >= 500 or s == 429
        return False

    async def fetch_url(self, url: str, max_bytes: int, *,
                        timeout: float | None = None, retries: int = 0) -> tuple[bytes, str | None]:
        """代取调用方给的输入 / 取回上游结果（有硬字节上限 + **瞬时错误重试**）。

        `retries` 只对瞬时错误生效（见 `_fetch_retryable`），退避 0.5s/1s/2s…
        —— 上游结果 CDN 跨境偶发慢时的 504 就是靠它消化的。
        """
        tries = max(0, int(retries))
        for attempt in range(tries + 1):
            try:
                return await self._fetch_once(url, max_bytes, timeout)
            except (UpstreamTimeout, UpstreamUnavailableError, ApiError) as exc:
                if attempt >= tries or not self._fetch_retryable(exc):
                    raise
                delay = 0.5 * (2 ** attempt)
                logger.warning(f"取回瞬时失败（{type(exc).__name__}: {exc}），"
                               f"{delay:.1f}s 后重试 {attempt + 1}/{tries}（{url[:80]}）")
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")           # pragma: no cover

    async def _fetch_once(self, url: str, max_bytes: int,
                          timeout: float | None = None) -> tuple[bytes, str | None]:
        """单次取回（不做重试）。预检 `Content-Length` + 读流封顶双保险；
        取不到就显式失败，**绝不把半个文件当完整内容用**。
        """
        declared_ct: str | None = None
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._client.stream("GET", url, timeout=timeout or _FETCH_TIMEOUT,
                                           follow_redirects=True) as resp:
                if resp.status_code >= 400:
                    raise UpstreamUnavailableError(
                        f"取回失败：HTTP {resp.status_code}（{url[:120]}）",
                        http_status=resp.status_code,
                    )
                declared_ct = resp.headers.get("content-type")
                declared_len = resp.headers.get("content-length")
                if declared_len and declared_len.isdigit() and int(declared_len) > max_bytes:
                    raise ApiError(
                        400, "content_too_large",
                        f"取回内容超过上限（{declared_len} 字节 > {max_bytes} 字节）",
                    )
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ApiError(
                            400, "content_too_large",
                            f"取回内容超过上限（已读 {total} 字节 > {max_bytes} 字节）",
                        )
                    chunks.append(chunk)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(f"取回超时（{url[:120]}）") from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"取回网络错误：{exc}") from exc
        return b"".join(chunks), declared_ct

    # ------------------------------------------------------------------ BOS 转存

    async def _sts(self, client: httpx.AsyncClient) -> dict[str, Any]:
        r = await client.get("/aichat/api/file/sts", params={"tk": await self._tk()},
                             headers=self._browser_headers(content_type="application/json"),
                             timeout=30)
        try:
            d = r.json()
        except ValueError as exc:
            raise UpstreamUnavailableError(
                f"STS 返回非 JSON（HTTP {r.status_code}），可能被网关拦截",
                detail={"body_head": r.text[:200]},
            ) from exc
        if d.get("status") != 0:
            raise AuthError(f"获取上传凭证失败：{d.get('msg') or d}",
                            detail={"status": d.get("status")})
        return d["data"]

    async def _register(self, client: httpx.AsyncClient, path: str, size: int,
                        name: str) -> dict[str, Any]:
        bid = base64.b64encode(
            f"{int(time.time() * 1000)}{path.split('/')[-1]}".encode()
        ).decode()
        payload = {"path": path, "size": size, "name": name, "id": bid, "type": "image"}
        r = await client.post("/aichat/api/file/upload", params={"tk": await self._tk()},
                              headers=self._browser_headers(content_type="application/json"),
                              content=json.dumps(payload, ensure_ascii=False).encode(),
                              timeout=60)
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text[:200]}

    async def _bos_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        query: str,
        sts: dict[str, Any],
        *,
        body: bytes | None = None,
        content_type: str = "application/octet-stream",
    ) -> httpx.Response:
        # BOS_HOST 允许带 scheme：联调/冒烟时指向本地假上游（默认值不带 = https）
        raw_host = self._s.BOS_HOST.strip().rstrip("/")
        if raw_host.startswith(("http://", "https://")):
            import urllib.parse  # noqa: PLC0415

            bos_base = raw_host
            host_header = urllib.parse.urlparse(raw_host).netloc
        else:
            bos_base = f"https://{raw_host}"
            host_header = raw_host
        url = f"{bos_base}{path}" + (f"?{query}" if query else "")
        headers: dict[str, str] = {
            "host": host_header,
            "content-type": content_type,
            "x-bce-security-token": sts["token"],
        }
        signed = ["content-type", "host", "x-bce-date", "x-bce-security-token"]
        if body is not None:
            headers["content-length"] = str(len(body))
            signed.append("content-length")
        auth, ts = bce_sign(method, path, query, headers, sts["ak"], sts["sk"], signed)
        headers["x-bce-date"] = ts
        headers["authorization"] = auth
        send = {k: v for k, v in headers.items() if k != "host"}
        return await client.request(method, url, headers=send, content=body, timeout=120)

    async def upload_image(self, data: bytes, kind: str) -> tuple[str, dict[str, Any]]:
        """图片字节 → 百度自家 BOS + 登记，返回上游抓得到的 CDN URL（免登录，纯 HTTP）。

        链路（契约 §7 兜底方案）：① `GET /aichat/api/file/sts` → ② 初始化分片 →
        ③ PUT 分片 → ④ 合并 → ⑤ `POST /aichat/api/file/upload` 登记。

        ⚠️ `jpg` 必须映射为 `image/jpeg`（写成 `image/jpg` 是历史上的真实失败诱因）；
        整条链**共用一个 session** —— 有池时保证 5 步落在同一出口上。
        """
        ctype = mime_ext(kind)[0]
        name = f"{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}.{upload_ext(kind)}"
        async with self._session() as client:
            sts = await self._sts(client)
            prefix = str(sts.get("preFixPath") or sts.get("prefixPath") or "")
            path = f"/{prefix}{name}"

            r1 = await self._bos_request(client, "POST", path, "uploads=", sts,
                                         body=b"", content_type=ctype)
            upload_id = ""
            try:
                upload_id = str(r1.json()["uploadId"])
            except Exception:  # noqa: BLE001
                import re  # noqa: PLC0415

                m = re.search(r"<UploadId>([^<]+)</UploadId>", r1.text)
                upload_id = m.group(1) if m else ""
            if not upload_id:
                raise UpstreamUnavailableError(
                    "初始化分片上传失败",
                    detail={"status": r1.status_code, "body_head": r1.text[:200]},
                )

            r2 = await self._bos_request(client, "PUT", path,
                                         f"partNumber=1&uploadId={upload_id}", sts,
                                         body=data, content_type=ctype)
            if r2.status_code != 200:
                raise UpstreamUnavailableError(
                    "上传分片失败",
                    detail={"status": r2.status_code, "body_head": r2.text[:200]},
                )

            etag = hashlib.md5(data).hexdigest()
            r3 = await self._bos_request(
                client, "POST", path, f"uploadId={upload_id}", sts,
                body=json.dumps({"parts": [{"partNumber": 1, "partSize": len(data),
                                            "eTag": etag}]}).encode(),
                content_type="application/json; charset=UTF-8",
            )
            if r3.status_code != 200:
                raise UpstreamUnavailableError(
                    "合并分片失败",
                    detail={"status": r3.status_code, "body_head": r3.text[:200]},
                )

            reg = await self._register(client, prefix + name, len(data), name)
        return f"{self._s.CDN_BASE.rstrip('/')}/{prefix}{name}", reg

    # ------------------------------------------------------------------ 老接口兜底

    def _legacy_headers(self) -> dict[str, str]:
        """老接口（`image.baidu.com`）的出站头：表单提交 + 同款 cookie。

        ⚠️ `Origin/Referer` 必须是 **image.baidu.com**（不是 wenxin.baidu.com）——
        两条链路的站点上下文不同。
        """
        base = self._s.LEGACY_BASE.rstrip("/")
        return {
            "User-Agent": _UA,
            "Cookie": self._s.COOKIE,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Referer": f"{base}/",
            "Origin": base,
        }

    async def legacy_process(self, legacy_type: str, data: bytes, *,
                             ext_ratio: str = "", create_level: str = "",
                             mask: bytes | None = None,
                             text: str = "") -> tuple[bytes, dict[str, Any]]:
        """老接口（百度AI图片助手）：`pccreate` 建任务 → `pcquery` 轮询 → 结果字节。

        实测（2026-09-24）：匿名可用；`picInfo` 直传 **base64**（无需先上传、绕开
        "服务端抓不到 URL"类失败）；create ~0.7–1.0s，**首次轮询（+1.5s）即就绪**；
        结果在 `picArr[0].src`（data URI）。

        - `mask`（**黑底白框**，白色=要处理的区域）→ `picInfo2`：消除(8)/局部替换(5) 实测生效；
        - `create_level` → AI重绘(2)/相似图(5)；`ext_ratio` → 扩图；`text` → 局部替换。

        返回 `(图片字节, 过程记录)`；失败一律显式抛错（带原始报文片段，便于排障）。
        与主链是**两条独立链路**（不同 host / 不同产品）——这是兜底成立的结构性依据。
        """
        import base64 as _b64  # noqa: PLC0415

        base = self._s.LEGACY_BASE.rstrip("/")
        # 老接口对入参体积敏感（实测 40KB 收 / 63KB 拒）⇒ 超限先等比压缩，别让调用方踩
        data, fitted = fit_legacy_image(data,
                                       max_side=self._s.LEGACY_MAX_IMAGE_SIDE,
                                       max_bytes=self._s.LEGACY_MAX_IMAGE_BYTES)
        form = legacy_form(
            legacy_type, _b64.b64encode(data).decode("ascii"),
            ext_ratio=ext_ratio, create_level=create_level,
            mask_b64=_b64.b64encode(mask).decode("ascii") if mask else "",
        )
        if text:
            form["text"] = text
        info: dict[str, Any] = {
            "type": str(legacy_type), "bytes_in": len(data),
            "has_mask": bool(mask), "create_level": str(create_level or ""),
        }
        if fitted:
            info["fitted"] = fitted

        async with self._session() as client:
            try:
                r = await client.post(f"{base}/aigc/pccreate", data=form,
                                      headers=self._legacy_headers(), timeout=60)
            except httpx.HTTPError as exc:
                raise UpstreamUnavailableError(
                    f"老接口创建失败（网络）：{exc}") from exc
            try:
                created = r.json()
            except ValueError as exc:
                raise UpstreamUnavailableError(
                    "老接口 pccreate 返回非 JSON",
                    detail={"http_status": r.status_code, "body_head": r.text[:200]},
                ) from exc
            if created.get("status") != 0 or not created.get("pcEditTaskid"):
                raise UpstreamUnavailableError(
                    f"老接口拒绝创建（status={created.get('status')}，"
                    f"message={created.get('message')}）",
                    detail={"body": json.dumps(created, ensure_ascii=False)[:300]},
                )
            task_id = str(created["pcEditTaskid"])
            info.update({"task_id": task_id, "create_status": created.get("status")})

            t0 = time.time()
            last: dict[str, Any] = {}
            for i in range(1, max(1, self._s.LEGACY_POLL_TRIES) + 1):
                await asyncio.sleep(max(0.1, self._s.LEGACY_POLL_INTERVAL))
                try:
                    q = await client.get(f"{base}/aigc/pcquery",
                                         params={"taskId": task_id},
                                         headers=self._legacy_headers(), timeout=30)
                    payload = q.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise UpstreamUnavailableError(f"老接口轮询失败：{exc}") from exc
                if isinstance(payload, dict):
                    last = payload
                    raw = extract_legacy_result(payload)
                    if raw:
                        info.update({"polls": i, "elapsed_s": round(time.time() - t0, 1),
                                     "progress": payload.get("progress")})
                        return raw, info
            raise UpstreamUnavailableError(
                "老接口超时未出图",
                detail={"task_id": task_id, "polls": self._s.LEGACY_POLL_TRIES,
                        "last": json.dumps(last, ensure_ascii=False)[:200]},
            )

    # ------------------------------------------------------------------ 主链路

    async def converse(
        self,
        cap: Capability,
        *,
        query_text: str,
        image_url: str,
        rank: int,
        expand: str | None = None,
        strip_watermark: bool = False,
    ) -> SseOutcome:
        """打一次 `/aichat/api/conversation` 并解析 SSE；返回 `SseOutcome`。

        `tokenFail` 时（且凭据是**现取**而非显式配置）自动刷新 token 重试**一次**。
        """
        token, lid = await self.creds.get()
        try:
            out = await self._converse_once(cap, query_text=query_text, image_url=image_url,
                                            rank=rank, expand=expand,
                                            strip_watermark=strip_watermark,
                                            token=token, lid=lid)
        except AuthError:
            # `tokenFail` 只有两个已知成因：token 过期（≥10min）或 lid 用错。
            # 现取一次重试**一次**（不递归、不循环）—— 这正是「token 可现取」的价值。
            # 显式配置了凭据就不替它改写意图（上面 creds.get 已保证不会走到这）。
            if self.creds.explicit():
                raise
            token, lid = await self.creds.get(force=True)
            out = await self._converse_once(cap, query_text=query_text, image_url=image_url,
                                            rank=rank, expand=expand,
                                            strip_watermark=strip_watermark,
                                            token=token, lid=lid)
            out.refreshed = True
        out.cred_source = self.creds.diagnostics()["source"]
        return out

    async def _converse_once(
        self,
        cap: Capability,
        *,
        query_text: str,
        image_url: str,
        rank: int,
        expand: str | None,
        strip_watermark: bool,
        token: str,
        lid: str,
    ) -> SseOutcome:
        payload = build_body(cap, query_text=query_text, image_url=image_url, rank=rank,
                             token=token, lid=lid, expand=expand,
                             ori_lid=self._s.ORI_LID)
        headers = self._browser_headers(content_type="application/json",
                                        accept="text/event-stream")
        headers["isDeepseek"] = "1"
        headers["personifiedSwitch"] = "0"
        headers["source"] = "pc_csaitab"
        headers["X-Chat-Message"] = f"enter_type:unknown,re_rank:{rank},modelName:smartMode"

        async with self._session() as client:
            try:
                async with client.stream(
                    "POST", "/aichat/api/conversation", headers=headers,
                    content=json.dumps(payload, ensure_ascii=False).encode(),
                    timeout=self._s.TIMEOUT,
                ) as resp:
                    if resp.status_code != 200:
                        body_head = (await resp.aread())[:200].decode("utf-8", "replace")
                        if resp.status_code == 400:
                            raise UpstreamParamError(
                                "上游拒绝请求（HTTP 400）", http_status=400,
                                detail={"body_head": body_head})
                        raise UpstreamUnavailableError(
                            f"上游 HTTP {resp.status_code}", http_status=resp.status_code,
                            detail={"body_head": body_head})
                    # 逐行喂纯函数：kunlun 命中 ⇒ 立刻退出循环（=立刻断连，不施压）
                    out = SseOutcome()
                    async for raw in resp.aiter_lines():
                        if not apply_frame(out, raw, strip_watermark=strip_watermark):
                            break
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout(
                    f"文心请求超时（{self._s.TIMEOUT:.0f}s）") from exc
            except httpx.HTTPError as exc:
                raise UpstreamUnavailableError(f"文心网络错误：{exc}") from exc

        # ---- 帧后判读：风控 / hints / 静默失败，三层都要显式 ----
        if out.kunlun:
            raise RiskControlError(
                f"文心上游返回风控验证挑战（{out.kunlun}），已立即断开、不重试；"
                f"服务将在冷却窗内静默（窗内零上游请求）",
                detail={"frames": out.frames},
            )
        if out.hint and not out.images:
            htype = out.hint.get("type")
            msg = out.hint.get("text") or htype
            if htype == "tokenFail":
                raise AuthError(
                    f"文心 chat_token 校验失败（{msg}）：通常是 LID 用了会话 ori_lid "
                    f"而非页面级 searchframeLid，或 SESSION_TOKEN 已过期（实测存活 ≥10min）",
                    detail={"hint": htype},
                )
            raise UpstreamUnavailableError(f"文心上游返回提示：{msg}",
                                           detail={"hint": htype})
        if not out.images:
            why = ("上游只返回了交互式编辑链接（picEditBaseUrl），没有结果图 —— "
                   "该 toolType 可能已改为「跳编辑器」形态" if out.editor_url else
                   "上游未给出任何图或链接")
            raise UpstreamUnavailableError(
                f"文心上游未产出图片（已收到 {out.frames} 帧）：{why}"
                f"{'，文案：' + out.text if out.text else ''}",
                detail={"frames": out.frames, "kunlun": out.kunlun, "note": out.text,
                        "editor_url": (out.editor_url or "")[:300] or None},
            )
        return out
