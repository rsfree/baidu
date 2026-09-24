#!/usr/bin/env python3
"""baidu-service 多形态接口测试 —— **打真实服务**（默认 ai-prod 回环 127.0.0.1:39014）。

⚠️ 与 `scripts/probe.py` / `scripts/smoke.sh` 的分工：那两个是**离线**套件（假上游、零触网）；
本脚本故意打真上游，用来验「真实链路的形态覆盖」，**会产生真实调用**（老接口免费、主链走风控额度）。
夹具：`baidu_fix_img.jpg`(900x600) + `baidu_fix_mask.png`（黑底白框，同尺寸）——本机 PIL 生成后投递。


三部分：
  A. 形态矩阵 —— 输入（URL / data URI / 裸 base64 / 遮罩）× 输出（b64_json / url）+ dry_run
  B. 全量 dry_run —— 19 项能力逐条验「请求形状」（形状从 /capabilities 自描述，不手抄）
  C. 真跑子集 —— 主链 + 老接口各取代表，记录 耗时/尺寸/通路/字节数

判据一律「业务字段 + 产物非空」，不只看 HTTP 200。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

import argparse

_ap = argparse.ArgumentParser(description="baidu-service 多形态接口测试（**打真实服务**，非离线套件）")
_ap.add_argument("--base", default="http://127.0.0.1:39014", help="服务地址（默认本机回环，ai-prod 部署端口）")
_ap.add_argument("--key-file", default="/opt/baidu/.env", help="从中读 BAIDU_API_KEYS 的 env 文件")
_ap.add_argument("--fixtures", default="/tmp", help="夹具目录（需含 baidu_fix_img.jpg 与 baidu_fix_mask.png）")
_ap.add_argument("--report", default="/tmp/baidu_multiform_report.md", help="报告输出路径")
_a = _ap.parse_args()

BASE = _a.base.rstrip("/")
KEY = [l.split("=", 1)[1].strip() for l in open(_a.key_file) if l.startswith("BAIDU_API_KEYS")][0]
CDN_IMG = "https://aisearch.cdn.bcebos.com/homepage/chat_tool/v2/ai_image.png"


def post(path: str, payload: dict, *, timeout: int = 300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
    t0 = time.time()
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        return 200, r, time.time() - t0
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except (ValueError, OSError):
            body = {"error": {"code": "unparsable"}}
        return e.code, body, time.time() - t0
    except (urllib.error.URLError, OSError) as exc:      # 超时/连接类
        return 0, {"error": {"code": type(exc).__name__, "message": str(exc)[:120]}}, time.time() - t0


def get(path: str):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + KEY})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def make_images():
    """夹具：优先读预置文件（本机 PIL 生成后投递；节点 host 无 PIL，别指望现场合成）。"""
    j = os.path.join(_a.fixtures, "baidu_fix_img.jpg")
    m = os.path.join(_a.fixtures, "baidu_fix_mask.png")
    if os.path.exists(j) and os.path.exists(m):
        jpg = open(j, "rb").read()
        png = open(m, "rb").read()
        return ("data:image/jpeg;base64," + base64.b64encode(jpg).decode(),
                "data:image/png;base64," + base64.b64encode(png).decode())
    raise SystemExit(f"❌ 缺夹具 {_a.fixtures}/baidu_fix_img.jpg 与 baidu_fix_mask.png（本机 PIL 生成后投递）")


IMG_URI, MASK_URI = make_images()
RAW_B64 = IMG_URI.split(",", 1)[1] if IMG_URI else ""
OUT: list[str] = []
def say(s=""):
    print(s, flush=True); OUT.append(s)


# ---------------------------------------------------------------- A. 形态矩阵
say("## A. 形态矩阵（模型 `wenxin:clarity`，输入/输出形态组合）\n")
say("| # | 输入形态 | 输出形态 | HTTP | 结果 | 耗时 |")
say("|---|---|---|---|---|---|")
FORM_CASES = [
    ("URL（aisearch CDN）", {"image": CDN_IMG}, "b64_json"),
    ("data URI（JPEG）", {"image": IMG_URI}, "b64_json"),
    ("裸 base64", {"image": RAW_B64}, "b64_json"),
    ("URL + response_format=url", {"image": CDN_IMG}, "url"),
    ("data URI + dry_run（不触网）", {"image": IMG_URI, "dry_run": True}, "b64_json"),
]
A_OK = 0
for i, (name, fields, fmt) in enumerate(FORM_CASES, 1):
    body = {"model": "wenxin:clarity", "response_format": fmt, **fields}
    st, r, dt = post("/v1/images/generations", body)
    if st == 200 and r.get("dry_run"):
        ok = "✅ dry_run（零触网）" if "conversation" in json.dumps(r.get("preview", {})) else "⚠️ 计划缺 conversation"
    elif st == 200:
        item = (r.get("data") or [{}])[0]
        if fmt == "url":
            u = item.get("url", "")
            try:                                   # url 是**相对路径**（/files/<name>），按服务 origin 取回
                raw = urllib.request.urlopen(BASE + u, timeout=60).read() if u.startswith("/files/") else None
            except (urllib.error.URLError, OSError) as exc:
                raw = None
                u += f" (取回失败 {type(exc).__name__})"
            ok = f"✅ {u} 可取回 {len(raw)//1024}KB" if raw else f"❌ url 取不回：{u[:70]}"
        else:
            b64 = item.get("b64_json", "")
            ok = f"✅ b64 非空 {len(b64)*3//4}B size={item.get('size')}" if b64 else "❌ b64 空"
    else:
        ok = f"❌ {r.get('error', {}).get('code')}"
    A_OK += 1 if ok.startswith("✅") else 0
    say(f"| {i} | {name} | {fmt} | {st} | {ok} | {dt:.1f}s |")
say(f"\nA 段：{A_OK}/{len(FORM_CASES)} 通过\n")

# ---------------------------------------------------------------- B. 全量 dry_run
caps = get("/capabilities")
models = {m["id"]: m for m in caps["models"]}
say(f"## B. 全量 dry_run（{len(caps['models']) + len(caps['not_available'])} 项能力的请求形状）\n")
say("| model | 名称 | 必填输入 | HTTP | 形状判据 |")
say("|---|---|---|---|---|")
B_OK = 0
ALL = [(m, "可用") for m in caps["models"]] + [(m, "门禁") for m in caps["not_available"]]
for m, state in sorted(ALL, key=lambda x: x[0]["id"]):
    mid = m["id"]
    payload = {"model": mid, "image": CDN_IMG, "dry_run": True}
    if m.get("requires_mask") or mid in ("wenxin:erase", "wenxin:replace", "wenxin:bgreplace"):
        payload["mask"] = MASK_URI
    if m.get("needs_style") or mid == "wenxin:restyle":
        payload["style"] = "宫崎骏风"
    if mid in ("wenxin:replace", "wenxin:bgreplace"):
        payload["prompt"] = "换成大雪纷飞的街道"
    if mid == "wenxin:expand":
        payload["size"] = "4:3"
    st, r, dt = post("/v1/images/generations", payload)
    shape = ""
    if st == 200:
        prev = r.get("preview") or {}
        leg = prev.get("legacy") or {}
        if leg.get("type"):
            shape = f"老接口 type={leg['type']} mask={leg.get('mask')}"
            if m.get("needs_style"):
                shape += "（风格走主链，预览为兜底计划）"
        else:
            search = (prev.get("body") or {}).get("message", {}).get("searchInfo", {})
            ext = json.loads(search.get("mcpInfo", {}).get("ext", "{}")) if search.get("mcpInfo") else {}
            shape = f"sa={search.get('sa')} toolType={ext.get('type')}"
            if ext.get("style"):
                shape += f" style={ext.get('style')}"
    elif st == 503:
        shape = f"门禁（预期）：{r.get('error', {}).get('code')}"
    else:
        shape = f"❌ {r.get('error', {}).get('code')}"
    good = shape.startswith(("老接口", "sa=", "门禁（预期）"))
    B_OK += 1 if good else 0
    req = "+".join([x for x in (["mask"] if "mask" in payload else []) + (["style"] if "style" in payload else [])
                    + (["prompt"] if "prompt" in payload else []) + (["size"] if "size" in payload else [])] or ["image"])
    say(f"| `{mid}` | {m['title']} | {req} | {st} | {shape} |")
say(f"\nB 段：{B_OK}/{len(ALL)} 形状符合预期\n")

# ---------------------------------------------------------------- C. 真跑子集
say("## C. 真跑（每条 1 次，记录通路与产物）\n")
say("| model | 必填 | HTTP | 通路 | 尺寸 | 产物 | 耗时 |")
say("|---|---|---|---|---|---|---|")
REAL = [
    ("wenxin:clarity", {"image": IMG_URI}),
    ("wenxin:matting", {"image": IMG_URI}),
    ("wenxin:expand", {"size": "4:3", "image": IMG_URI}),
    ("wenxin:restyle", {"style": "宫崎骏风", "image": IMG_URI}),
    ("wenxin:dewatermark", {"image": IMG_URI}),
    ("wenxin:erase", {"mask": MASK_URI, "image": IMG_URI}),
    ("wenxin:replace", {"mask": MASK_URI, "prompt": "换成一片星空", "image": IMG_URI}),
    ("wenxin:bgreplace", {"mask": MASK_URI, "prompt": "大雪纷飞的街道", "image": IMG_URI}),
    ("wenxin:redraw", {"image": IMG_URI}),
    ("wenxin:similar", {"image": IMG_URI}),
]
C_OK = 0
for mid, extra in REAL:
    st, r, dt = post("/v1/images/generations", {"model": mid, **({"image": CDN_IMG} | extra)})
    if st == 200:
        it = (r.get("data") or [{}])[0]
        nb = len(it.get("b64_json", "")) * 3 // 4
        up = (r.get("upstream") or {})
        leg = up.get("legacy") or {}
        route = f"legacy type={leg.get('type')}" if leg.get("type") else f"conversation toolType={up.get('tool_type')}"
        ok = "✅" if nb > 1000 else "❌ 产物过小"
        C_OK += 1 if nb > 1000 else 0
        say(f"| `{mid}` | {', '.join(k for k in extra if k != 'mask') or '—'} | {st} | {route} | {it.get('size')} | {nb//1024}KB {ok} | {dt:.1f}s |")
    else:
        e = r.get("error", {})
        say(f"| `{mid}` | — | {st} | — | — | ❌ {e.get('code')} {str(e.get('message'))[:70]} | {dt:.1f}s |")
say(f"\nC 段：{C_OK}/{len(REAL)} 真跑成功\n")

say(f"## 汇总\n\n- A 形态矩阵：**{A_OK}/{len(FORM_CASES)}**\n- B 形状自证：**{B_OK}/{len(ALL)}**\n- C 真跑：**{C_OK}/{len(REAL)}**\n")
open(_a.report, "w", encoding="utf-8").write("\n".join(OUT) + "\n")
print(f"报告已写 {_a.report}")
