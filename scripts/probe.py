#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零成本自检：把每条能力的**出站请求形状**与**响应装配回路**打出来核对。

三个阶段，默认只跑前两个（**一个字节都不发给真实上游**）：

  `shapes` 对全部能力构造出站请求（dry_run，零网络）→ 打印 tool_type / query / 上传计划 / 打码检查
  `loop`   起本地假上游，把每条能力从 HTTP 端点打到"上游"再回装配（全链路回路）
  `live`   ⚠️ **真实调用上游**（免费，但失败请求计入风控评分；别连打）—— 必须显式 `--live`

说明：shapes/loop 一律以 `BAIDU_LEGACY=fallback` 运行（dry_run/假上游都不触网）——
这样 legacy-only 与 `requires_mask` 能力的**实际执行计划**才可见、可回归。

用法：
    python scripts/probe.py
    python scripts/probe.py --phases shapes,loop
    python scripts/probe.py --live --cap wenxin:clarity --image https://... [--cookie-file <identity.json>]

`--cookie-file` 可指向 `wenxin/tools/mint_identity.py` 的产物（identity.json），
自动取其中的 `cookie` 字段 —— 这样 live 自检不需要手工贴 cookie。

⚠️ `live` 阶段只覆盖**主链**。老接口通路（含 legacy-only / 遮罩类能力）的线上核验
用 `scripts/live_legacy.py`（经服务完整链路，见其 `--help`）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import CAPABILITIES, availability, lookup  # noqa: E402
from app.upstream.baidu import BaiduClient  # noqa: E402

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8ffff3f0005fe02fea735d8850000000049454e44ae426082")
DATA_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()
#: 遮罩样本（shapes/loop 只验证"字段传到了"，语义由判决实验背书）：黑底 + 白框
MASK_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()


def _settings_offline(*, base: str = "http://127.0.0.1:8800") -> Settings:
    # 离线自检要放开每分钟上限（否则第 9 条起会撞上自家速率闸门 —— 那是线上纪律，
    # 不是形状问题）；对应地这也证明闸门真的在工作。
    # LEGACY=fallback：让 legacy-only / 遮罩类能力的执行计划可见（不触网）。
    # 🔴 LEGACY_BASE 必须一起指到假上游 —— 漏了它，legacy-only 能力会**打到真实端点**
    #    （2026-09-24 踩过：probe 把 erase/redraw/replace/similar 打到了 image.baidu.com）。
    return Settings(_env_file=None, API_KEYS="", COOKIE="BAIDUID=probe",
                    SESSION_TOKEN="tok-probe", LID="lid-probe", UPLOAD_MODE="bos",
                    NORMALIZE=False, MIN_INTERVAL=0.0, PER_MINUTE=100, MAX_WAIT=5.0,
                    COOLDOWN=0.0, BASE_URL=base, BOS_HOST=base + "/bos",
                    CDN_BASE=base + "/img", MEDIA_DIR="/tmp/baidu-probe-media",
                    LEGACY="fallback", LEGACY_BASE=base,
                    LEGACY_POLL_INTERVAL=0.05, LEGACY_POLL_TRIES=20)


# --------------------------------------------------------------------------- shapes


def phase_shapes() -> int:
    app = create_app(_settings_offline())
    bad = 0
    with TestClient(app) as c:
        for name in sorted(CAPABILITIES):
            cap = CAPABILITIES[name]
            payload: dict[str, object] = {"model": name, "image": DATA_URI, "dry_run": True}
            if cap.supports_ratio:
                payload["size"] = "4:3"
            if cap.requires_mask:
                payload["mask"] = MASK_URI
            if cap.needs_style:
                payload["style"] = cap.style_table[0][1]
            elif cap.legacy_uses_prompt:
                payload["prompt"] = "一只橘猫"
            r = c.post("/v1/images/generations", json=payload)
            body = r.json()
            gate = "" if cap.verified else "（门禁中，dry 放行）"
            ok = r.status_code == 200 and body.get("dry_run") is True
            prev = body.get("preview") or {}
            if cap.legacy_only:
                # legacy-only：没有主链 body，形状核对的是**老接口计划**
                shape_ok = (prev.get("body") is None
                            and (prev.get("legacy") or {}).get("type") == cap.legacy_type
                            and str(prev.get("would_post_to") or "").endswith("/aigc/pccreate"))
                detail = f"legacy_type={cap.legacy_type}"
            else:
                msg = prev["body"]["message"]
                search = msg["searchInfo"]
                if cap.entry_type and not cap.needs_style:
                    # 工具入口形状：sa=searchbox_image + enter_type=<码> + **无 mcpInfo**
                    shape_ok = (search["sa"] == "searchbox_image"
                                and search["enter_type"] == cap.entry_type
                                and "mcpInfo" not in search
                                and msg["query"][-1]["data"]["text"]["query"] != cap.title)
                    detail = f"entry={cap.entry_type}"
                else:
                    ext = json.loads(search["mcpInfo"]["ext"])
                    shape_ok = (ext["type"] == cap.tool_type
                                and search["chatParams"]["chat_token"] == "***")
                    if cap.requires_mask:
                        shape_ok = shape_ok and (prev.get("legacy") or {}).get("mask") is True
                    if cap.needs_style:
                        # 实测形状：专属 sa + ext.style/text + image_source=1
                        shape_ok = (shape_ok and search["sa"] == cap.workspace_sa
                                    and ext.get("style") == cap.style_table[0][0]
                                    and ext.get("text") == cap.style_table[0][1]
                                    and ext.get("image_source") == 1)
                        detail = f"sa={cap.workspace_sa} style={ext.get('style')}"
                    else:
                        detail = f"tt={cap.tool_type}"
            if not (ok and shape_ok):
                bad += 1
            print(f"{'✅' if ok and shape_ok else '❌'} {name:<22s} {detail:<16s} "
                  f"query={cap.title:<6s} upload={body['effective'].get('upload_mode', '')} "
                  f"{gate}")
    print(f"shapes: {'全部通过' if bad == 0 else f'失败 {bad} 项'}（共 {len(CAPABILITIES)} 条）")
    return bad


