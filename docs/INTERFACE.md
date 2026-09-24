# baidu-service 对外契约（冻结草案 · 2026-09-24）

> 单端点、**同步直给**的图片编辑出口。上游 = 文心助手（`chat.baidu.com`）的
> `POST /aichat/api/conversation`（SSE 长连接直到出图，**没有任务 id、没有轮询通道**）；
> 风控期的兜底与部分能力的补位走**老接口**（`image.baidu.com/aigc`，见 §8）。
> 上游侧事实与取证见 [`UPSTREAM.md`](UPSTREAM.md)。

---

## 0. 端点总表

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| `POST` | `/v1/images/generations` | ✅ Key | **执行端点**（图进图出；唯一语义入口） |
| `GET` | `/v1/models` | 🔓 免 | 能力清单（OpenAI 四键形态；**只列本部署可用**） |
| `GET` | `/capabilities` | ✅ Key | 全量能力 + 未取证项的**原因与开启方式** + 老接口映射 + 刻意缺席 |
| `GET` | `/healthz` `/readyz` | 🔓 免 | 存活 / 就绪（运维面，无凭据原文） |
| `GET` | `/stats` | ✅ Key | 闸门快照 + 最近 50 条 span |
| `GET` | `/files/{name}` | 🔓 免 | `response_format=url` 的取件（内容寻址文件名） |

> ⚠️ `/stats` 上反代时**必须挡住**（含闸门状态与调用统计）。

---

## 1. 鉴权

- `Authorization: Bearer <Key>`；`BAIDU_API_KEYS` **留空 = 鉴权整体关闭**（仅限内网，启动 WARNING）。
- 比对是**恒定时间**的静态白名单；只支持逗号分隔多 Key。
- 🔑 上游凭据（`BAIDU_COOKIE`）是**服务侧的部署凭据**，与调用方 Key 无关；
  未配 cookie 时执行端点回 **503 `upstream_not_configured`**（含铸造指引）。
  老接口与主链**共用这份 cookie**（实测两条链都不需要更多凭据）。

---

## 2. 请求（`POST /v1/images/generations`）

