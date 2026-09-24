# 上游取证与实现说明（文心助手 · chat.baidu.com）

> **唯一研究真源**在另仓：`reverse-proxy/docs/upstream/wenxin-image-edit-api.md`（实证版契约，
> 含完整逆向过程与实验记录）与 `reverse-proxy/wenxin/probe/`（探针）。
> 本文只留**本服务实现所依赖的结论**与**工程取舍**，不复制研究过程。

---

## 0. 一句话

文心助手「图片编辑」= **一条同步 SSE 接口**（`POST /aichat/api/conversation`）+
**Cookie（免登录也需要）** + **body 内自算 `chat_token`**；
门禁是**出口维度的风险评分挑战**（昆仑），不是计数器频控。

---

## 1. 请求骨架（本服务逐字复刻）

端点 `POST https://chat.baidu.com/aichat/api/conversation`（`Origin/Referer` 指 wenxin.baidu.com）。

**能力由三处同时决定**（`app/upstream/baidu/capabilities.build_body` 从同一来源派生）：

| 位置 | 取值 |
|---|---|
| `searchInfo.mcpInfo.ext.type` | toolType 字符串（如 `"3"`） |
| `searchInfo.sa` / 顶层 `sa` | `workspace_piccreate_<toolType>` |
| `query[1].data.text.query` | 能力中文名（如 `变清晰`）——**必须与 `chat_token` 的 md5 完全一致** |

> ⚠️ **两种请求形状**（2026-09-24 实测）：以上是 **workspace 形状**（默认，覆盖绝大多数能力）。
> 少数工具在站点 UI 里走**工具入口形状**：`searchInfo.sa = searchbox_image` +
> ⚠️ 另有**专属 sa 覆写**：换风格实测 `sa=workspace_piccreate_hfg`（不是 `workspace_piccreate_14`），
> 且 `ext` 里带 `style`(id)/`text`(标签)、`image_source=1`，`query[TEXT]` 为**风格标签** ——
> 见 §7 的攻克记录。
>
> `searchInfo.enter_type = pic_picfunc_<N>` + **无 `mcpInfo`** + `query[TEXT].query = 自然语言指令`
> （UI 实测入口码：背景替换 11 / 风格转换 14 / 局部替换 5 / 涂抹消除 8 / 相似图 7）。
> 本服务对 `restyle`/`bgreplace` 已按该形状构造（`Capability.entry_type`），但**实测这两条
> 仍不出图**（编辑器/agent 形态）⇒ 门禁；详见 §7。

`chat_token = btoa(<8位session token>|<md5(query)>|<ts_ms>|<lid>)-<lid>-3`：

- `lid` 必须是**页面级 searchframeLid**（用会话 `ori_lid` 会稳定吃 `tokenFail`，
  而报错文案只说「出了点小问题」，极易误判成风控）；
- `session token` 每次加载页面都会变、实测存活 ≥10 分钟 ⇒ **不手工填，现取**。

## 2. 响应骨架（SSE）

```
帧序：basedata → waiting-resp → markdown-yiyan（文案）→ image-generate（图）→ generate-complete → endTurn
```

- 结果图：`generator.component == "image-generate"` 的 `data.items[{originUrl,previewUrl,width,height}]`；
- 🔴 `items` 会被上游**显式给成 `null`**（此时可能只给一个 `picEditBaseUrl` 的 App 深链）
  ⇒ 消费侧必须写 `data.get("items") or []`；
- 🔴 **首帧顶层 `chatHitKunlun`**（约 0.1s 可知）= 风控信号；命中即断连、**不重试**。

## 3. 凭据模型（本服务的关键结论）

| 凭据 | 必需性 | 说明 |
|---|---|---|
| `BAIDU_COOKIE` | **必需**（唯一硬条件） | 免登录 ≠ 免 cookie：去掉 cookie 立刻「服务繁忙」无图（A/B 实证） |
| `token` / `lid` | 可选 | 首页 HTML 内嵌（`aiTabFrameBaseData`），**现取 + TTL 540s 缓存** |
| `BAIDU_SESSION_TOKEN` / `BAIDU_LID` | 可选 | 只在排障/上游改版时钉值；显式配置时**不自动现取、失败不重试** |

