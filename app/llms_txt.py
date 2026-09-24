"""`/llms.txt` —— 给 LLM / Agent 读的服务说明书（llmstxt.org 约定）。

内容**从注册表派生**（能力表、字段约束、风格表、扩图比例、本部署可用性），
只有小节骨架是静态文案 —— 目的是**不让说明书漂移成假信息**（项目纪律：单一事实源）。
"""

from __future__ import annotations

from app import __version__
from app.config import Settings
from app.errors import UPSTREAM_KIND_STATUS
from app.models import CAPABILITIES, EXPAND_RATIOS, MODEL_RELEASED_AT, availability

#: 客户端需要处理的错误码（`test_llms_txt_codes_exist_in_source` 会**回源码反查**，
#: 保证这里不出现"文档里有、代码里没有"的幽灵码）。
_ERRORS: tuple[tuple[str, str, str], ...] = (
    ("unknown_model", "400", "模型名不在 `/v1/models` 里（含已撤销的 `wenxin:reimagine`）"),
    ("missing_model", "400", "请求体缺 `model`"),
    ("invalid_body", "400", "请求体不是 JSON 对象 / 键集不合法"),
    ("empty_input", "400", "`image` 为空"),
    ("url_input_unsupported", "400", "该部署未开 URL 输入（见 `/readyz` 的 `upload_mode`）"),
    ("missing_mask", "400", "该能力**必填 `mask`**（消除 / 局部替换 / 背景替换）"),
    ("invalid_mask", "400", "`mask` 形态不合法（支持 http(s) URL / data URI / base64）"),
    ("missing_style", "400", "该能力**必填 `style`**（换风格），错误体里带 17 项候选"),
    ("unknown_style", "400", "`style` 不在风格表里（错误体 `styles[]` 给候选）"),
    ("invalid_style", "400", "`style` 不是字符串"),
    ("invalid_size", "400", "`size` 不是字符串"),
    ("invalid_prompt", "400", "`prompt` 不是字符串"),
    ("invalid_response_format", "400", "`response_format` 只支持 `b64_json` / `url`"),
    ("unauthorized", "401", "受保护端点缺 Bearer 或 key 不对"),
    ("capability_not_verified", "503", "该能力**未取证**且未开 `BAIDU_ALLOW_UNVERIFIED`"
                                      "（`error.how_to_enable` 给开启方式）"),
    ("media_write_failed", "503", "`response_format=url` 落盘失败（磁盘/权限）"),
    ("internal", "500", "未预期异常（已脱敏；细节在服务端日志）"),
)

#: 上游错误（`error.kind`）的解释 —— **词表从 `UPSTREAM_KIND_STATUS` 派生**（见 render），
#: 这里只给「人话解释」；新增一个 kind 时即使忘了写解释，它也会照常出现在说明书里。
_UPSTREAM_KIND_NOTES: dict[str, str] = {
    "risk_control": "上游风控（本服务会进冷却窗，响应带 `retry-after`）",
    "auth": "上游凭据/会话失效（检查 `/readyz` 的 `cookie_configured` 与 `token_alive`）",
    "param": "上游认为参数不合法（多半是输入图/遮罩形态问题）",
    "timeout": "上游超时（可安全重试）",
    "upstream": "上游未出图或返回结构不符（`note` 给上游原话）",
}


def _inputs_of(cap) -> str:
    """该能力的**必填输入**（从注册表派生，不手抄）。"""
    need = []
    if cap.requires_mask:
        need.append("`mask`")
    if cap.needs_style:
        need.append("`style`")
    if cap.legacy_uses_prompt:
        need.append("`prompt`")
    if cap.supports_ratio:
        need.append("`size`")
    return " + ".join(need) if need else "`image`"


def _route_of(cap) -> str:
    if cap.legacy_only:
        return f"老接口 type={cap.legacy_type}"
    if cap.legacy_type and not cap.verified:
        return f"老接口 type={cap.legacy_type}（主链形态不可用）"
    if cap.legacy_type:
        return f"主链 toolType={cap.tool_type} + 老接口 type={cap.legacy_type}"
    return f"主链 toolType={cap.tool_type}"


def _availability_of(cap, *, allow: bool, legacy: bool) -> str:
    ok, reason = availability(cap, allow_unverified=allow, legacy=legacy)
    if ok:
        return "✅ 可调用"
    return f"⛔ {reason}"


