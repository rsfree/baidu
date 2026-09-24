#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体处理：输入归一 / 类型嗅探 / 图像标准化 / 结果落盘 / 像素尺寸。

四条硬规矩（前三条来自实测踩坑，与 reverse-proxy 的适配层同源）：

  1. **按 magic bytes 嗅探，不信扩展名也不信 Content-Type。**
     真实案例（上游侧历史）：`.jpg` 结尾、Content-Type 写成非标的 `image/jpg`，
     实际是 PNG，服务端按 Content-Type 抓取直接失败。
  2. **外链一律由本服务代取并设硬上限**（预检 Content-Length + 读流封顶），
     取不到就显式失败，绝不把半个文件发给上游。
  3. **转存前标准化**：限最长边 / 压到字节上限内 / 统一标准 MIME —— 消除上游
     抓取失败的两个已知诱因（体积、非标 MIME）。**解不开的图原样上送并留 warning**，
     由上游决定（见 `normalize_image` 的返回值约定）。
  4. 结果落盘**内容寻址**（sha256 前缀命名）：同名同内容，天然幂等。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable

from .errors import ApiError

__all__ = [
    "Blob", "sniff", "mime_ext", "resolve_input", "save_media", "image_size",
    "normalize_image", "DATA_URI_RE", "IMAGE_KINDS",
]

#: data URI：`data:[<mime>][;base64],<payload>`。mime 只作参考，**以嗅探为准**。
DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]*)?(?P<b64>;base64)?,(?P<payload>.*)$", re.S)

_B64_RE = re.compile(r"^[A-Za-z0-9+/=\s]{32,}$")

#: 本服务接受的输入家族（按真实字节嗅探判定）。
#: ⚠️ GIF 刻意不收：上游对它的行为**未取证**（与 textin「GIF 未测不收」同一纪律）。
IMAGE_KINDS: tuple[str, ...] = ("png", "jpeg", "webp", "bmp")

_KIND_MIME: dict[str, tuple[str, str]] = {
    "png": ("image/png", ".png"),
    "jpeg": ("image/jpeg", ".jpg"),
    "webp": ("image/webp", ".webp"),
    "bmp": ("image/bmp", ".bmp"),
    "gif": ("image/gif", ".gif"),
    "unknown": ("application/octet-stream", ".bin"),
}

#: 出站 Content-Type 用 `_KIND_MIME[kind][0]`；`jpg` 必须映射为 `image/jpeg`
#: （写成 `image/jpg` 是历史上的真实失败诱因）。
_UPLOAD_EXT: dict[str, str] = {"png": "png", "jpeg": "jpg", "webp": "webp", "bmp": "bmp"}


def mime_ext(kind: str) -> tuple[str, str]:
    return _KIND_MIME.get(kind, _KIND_MIME["unknown"])


def sniff(data: bytes) -> str:
    """按 magic bytes 判定图像家族；认不出返回 "unknown"。"""
    if len(data) < 4:
        return "unknown"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:2] == b"BM" and len(data) >= 14:
        return "bmp"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    return "unknown"


def upload_ext(kind: str) -> str:
    """BOS 转存用的扩展名（`jpg` 而非 `jpeg`；MIME 由 `mime_ext` 给标准值）。"""
    return _UPLOAD_EXT.get(kind, "png")


@dataclass(frozen=True)
class Blob:
    """一次输入：**已归一为本地字节**，类型来自嗅探。"""

    data: bytes
    kind: str
    mime: str
    ext: str
    origin: str = "base64"       # data-uri / url / base64

    @property
    def size(self) -> int:
        return len(self.data)


def _make_blob(data: bytes, *, origin: str, accepts: tuple[str, ...]) -> Blob:
    if not data:
        raise ApiError(400, "empty_input", "输入是 0 字节（fetch/解码得到空内容）")
    kind = sniff(data)
    if kind == "gif":
        raise ApiError(
            400, "input_kind_not_accepted",
            "GIF 的上游行为未取证，本服务不收（请先转成 PNG/JPEG）；"
            "已接受：" + "、".join(accepts),
            sniffed_kind=kind, accepted=list(accepts),
        )
    if kind not in accepts:
        raise ApiError(
            400, "input_kind_not_accepted",
            f"输入是 {kind}（按真实字节嗅探；扩展名/Content-Type 不作数），"
            f"但本服务只接受：{'、'.join(accepts)}",
            sniffed_kind=kind, accepted=list(accepts),
        )
    mime, ext = mime_ext(kind)
    return Blob(data=data, kind=kind, mime=mime, ext=ext, origin=origin)


