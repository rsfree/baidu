# baidu-service

**文心助手**（`wenxin.baidu.com` / 后端 `chat.baidu.com`）图片编辑的**同步出口**：
19 项能力、图进图出、一条 `POST` 拿到结果；`GET /llms.txt` 给 LLM 的自述说明书。工程骨架参考 `../textin` / `../jimeng`。

```bash
# 1) 铸造匿名身份（免登录、零生成请求；在 reverse-proxy 仓）
python wenxin/tools/mint_identity.py        # 产物 wenxin/var/identity.json 的 `cookie` 字段

# 2) 起服务
cp .env.example .env                        # 把上一步的 cookie 填进 BAIDU_COOKIE
pip install -r requirements.txt
python -m app.main                          # 或 gunicorn -c gunicorn_conf.py "app.main:create_app()"

# 3) 调用（默认 b64_json 内联返回）
curl -s localhost:8700/v1/images/generations -H 'content-type: application/json' -d '{
  "model": "wenxin:clarity",
  "image": "data:image/png;base64,…"
}' | jq '.data[0].size, .upstream.text'
# → "5472x3072"  "好的，根据你的需求，已经完成了 变清晰 操作。"

# 4) 干跑（零上游请求，预览完整出站体）
curl -s localhost:8700/v1/images/generations -H 'content-type: application/json' \
  -H 'X-Avm-Dry-Run: 1' -d '{"model":"wenxin:expand","image":"https://…","size":"4:3"}' | jq .preview
```

**对外契约全文**：[`docs/INTERFACE.md`](docs/INTERFACE.md)（冻结草案）｜
**上游取证与限流依据**：[`docs/UPSTREAM.md`](docs/UPSTREAM.md)。

---

## 0. 我要做什么 → 看哪个文件

| 我想… | 看这里 |
|---|---|
| 接这个服务 | `docs/INTERFACE.md` |
| 改「谁能做什么 / 门禁」 | `app/models.py`（唯一能力注册表） |
| 改上游请求构造 / SSE 解析 / 签名 | `app/upstream/baidu/capabilities.py`（纯函数，测试主战场） |
| 改上游调用 / 凭据现取 / BOS 转存 / 出口池 | `app/upstream/baidu/client.py` |
| 改**老接口兜底**（开关 / 映射 / 轮询） | `app/config.py`（`LEGACY*`）+ `client.py::legacy_process` + `app/models.py`（`legacy_type`） |
| 改响应形状 / 校验 / 闸门编排 | `app/service.py`（响应唯一出口） |
| 改路由 / 鉴权 / 运维面 | `app/main.py` |
| 改速率闸门 / 冷却窗 | `app/gate.py` |
| 改配置 | `app/config.py`（**每个旋钮都必须有人读**，有门禁） |
| 跑零成本自检 | `python scripts/probe.py`（shapes + loop；live 需 `--live`） |
| 老接口通路的线上核验 | `python scripts/live_legacy.py --image <本地图>`（经服务；真实调用、免费、别连打） |
| 端到端冒烟（真起服务 + 假上游） | `PY=<venv>/bin/python zsh scripts/smoke.sh` |

---

## 1. 三句话讲清上游

1. **一条同步 SSE 接口**打所有能力（`/aichat/api/conversation`）：能力由
   `toolType` + `sa` + 中文能力名三处同时决定；**没有任务 id、没有轮询通道**。
2. **cookie 是唯一硬凭据**（免登录 ≠ 免 cookie）；`token/lid` 现取自首页 HTML；
   匿名身份可**纯 HTTP 铸造**（见 `docs/UPSTREAM.md §3`）。
3. 门禁 = **出口维度的风险评分（昆仑）**：降速不触发、命中即冷却；
   换线只认**真出口**（`BAIDU_PROXY_POOL`），伪造头部未被证明有效。

## 2. 本服务的骨架（与兄弟服务同源）

```
app/config.py           配置（BAIDU_* 前缀；每个旋钮有读者）
app/errors.py           错误分类：risk_control→429(不可重试) / auth→503 / param→400 / timeout→504 / upstream→502
app/gate.py             速率闸门（串行+最小间隔+滑窗）+ 昆仑冷却窗（进程内 ⇒ 单 worker）
app/media.py            输入嗅探（magic bytes）/ 三形态归一 / 图像标准化 / 落盘 / 尺寸
app/models.py           能力注册表（19 项 + 6 项门禁 + 刻意缺席）
app/service.py          校验 → 门禁 → 输入准备 → 调用 → 装配（响应唯一出口）
app/main.py             HTTP 层（错误信封 / 鉴权 / 运维面 / /files）
app/upstream/baidu/     翻译层（纯函数）+ HTTP 客户端（凭据/BOS/SSE/出口池）
```

**两条纪律**（从 reverse-proxy 项目继承）：

1. **不制造假能力**：未取证的能力登记但默认不可用（503+开启方式）；发现端点只列可用的；
2. **不制造假配置/假测试**：没人读的旋钮要删（`tests/test_wiring.py` 门禁守着）；
   「字段传下去了」≠「字段用上了」；测试全部零真实上游请求（socket 级门禁）。

## 3. 测试与自检

```bash
<venv>/bin/python -m pytest -q --basetemp=/tmp/baidu-pytest   # 119 例，零出网
<venv>/bin/python -m ruff check app tests scripts
<venv>/bin/python scripts/probe.py            # shapes 19 条 + loop 19 条（假上游，零触网）
PY=<venv>/bin/python zsh scripts/smoke.sh     # 真起服务+真 HTTP+假上游，22 项
<venv>/bin/python scripts/probe.py --live --cap wenxin:clarity --image <url> \
    --cookie-file ../reverse-proxy/wenxin/var/identity.json    # ⚠️ 真实调用（免费，别连打）
<venv>/bin/python scripts/live_legacy.py --image /tmp/photo.jpg \
    --base http://127.0.0.1:8700              # ⚠️ 老接口通路真实核验（免费，别连打；--dry 可零成本预演）
```

## 4. 部署

```bash
docker compose up -d          # 端口 8700；MEDIA_DIR 卷；健康检查打 /healthz
```

- ⚠️ **单 worker 是架构决定**：速率闸门与冷却窗是进程内状态（`gunicorn_conf.py` 有详注）；
- 反代时 **`/stats` 必须挡住**；`/readyz` 给出 cookie/凭据来源/闸门状态（无凭据原文）；
- 上游风控命中后：在真实浏览器打开文心页面过一次验证框，或等冷却窗过期。
