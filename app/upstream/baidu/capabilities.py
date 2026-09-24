#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""翻译层（**纯函数**，离网可测）：chat_token / 请求体构造 / SSE 解析 / BCE v1 签名。

字段与判据的来源：`docs/UPSTREAM.md`（实证版契约的浓缩）。三条必须照做的结论：

  1. **能力由三处同时决定**：`mcpInfo.ext.type`(toolType) + `sa`
     (`workspace_piccreate_<tt>`) + `query[TEXT].query`（中文能力名）。
     `build_body()` 从**同一个 tool_type / query 文本**派生这三处，杜绝手工写不一致。
  2. **`chat_token` 的 md5 必须与 body 里的 query 完全一致**，且 `lid` 段必须是
     **页面级 searchframeLid**（用会话 ori_lid 会稳定吃 `tokenFail`，
     而报错文案只说"出了点小问题"，极易误判成风控）。
  3. **首帧 `chatHitKunlun` 就是风控信号**（约 0.1s 可知）⇒ `parse_sse()` 读到即
     **提前中断迭代**（调用方随即可断开连接、不施压）。

⚠️ `parse_sse` 会在 kunlun 命中时**提前 break** —— 传进来的可以是网络流生成器，
提前中断 = 不再继续拉流。这是设计，不是 bug。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from ...errors import AuthError, UpstreamUnavailableError
from ...models import Capability

__all__ = [
    "KUNLUN_FIELD",
    "SseImage",
    "SseOutcome",
    "build_chat_token",
    "extract_creds_from_homepage",
    "build_body",
    "redact_payload",
    "apply_frame",
    "parse_sse",
    "legacy_form",
    "extract_legacy_result",
    "uri_encode",
    "canon_query",
    "bce_sign",
]

KUNLUN_FIELD = "chatHitKunlun"

#: 首页里内嵌的凭据：<script type="application/json" name="aiTabFrameBaseData">{"token":"…","lid":"…",…}
_CRED_RE = re.compile(r'name="aiTabFrameBaseData">(\{.*?\})</script>', re.S)


# ---------------------------------------------------------------------------
# chat_token / 凭据
# ---------------------------------------------------------------------------


def build_chat_token(session_token: str, query: str, lid: str) -> str:
    """`btoa(<session_token>|<md5(query)>|<ts_ms>|<lid>)-<lid>-3`（实证向量见测试）。"""
    raw = (f"{session_token}|{hashlib.md5(query.encode()).hexdigest()}"
           f"|{int(time.time() * 1000)}|{lid}")
    return base64.b64encode(raw.encode()).decode() + f"-{lid}-3"


def extract_creds_from_homepage(html: str, *, http_status: int | None = None) -> tuple[str, str]:
    """从首页 HTML 取 (token, lid)；取不到/不合法则抛**可行动**的异常。

    `token` 每次加载页面都会变、实测存活 ≥10 分钟 ⇒ 手工填从来不是正确用法，应当现取
    （契约 §8.2）。显式配置的 `BAIDU_SESSION_TOKEN` / `BAIDU_LID` 只是排障时的钉值手段。
    """
    suffix = f"（HTTP {http_status}）" if http_status is not None else ""
    m = _CRED_RE.search(html or "")
    if not m:
        raise AuthError(
            f"文心首页里取不到 aiTabFrameBaseData{suffix} —— 可能是站点改版或被验证页拦住；"
            f"可显式配 BAIDU_SESSION_TOKEN + BAIDU_LID 兜底",
            detail={"http_status": http_status},
        )
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise UpstreamUnavailableError(
            "aiTabFrameBaseData 不是合法 JSON（上游可能改版）",
            detail={"http_status": http_status},
        ) from exc
    tok = str(data.get("token") or "")
    lid = str(data.get("lid") or "")
    if not (tok and lid):
        raise AuthError(
            "aiTabFrameBaseData 里缺 token 或 lid（上游可能改版）",
            detail={"keys": sorted(data)[:12]},
        )
    return tok, lid


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