def render(settings: Settings) -> str:
    """生成 `/llms.txt` 正文（纯函数：同一份 settings 永远得到同一份文本）。"""
    allow = bool(settings.ALLOW_UNVERIFIED)
    legacy = settings.LEGACY != "off"
    caps = sorted(CAPABILITIES)
    usable = [n for n in caps
              if availability(CAPABILITIES[n], allow_unverified=allow, legacy=legacy)[0]]

    out: list[str] = []
    add = out.append
    add("# baidu-service · 文心助手图片编辑 API")
    add("")
    add("> 把 `wenxin.baidu.com` / `chat.baidu.com`（文心助手「图片编辑工具」）包成"
        " **OpenAI 风格**的同步图片接口：**一条 POST、进图出图**。")
    add(f"> 对外模型名 `wenxin:*`；本服务注册 **{len(caps)} 项能力**，"
        f"本部署当前可调用 **{len(usable)} 项**（口径见「兜底与降级」）。")
    add("")
    add("## 怎么调")
    add("")
    add("```bash")
    add("curl -sS https://<host>/v1/images/generations \\")
    add("  -H \"Authorization: Bearer $BAIDU_API_KEY\" \\")
    add("  -H 'Content-Type: application/json' \\")
    add("  -d '{\"model\": \"wenxin:clarity\", \"image\": \"https://example.com/a.jpg\"}'")
    add("```")
    add("")
    add("- `image` 支持 **http(s) URL** 或 **data URI**（`data:image/png;base64,...`）；"
        "裸 base64 亦可（按 PNG 处理）。")
    add("- **想零成本试跑**：任何请求体加 `\"dry_run\": true`（或请求头 `X-Avm-Dry-Run: 1`）——"
        "只回**将要发出的上游请求计划**（凭据已打码），不触网、不消耗额度。")
    add("- 结果：`response_format=b64_json`（默认）在 `data[0].b64_json`；"
        "`url` 则由本服务落盘并给链接。")
    add("")
    add("## 鉴权")
    add("")
    add("- 免鉴权：`GET /healthz`、`GET /readyz`、`GET /v1/models`、`GET /llms.txt`；")
    add("- 其余端点（含 `POST /v1/images/generations`、`GET /capabilities`、`GET /stats`）"
        "需 `Authorization: Bearer <key>`；未配 key 的部署会拒绝挂载受保护面。")
    add("")
    add(f"## 能力清单（{len(caps)} 项，`model` 取值即 `wenxin:*`）")
    add("")
    add("| model | 名称 | 必填输入 | 上游通路 | 本部署 |")
    add("|---|---|---|---|---|")
    for name in caps:
        cap = CAPABILITIES[name]
        add(f"| `{name}` | {cap.title} | {_inputs_of(cap)} | {_route_of(cap)} | "
            f"{_availability_of(cap, allow=allow, legacy=legacy)} |")
    add("")
    add("## 参数")
    add("")
    add("| 字段 | 适用 | 说明 |")
    add("|---|---|---|")
    add("| `model` | 必填 | 见上表 |")
    add("| `image` | 必填 | 输入图（URL / data URI / base64） |")
    add("| `mask` | 消除 / 局部替换 / 背景替换 **必填** | **黑底白框**：白色 = 要处理的区域（黑=保留）；"
        "与 `image` **同尺寸**；形态同 `image` |")
    add("| `style` | 换风格 **必填** | 风格 **id** 或**中文标签**（下表 17 项）；"
        "错误体 `styles[]` 会回候选 |")
    add("| `prompt` | 局部替换 / 背景替换 | 替换内容（老接口 `text`）；其余能力忽略并在 `warnings[]` 明示 |")
    add("| `size` | 扩图 | 目标比例，见「扩图比例」；其余能力忽略并提示 |")
    add("| `response_format` | 可选 | `b64_json`（默认）/ `url` |")
    add("| `dry_run` | 可选 | 只回计划不触网 |")
    add("")
    restyle = CAPABILITIES.get("wenxin:restyle")
    if restyle and restyle.style_table:
        add("## 风格表（`style` 取值；id 与标签等价）")
        add("")
        add("| id | 标签 |")
        add("|---|---|")
        for sid, label in restyle.style_table:
            add(f"| `{sid}` | {label} |")
        add("")
    add("## 扩图比例")
    add("")
    add("- `size` 取：" + "、".join(f"`{r}`" for r in EXPAND_RATIOS))
    add("")
    add("## 上游边界（实测，服务端已尽力兼容）")
    add("")
    add("- **老接口入参体积**：`type=1`（去水印）实测 600×400/39.9KB 收单、800×533/63.2KB 被拒 ⇒")
    add(f"  本服务**超限自动等比压缩**（当前上限 {settings.LEGACY_MAX_IMAGE_SIDE}px / "
        f"{settings.LEGACY_MAX_IMAGE_BYTES}B），压缩前后在 `upstream.legacy.fitted` 与 `warnings[]` 里如实给出。")
    add("- **老接口频率/身份**：短时间连发（实测约数十次）会触发反爬标记"
        "（响应形如 `{'antiFlag': 1, 'message': 'Forbid spider access'}`，**没有 status 键**）。实测三维对照："
        "标记**粘在身份（cookie）上**、**会自愈**（约 10~15 分钟）、而**出口 IP 不是维度**。")
    add(f"  ⇒ 本服务已**自动兼容**：遇拒单会现场铸一个新匿名身份并重试一次，铸到后缓存复用"
        f"（当前 `BAIDU_ROTATE_COOKIE_ON_BURN={int(bool(settings.ROTATE_COOKIE_ON_BURN))}`，"
        f"`/readyz.checks.legacy_limits` 可见）；换身份过程写在 `upstream.legacy.identity_rotated` 与 `warnings[]`。"
        f"本服务自身另有节流（`BAIDU_MIN_INTERVAL`）与闸门，正常经本服务调用不会这么快。")
    add("- **结果图取回**：上游结果 CDN 偶发慢（实测 38~47s）⇒ 已加超时 + 重试"
        f"（`BAIDU_RESULT_FETCH_TIMEOUT={settings.RESULT_FETCH_TIMEOUT:g}` / "
        f"`BAIDU_RESULT_FETCH_RETRIES={settings.RESULT_FETCH_RETRIES}`）。")
    add("- **退化输入**：极小图（如 1×1）在部分能力上必失败（上游假阴性）—— 请用真实尺寸的图。")
    add("")
    add("## 兜底与降级")
    add("")
    add("- 本服务有**两条上游通路**：主链（`chat.baidu.com`）为主，"
        "**老接口**（`image.baidu.com/aigc`）为辅。")
    add(f"- 当前 `BAIDU_LEGACY={settings.LEGACY}`"
        f"{'（老接口可用 ⇒ 上表里带「老接口」通路的能力都可调）' if legacy else '（老接口关闭 ⇒ 仅纯主链能力可调）'}；")
    add(f"- 未取证闸门 = `{settings.ALLOW_UNVERIFIED}`（开启后会放行未端到端验证的能力，"
        "仅供排障，正常调用别开）。")
    add(f"- 版本 `{__version__}`；能力表版本时间（`/v1/models` 的 `created`）="
        f"`{MODEL_RELEASED_AT}`。")
    add("")
    add("## 常见错误")
    add("")
    add("| code | HTTP | 何时出现 |")
    add("|---|---|---|")
    for code, http, why in _ERRORS:
        add(f"| `{code}` | {http} | {why} |")
    add("")
    add("上游类失败在 `error.kind` 归因（词表与源码同源）：")
    add("")
    for kind, http in sorted(UPSTREAM_KIND_STATUS.items()):
        note = _UPSTREAM_KIND_NOTES.get(kind, "")
        add(f"- `{kind}`（HTTP {http}）：{note}" if note else f"- `{kind}`（HTTP {http}）")
    add("")
    add("## 其他端点")
    add("")
    add("| 端点 | 鉴权 | 说明 |")
    add("|---|---|---|")
    add("| `GET /healthz` | 免 | 存活 + 版本 |")
    add("| `GET /readyz` | 免 | 就绪（凭据/闸门/冷却/能力计数） |")
    add("| `GET /v1/models` | 免 | **本部署可调用**的模型（OpenAI 四键形态） |")
    add("| `GET /capabilities` | 需 | 全集 + 未取证项的原因与开启方式 + `styles[]` |")
    add("| `GET /stats` | 需 | 进程内窗口/闸门/最近 span |")
    add("")
    add("## 更多")
    add("")
    add("- 仓库（公开）：<https://github.com/rsfree/baidu>")
    add("- 契约：`docs/INTERFACE.md`（对外）· `docs/UPSTREAM.md`（上游实测与判断依据）")
    return "\n".join(out) + "\n"