**cookie 从哪来**（免登录、零生成请求）：

```bash
python wenxin/tools/mint_identity.py            # 在 reverse-proxy 仓；产物 wenxin/var/identity.json
# 该文件 {"cookie": "BAIDUID=…; BIDUPSID=…", …} —— 把 cookie 字段填进 BAIDU_COOKIE
```

- 实测（2026-09-24）：**纯 HTTP 铸造的 7 项 cookie 即可跑通完整流程**（真实出图验收）；
  不需要浏览器、不需要登录；curl_cffi/Playwright 档保留为兜底。

## 4. 输入图：三级策略

| `BAIDU_UPLOAD_MODE` | 行为 | 建议 |
|---|---|---|
| `bos`（**默认**） | 恒转存：代取 → 标准化 → BOS 四段式上传 → 登记 → CDN URL | 生产推荐（多花 1~2s 换稳定） |
| `direct` / `auto` | 外链直传；非 URL 输入自动降级为转存 | 图小、MIME 标准、域名主流时可用 |

- 上游**只认自己抓得到的 URL**；外链直传失败的根因是「百度服务端抓取失败」
  （体积/非标 MIME 两大诱因），不是"外链不支持"；
- BOS 链（`app/upstream/baidu/client.upload_image`，纯 HTTP、免登录、不计费）：
  `GET /aichat/api/file/sts` → 初始化分片 → PUT → 合并 → `POST /aichat/api/file/upload`，
  签名 = **BCE v1**（`bce-auth-v1/...`）；
- 🔴 `jpg` 必须映射 `image/jpeg`（写成 `image/jpg` 是历史上的真实失败诱因）；
- 转存前标准化（`BAIDU_NORMALIZE=1`）：限最长边 ≤2048、≤2MB、统一标准 MIME；
  透明通道保留 PNG；**解不开的图原样上送并留 warning**（magic 已证明它是图片）。

## 5. 风控（昆仑）—— 本服务限流设计的依据

> 研究结论（截至 2026-09-24，含两轮复核）：

- **不是身份/cookie 维度**：热/新/无 cookie 三方负对照同刻同型命中；
  且「同身份换出口立即恢复」⇒ 判定不含身份维度。
- **不是计数器频控**：45 次连打全成功已证否；受控诱导 30 次（24 次/分）也未触发。
- **是出口（IP）维度的风险评分 → 验证挑战**：
  - 换到**未滥用**出口立即恢复（实证）；
  - **滥用一轮会把整片出口/池一起覆盖**（"换 IP 无效"的观感由此而来）；
  - 「客户端 TLS/JS 指纹」轴**未被单独隔离**（无正面证据，也未证伪）。
- 首帧 `chatHitKunlun` 命中 ⇒ 本服务**立即断连 + 开冷却窗（默认 600s，窗内零上游请求）**。

**因此本服务的策略**（进程内闸门，单 worker）：

1. **降速不触发**：串行 + 最小间隔 4s + 每分钟 8 次（`BAIDU_MIN_INTERVAL/PER_MINUTE`）；
2. **命中即熔断**：冷却窗（`BAIDU_COOLDOWN`）+ `/readyz` 可见；
3. **换线靠真出口**：`BAIDU_PROXY_POOL`（逗号分隔；每请求新建连接轮换）——
   这是**唯一被证明有效**的换线手段；**伪造 XFF/头部未被证明有效**（未做实证隔离实验）。
   ⚠️ 用它跑量会把整池出口一起覆盖，由部署方决定。

## 6. 结果与水印

- 结果 URL 尾部带 `?x-bce-process=image/watermark,...`（AI 水印处理参数）；
  **默认剥离**（`BAIDU_STRIP_WATERMARK=1`）⇒ 下载即无水印原图；
- 本服务把结果图**取回并转存**（上游 URL 带签名、有有效期）——调用方拿到稳定副本。

## 6.5 老接口（image.baidu.com/aigc）—— 兜底链路的取证（2026-09-24）