def build_body(
    cap: Capability,
    *,
    query_text: str,
    image_url: str,
    rank: int,
    token: str,
    lid: str,
    expand: str | None = None,
    ori_lid: str = "",
    style_id: str = "",
    style_label: str = "",
) -> dict[str, Any]:
    """构造 `/aichat/api/conversation` 的请求体。

    **两种形状，别混**（都是实测所得）：

    - **workspace 形状**（默认，契约 §1.2）：`sa=workspace_piccreate_<tt>` +
      `mcpInfo.ext{type,image,...}` + TEXT(query=能力名, disableReply)；
    - **工具入口形状**（`cap.entry_type` 非空）：`sa=searchbox_image` +
      `enter_type=<cap.entry_type>` + **无 mcpInfo** + TEXT(query=调用方指令)。
      来源：2026-09-24 从站点 UI 抓的真实报文（点「风格转换」落在
      `enter_type=pic_picfunc_14`、「背景替换」`pic_picfunc_11`）。旧 workspace 形状对这两条
      只回 `picEditBaseUrl`（跳编辑器）⇒ 入口形状才是当前可用形态。

    `token` / `lid` 由**调用方**传入而不再从配置直接读 —— 这样本函数保持纯函数，
    干跑才能在不触网的前提下构造请求体。
    """
    tt = cap.tool_type
    # 注：站点 UI 的「工具入口形状」（sa=searchbox_image + enter_type + 无 mcpInfo）
    # **本服务不使用**（实测只回对话文字/编辑器链接）—— 证据留在 docs/UPSTREAM.md §7。
    # enter_type 仅作为 workspace 形状里的 `enter_type` 取值（见下）。
    ext: dict[str, Any] = {"type": tt, "image": image_url, "image_source": 0, "channel": "edit"}
    if expand:
        ext["image_expand"] = expand
    sa = cap.workspace_sa or f"workspace_piccreate_{tt}"
    if cap.needs_style:
        # 实测（2026-09-24，站点 UI 真实报文）：风格类工具的 `sa` 是**专属字母码**
        # （换风格 = `workspace_piccreate_hfg`），且 ext 里必须带 `style`(id) / `text`(标签)、
        # `image_source=1` —— 缺 style/text 时上游只回编辑器链接（`picEditBaseUrl`）不出图。
        sid, slabel = style_id, style_label
        if not sid:                      # 由 query_text（= 风格标签）自解析 id
            for _id, _label in cap.style_table:
                if query_text.strip() == _label:
                    sid, slabel = _id, _label
                    break
        ext.update({"imageId": "", "image_source": 1,
                    "style": sid or query_text, "text": slabel or query_text})
    return {
        "message": {
            "inputMethod": "chat_search",
            "isRebuild": False,
            "content": {"query": "", "agentInfo": {"agent_id": [""], "params": ""},
                        "agentInfoList": [], "extData": {}},
            "searchInfo": {
                "srcid": "", "order": "", "tplname": "", "dqaKey": "",
                "re_rank": str(rank), "ori_lid": ori_lid or lid, "sa": sa,
                "enter_type": cap.entry_type or "unknown",
                "chatParams": {"setype": "csaitab",
                               "chat_token": build_chat_token(token, query_text, lid)},
                "isPrivateChat": False,
                "usedModel": {"modelFunction": {"thinkMode": "0"}, "modelName": "smartMode"},
                "landingPageSwitch": "", "landingPage": "aitab", "ecomFrom": "",
                "hasLocPermission": "", "lid": "",
                "mcpInfo": {"type": "workspaceImage",
                            "ext": json.dumps(ext, ensure_ascii=False)},
                "showMindMap": False,
                "deepDecisionInfo": {"isDeepDecision": 0},
            },
            "from": "", "source": "pc_csaitab",
            "query": [
                {"type": "IMAGE", "data": {"image": {"image_id": "", "image_url": image_url}}},
                {"type": "TEXT", "data": {"text": {
                    "query": query_text, "text_type": "",
                    "extData": json.dumps({"toolType": tt, "disableReply": True},
                                          ensure_ascii=False)}}},
            ],
            "agent_id": "",
        },
        "sa": sa,
        "rank": rank,
    }


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """干跑预览用：深拷贝并打码 `chat_token`（凭据不出现在对外报文里）。"""
    out = json.loads(json.dumps(payload, ensure_ascii=False))
    try:
        out["message"]["searchInfo"]["chatParams"]["chat_token"] = "***"
    except (KeyError, TypeError):  # pragma: no cover - 结构性防御
        pass
    return out


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SseImage:
    """结果图条目（originUrl 去掉水印参数后为「相对干净的原图」）。"""

    url: str
    width: int | None = None
    height: int | None = None
    note: str = ""