```json
{
  "model": "wenxin:erase",
  "image": "data:image/png;base64,…",
  "mask": "data:image/png;base64,…",
  "response_format": "b64_json",
  "size": "4:3",
  "prompt": "（只有局部替换透传，见下）",
  "dry_run": false
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `model` | ✅ | `wenxin:<name>`（20 项，见 §7）；未注册名 ⇒ 400 `unknown_model` |
| `image` | ✅ | **三种形态**：data URI / http(s) URL / 裸 base64。类型**按真实字节嗅探**（png/jpeg/webp/bmp；**GIF 不收**） |
| `mask` | ❌ | **只有「消除 / 局部替换」消费**（这两个能力**必填**，缺 ⇒ 400 `missing_mask`）：**黑底 + 白框**的图片，**白色标记「要处理的区域」**；形态同 `image`。其余能力给了 mask ⇒ 忽略 + `warnings[]` 明示 |
| `response_format` | ❌ | `b64_json`（默认）/ `url`（落盘 + `/files/{name}`） |
| `size` | ❌ | **只有「扩图」消费**：解释为 `image_expand` 比例（`"4:3"`；也接受 `"WxH"` 并化简）。其余能力**忽略 + `warnings[]` 明示** |
| `prompt` | ❌ | 主链 workspace 形状要求 query 严格等于能力名 ⇒ **默认忽略（`warnings[]` 明示）**；**`wenxin:replace` 上映射为老接口 `text`**；**`wenxin:restyle` / `wenxin:bgreplace` 上是「指令文本」**（工具入口形状 `sa=searchbox_image` + `enter_type`，缺 ⇒ 400 `missing_instruction`；干跑放行）；`BAIDU_PROMPT_MODE=prepend` 才在主链拼接（**未经验证**） |
| `dry_run` | ❌ | 也可用请求头 `X-Avm-Dry-Run: 1`；**零上游请求**，返回完整出站预览（`chat_token` 已打码） |

**键集纪律**（与兄弟服务同源）：

- **不认识的字段 ⇒ 400 `unknown_field`**（拼写错误不许被静默吞掉）；
- **认识但做不到的字段**（`n`/`quality`/`style`/`seed`/`negative_prompt`/`watermark`/`user`/
  `stream`/`background`/`output_format`/`moderation`/`sequential_image_generation*`/`extra_body`）
  ⇒ 不报错，进响应 `unsupported[]` —— 静默丢弃是大忌。

---

## 3. 响应（200）

```json
{
  "created": 1789000000,
  "model": "wenxin:clarity",
  "dry_run": false,
  "data": [
    {"b64_json": "…", "mime": "image/jpeg", "size": "5472x3072"}
  ],
  "usage": {"generated_images": 1},
  "requested": {"model": "wenxin:clarity", "response_format": "b64_json",
                "image": {"form": "data-uri", "length": 123456}},
  "effective": {"tool_type": "3", "query": "变清晰",
                "upload_mode": "bos", "strip_watermark": true, "proxy": "direct",
                "legacy": {"mode": "fallback", "type": "3", "enabled": true},
                "rank": 42, "image_url": "https://aisearch.cdn.bcebos.com/pic_create/…",
                "upload": {"path": "bos", "input": {"kind": "png", "bytes": 142245}},
                "cred_source": "fetched"},
  "warnings": ["结果 URL 的水印处理参数已剥离（BAIDU_STRIP_WATERMARK=1）"],
  "unsupported": [],
  "upstream": {"tool_type": "3", "frames": 4, "ratio": "3-2",
               "text": "好的，根据你的需求，已经完成了 变清晰 操作。",
               "cred_source": "fetched", "refreshed": false}
}
```

- `data[].size` 是**结果图的真实像素尺寸**（从字节解析；读不到就不给该字段）；
- `data[].mime` 按**真实字节**嗅探（上游产出恒 JPEG，但不写死）；
- `effective.path` / `upstream.path` 标明**走的是哪条链路**：`conversation`（主链）或
  `legacy`（老接口，见 §8）；
- `warnings[]` 说明一切"降级/忽略/剥离"（尺寸被忽略、prompt 未透传、mask 被忽略、
  token 刷新、水印剥离、兜底/直通切换…）；
- `requested / effective / warnings / unsupported / upstream` 是本服务在标准之外的**加性扩展**，
  不改变标准字段语义。

### `dry_run: true` 时

`data` 为空、`usage` 为 null，额外给 `preview`（老接口可用时 `preview.legacy` 给出
`would_post_to / type / mode / create_level / mask / text_from_prompt`）：

```json
{"preview": {"would_post_to": "https://chat.baidu.com/aichat/api/conversation",
             "method": "POST",
             "body": {…完整出站体，chat_token 已打码…},
             "upload": {"path": "bos", "would_upload": true}}}
```

---

## 4. 错误信封

```json
{"error": {"code": "risk_control", "kind": "risk_control", "upstream": true,
           "message": "文心上游返回风控验证挑战（kunlun_popup），已立即断开、不重试；…",
           "frames": 1}}
