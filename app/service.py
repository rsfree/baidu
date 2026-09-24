#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务编排：校验 → 输入准备 → 调上游 → 装配响应。

**响应的唯一出口是本文件的 `run()`** —— 改响应形状只改这里；
对外契约的真相在 `docs/INTERFACE.md`。

契约要点（2026-09-24 定稿）：

- **同步直给**：上游就是同步 SSE（无任务 id、无轮询通道），不做假异步；
- 单端点 `/v1/images/generations`（**图进图出**；`wenxin:*` 全部能力都需要输入图）；
- 结果默认内联 `b64_json`；`response_format=url` 时落盘并提供 `/files/{name}`；
- 诊断字段 `requested` / `effective` / `warnings` / `unsupported` / `upstream` 是本层
  在标准之外的加性扩展，**不改变标准字段语义**。
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any

from .config import Settings
from .errors import ApiError, RiskControlError, UpstreamError, UpstreamUnavailableError
from .gate import GateBusy, RateGate, RiskWindow
from .media import (
    IMAGE_KINDS,
    image_size,
    mime_ext,
    normalize_image,
    resolve_input,
    save_media,
    sniff,
)
from .models import EXPAND_RATIOS, Capability, availability, lookup
from .observability import report, span
from .upstream.baidu import BaiduClient
from .upstream.baidu.capabilities import build_body, redact_payload

__all__ = ["run", "validate_request"]

#: 请求里允许出现的键（其余键 → 400，避免拼写错误被静默吞掉）
_ALLOWED_KEYS = ("model", "image", "mask", "style", "size", "prompt", "response_format", "dry_run")

#: 认识、但**上游/本服务不支持**的字段：不报错，进 `unsupported[]`。
#: 静默丢弃是大忌 —— 调用方会以为控制生效了。这份清单与 OpenAI/seedream 图片契约
#: 的常见字段对齐；上游（文心）只认「能力名 + 输入图 + 扩图比例 + 风格 id」几件事。
_KNOWN_UNSUPPORTED = (
    "n", "quality", "seed", "negative_prompt", "watermark", "user",
    "stream", "background", "output_format", "moderation",
    "sequential_image_generation", "sequential_image_generation_options", "extra_body",
)

_RESPONSE_FORMATS = ("b64_json", "url")
_MB = 1024 * 1024


# ---------------------------------------------------------------------------
# 校验（入口）
# ---------------------------------------------------------------------------