**结论：活着、匿名可用、秒级出图；全量类型矩阵已实测。**（旧研究里"已失效"的说法是误判
—— 那次的「网络异常」实为**服务端抓不到输入 URL**，与 `picInfo` base64 直传无关。）

链路（复刻自 `MeUtils/meutils/apis/baidu/bdaitpzs.py`）：

```
POST {LEGACY_BASE}/aigc/pccreate     表单：type=<tt> / picInfo=<纯 base64> / picInfo2=<遮罩> / …
  → {"status":0,"pcEditTaskid":"daq25sufqu04ejcm9j00","resType":0,"token":"…"}
GET  {LEGACY_BASE}/aigc/pcquery?taskId=…
  → {"isGenerate":true,"progress":100,…,"picArr":[{"src":"data:image/jpeg;base64,…"}]}
```

- **凭据**：同款百度系 cookie（本服务的 `BAIDU_COOKIE` 直接可用；实测匿名即可）；
- **输入**：`picInfo` 直传 base64（无需先上传、绕开"抓不到 URL"类失败）；`picInfo2` = 遮罩位（见下）；
- **type 全量矩阵（每型 ≥1 次真实出图；经本服务端到端 3.1–3.2s，比主链 6.0s 还快
  —— 免 BOS 上传与 chat_token 链路）**：

  | type | 能力 | 实测结果 | 附加参数 |
  |---|---|---|---|
  | 3 | 变清晰 | ✅ 3600×2400 | — |
  | 1 | 去水印 | ✅ 3.1s / 900×600 | — |
  | 9 | 抠图 | ✅ 4.1s / 900×600 | — |
  | 15 | 提线稿 | ✅ 10.8s / 3600×2400 | — |
  | 4 | 扩图 | ✅ 1368×1024 —— **慢，首次轮询停在 `progress:99`，长轮询 11.6s 才出图** | `ext_ratio` |
  | 8 | 消除 | ✅ 7.3s / 900×600（经服务） | `picInfo2` |
  | 5 | 局部替换 | ✅ 5.5s / 900×600（经服务，`prompt`→`text`） | `picInfo2` + `text` |
  | 6 | AI重绘 | ✅ 7.2s / 1440×960（经服务） | `create_level=2` |
  | 7 | 相似图 | ✅ 10.7s / 900×600（经服务） | `create_level=5` |
  | 12 / 14 | 背景替换 / 风格 | ⛔ **未实测**：需 `text`/`style` 语义字段，本入口面无对应字段 ⇒ 不接 | — |

- **遮罩语义（判决实验，2026-09-24）**：`picInfo2` = **黑底 + 白框**（**白色=要处理的区域**）。
  合成目标（红块 60000 px）对照：**黑底白框 → 残留 0**（完全消除）；白底黑框 → 残留 59972
  （未处理）。⇒ **反约定无效**，别用白底黑框。
- **"慢"≠失败**：`type=4` 的 `progress:99` 是假终点 ⇒ 轮询预算按 ≥60s 配
  （`LEGACY_POLL_TRIES × LEGACY_POLL_INTERVAL`，默认 40 × 1.5s）。
- **与主链的关系**：不同 host / 不同产品 ⇒ 昆仑挑战（只卡 chat.baidu.com 的 conversation）
  **不覆盖它** —— 这是「风控兜底」成立的结构性依据。⚠️ 但「主链被标记时老接口仍通」
  这一格**尚未在实时被标记场景下对照过**（手上没有被标记出口）；上线
  `BAIDU_LEGACY=fallback` 后一旦发生风控会自动切换并留痕（`effective.path=legacy`）。
- 老接口自身的风控/限流**未观测到**（本地调用量小）—— 按同样的纪律使用（别连打）。

---

## 7. 未取证 / 刻意不做（别假装知道）

**主链侧**：

- 去水印（toolType 2）：2026-09-12 起复测为「跳交互式编辑器」形态（`items:null` + 深链），
  未复现出图 ⇒ 默认门禁（**老接口 type=1 已补位**）；