@dataclass
class SseOutcome:
    """一次 SSE 流的结果。`cred_source` / `refreshed` 由客户端补充（非解析产物）。"""

    images: list[SseImage] = field(default_factory=list)
    text: str | None = None          # markdown-yiyan 文案（上游的"已完成 xxx"）
    ratio: str | None = None         # image-generate 的 ratio（如 "3-2"）
    kunlun: str | None = None        # 非空 = 命中风控（kunlun_popup）
    hint: dict[str, Any] | None = None   # content.hints.parts[0]（tokenFail 等）
    frames: int = 0
    editor_url: str | None = None    # items 为空 + picEditBaseUrl：跳交互式编辑器的 App 深链
    cred_source: str = ""            # explicit / fetched / …
    refreshed: bool = False          # 是否发生过 tokenFail ⇒ 现取重试


def apply_frame(out: SseOutcome, raw: str, *, strip_watermark: bool = False) -> bool:
    """把**一行** SSE 数据并入 `out`；返回 `False` = 应当**停止迭代**（kunlun 命中）。

    独立成纯函数，是为了让客户端能「**逐行 async 喂**」—— 首帧熔断要求读到 kunlun 就
    立刻断连（0.1s 级），不能等整个流读完。只认 `data:` 行；非 JSON / 空行跳过。
    """
    if not raw or not raw.startswith("data:"):
        return True
    try:
        d = json.loads(raw[5:].strip())
    except json.JSONDecodeError:
        return True
    out.frames += 1

    # ---- 首帧熔断：命中风控立即中断（调用方负责断连；持续施压会延长标记）----
    if KUNLUN_FIELD in d:
        out.kunlun = d.get(KUNLUN_FIELD)
        return not out.kunlun
    c = (d.get("data") or {}).get("message", {}).get("content", {})
    g = (c.get("generator") or {}) if isinstance(c, dict) else {}
    comp = g.get("component")
    if comp == "markdown-yiyan":
        out.text = (g.get("data") or {}).get("value")
    elif comp == "image-generate":
        data = g.get("data") or {}
        out.ratio = data.get("ratio")
        # 🔴 `items` 会被上游**显式给成 null** —— `.get(k, [])` 的默认值不生效。
        items = data.get("items") or []
        if not items and data.get("picEditBaseUrl"):
            out.editor_url = data["picEditBaseUrl"]
        for it in items:
            url = it.get("originUrl") or it.get("previewUrl") or ""
            note = ""
            if strip_watermark and url and "x-bce-process" in url:
                url, note = url.split("?", 1)[0], "已剥离水印处理参数"
            out.images.append(SseImage(url=url, width=it.get("width"),
                                       height=it.get("height"), note=note))
    if isinstance(c, dict) and c.get("hints"):
        parts = (c["hints"] or {}).get("parts") or [{}]
        out.hint = parts[0]
    return True


def parse_sse(lines: Iterable[str], *, strip_watermark: bool = False) -> SseOutcome:
    """同步便捷版（列表 / 测试用）：逐行 `apply_frame`，直到流结束或熔断。"""
    out = SseOutcome()
    for raw in lines:
        if not apply_frame(out, raw, strip_watermark=strip_watermark):
            break
    return out


# ---------------------------------------------------------------------------
# 老接口（image.baidu.com/aigc）—— 兜底通路（2026-09-24 实测）
# ---------------------------------------------------------------------------

#: 老接口的固定表单字段（与 2024 版实现逐字对齐；`type` = 能力编号）。
#: `picInfo` = **纯 base64**（无 `data:` 前缀）—— base64 直传可绕开「服务端抓不到 URL」类失败。
_LEGACY_FORM_TEMPLATE: dict[str, str] = {
    "query": "bdaitpzs百度AI图片助手bdaitpzs",
    "text": "", "ext_ratio": "", "expand_zoom": "",
    "clid": "", "front_display": "2", "create_level": "0",
    "image_source": "1", "style": "",
    "original_url": "", "thumb_url": "", "is_first": "true",
}


