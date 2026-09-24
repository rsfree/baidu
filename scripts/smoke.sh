#!/usr/bin/env bash
# 端到端冒烟：**真起服务 + 真 HTTP + 假上游**（零真实上游请求）。
#
#   PY=<venv>/bin/python zsh scripts/smoke.sh [端口]   # 默认 8799（服务）/ 8800（假上游）
#
# 覆盖两条链路：主链（chat.baidu.com）+ 老接口兜底（image.baidu.com/aigc，LEGACY=fallback）。
# 为什么用假上游：真实上游有**风控成本**（失败请求同样计入评分）。冒烟要验证的是
# **我们这条链路**（鉴权、参数校验、翻译、错误信封、冷却窗、取件、BOS 转存、兜底切换），
# 而"上游能不能出活"由真实调用单独验证（已做过：变清晰 / 扩图 / 去水印）。
set -euo pipefail

PORT=${1:-8799}
MOCK_PORT=$((PORT + 1))
PY=${PY:-python3}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
BASE="http://127.0.0.1:${PORT}"
MEDIA_DIR="$(mktemp -d /tmp/baidu-smoke-media.XXXXXX)"

cleanup() {
  [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
  [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null || true
  rm -rf "$MEDIA_DIR"
}
trap cleanup EXIT

echo "== 起假上游 :$MOCK_PORT =="
"$PY" scripts/mock_upstream.py "$MOCK_PORT" > /tmp/baidu_mock.log 2>&1 &
MOCK_PID=$!

echo "== 起服务 :$PORT（主链+老接口全指向假上游；兜底=fallback；冷却窗 600s）=="
BAIDU_BASE_URL="http://127.0.0.1:${MOCK_PORT}" \
BAIDU_BOS_HOST="http://127.0.0.1:${MOCK_PORT}/bos" \
BAIDU_CDN_BASE="http://127.0.0.1:${MOCK_PORT}/img" \
BAIDU_LEGACY_BASE="http://127.0.0.1:${MOCK_PORT}" \
BAIDU_LEGACY=fallback BAIDU_LEGACY_POLL_INTERVAL=0.05 BAIDU_LEGACY_POLL_TRIES=20 \
BAIDU_COOKIE="BAIDUID=smoke" BAIDU_SESSION_TOKEN="tok-smoke" BAIDU_LID="lid-smoke" \
BAIDU_API_KEYS="" BAIDU_COOLDOWN=600 BAIDU_MIN_INTERVAL=0 BAIDU_MAX_WAIT=5 \
BAIDU_PER_MINUTE=100 \
BAIDU_MEDIA_DIR="$MEDIA_DIR" \
  "$PY" -m uvicorn "app.main:create_app" --factory --host 127.0.0.1 --port "$PORT" \
  > /tmp/baidu_smoke_app.log 2>&1 &
APP_PID=$!

for _ in $(seq 1 40); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.25
done
curl -sf "$BASE/healthz" >/dev/null || { echo "服务没起来；日志：/tmp/baidu_smoke_app.log"; exit 1; }
echo "   healthz ok"

"$PY" - "$BASE" "$MOCK_PORT" <<'PY'
import base64, json, sys, urllib.error, urllib.request

base, mock = sys.argv[1], sys.argv[2]
fail = 0

def post(path, payload, headers=None):
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.load(resp), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc), dict(exc.headers)

def get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.load(resp)

def mock_log():
    _, body = get(f"http://127.0.0.1:{mock}/mock/log")
    return body["entries"]

def conv_count(entries=None):
    return len([e for e in (entries or mock_log())
                if e["path"] == "/aichat/api/conversation"])

def legacy_creates(entries=None):
    return [e for e in (entries or mock_log()) if e["path"] == "/aigc/pccreate"]

def scenario(mode):
    req = urllib.request.Request(
        f"http://127.0.0.1:{mock}/mock/scenario",
        data=json.dumps({"mode": mode}).encode(),
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)["mode"]

def check(label, ok, extra=""):
    global fail
    print(("✅ " if ok else "❌ ") + label + (f"  {extra}" if extra else ""))
    fail += 0 if ok else 1

png = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000018000000100802000000aa17c1a0"
    "0000001a49444154789c63fcffff3f0326c8280a6360148500a3280c0083b80a1d"
    "0000000049454e44ae426082")
data_uri = "data:image/png;base64," + base64.b64encode(png).decode()