def validate_request(
    payload: Any,
) -> tuple[Capability, str, str | None, str | None, str | None, str | None, str, bool, list[str]]:
    """校验请求体的**键集与模型名**。

    返回 `(cap, image, mask, style, size, prompt, response_format, dry_run, unsupported)`。
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ApiError(400, "missing_model", "缺少 model（如 wenxin:clarity）")

    try:
        cap = lookup(model.strip())
    except KeyError as exc:
        raise ApiError(400, "unknown_model", str(exc)) from exc

    unknown = sorted(k for k in payload
                     if k not in _ALLOWED_KEYS and k not in _KNOWN_UNSUPPORTED)
    if unknown:
        raise ApiError(
            400, "unknown_field",
            f"不认识的字段：{'、'.join(unknown)}。本端点接受的字段：{'、'.join(_ALLOWED_KEYS)}",
            unknown=unknown, accepted=list(_ALLOWED_KEYS),
        )

    image = payload.get("image")
    if not isinstance(image, str) or not image.strip():
        raise ApiError(
            400, "missing_input",
            "缺少输入图片（image 字段）。接受三种形态："
            "data URI（`data:image/png;base64,...`）、http(s) URL、裸 base64",
        )

    mask = payload.get("mask")
    if mask is not None and not isinstance(mask, str):
        raise ApiError(400, "invalid_mask", "mask 必须是字符串（与 image 同三种形态）")
    if cap.requires_mask and not (isinstance(mask, str) and mask.strip()):
        raise ApiError(
            400, "missing_mask",
            f"「{cap.title}」需要遮罩（mask 字段）：**黑底 + 白框**的图片，"
            f"**白色标记「要处理的区域」**（反约定实测无效）。形态与 image 相同："
            f"data URI / http(s) URL / 裸 base64；底色为黑、要处理区域涂白",
            model=cap.name,
        )

    size = payload.get("size")
    if size is not None and not isinstance(size, str):
        raise ApiError(400, "invalid_size",
                       "size 必须是字符串（扩图用比例，如 \"4:3\"；也接受 \"1024x1024\"）")
    prompt = payload.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise ApiError(400, "invalid_prompt", "prompt 必须是字符串")

    response_format = payload.get("response_format") or "b64_json"
    if response_format not in _RESPONSE_FORMATS:
        raise ApiError(400, "invalid_response_format",
                       f"response_format 只支持 {'、'.join(_RESPONSE_FORMATS)}")

    dry = bool(payload.get("dry_run"))
    unsupported = [k for k in _KNOWN_UNSUPPORTED if payload.get(k) is not None]
    style = payload.get("style")
    if style is not None and not isinstance(style, str):
        raise ApiError(400, "invalid_style", "style 必须是字符串（风格 id 或中文标签）")

    return (cap, image.strip(), (mask.strip() if isinstance(mask, str) else None),
            (style.strip() if isinstance(style, str) else None),
            (size.strip() if isinstance(size, str) else None),
            (prompt if isinstance(prompt, str) else None), response_format, dry, unsupported)


# ---------------------------------------------------------------------------
# 编排（唯一出口）
# ---------------------------------------------------------------------------


async def run(
    *,
    settings: Settings,
    client: BaiduClient,
    gate: RateGate,
    risk: RiskWindow,
    cap: Capability,
    image_value: str,
    mask_value: str | None = None,
    style_value: str | None = None,
    size: str | None = None,
    prompt: str | None = None,
    response_format: str = "b64_json",
    dry_run: bool = False,
    unsupported: list[str] | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    unsupported = list(unsupported or [])

    # 老接口兜底：mode=off/fallback/prefer；只有**有老接口映射**的能力才享受兜底（不制造假能力）
    legacy_mode = settings.LEGACY
    legacy_on = legacy_mode != "off" and bool(cap.legacy_type)
    if legacy_mode != "off" and not cap.legacy_type:
        warnings.append(f"BAIDU_LEGACY={legacy_mode} 但「{cap.title}」无老接口映射，已走主链")

    # ---- 1) 未取证能力闸门（**必须生效在调用上游之前**；dry_run 穿透）----
    ok, reason = availability(cap, allow_unverified=settings.ALLOW_UNVERIFIED,
                              legacy=legacy_on)
    if not ok and not dry_run:
        raise ApiError(503, "capability_not_verified", reason, model=cap.name)

    # ---- 2) 就绪闸门：cookie 是硬条件（免登录≠免 cookie，见 docs/UPSTREAM.md §4）----
    if not dry_run and not settings.ready:
        raise ApiError(
            503, "upstream_not_configured",
            "本部署未配 BAIDU_COOKIE —— 文心免登录也需要一份 cookie；可用 "
            "`python wenxin/tools/mint_identity.py` 铸造（免登录、零生成请求），"
            "把产物 identity.json 的 `cookie` 字段填进 BAIDU_COOKIE",
            hint="docs/UPSTREAM.md §8",
        )

    # ---- 3) 昆仑冷却窗：窗内不发**主链**请求；有兜底通路的能力改走老接口（这才是兜底的意义）----
    risk_open = (not dry_run) and risk.open()
    if risk_open and not legacy_on:
        snap = risk.snapshot()
        raise ApiError(
            429, "risk_control_cooldown",
            f"命中昆仑风控后的冷却窗内（剩 {int(snap['remaining_s'])}s）—— 窗内不发上游请求；"
            f"请在真实浏览器打开文心页面过一次验证框，或等窗口自然过期",
            retry_after=snap["remaining_s"], kind="risk_control",
        )

    # ---- 4) query 文本 ----
    #  · 工具入口形状（cap.entry_type）：**prompt 就是指令文本**（风格名 / 背景描述）——
    #    实测：该形状下上游按自然语言指令出图，没有指令只会回风格分析或编辑器链接；
    #  · workspace 形状：query 必须严格等于能力名（chat_token 的 md5 与它绑定）。
    query_text = cap.title
    if cap.needs_style:
        # 风格类（实测形状：sa=workspace_piccreate_hfg + ext.style/text + TEXT=标签）
        raw_style = (style_value or prompt or "").strip()
        sid = label = ""
        for _id, _label in cap.style_table:
            if raw_style in (_id, _label):
                sid, label = _id, _label
                break
        if not sid:
            options = "、".join(f"{l}({i})" for i, l in cap.style_table)
            if not raw_style and dry_run:      # 干跑穿透
                sid, label = cap.style_table[0]
                warnings.append(f"「{cap.title}」需要 style；干跑未提供 ⇒ 预览按首个风格"
                                f"「{label}」构造（可选 {len(cap.style_table)} 项）")
            elif not raw_style:
                raise ApiError(400, "missing_style",
                               f"「{cap.title}」需要 style（id 或中文标签）—— 可选：{options}",
                               model=cap.name, styles=[{"id": i, "label": l}
                                                       for i, l in cap.style_table])
            else:
                raise ApiError(400, "unknown_style",
                               f"未知风格 {raw_style!r} —— 可选：{options}",
                               model=cap.name, styles=[{"id": i, "label": l}
                                                       for i, l in cap.style_table])
        query_text = label
        warnings.append(f"style={sid}（{label}）→ ext.style/text + TEXT query；"
                        f"sa={cap.workspace_sa}（实测形状）")
    elif prompt:
        if settings.PROMPT_MODE == "prepend" and prompt.strip() not in cap.title:
            query_text = f"{prompt.strip()}\n{cap.title}"
            warnings.append("prompt 已拼接到能力名前透传（BAIDU_PROMPT_MODE=prepend）；"
                            "该用法未经上游验证，若失败请改回 ignore")
        else:
            warnings.append(f"上游的 query 必须严格等于能力名「{cap.title}」，"
                            f"prompt 未透传（已忽略）；如需透传请设 BAIDU_PROMPT_MODE=prepend")

    # ---- 5) size → 扩图比例（唯 wenxin:expand 消费；其余一律忽略 + 提示）----
    expand: str | None = None
    if cap.supports_ratio:
        expand, ratio_note = _ratio_of(size)
        if ratio_note:
            warnings.append(ratio_note)
        elif expand and expand not in EXPAND_RATIOS:
            warnings.append(f"扩图比例 {expand} 不在实测支持集合 {EXPAND_RATIOS} 内，"
                            f"可能导致处理失败")
        elif expand is None:
            warnings.append("扩图能力需要比例（如 size=\"4:3\"）；当前未提供，"
                            "已省略 image_expand，行为由上游默认决定")
    elif size:
        warnings.append(f"上游能力「{cap.title}」不接受 size 参数，已忽略 size={size!r}")

    requested: dict[str, Any] = {"model": cap.name, "response_format": response_format,
                                 "image": _input_summary(image_value)}
    if mask_value:
        requested["mask"] = _input_summary(mask_value)
    if size:
        requested["size"] = size
    if prompt:
        requested["prompt"] = prompt

    # ---- 5.5) 遮罩解析（黑底白框；**仅 requires_mask 能力**消费；dry_run 零网络）----
    mask_bytes: bytes | None = None
    if mask_value and not cap.requires_mask:
        warnings.append(f"「{cap.title}」不使用 mask，已忽略（mask 仅用于：消除 / 局部替换）")
    elif cap.requires_mask and mask_value and not dry_run:
        mask_blob = await resolve_input(
            mask_value, accepts=IMAGE_KINDS, fetch=client.fetch_url,
            max_bytes=max(1, settings.MAX_DOWNLOAD_MB) * _MB, warnings=warnings,
        )
        mask_bytes = mask_blob.data

    effective: dict[str, Any] = {
        "tool_type": cap.tool_type,
        "query": query_text,
        "entry_type": cap.entry_type,
        "upload_mode": settings.UPLOAD_MODE,
        "strip_watermark": settings.STRIP_WATERMARK,
        "proxy": "pool" if client.pool_size else "direct",
        "legacy": {"mode": legacy_mode, "type": cap.legacy_type, "enabled": legacy_on},
    }

    # ---- 6) 输入图准备（dry_run 零网络；老接口直通时只需要字节）----
    rank = _next_rank()
    effective["rank"] = rank
    # 老接口直通：prefer / 主链冷却窗 / legacy-only / 需要遮罩（主链遮罩参数未逆向）
    intents_legacy = legacy_on and (legacy_mode == "prefer" or risk_open
                                    or cap.legacy_only or cap.requires_mask)
    go_legacy_direct = intents_legacy and not dry_run
    if dry_run:
        token, lid = client.creds.cached()
        token = token or "<chat-token:干跑未取>"
        lid = lid or "<lid:干跑未取>"
        image_url, upload_info = _dry_image_plan(settings, image_value)
        effective["image_url"] = image_url
        effective["cred_source"] = client.creds.diagnostics()["source"]
        body: dict[str, Any] | None = None
        if cap.tool_type is not None:
            body = build_body(cap, query_text=query_text, image_url=image_url, rank=rank,
                              token=token, lid=lid, expand=expand, ori_lid=settings.ORI_LID)
        preview: dict[str, Any] = {
            "would_post_to": client.endpoint if body is not None else None,
            "method": "POST",
            "body": redact_payload(body) if body is not None else None,  # chat_token 已打码
            "upload": upload_info,
        }
        if legacy_on:
            preview["legacy"] = {
                "would_post_to": f"{settings.LEGACY_BASE.rstrip('/')}/aigc/pccreate",
                "type": cap.legacy_type, "mode": legacy_mode,
                "create_level": _legacy_level(cap) or None,
                "mask": bool(mask_value), "text_from_prompt": cap.legacy_uses_prompt,
            }
            if intents_legacy:
                preview["would_post_to"] = preview["legacy"]["would_post_to"]
        report("baidu.dry_run", tool_type=cap.tool_type)
        return {
            "created": int(time.time()),
            "model": cap.name,
            "dry_run": True,
            "data": [],
            "usage": None,
            "requested": requested,
            "effective": effective,
            "warnings": warnings,
            "unsupported": unsupported,
            "upstream": None,
            "preview": preview,
        }

    img_bytes: bytes | None = None
    if go_legacy_direct:
        if cap.legacy_only:
            warnings.append(f"「{cap.title}」是 legacy-only 能力（主链形态未坐实），"
                            f"直接走老接口（type={cap.legacy_type}）")
        elif cap.requires_mask:
            warnings.append(f"「{cap.title}」需要遮罩（主链遮罩参数未逆向），"
                            f"直接走老接口（type={cap.legacy_type}）")
        elif legacy_mode == "prefer":
            warnings.append(f"BAIDU_LEGACY=prefer：直接走老接口（type={cap.legacy_type}）")
        else:
            warnings.append("主链处于昆仑冷却窗内，已直接走老接口（BAIDU_LEGACY=fallback）")
        image_url, upload_info, img_bytes = await _prepare_legacy_input(
            settings, client, image_value, warnings)
    else:
        if cap.requires_mask:
            warnings.append("主链不支持遮罩参数（未逆向），mask 未发送；建议开 "
                            "BAIDU_LEGACY=fallback 走老接口（遮罩在那里已实测生效）")
        image_url, upload_info, img_bytes = await _prepare_image(
            settings, client, image_value, warnings)
    effective["image_url"] = image_url
    effective["upload"] = upload_info

    # ---- 7) 速率闸门（覆盖整个生成过程：主链 + 可能的兜底；一个客户请求一个名额）----
    try:
        waited = await gate.acquire(settings.MAX_WAIT)
    except GateBusy as busy:
        raise ApiError(
            429, "rate_limited",
            f"排队会超过上限（需再等 {busy.wait:.0f}s）—— 上游节奏约束："
            f"串行 + 最小间隔 {settings.MIN_INTERVAL:.0f}s + 每分钟 {settings.PER_MINUTE} 次",
            retry_after=busy.wait, kind="capacity",
        ) from None

    legacy_used = False
    legacy_info: dict[str, Any] = {}
    legacy_raw: bytes | None = None
    out = None
    try:
        if go_legacy_direct:
            with span("baidu.legacy", tool_type=cap.tool_type) as rec:
                legacy_raw, legacy_info = await client.legacy_process(
                    cap.legacy_type or "", img_bytes or b"",
                    ext_ratio=expand or "", create_level=_legacy_level(cap),
                    mask=mask_bytes,
                    text=(prompt or "") if cap.legacy_uses_prompt else "")
                rec["outcome"] = "ok"
            legacy_used = True
        else:
            with span("baidu.converse", tool_type=cap.tool_type) as rec:
                try:
                    out = await client.converse(cap, query_text=query_text, image_url=image_url,
                                                rank=rank, expand=expand,
                                                strip_watermark=settings.STRIP_WATERMARK)
                    rec["outcome"] = "ok"
                except (RiskControlError, UpstreamUnavailableError) as exc:
                    if isinstance(exc, RiskControlError):
                        cooldown = risk.trip()
                        if cooldown:
                            exc.retry_after = cooldown
                        rec["outcome"] = "risk_control"
                    else:
                        rec["outcome"] = "upstream_failed"
                    if not legacy_on:
                        raise
                    # —— 兜底：同一闸门名额内改走老接口（不额外占用节奏）——
                    warnings.append(
                        f"主链未产出（{exc.kind}），已按 BAIDU_LEGACY=fallback 改走老接口"
                        f"（type={cap.legacy_type}）")
                    data = img_bytes
                    if data is None:      # direct/auto 的外链直传没有本地字节 ⇒ 现取
                        data, _ = await client.fetch_url(
                            image_value, max(1, settings.MAX_DOWNLOAD_MB) * _MB)
                    try:
                        with span("baidu.legacy", tool_type=cap.tool_type) as rec2:
                            legacy_raw, legacy_info = await client.legacy_process(
                                cap.legacy_type or "", data,
                                ext_ratio=expand or "", create_level=_legacy_level(cap),
                                mask=mask_bytes,
                                text=(prompt or "") if cap.legacy_uses_prompt else "")
                            rec2["outcome"] = "ok"
                    except Exception as lexc:  # noqa: BLE001
                        # 兜底也失败：**保留主因**（调用方据此判断是风控/上游问题），
                        # 把兜底失败挂进 detail —— 两段信息都不可丢。
                        if isinstance(lexc, (UpstreamError, ApiError)):
                            exc.detail = {**(exc.detail or {}),
                                          "legacy_failed": f"{type(lexc).__name__}: "
                                                           f"{str(lexc)[:200]}"}
                            raise exc from lexc
                        raise
                    legacy_used = True
    finally:
        gate.release()

    if waited >= 1.0:
        effective["waited_s"] = round(waited, 1)

    # ---- 8) 装配（唯一出口）----
    if legacy_used:
        _fitted = (legacy_info or {}).get("fitted")
        if _fitted:
            warnings.append(
                f"输入已按老接口实测上限自动等比压缩："
                f"{_fitted['from_size'][0]}x{_fitted['from_size'][1]} → "
                f"{_fitted['to_size'][0]}x{_fitted['to_size'][1]}"
                f"（{_fitted['bytes_in']} → {_fitted['bytes_out']} 字节）——"
                "老接口对入参体积敏感（实测 40KB 收 / 63KB 拒），需要原分辨率请走主链能力")
        effective["path"] = "legacy"
        items = [_deliver_one(settings, legacy_raw or b"", response_format)]
        strip_notes = False
        upstream_block: dict[str, Any] = {"path": "legacy", "legacy": legacy_info}
    else:
        if out is None:  # pragma: no cover - 逻辑上不可达（legacy_used 为 False ⇒ 主链成功）
            raise ApiError(500, "internal", "内部错误：结果状态缺失")
        effective["path"] = "conversation"
        if out.refreshed:
            warnings.append("chat_token 校验失败（多为过期），已现取新 token 重试一次")
        if out.cred_source:
            effective["cred_source"] = out.cred_source
        items, strip_notes = await _collect(settings, client, out, response_format)
        upstream_block = {
            "path": "conversation",
            "tool_type": cap.tool_type,
            "frames": out.frames,
            "ratio": out.ratio,
            "text": out.text,
            "cred_source": out.cred_source,
            "refreshed": out.refreshed,
        }
    if strip_notes:
        warnings.append("结果 URL 的水印处理参数已剥离（BAIDU_STRIP_WATERMARK=1）")

    report("baidu.done", tool_type=cap.tool_type, items=len(items),
           path=effective["path"])
    return {
        "created": int(time.time()),
        "model": cap.name,
        "dry_run": False,
        "data": items,
        "usage": {"generated_images": len(items)},
        "requested": requested,
        "effective": effective,
        "warnings": warnings,
        "unsupported": unsupported,
        "upstream": upstream_block,
    }


# ---------------------------------------------------------------------------
# 输入准备 / 结果装配（辅助）
# ---------------------------------------------------------------------------


def _next_rank() -> int:
    """会话轮次：实测递增即可、不影响成败（契约 §1.2）。"""
    import random  # noqa: PLC0415

    return random.randint(3, 90)


def _legacy_level(cap: Capability) -> str:
    """老接口的档位参数（AI重绘=2 / 相似图=5；其他能力为空）。"""
    return dict(cap.legacy_extra).get("create_level", "")


def _ratio_of(size: str | None) -> tuple[str | None, str | None]:
    """把 `size` 解释成扩图比例。返回 `(ratio, 提示)`。"""
    if not size:
        return None, None
    s = size.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d{1,3}):(\d{1,3})", s)
    if m:
        return f"{int(m.group(1))}:{int(m.group(2))}", None
    m = re.fullmatch(r"(\d+)x(\d+)", s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if w <= 0 or h <= 0:
            return None, f"size={size!r} 不是合法尺寸，已忽略"
        from math import gcd  # noqa: PLC0415

        g = gcd(w, h)
        return f"{w // g}:{h // g}", None
    return None, (f"size={size!r} 无法解释为比例（扩图需要如 \"4:3\"），已忽略；"
                  f"不受支持的档位（1K/2K/4K 等）上游没有对应语义")


def _dry_image_plan(settings: Settings, image_value: str) -> tuple[str, dict[str, Any]]:
    """干跑时的输入图计划（**零网络**）。"""
    is_url = image_value.lower().startswith(("http://", "https://"))
    if is_url and settings.UPLOAD_MODE in ("direct", "auto"):
        return image_value, {"path": "direct", "would_upload": False}
    if is_url:
        return image_value, {
            "path": "bos", "would_upload": True,
            "note": "干跑未取回；真实调用会先代取再转存 BOS",
        }
    return "<data-uri/base64 将转存 BOS>", {"path": "bos", "would_upload": True}


async def _prepare_legacy_input(
    settings: Settings, client: BaiduClient, image_value: str, warnings: list[str],
) -> tuple[str, dict[str, Any], bytes]:
    """老接口直通时的输入准备：**只要字节**（`picInfo` 直传 base64，不转存、不取 URL）。

    返回 `(占位 image_url, info, 字节)`；`image_url` 只用于 `effective` 展示。
    """
    max_bytes = max(1, settings.MAX_DOWNLOAD_MB) * _MB
    blob = await resolve_input(
        image_value, accepts=IMAGE_KINDS, fetch=client.fetch_url,
        max_bytes=max_bytes, warnings=warnings,
    )
    info: dict[str, Any] = {
        "path": "legacy-picInfo",
        "input_origin": blob.origin,
        "input": {"kind": blob.kind, "mime": blob.mime, "bytes": blob.size},
        "note": "老接口直传 base64（无需 BOS；原样字节、不做标准化）",
    }
    return f"<legacy:picInfo {blob.size}B>", info, blob.data


async def _prepare_image(
    settings: Settings, client: BaiduClient, image_value: str, warnings: list[str],
) -> tuple[str, dict[str, Any], bytes | None]:
    """把输入图变成「上游抓得到的 URL」（主链用）。

    - `bos`（默认）：恒转存（代取 → 标准化 → BOS 四段式上传）；
    - `direct` / `auto`：外链直传；非 URL 输入自动降级为转存（留 warning）。

    返回值第三项是**本地字节**（转存路径下为上传的那份；外链直传为 None）——
    兜底改走老接口时要用它，没有就现场取回。
    """
    mode = settings.UPLOAD_MODE
    is_url = image_value.lower().startswith(("http://", "https://"))
    if mode in ("direct", "auto") and is_url:
        return image_value, {
            "path": "direct", "input_origin": "url",
            "note": "外链直传（未取回校验；上游抓不到该 URL 时会失败）",
        }, None

    max_bytes = max(1, settings.MAX_DOWNLOAD_MB) * _MB
    blob = await resolve_input(
        image_value, accepts=IMAGE_KINDS, fetch=client.fetch_url,
        max_bytes=max_bytes, warnings=warnings,
    )
    info: dict[str, Any] = {
        "path": "bos",
        "input_origin": blob.origin,
        "input": {"kind": blob.kind, "mime": blob.mime, "bytes": blob.size},
    }
    data, kind = blob.data, blob.kind
    if settings.NORMALIZE:
        got = normalize_image(data, kind, max_side=settings.MAX_SIDE,
                              max_bytes=settings.MAX_BYTES)
        if got is None:
            warnings.append("图像标准化：本地解码失败，已原样上送（由上游裁决）")
        else:
            new_data, new_kind = got
            if (new_data, new_kind) != (data, kind):
                info["normalized"] = {"from": f"{blob.kind} {blob.size}B",
                                      "to": f"{new_kind} {len(new_data)}B"}
            data, kind = new_data, new_kind

    url, reg = await client.upload_image(data, kind)
    info["bos_url"] = url
    info["register_ok"] = bool(reg.get("data") or reg.get("status") == 0)
    if mode == "direct":
        warnings.append("BAIDU_UPLOAD_MODE=direct 但输入不是可用外链，已自动转存 BOS")
    return url, info, data


def _deliver_one(settings: Settings, raw: bytes, response_format: str, *,
                 url_head: str = "") -> dict[str, Any]:
    """结果字节 → `data[]` 条目（主链与老接口**共用同一交付形态**）。"""
    kind = sniff(raw)
    if kind == "unknown":
        raise UpstreamUnavailableError(
            "结果不是可识别的图片（按真实字节嗅探失败）",
            detail={"url_head": url_head[:120] or None, "bytes": len(raw)},
        )
    mime, ext = mime_ext(kind)
    item: dict[str, Any] = {"mime": mime}
    if response_format == "url":
        try:
            name = save_media(raw, settings.MEDIA_DIR, ext)
        except OSError as exc:
            raise ApiError(503, "media_write_failed",
                           f"结果落盘失败（MEDIA_DIR={settings.MEDIA_DIR}）：{exc} —— 部署问题") from exc
        item["url"] = f"/files/{name}"
    else:
        item["b64_json"] = base64.b64encode(raw).decode("ascii")
    px = image_size(raw)
    if px:
        item["size"] = f"{px[0]}x{px[1]}"
    return item


async def _collect(
    settings: Settings, client: BaiduClient, out: Any, response_format: str,
) -> tuple[list[dict[str, Any]], bool]:
    """取回主链结果图并装配 `data[]`（下载 → 交付）。"""
    max_bytes = max(1, settings.MAX_DOWNLOAD_MB) * _MB
    items: list[dict[str, Any]] = []
    stripped = False
    for img in out.images:
        if not img.url:
            continue
        raw, _ct = await client.fetch_url(img.url, max_bytes,
                                          timeout=settings.RESULT_FETCH_TIMEOUT or None,
                                          retries=settings.RESULT_FETCH_RETRIES)
        item = _deliver_one(settings, raw, response_format, url_head=img.url)
        if "size" not in item and img.width and img.height:
            item["size"] = f"{img.width}x{img.height}"
        if img.note:
            stripped = True
        items.append(item)
    if not items:
        raise UpstreamUnavailableError("上游未给出可交付的图片 URL（全部条目为空）")
    return items, stripped


def _input_summary(value: Any) -> dict[str, Any]:
    """输入摘要（**不回显原文**：可能是几 MB 的 base64）。"""
    if isinstance(value, str):
        if value.startswith("data:"):
            form = "data-uri"
        elif value[:4].lower() == "http":
            form = "url"
        else:
            form = "base64"
        return {"form": form, "length": len(value)}
    if isinstance(value, list):
        return {"form": "list", "items": len(value)}
    return {"form": type(value).__name__}