def legacy_form(legacy_type: str, image_b64: str, *, ext_ratio: str = "",
                create_level: str = "", mask_b64: str = "") -> dict[str, str]:
    """构造老接口 `POST /aigc/pccreate` 的表单（纯函数）。

    - `mask_b64` → `picInfo2`：**黑底白框**遮罩（白色=要处理的区域；2026-09-24 判决实验）；
    - `ext_ratio` → 扩图的 `ext_ratio`；`create_level` → AI重绘(2)/相似图(5) 的档位。
    """
    form = dict(_LEGACY_FORM_TEMPLATE)
    form["type"] = str(legacy_type)
    form["picInfo"] = image_b64
    if ext_ratio:
        form["ext_ratio"] = ext_ratio
    if create_level:
        form["create_level"] = str(create_level)
    if mask_b64:
        form["picInfo2"] = mask_b64
    return form


def extract_legacy_result(payload: dict[str, Any]) -> bytes | None:
    """从 `pcquery` 响应里取结果图字节；没就绪 / 没图返回 None。

    结果形态（2026-09-24 实测）：`picArr[0].src` 是 **data URI**
    （`data:image/jpeg;base64,…`）；防御性兼容裸 base64。
    **解析不出来就返回 None** —— 由调用方翻译成显式错误，绝不返回空成功。
    """
    arr = payload.get("picArr") or []
    if not isinstance(arr, list) or not arr:
        return None
    first = arr[0]
    src = str(first.get("src") or "") if isinstance(first, dict) else str(first or "")
    if not src:
        return None
    if src.startswith("data:"):
        _, _, src = src.partition(",")
    try:
        raw = base64.b64decode(src, validate=False)
    except ValueError:
        return None
    return raw or None


# ---------------------------------------------------------------------------
# BCE v1 签名（BOS 转存用；抓自浏览器真实请求的格式）
# ---------------------------------------------------------------------------


def uri_encode(s: str, encode_slash: bool = True) -> str:
    return urllib.parse.quote(s, safe="" if encode_slash else "/")


def canon_query(query: str) -> str:
    if not query:
        return ""
    parts = []
    for kv in query.split("&"):
        if not kv:
            continue
        k, _, v = kv.partition("=")
        parts.append(f"{uri_encode(k)}={uri_encode(v)}")
    return "&".join(sorted(parts))


def bce_sign(
    method: str,
    path: str,
    query: str,
    headers: dict[str, str],
    ak: str,
    sk: str,
    signed_headers: list[str],
    expire: int = 1800,
    *,
    ts: str | None = None,
) -> tuple[str, str]:
    """BCE v1 签名。返回 `(authorization, x_bce_date)`。

    格式（抓自浏览器真实请求）::

        Authorization: bce-auth-v1/{ak}/{ISO8601Z}/{expire}/{signedHeaders}/{hmac_sha256}
        signingKey  = HMAC-SHA256(sk, "bce-auth-v1/{ak}/{ts}/{expire}")
        canonicalReq = METHOD \\n CanonicalURI \\n CanonicalQuery \\n CanonicalHeaders

    `ts` 可注入（测试固定时间用）；默认取当前 UTC。
    """
    stamp = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers["x-bce-date"] = stamp
    prefix = f"bce-auth-v1/{ak}/{stamp}/{expire}"
    signing_key = hmac.new(sk.encode(), prefix.encode(), hashlib.sha256).hexdigest()

    signed = sorted(h.lower() for h in signed_headers)
    canon_headers = "\n".join(
        f"{uri_encode(h)}:{uri_encode(str(headers[h]).strip())}" for h in signed
    )
    canon_req = "\n".join([method.upper(), uri_encode(path, False),
                           canon_query(query), canon_headers])
    sig = hmac.new(signing_key.encode(), canon_req.encode(), hashlib.sha256).hexdigest()
    return f"{prefix}/{';'.join(signed)}/{sig}", stamp