async def resolve_input(
    value: str,
    *,
    accepts: tuple[str, ...] = IMAGE_KINDS,
    fetch: Callable[[str, int], Awaitable[tuple[bytes, str | None]]] | None = None,
    max_bytes: int,
    warnings: list[str] | None = None,
) -> Blob:
    """把 `image` 字段的三种到达形态归一成 Blob：data URI / http(s) URL / 裸 base64。

    - URL 形态需要 `fetch`（由 client 提供；测试用假 fetch / MockTransport，零出网）；
    - 类型一律**以嗅探为准**；data URI 声明的 mime 与真实不符时留 warning（不拒绝）。
    """
    warn = warnings if warnings is not None else []
    if not isinstance(value, str) or not value.strip():
        raise ApiError(
            400, "missing_input",
            "缺少输入图片（image 字段）。接受三种形态："
            "data URI（`data:image/png;base64,...`）、http(s) URL、裸 base64",
        )
    value = value.strip()

    m = DATA_URI_RE.match(value)
    if m:
        payload = m.group("payload") or ""
        declared = (m.group("mime") or "").strip() or None
        if m.group("b64"):
            try:
                data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ApiError(400, "invalid_base64", f"data URI 的 base64 解不开：{exc}") from exc
        else:
            from urllib.parse import unquote_to_bytes  # noqa: PLC0415 - 局部使用

            data = unquote_to_bytes(payload)
        blob = _make_blob(data, origin="data-uri", accepts=accepts)
        if declared and declared != blob.mime:
            warn.append(
                f"data URI 声明的 mime（{declared}）与真实类型不符，已按真实类型 {blob.mime} 发出"
            )
        return blob

    if value.lower().startswith(("http://", "https://")):
        if fetch is None:
            raise ApiError(400, "url_input_unsupported", "本部署未启用外链输入")
        data, declared_ct = await fetch(value, max_bytes)
        warn.append(f"已由本服务代取外链（{len(data)} 字节）")
        blob = _make_blob(data, origin="url", accepts=accepts)
        if declared_ct:
            declared_base = declared_ct.split(";")[0].strip().lower()
            if declared_base and declared_base not in (blob.mime, "application/octet-stream"):
                warn.append(
                    f"外链响应的 Content-Type（{declared_base}）与真实类型不符，"
                    f"已按真实类型 {blob.mime} 发出"
                )
        return blob

    if _B64_RE.match(value):
        try:
            data = base64.b64decode(re.sub(r"\s+", "", value), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ApiError(400, "invalid_base64", f"裸 base64 解不开：{exc}") from exc
        return _make_blob(data, origin="base64", accepts=accepts)

    raise ApiError(
        400, "invalid_input_format",
        "输入既不是 data URI、也不是 http(s) URL、也不是合法 base64。"
        "裸 base64 至少 32 字符；本地文件请自行编码后传入",
    )


# ---------------------------------------------------------------------------
# 图像标准化（BOS 转存前）
# ---------------------------------------------------------------------------


def normalize_image(
    data: bytes, kind: str, *, max_side: int, max_bytes: int,
) -> tuple[bytes, str] | None:
    """限最长边、压到字节上限内、统一标准 MIME。

    返回 `(bytes, kind)`；**解不开（PIL 失败）返回 None** —— 调用方应原样上送并留
    warning，而不是把它当"非图片"拒掉（magic bytes 已经证明它是个图片容器）。

    两条保真约定（沿用探针实现）：
      · 透明通道（RGBA/LA/P）保留为 PNG，不误转 JPEG 丢 alpha；
      · **只缩不放**；原图已达标且重编码反而更大时保留原图。
    """
    try:
        from PIL import Image  # noqa: PLC0415 - 可选依赖路径（未装时调用方已跳过）

        im = Image.open(BytesIO(data))
        im.load()
    except Exception:  # noqa: BLE001 - 解码失败不是"非图片"，交给上游判
        return None

    resized = max(im.size) > max_side
    if resized:
        ratio = max_side / max(im.size)
        im = im.resize((max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                       Image.LANCZOS)

    buf = BytesIO()
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA") if im.mode != "RGBA" else im
        out_kind = "png"
        im.save(buf, "PNG", optimize=True)
    else:
        im = im.convert("RGB")
        out_kind = "jpeg"
        quality = 90
        while True:
            buf = BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes or quality <= 50:
                break
            quality -= 10
    out = buf.getvalue()

    if not resized and len(data) <= max_bytes and len(out) >= len(data):
        return data, kind              # 原图更小且已达标：保留原图（避免"标准化变大"）
    return out, out_kind


# ---------------------------------------------------------------------------
# 落盘与尺寸
# ---------------------------------------------------------------------------


def save_media(data: bytes, media_dir: str | Path, ext: str) -> str:
    """把结果落盘（`response_format=url` 时）。内容寻址：同名同内容，天然幂等。"""
    name = hashlib.sha256(data).hexdigest()[:20] + ext
    target = Path(media_dir) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return name


def image_size(data: bytes) -> tuple[int, int] | None:
    """PNG / JPEG 的像素尺寸（只在**确实解析出来**时返回，绝不编造）。"""
    kind = sniff(data)
    if kind == "png" and len(data) >= 24:
        import struct  # noqa: PLC0415

        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    if kind == "jpeg":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if i + 9 < n:
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return int(w), int(h)
                return None
            if seg_len <= 0:
                return None
            i += 2 + seg_len
    return None