```

| 场景 | HTTP | `code` / `kind` | 可重试 | 备注 |
|---|---|---|---|---|
| 参数/键集/模型错 | 400 | 各种本地 code | ❌ | 我们自己的报文，**不脱敏** |
| 遮罩类能力缺 `mask` | 400 | `missing_mask` | ❌ | 含遮罩语义说明（白=要处理），触网前即拒 |
| 排队超上限（本地速率闸门） | 429 | `rate_limited` / `capacity` | ✅（退避） | 带 `Retry-After` |
| **命中昆仑风控** | 429 | `risk_control` | ❌ **别重试** | 带 `Retry-After`= 冷却剩余；重试会延长标记 |
| 冷却窗内再请求（**无兜底通路**的能力） | 429 | `risk_control_cooldown` | ❌ | 有映射的能力窗内**改走老接口**，不 429 |
| 上游参数错（HTTP 400） | 400 | `param` | ❌ | 罕见，多为上游改版 |
| 上游超时 | 504 | `timeout` | ✅ | `BAIDU_TIMEOUT`（默认 180s） |
| 上游不可用 / 静默型失败 | 502 | `upstream` | ✅ | 「无图无 hint」也走这里；兜底失败时主因保留 + `error.legacy_failed` |
| 凭据问题（tokenFail / 首页取不到） | 503 | `auth` | ❌ | **部署问题**（cookie/改版），修完再打；**不触发兜底** |
| 未配 cookie | 503 | `upstream_not_configured` | ❌ | 含铸造指引 |
| 未取证能力（默认门禁） | 503 | `capability_not_verified` | ❌ | 含开启方式（`BAIDU_ALLOW_UNVERIFIED=1`；有映射的提示 `BAIDU_LEGACY`） |

> 🔴 **风控（429 `risk_control`）与限流是两回事**：前者是验证挑战，重试会加重标记；
> 正确处置是「人工在真实浏览器过一次验证框」或等冷却窗自然过期。

---

## 5. 闸门语义（dry_run 的穿透规则）

| 闸门 | 真跑 | dry_run | 说明 |
|---|---|---|---|
| 未取证能力（8 项） | 503 | ✅ 放行 | 开 `BAIDU_LEGACY` 后其中 5 项经老接口可用；另 3 项（`reimagine` / `restyle` / `bgreplace`）**无可用通路**（后两者 2026-09-24 复测为编辑器/agent 形态） |
| 工具入口形状缺指令（`restyle`/`bgreplace`） | 400 | ✅ 放行 | 真跑缺 `prompt` ⇒ `missing_instruction`；干跑按占位预览 |
| 遮罩类缺 `mask` | 400 | ✅ 放行 | 必填字段校验在触网前；干跑只做计划 |
| 未配 cookie | 503 | ✅ 放行 | 干跑不触网 |
| 昆仑冷却窗 | 429（无兜底通路时） | ✅ 放行 | 窗口是进程内状态，`/readyz` 可见 |
| 速率闸门（串行+最小间隔+每分钟上限） | 排队/429 | ✅ 放行 | dry_run 不占名额 |

---

## 6. 交付与取件

- 默认 `b64_json`（结果直接进响应体）；`response_format=url` 时落盘 `BAIDU_MEDIA_DIR`，
  以 `/files/{name}` 提供；**文件不设 TTL**，由部署方清理。
- 结果图是**服务自己取回并转存**的（上游 URL 带签名、有有效期）——调用方拿到的是稳定副本。
  老接口的结果（`picArr[0].src` 的 data URI）**走同一交付形态**。

---

## 7. 能力清单（20 项，全部 `wenxin:*`）

| 模型 | 上游能力 | 状态 |
|---|---|---|
| `wenxin:clarity` | 变清晰（4× 放大，上限 5472×3072） | ✅ |
| `wenxin:expand` | 扩图（消费 `size`） | ✅ |
| `wenxin:matting` / `wenxin:matting-pro` | 抠图 / 背景抠图（同能力域） | ✅ |
| `wenxin:bgreplace` | 背景替换 | ⛔ 未取证（**2026-09-24 复测已变编辑器/agent 形态**：workspace 形状回「未识别到主体」，工具入口形状只回对话文字；老接口 12 已死） |
| `wenxin:sketch` | 提线稿（最慢，20~25s） | ✅ |
| `wenxin:restyle` | 换风格 | ⛔ 未取证（**2026-09-24 复测**：workspace 形状只回 `picEditBaseUrl` 编辑器链接；工具入口形状只回风格分析与追问，两轮亦不出图；老接口 14 已死） |
| `wenxin:textreplace` | 文字替换 | ✅ |
| `wenxin:restore` / `wenxin:removeperson` / `wenxin:removetext` | 图片修复 / 去路人 / 去文字 | ✅ |
| `wenxin:filter` / `wenxin:beauty` / `wenxin:ps` | 滤镜 / 美颜 / P图 | ✅ |
| `wenxin:dewatermark` | 去水印 | ⛔ 主链未取证（复测为编辑器形态）；**开 `BAIDU_LEGACY` 后经老接口可用**（type=1） |
| `wenxin:erase` | 消除（**需 `mask`**） | ⛔ 主链遮罩参数未逆向；**开 `BAIDU_LEGACY` 后经老接口可用**（type=8，遮罩实测生效） |
| `wenxin:replace` | 局部替换（**需 `mask`**，可选 `prompt`） | ⛔ 同上；**开 `BAIDU_LEGACY` 后经老接口可用**（type=5，`prompt`→`text`） |
| `wenxin:redraw` / `wenxin:similar` | AI重绘 / 相似图 | ⛔ **legacy-only**（主链形态未坐实）：**开 `BAIDU_LEGACY` 后经老接口可用**（type=6/7 + 档位） |
| `wenxin:reimagine` | 相关图编 | ⛔ 未取证（主链语义待坐实；无老接口映射） |

**刻意缺席**：生成文案（不产图）；GIF 输入（未取证）；多图输入（上游单图语义）；
**假异步**（上游同步 SSE，不做假的 job 轮询）；老接口的「风格(14+style) / 背景替换(12+text)」
（需要本入口面没有的语义字段，不做无参透传的假能力）。见 `GET /capabilities` 的
`not_registered` / `deliberately_absent`。

---

## 8. 老接口（`BAIDU_LEGACY`，2026-09-24 新增）

主链（chat.baidu.com）与老接口（`image.baidu.com/aigc`，百度AI图片助手）是
**两条独立的生成链路**（不同 host、不同产品、不同风控体系）。老接口实测**匿名可用、
秒级出图**，承担两个职责：①主链风控期的**兜底**；②主链做不了/未坐实能力的**补位**。

**架构约定：新接口为主、老接口兜底**（老接口不做常规主选路径）。

| `BAIDU_LEGACY` | 行为 |
|---|---|
| `off`（默认） | 纯主链；未取证能力全部被门禁挡住 |
| `fallback`（推荐部署值） | ①主链命中**风控 / 上游未产出** ⇒ 自动改走老接口；②**冷却窗内直接走老接口**（不 429）；③`requires_mask` / legacy-only 能力**直接走老接口**（主链本就不支持） |
| `prefer` | 直接走老接口（演练 / 主链故障期）；无映射的能力自动回主链并留 warning |

- **有映射的能力**（`/capabilities` 的 `legacy_type` 字段，均为实测出图）：
  `clarity`(3)、`expand`(4)、`matting`(9)、`sketch`(15)、`dewatermark`(1)、
  `erase`(8)、`replace`(5)、`redraw`(6)、`similar`(7)。
  其余能力**不因开启兜底而变可用**（不制造假能力）。
- **遮罩约定**（`erase`/`replace`）：`mask` → 老接口 `picInfo2`（**黑底 + 白框，白色=要处理的
  区域**；反约定实测完全无效）。`replace` 的 `prompt` → `text`。
  **判决实验**（合成红块）：黑底白框下目标残留 **0**（完全消除），白底黑框残留 59972（未处理）。
- **不触发兜底的错误**：`auth`（部署凭据问题；两条链同用一份 cookie）、`param`、`timeout`
  维持原有语义。**兜底也失败时保留主因**，详情挂进 `error.legacy_failed`（两段信息都不丢）。
- 响应（加性）：`effective.path` ∈ {`conversation`,`legacy`}；`effective.legacy` =
  `{mode, type, enabled}`；`upstream.legacy` =
  `{type, task_id, create_status, polls, elapsed_s, progress, has_mask, create_level, bytes_in}`；
  老接口直通时**不做 BOS 转存**（`picInfo` base64 直传）。
- **实测（经本服务完整链路，纯 HTTP、零浏览器）**：
  去水印 3.1s / 900x600、变清晰 3.2s / 3600x2400、**消除 7.3s / 900x600（残留 0）**、
  **局部替换 5.5s / 900x600（`prompt`→`text`）**、**AI重绘 7.2s / 1440x960**、
  **相似图 10.7s / 900x600**；扩图 4:3 约 11.6s（较慢）。
- ⚠️ `fallback` 模式下主链会**先尝试一次**（BOS 转存已发生）——要免掉这一步请用 `prefer`；
  `requires_mask` / legacy-only 能力**不先打主链**（直通老接口）。