# 1) happy path（主链：bos 转存 + SSE + 结果取回）
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:clarity", "image": data_uri})
item = body.get("data", [{}])[0]
check("happy path 200 + b64 PNG + size + path=conversation",
      st == 200 and base64.b64decode(item.get("b64_json", "")) == png
      and item.get("size") == "24x16"
      and body["effective"]["path"] == "conversation",
      f"status={st} path={body.get('effective', {}).get('path')}")
check("上游确实收到转存链（sts + BOS + 登记）",
      any(e["path"] == "/aichat/api/file/sts" for e in mock_log())
      and any(e["path"].startswith("/bos/") for e in mock_log())
      and any(e["path"] == "/aichat/api/file/upload" for e in mock_log()))

first_conv = [e for e in mock_log() if e["path"] == "/aichat/api/conversation"][0]
check("conversation 三处一致（tool_type/sa/query）",
      first_conv["tool_type"] == "3" and first_conv["sa"] == "workspace_piccreate_3"
      and first_conv["query"] == "变清晰" and first_conv["has_chat_token"] is True,
      f'{first_conv.get("tool_type")}/{first_conv.get("sa")}')
sts_entry = [e for e in mock_log() if e["path"] == "/aichat/api/file/sts"][0]
check("上游收到 cookie（唯一凭据）与 tk（STS 链路）",
      first_conv["has_cookie"] and sts_entry["tk"],
      f'cookie={first_conv["has_cookie"]} tk={sts_entry["tk"]}')
bos_put = [e for e in mock_log() if e["method"] == "PUT"][0]
check("BOS 分片 content-type=image/png（真实类型）",
      bos_put["content_type"] == "image/png", bos_put["content_type"])
check("BOS 分片 body 是 PNG magic 的裸字节",
      bos_put["body_head"].startswith("89504e47"), bos_put["body_head"])

# 2) response_format=url → 落盘 + /files 取件
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:clarity", "image": data_uri, "response_format": "url"})
url = body["data"][0].get("url", "") if st == 200 else ""
ok_served = False
if url:
    with urllib.request.urlopen(base + url, timeout=10) as resp:
        ok_served = resp.status == 200 and resp.read().startswith(b"\x89PNG")
check("response_format=url → /files 取件", st == 200 and ok_served, f"url={url}")

# 3) dry_run：零上游请求
before = len(mock_log())
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:expand", "image": data_uri, "size": "4:3",
                    "dry_run": True})
after = len(mock_log())
check("dry_run 有 preview 且零上游请求",
      st == 200 and body["dry_run"] is True
      and "aichat/api/conversation" in body["preview"]["would_post_to"]
      and after == before)

# 4) 已移除的兜底编号（toolType 13/28~34 = 通用编辑器兜底，不是能力 ⇒ 不注册）
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:reimagine", "image": data_uri})
check("已移除的兜底编号 → 400 unknown_model（锁定「不注册」决定）",
      st == 400 and body["error"]["code"] == "unknown_model",
      f"status={st}")

# 4.5) 遮罩类能力缺 mask → 400（必填字段，触网之前就拒）
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:erase", "image": data_uri})
check("消除缺 mask → 400 missing_mask（含遮罩语义说明）",
      st == 400 and body["error"]["code"] == "missing_mask"
      and "白" in body["error"]["message"], f"status={st}")

# 5) 校验：拼错字段
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:clarity", "image": data_uri, "typo": 1})
check("不认识的字段 → 400",
      st == 400 and body["error"]["code"] == "unknown_field", f"status={st}")

# 6) tokenFail → 503（auth 属"部署问题"，**不触发兜底**——语义约定）
scenario("tokenfail")
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:clarity", "image": data_uri})
check("tokenFail → 503 auth（不兜底）",
      st == 503 and body["error"]["kind"] == "auth", f"status={st}")

# 7) items_null（无映射能力）→ 502（静默型失败必须显式）
scenario("items_null")
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:filter", "image": data_uri})
check("items:null（无映射）→ 502 显式失败",
      st == 502 and "未产出图片" in body["error"]["message"], f"status={st}")

# 8) ★ 老接口兜底：dewatermark 主链 items:null ⇒ 自动改走老接口（type=1）
creates_before = len(legacy_creates())
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:dewatermark", "image": data_uri})
entries = mock_log()
lg = legacy_creates(entries)
item = body.get("data", [{}])[0]
check("★ dewatermark 主链空图 ⇒ 兜底老接口 200（type=1，picInfo 直传）",
      st == 200 and base64.b64decode(item.get("b64_json", "")) == png
      and body["effective"]["path"] == "legacy"
      and body["upstream"]["legacy"]["type"] == "1"
      and len(lg) == creates_before + 1
      and lg[-1]["type"] == "1" and lg[-1]["picInfo_len"] > 0
      and any("改走老接口" in w for w in body["warnings"]),
      f"status={st} path={body.get('effective', {}).get('path')}")