- ✅ **换风格（toolType 14）2026-09-24 攻克**（从站点真实报文逆出，已上线）：
  `sa=workspace_piccreate_hfg`（**专属字母码**）+ `enter_type=pic_picfunc_14` +
  `mcpInfo.ext{type:14, image, image_source:**1**, channel:edit, style:<id>, text:<标签>}` +
  `query=[IMAGE, TEXT(<标签>)]` ⇒ **出图**（真跑：宫崎骏风 10.3s / 油画风 10.7s，900×600）。
  风格表 = `GET image.baidu.com/aigc/extinfo` 的 `style[]`（**17 项 id↔标签**，服务端下发）。
  对照（三条都试过、都不出图）：旧 workspace 形状（`sa=workspace_piccreate_14`、无 style/text、
  `image_source=0`）只回 `picEditBaseUrl`（跳编辑器）；工具入口形状（无 mcpInfo）只回对话文字；
  老接口 `type=14` 九次尝试恒 status 5。
- ✅ **背景替换（toolType 11）2026-09-24 攻克**（经老接口）：主链形态不可用（workspace 回「未识别到主体」、
  工具入口形状只回对话文字），但**老接口 `type=12` + `picInfo2`(遮罩) + `text` 实测出图**
  （全白遮罩 92KB / 上半框遮罩 76KB；经服务 4.1s / 3.8s，900x600）。
  🔴 **此前 7 次失败全因没带遮罩** —— 站点该工具面板正是「识别背景 / 涂抹要替换区域 + 输入替换的内容」；
  遮罩语义与消除/局部替换一致（黑底白框，白=要处理）。
  两能力的**站点入口码实测**：背景替换 `pic_picfunc_11`、风格转换 `pic_picfunc_14`、
  局部替换 `_5`、涂抹消除 `_8`、相似图 `_7`；
- 局部替换 / 消除：主链需**涂抹遮罩**，遮罩参数未逆向 ⇒ 主链形态默认门禁
  （**老接口遮罩语义已判决并生效**，见 §6.5）；
- **toolType 13 / 28~34：不是能力**（2026-09-24 实测）—— 回 `image-generate` + `items:null` +
  `picEditBaseUrl`，且链接里 `toolType=0&word=` **为空** ⇒ 上游对「未知 toolType」的**通用编辑器兜底跳转**。
  §3 曾把这记为「相关图编 / 出图 ✅」，**是对兜底响应的误读，已撤**；该条目**不注册**（见 `NOT_REGISTERED`）；
- 生成文案（toolType 16）：不产图，刻意不暴露；
- 冷却时长（命中后多久自动解除）：**未定论**；本服务保守取 600s。

**老接口侧**：

- 🔴 **教训（2026-09-24 当日自我纠正）**：`type=12`（背景替换）曾被判「已死」——
  七次尝试（两种端点 × 两种输入形态）create 恒被拒。**该结论是错的**：那七次**都没带遮罩**，
  而遮罩是该 type 的**必需参数**（站点 UI 面板正是「涂抹要替换区域 + 输入替换的内容」）。
  补上 `picInfo2` 后**立刻出图**（全白遮罩 92KB、上半框 76KB；经服务 4.1s / 3.8s）。
  ⇒ **判死前必须把该 type 的必需参数集试全**：`参数不全的失败 ≠ 编号不存在`；
  凡"涂抹/局部"类工具，先怀疑缺遮罩。
- 风格(14)：**2026-09-24 判死（有对照）** —— 九次尝试（5 个 `style` 值 × 两种端点 × 两种输入形态），
  create 受理但任务恒 `status:5`；**同期 `type=3` 对照 4/4 出图**（对照有效）。
  ⚠️ 但同样的能力在**主链**用专属形状可用（见上：`workspace_piccreate_hfg` + `ext.style/text`）—— 
  「老接口编号死了」≠「能力做不了」，两条通路要分别下结论。
- ⚠️ 老接口 `type=6/7` 与新接口 toolType `6/7` **不是一回事**：新接口 6/7 返回「服务繁忙」
  （编号不存在），老接口 6/7 = AI重绘/相似图 —— **两套编号体系别混**。