# --------------------------------------------------------------------------- loop


def phase_loop() -> int:
    import mock_upstream  # noqa: PLC0415 - 同在 scripts/ 下

    port = 8810
    mock_upstream.Handler.server_base = f"http://127.0.0.1:{port}"
    server = mock_upstream.ThreadingHTTPServer(("127.0.0.1", port), mock_upstream.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    conf = _settings_offline(base=f"http://127.0.0.1:{port}")
    app = create_app(conf)
    legacy = conf.LEGACY != "off"
    bad = 0
    with TestClient(app) as c:
        for name in sorted(CAPABILITIES):
            cap = CAPABILITIES[name]
            ok_gate = availability(cap, allow_unverified=False, legacy=legacy)[0]
            payload: dict[str, object] = {"model": name, "image": DATA_URI}
            if cap.requires_mask:
                payload["mask"] = MASK_URI
            if cap.needs_style:
                payload["style"] = cap.style_table[0][1]
            elif cap.legacy_uses_prompt:
                payload["prompt"] = "一只橘猫"
            r = c.post("/v1/images/generations", json=payload)
            body = r.json()
            if ok_gate:
                got = r.status_code == 200 and body.get("data") and body["data"][0].get("b64_json")
                bad += 0 if got else 1
                path = body.get("effective", {}).get("path", "?")
                extra = f"path={path}" if got else f"{r.status_code} {body.get('error', {}).get('code', '?')}"
                print(f"{'✅' if got else '❌'} {name:<22s} → "
                      f"{'200/' + str(len(body.get('data', []))) + ' 件' if got else 'FAIL'} "
                      f"({extra})")
            else:
                got = r.status_code == 503 and body["error"]["code"] == "capability_not_verified"
                bad += 0 if got else 1
                print(f"{'✅' if got else '❌'} {name:<22s} → 503（门禁，符合预期）")
    server.shutdown()
    print(f"loop: {'全部通过' if bad == 0 else f'失败 {bad} 项'}")
    return bad


# --------------------------------------------------------------------------- live


def _load_cookie(cookie_file: str | None) -> str:
    if not cookie_file:
        return ""
    raw = Path(cookie_file).read_text(encoding="utf-8").strip()
    if raw.startswith("{"):
        return str(json.loads(raw).get("cookie") or "")
    return raw


def phase_live(cap_name: str, image: str, cookie_file: str | None) -> int:
    s = get_settings()
    cookie = _load_cookie(cookie_file) or s.COOKIE
    if not cookie:
        print("❌ 没有可用 cookie：请配 BAIDU_COOKIE 或 --cookie-file <identity.json>")
        return 2
    s = s.model_copy(update={"COOKIE": cookie, "UPLOAD_MODE": "direct"})
    cap = lookup(cap_name)
    if cap.legacy_only or cap.requires_mask:
        print(f"❌ {cap_name} 不走主链（legacy-only / 需遮罩）—— 本阶段只覆盖主链；"
              f"请用 scripts/live_legacy.py（经服务完整链路）")
        return 2

    async def go() -> int:
        client = BaiduClient(s)
        try:
            out = await client.converse(cap, query_text=cap.title, image_url=image,
                                        rank=42, strip_watermark=s.STRIP_WATERMARK)
            print(f"✅ live {cap_name}：出图 {len(out.images)} 张（{out.frames} 帧）")
            print(f"   文案：{out.text}")
            for img in out.images:
                print(f"   结果：{img.url.split('?')[0][:110]}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"❌ live {cap_name} 失败：{type(exc).__name__}: {str(exc)[:200]}")
            return 1
        finally:
            await client.aclose()

    return asyncio.run(go())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phases", default="shapes,loop",
                    help="shapes,loop（默认；零网络）；live 需另行显式开启")
    ap.add_argument("--live", action="store_true", help="⚠️ 真实调用（免费但计入风控评分）")
    ap.add_argument("--cap", default="wenxin:clarity", help="live 用哪条能力")
    ap.add_argument("--image", default="", help="live 的输入图（URL；必填）")
    ap.add_argument("--cookie-file", default=None,
                    help="从 identity.json / 文本文件读 cookie（免手工粘贴）")
    a = ap.parse_args()

    bad = 0
    phases = [p.strip() for p in a.phases.split(",") if p.strip()]
    if "shapes" in phases:
        bad += phase_shapes()
    if "loop" in phases:
        bad += phase_loop()
    if a.live:
        if not a.image:
            print("--live 需要 --image <URL>")
            return 2
        print("⚠️ live：这是一次**真实**上游调用（免费；请勿连打）")
        bad += phase_live(a.cap, a.image, a.cookie_file)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
