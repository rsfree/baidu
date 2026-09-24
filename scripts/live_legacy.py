#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""老接口通路的**线上核验**（经服务完整链路；真实调用、免费、别连打）。

覆盖 `probe.py --live` 不碰的那几条：`erase`(8) / `replace`(5) / `redraw`(6) / `similar`(7)，
以及任意有映射的能力（`--caps clarity,dewatermark,...`）。

前置：服务已启动且 `BAIDU_LEGACY` ∈ {`fallback`,`prefer`}（否则未取证能力一律 503）：

    BAIDU_LEGACY=fallback BAIDU_PORT=8700 python -m app.main

用法：

    python scripts/live_legacy.py --image /tmp/photo.jpg                 # 四条全跑
    python scripts/live_legacy.py --image p.png --caps erase,redraw
    python scripts/live_legacy.py --image p.png --box 300,200,600,400    # 遮罩区域（x0,y0,x1,y1）
    python scripts/live_legacy.py --image p.png --dry                      # 零成本预演（不发真实请求）

产物落 `var/live/`（可用 `--out` 改）。**遮罩语义**：黑底 + 白框（白色=要处理的区域）。
"""

from __future__ import annotations

import argparse
import base64
import io
import time
from pathlib import Path

try:
    import httpx
    from PIL import Image, ImageDraw
except ImportError as _exc:  # pragma: no cover
    raise SystemExit(f"需要 httpx 与 pillow：<venv>/bin/pip install httpx pillow（{_exc}）") from None

DEFAULT_CAPS = "erase,replace,redraw,similar"


def _data_uri(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def _mask_uri(path: Path, box: tuple[int, int, int, int] | None) -> str:
    """黑底 + 白框遮罩（白色=要处理的区域；判决实验结论）。"""
    im = Image.open(path)
    w, h = im.size
    x0, y0, x1, y1 = box or (w // 4, h // 4, w * 3 // 4, h * 3 // 4)
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rectangle([x0, y0, x1, y1], fill=255)
    buf = io.BytesIO()
    m.convert("RGB").save(buf, "PNG")
    return _data_uri(buf.getvalue())


def main() -> int:
    ap = argparse.ArgumentParser(description="老接口通路线上核验（经服务）")
    ap.add_argument("--base", default="http://127.0.0.1:8700", help="服务地址")
    ap.add_argument("--image", required=True, help="输入图（本地文件）")
    ap.add_argument("--caps", default=DEFAULT_CAPS, help=f"逗号分隔（短名或 wenxin:*）；默认 {DEFAULT_CAPS}")
    ap.add_argument("--box", default="", help="遮罩区域 x0,y0,x1,y1（默认居中 1/2 区域）")
    ap.add_argument("--prompt", default="一只橘猫", help="局部替换的替换内容（→ 老接口 text）")
    ap.add_argument("--out", default="var/live", help="产物目录")
    ap.add_argument("--api-key", default="", help="BAIDU_API_KEYS 非空时必填")
    ap.add_argument("--dry", action="store_true", help="只做 dry_run 预演（零上游请求）")
    a = ap.parse_args()

    src_path = Path(a.image)
    if not src_path.is_file():
        print(f"❌ 输入图不存在：{src_path}")
        return 2
    box: tuple[int, int, int, int] | None = None
    if a.box:
        parts = [int(x) for x in a.box.replace(" ", "").split(",")]
        if len(parts) != 4:
            print("❌ --box 需要 4 个整数：x0,y0,x1,y1")
            return 2
        box = (parts[0], parts[1], parts[2], parts[3])

    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = src_path.read_bytes()
    mask = _mask_uri(src_path, box)
    headers = {"Authorization": f"Bearer {a.api_key}"} if a.api_key else {}
    stamp = time.strftime("%H%M%S")

    # 服务端的能力元数据：决定要带哪些字段（mask / prompt）与预期老接口 type
    with httpx.Client(trust_env=False, timeout=180.0) as c:
        try:
            caps = c.get(f"{a.base}/capabilities", headers=headers).json()
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 服务不可达（{a.base}）：{exc}")
            return 2
    meta = {m["id"]: m for m in caps["models"] + caps["not_available"]}
    if caps.get("legacy", {}).get("mode", "off") == "off":
        print("⚠️ 服务侧 BAIDU_LEGACY=off：未取证能力会 503（要跑请用 fallback/prefer 重启）")

    bad = 0
    with httpx.Client(trust_env=False, timeout=180.0) as c:
        for short in [s.strip() for s in a.caps.split(",") if s.strip()]:
            name = short if short.startswith("wenxin:") else f"wenxin:{short}"
            m = meta.get(name)
            if not m:
                print(f"❌ {name}：能力表里没有这个模型")
                bad += 1
                continue
            payload: dict[str, object] = {"model": name, "image": _data_uri(src)}
            if m.get("requires_mask"):
                payload["mask"] = mask
            if m.get("legacy_uses_prompt"):
                payload["prompt"] = a.prompt
            if a.dry:
                payload["dry_run"] = True
            t0 = time.time()
            r = c.post(f"{a.base}/v1/images/generations", json=payload, headers=headers)
            dt = time.time() - t0
            body = r.json()
            if a.dry:
                ok = r.status_code == 200 and body.get("dry_run") is True
                plan = (body.get("preview") or {}).get("legacy") or {}
                print(f"{'✅' if ok else '❌'} {name:<18s} dry_run 计划："
                      f"type={plan.get('type')} level={plan.get('create_level') or '-'} "
                      f"mask={plan.get('mask')}")
                bad += 0 if ok else 1
                continue
            if r.status_code != 200:
                err = body.get("error", {})
                print(f"❌ {name:<18s} {dt:.1f}s HTTP {r.status_code} "
                      f"{err.get('code')}/{err.get('kind')}: {str(err.get('message'))[:120]}")
                bad += 1
                continue
            item = (body.get("data") or [{}])[0]
            raw = base64.b64decode(item.get("b64_json", ""))
            fname = f"{name.split(':')[1]}_legacy_{stamp}.png"
            (out_dir / fname).write_bytes(raw)
            lg = (body.get("upstream") or {}).get("legacy") or {}
            print(f"✅ {name:<18s} {dt:.1f}s {item.get('size')} "
                  f"path={body['effective'].get('path')} "
                  f"type={lg.get('type')} polls={lg.get('polls')} "
                  f"level={lg.get('create_level') or '-'} → {out_dir / fname}")

    print(f"\n结果：{'全部通过' if bad == 0 else f'失败 {bad} 项'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