check("★ 老接口走 base64 直传（picInfo 长度=输入图 b64 量级）",
      lg[-1]["picInfo_len"] > 100, f"picInfo_len={lg[-1]['picInfo_len']}")

# 9) 昆仑 → 429（filter 无映射；窗内第二次直接 429 且不打上游）
scenario("kunlun")
before = conv_count()
st1, body1, h1 = post("/v1/images/generations",
                      {"model": "wenxin:filter", "image": data_uri})
st2, body2, _ = post("/v1/images/generations",
                     {"model": "wenxin:filter", "image": data_uri})
after = conv_count()
check("昆仑 → 429（kind=risk_control）+ Retry-After=600",
      st1 == 429 and body1["error"]["kind"] == "risk_control"
      and h1.get("retry-after") == "600", f"status={st1} retry-after={h1.get('retry-after')}")
check("冷却窗内第二次直接 429 且**不打上游**",
      st2 == 429 and body2["error"]["code"] == "risk_control_cooldown"
      and after == before + 1, f"upstream_calls=+{after - before}")

# 10) ★ 冷却窗内、有映射的能力（clarity）⇒ 直接走老接口（不 429、不打主链）
before = conv_count()
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:clarity", "image": data_uri})
after = conv_count()
check("★ 冷却窗内 clarity ⇒ 直接走老接口 200（不 429、不打主链）",
      st == 200 and body["effective"]["path"] == "legacy"
      and after == before
      and any("冷却窗" in w for w in body["warnings"]),
      f"status={st} path={body.get('effective', {}).get('path')} conv+{after - before}")

# 11) ★ 遮罩类：消除（requires_mask ⇒ 直达老接口，picInfo2 直传）
before_conv, before_c, before_n = conv_count(), len(legacy_creates()), len(mock_log())
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:erase", "image": data_uri, "mask": data_uri})
lg = legacy_creates()
check("★ 消除 200（type=8 + picInfo2 直传，不打主链/不转存）",
      st == 200 and body["effective"]["path"] == "legacy"
      and lg[-1]["type"] == "8" and lg[-1]["picInfo2_len"] > 100
      and len(lg) == before_c + 1 and conv_count() == before_conv
      and not any(e["path"] == "/aichat/api/file/sts" for e in mock_log()[before_n:]),
      f"status={st} type={lg[-1]['type']} mask_len={lg[-1]['picInfo2_len']}")

# 12) ★ 遮罩类：局部替换（picInfo2 + prompt→text）
before_c = len(legacy_creates())
st, body, _ = post("/v1/images/generations",
                   {"model": "wenxin:replace", "image": data_uri, "mask": data_uri,
                    "prompt": "一只橘猫"})
lg = legacy_creates()
check("★ 局部替换 200（type=5 + mask + prompt→text 透传）",
      st == 200 and body["effective"]["path"] == "legacy"
      and lg[-1]["type"] == "5" and lg[-1]["picInfo2_len"] > 100
      and lg[-1]["text"] == "一只橘猫" and len(lg) == before_c + 1,
      f"status={st} type={lg[-1]['type']} text={lg[-1]['text']!r}")

# 13) ★ legacy-only：AI重绘 / 相似图（create_level 档位）
before_c = len(legacy_creates())
st1, body1, _ = post("/v1/images/generations",
                     {"model": "wenxin:redraw", "image": data_uri})
st2, body2, _ = post("/v1/images/generations",
                     {"model": "wenxin:similar", "image": data_uri})
lg = legacy_creates()
check("★ AI重绘/相似图 200（type=6/7 + create_level=2/5，legacy-only）",
      st1 == 200 and st2 == 200
      and (lg[-2]["type"], lg[-2]["create_level"]) == ("6", "2")
      and (lg[-1]["type"], lg[-1]["create_level"]) == ("7", "5")
      and len(lg) == before_c + 2
      and any("legacy-only" in w for w in body1["warnings"]),
      f"status={st1}/{st2} lg={[e['type'] for e in lg[-2:]]}")

print()
print("冒烟结果：" + ("全部通过" if fail == 0 else f"失败 {fail} 项"))
sys.exit(0 if fail == 0 else 1)
PY

echo "== 收尾 =="
