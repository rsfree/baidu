#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力注册表（本服务的唯一真相）。

**注册 = 本服务真的能跑通的能力。** 判断依据只有实测：主链＝toolType 全表枚举 +
端到端产物（`reverse-proxy/wenxin/probe/`）；老接口＝2026-09-24 的全量矩阵实测。
不是页面菜单、也不是名字看起来对不对。

五条纪律（与兄弟服务同源）：

1. **能力名必须与上游的 toolType 对齐**：`mcpInfo.ext.type`(toolType) + `sa`
   (`workspace_piccreate_<tt>`) + `query[TEXT].query`(中文名) **三处同时决定能力**。
2. **对外模型名 = `wenxin:<name>`**，与 reverse-proxy/biz-api 逐字一致；
   —— 目录/服务叫 baidu（env 前缀 `BAIDU_`），但**模型命名空间是 wenxin**。
3. **没跑通的不注册假能力**。四种「没有」分开表示：
   · `verified=False` ⇒ 登记但**默认不可用**（带原因与开启方式）；
   · `NOT_REGISTERED` ⇒ 连注册都没有；
   · `deliberately absent` ⇒ 刻意不暴露（不产图 / 缺输入字段的能力）；
   · 未在表中的 toolType ⇒ **无效编号**。
4. **`legacy_type` = 老接口（`image.baidu.com/aigc`）通路**（2026-09-24 实测）。
   有它 ⇒ `BAIDU_LEGACY != off` 时该能力**额外获得一条可用路径**；
   没有的能力**不会**因为开兜底而变可用（不制造假能力）。
5. **`tool_type=None` = legacy-only**：页面有、主链形态未坐实（或不存在）、
   但老接口已实测的能力（AI重绘 / 相似图）—— 仅在老接口开启时可用。
   **架构约定：主链为主、老接口兜底**（老接口不做主选路径）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .media import IMAGE_KINDS

__all__ = [
    "Capability",
    "CAPABILITIES",
    "NOT_REGISTERED",
    "DELIBERATELY_ABSENT",
    "EXPAND_RATIOS",
    "RESTYLE_STYLES",
    "ACCEPTS",
    "MODEL_RELEASED_AT",
    "OWNED_BY",
    "lookup",
    "available",
    "all_names",
    "availability",
]

#: `/v1/models` 的 `created` 字段：**本服务能力表的版本时间**，不是上游模型创建时间。
MODEL_RELEASED_AT = int(datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp())
OWNED_BY = "wenxin"

#: 「扩图」的 `image_expand` / 老接口 `ext_ratio` 比例集合（biz-api 登记值 + 实测过 4:3）。
EXPAND_RATIOS: tuple[str, ...] = ("1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3")

#: 本服务接受的输入图家族（按真实字节嗅探）。GIF 未取证 ⇒ 不收（见 media.py）。
ACCEPTS = IMAGE_KINDS


#: 「换风格」的 17 种风格：`(id, 中文标签)`——**服务端下发**（`image.baidu.com/aigc/extinfo` 的
#: `style[]`，2026-09-24 实测抓取）。请求时 id 进 `ext.style`、标签进 `ext.text` 与 TEXT query。
RESTYLE_STYLES: tuple[tuple[str, str], ...] = (
    ("miyazaki", "宫崎骏风"), ("giboli", "吉卜力风"), ("pailide_clay", "拍立得风"),
    ("clay", "橡皮泥风"), ("monet", "油画风"), ("style_transfer12", "奇幻卡通"),
    ("style_transfer11", "梵高"), ("style_transfer4", "炫彩插画"),
    ("style_transfer9", "浪漫雕塑"), ("style_transfer3", "光的气息"),
    ("style_transfer1", "复古胶片"), ("style_transfer2", "美式画报"),
    ("style_transfer5", "法式风情"), ("style_transfer10", "童话镇"),
    ("style_transfer6", "水神"), ("style_transfer7", "野兽派"),
    ("style_transfer8", "白月光"),
)


@dataclass(frozen=True)
class Capability:
    """一条能力：对外模型名 ↔ 上游（主链 toolType / 老接口 type）。

    ## 遮罩约定（`requires_mask=True` 的能力，2026-09-24 判决实验）
    遮罩 = **黑底 + 白框**的图片，**白色标记「要处理的区域」**
    （反约定实测无效：白底黑框下目标区域完全未处理）。走老接口时以
    `picInfo2` 直传（纯 base64，与 `picInfo` 同格式）。
    """

    name: str                       # 对外模型名，如 wenxin:clarity
    title: str                      # 中文能力名（主链 query 必须与它一致）
    #: 主链 toolType；None = **legacy-only**（主链形态未坐实/不存在）
    tool_type: str | None = None
    #: 主链是否已端到端跑通（False = 默认被闸门挡住，见 availability()）
    verified: bool = True
    #: 老接口（image.baidu.com/aigc/pccreate）的 type 值；None = 无老接口通路。
    #: **只登记已实测出图的值**（2026-09-24 全量矩阵）。
    legacy_type: str | None = None
    #: 老接口的附加表单字段（如 create_level=2 属于 AI重绘）
    legacy_extra: tuple[tuple[str, str], ...] = ()
    #: 老接口是否需要 `text` 字段、且由请求的 `prompt` 填充（局部替换实测形态）
    legacy_uses_prompt: bool = False
    #: 该能力需要遮罩（黑底白框；老接口 picInfo2）
    requires_mask: bool = False
    #: **工具入口码**（主链 UI 实测）：设置后 `build_body` 改用「工具入口形状」
    #: （`sa=searchbox_image` + `enter_type=<码>` + **无 mcpInfo**）—— 2026-09-24 实测：
    #: UI 点「风格转换」落在 `enter_type=pic_picfunc_14`、「背景替换」= `pic_picfunc_11`；
    #: 而旧形状（`workspace_piccreate_14`）如今只回`picEditBaseUrl`（跳编辑器），不出图。
    entry_type: str | None = None
    #: 工具入口形状下，**调用方的 `prompt` 就是指令文本**（背景描述）——
    #: 实测：UI 第一轮只发图 + 入口码（助手回"你想换成什么背景"），第二轮用**自然语言**给描述。
    needs_instruction: bool = False
    #: **workspace 形状的 `sa` 覆写**：个别工具用专属字母码（实测：换风格 = `workspace_piccreate_hfg`，
    #: 而不是 `workspace_piccreate_<tt>`）。设置后 `build_body` 用它，并带 `image_source=1`。
    workspace_sa: str | None = None
    #: 该能力需要**风格选择**（`style` 字段）：`(id, label)` 表由服务端下发（见模块常量）。
    needs_style: bool = False
    style_table: tuple[tuple[str, str], ...] = ()
    evidence: str = ""              # 取证出处（实测记录）
    notes: str = ""

    @property
    def supports_ratio(self) -> bool:
        return self.name == "wenxin:expand"

    @property
    def legacy_only(self) -> bool:
        return self.tool_type is None


def _verified_main(tt: str, legacy: str | None = None, **kw) -> dict:
    """主链已实测的能力：默认字段一次性给齐（减少抄写错误）。"""
    return {"tool_type": tt, "verified": True, "legacy_type": legacy, **kw}


CAPABILITIES: dict[str, Capability] = {
    # ------------------------------------------- 主链已跑通（14 项；部分带老接口兜底）
    "wenxin:clarity": Capability(
        name="wenxin:clarity", title="变清晰", **_verified_main(
            "3", "3",
            evidence=(
                "主链：§7.5 固定 4× 放大、硬上限 5472x3072；§8.1 n=3 p50=5.50s；"
                "本服务真跑 2026-09-24：900x600 → 3600x2400（6.0s）。"
                "老接口：type=3 实测秒级出图（3600x2400）"
            ),
            notes="输出为 JPEG；触顶后再跑不会更大（封顶而非继续放大）",
        )),
    "wenxin:expand": Capability(
        name="wenxin:expand", title="扩图", **_verified_main(
            "4", "4",
            evidence=(
                "主链：§3 枚举出图 ✅；`size` → `image_expand`（实测 4:3）。"
                "老接口：type=4 + ext_ratio 实测出图 **1368x1024（≈11.6s，较慢）**"
            ),
            notes=f"`size` 被解释为比例，实测支持集合：{EXPAND_RATIOS}",
        )),
    "wenxin:matting": Capability(
        name="wenxin:matting", title="抠图", **_verified_main(
            "9", "9",
            evidence="主链：§3 枚举出图 ✅。老接口：type=9 实测出图（900x600）",
        )),
    "wenxin:matting-pro": Capability(
        name="wenxin:matting-pro", title="背景抠图",
        **_verified_main(
            "10", None,
            evidence="§3 枚举实证（toolType 10 出图 ✅，与 9 同能力、不同模型入口）",
            notes="与 wenxin:matting 同能力域；上游枚举回显名为「抠图」。"
                  "老接口只有 type=9（同一能力域），未单独登记映射",
        )),
    "wenxin:bgreplace": Capability(
        name="wenxin:bgreplace", title="背景替换",
        tool_type="11", verified=False,
        entry_type="pic_picfunc_11", needs_instruction=True,
        evidence=(
            "枚举期（§3）出图 ✅；**2026-09-24 复测：workspace 形状回「未识别到主体」**，"
            "工具入口形状（sa=searchbox_image + enter_type=pic_picfunc_11）只回对话文字与追问"
            "（两轮亦不出图，帧内无 image-generate）⇒ 与「去水印」同类的**编辑器/agent 形态**。"
            "老接口 type=12 已死：7 次尝试（两种端点 × 两种输入形态）create 恒被拒（status 5）"
        ),
        notes="要交付需逆向编辑器（picEditUrl）或 agent 多轮流程；"
              "试跑请设 BAIDU_ALLOW_UNVERIFIED=1（形状已按 UI 实测接好）",
    ),
    "wenxin:restyle": Capability(
        name="wenxin:restyle", title="换风格",
        tool_type="14", verified=True,
        entry_type="pic_picfunc_14", workspace_sa="workspace_piccreate_hfg",
        needs_style=True, style_table=RESTYLE_STYLES,
        evidence=(
            "**2026-09-24 从站点 UI 抓到的可用报文**：`sa=workspace_piccreate_hfg` + "
            "`enter_type=pic_picfunc_14` + `mcpInfo.ext{type:14, image, image_source:1, channel:edit, "
            "style:<id>, text:<标签>}` + `query=[IMAGE, TEXT(标签)]` ⇒ **出图**（浏览器实点「宫崎骏风」，"
            "结果图落在 `aisearch.cdn.bcebos.com/pic_create/…jpeg`）。风格表由 "
            "`image.baidu.com/aigc/extinfo` 服务端下发（17 项 id↔标签）。"
            "对照：旧形状（`sa=workspace_piccreate_14`、无 style/text、`image_source=0`）只回 "
            "`picEditBaseUrl`（跳编辑器）；工具入口形状（无 mcpInfo）只回对话文字。"
            "老接口 type=14 已死（9 次尝试恒 status=5）"
        ),
        notes="需 `style`（id 或中文标签，见 `/capabilities` 的 `style_table`）；"
              "17 项：宫崎骏风/吉卜力风/拍立得风/橡皮泥风/油画风/奇幻卡通/梵高/炫彩插画/浪漫雕塑/"
              "光的气息/复古胶片/美式画报/法式风情/童话镇/水神/野兽派/白月光",
    ),
    "wenxin:sketch": Capability(
        name="wenxin:sketch", title="提线稿", **_verified_main(
            "15", "15",
            evidence=(
                "主链：§3 枚举出图 ✅；§8 时序 20–25s（单次观测）。"
                "老接口：type=15 实测出图 **3600x2400（10.8s）**"
            ),
            notes="全表最慢的能力之一；超时按 180s 配置留足",
        )),
    "wenxin:textreplace": Capability(
        name="wenxin:textreplace", title="文字替换",
        **_verified_main(
            "17", None,
            evidence="§3 枚举实证（toolType 17 出图 ✅；前端 bundle 旧注册表缺此条，服务端下发）",
        )),
    "wenxin:restore": Capability(
        name="wenxin:restore", title="图片修复",
        **_verified_main("20", None, evidence="§3 枚举实证（toolType 20 出图 ✅）")),
    "wenxin:removeperson": Capability(
        name="wenxin:removeperson", title="去路人",
        **_verified_main("21", None, evidence="§3 枚举实证（toolType 21 出图 ✅）")),
    "wenxin:removetext": Capability(
        name="wenxin:removetext", title="去文字",
        **_verified_main("22", None, evidence="§3 枚举实证（toolType 22 出图 ✅）")),
    "wenxin:filter": Capability(
        name="wenxin:filter", title="滤镜",
        **_verified_main("23", None, evidence="§3 枚举实证（toolType 23 出图 ✅）")),
    "wenxin:beauty": Capability(
        name="wenxin:beauty", title="美颜",
        **_verified_main("24", None, evidence="§3 枚举实证（toolType 24 出图 ✅）")),
    "wenxin:ps": Capability(
        name="wenxin:ps", title="P图",
        **_verified_main("27", None, evidence="§3 枚举实证（toolType 27 出图 ✅）")),

    # ---------------------------- 主链未取证、**老接口已实测**（mask 类两条，4 项）
    "wenxin:dewatermark": Capability(
        name="wenxin:dewatermark", title="去水印",
        tool_type="2", verified=False, legacy_type="1",
        evidence=(
            "主链：§3 枚举期出图 ✅；**§8.3 复测两次均失败**（items:null + 编辑器深链）。"
            "老接口：**type=1 实测出图 ✅**（3.1s/900x600，经服务）"
        ),
        notes="主链形态疑似已改「跳转交互式编辑器」；开 BAIDU_LEGACY 走老接口即可用",
    ),
    "wenxin:erase": Capability(
        name="wenxin:erase", title="消除",
        tool_type="8", verified=False, legacy_type="8", requires_mask=True,
        evidence=(
            "主链：§3 枚举期出图 ✅，但涂抹遮罩参数未逆向。"
            "老接口：**type=8 + picInfo2 遮罩 实测生效** —— 判决实验："
            "黑底白框下合成目标「红块」残留 0（完全消除），白底黑框残留 59972（未处理）"
        ),
        notes="需 `mask` 字段（黑底白框，白色=要处理的区域）；主链不支持遮罩 ⇒ 经老接口执行",
    ),
    "wenxin:replace": Capability(
        name="wenxin:replace", title="局部替换",
        tool_type="5", verified=False, legacy_type="5", requires_mask=True,
        legacy_uses_prompt=True,
        evidence=(
            "主链：§3 枚举期出图 ✅，但涂抹遮罩参数未逆向。"
            "老接口：**type=5 + picInfo2 遮罩 + text 实测生效**（判决实验：目标红块 60000→11137）"
        ),
        notes="需 `mask`（黑底白框）＋建议给 `prompt`（替换内容，实测形态里映射到老接口 `text`）",
    ),
    "wenxin:reimagine": Capability(
        name="wenxin:reimagine", title="相关图编",
        tool_type="13", verified=False, legacy_type=None,
        evidence="§3：toolType 13/28~34 同指该能力，枚举期出图 ✅，但**具体语义待坐实**",
        notes=(
            "疑似即页面上的「AI重绘 / 相似图」家族（页面两个独立工具卡；本服务已分别以 "
            "wenxin:redraw / wenxin:similar 走老接口实现）。要试主链形态请设 "
            "BAIDU_ALLOW_UNVERIFIED=1（语义可能与预期不符）"
        ),
    ),
    # ------------------------------------------- legacy-only（页面有、主链未坐实，2 项）
    "wenxin:redraw": Capability(
        name="wenxin:redraw", title="AI重绘",
        tool_type=None, verified=False, legacy_type="6",
        legacy_extra=(("create_level", "2"),),
        evidence=(
            "老接口实测（2026-09-24）：**type=6 + create_level=2 出图** 1440x960（7.3s）；"
            "主链形态未坐实（疑似 13/28~34 家族，语义待证）"
        ),
        notes="legacy-only：需 BAIDU_LEGACY=fallback/prefer；主链不参与",
    ),
    "wenxin:similar": Capability(
        name="wenxin:similar", title="相似图",
        tool_type=None, verified=False, legacy_type="7",
        legacy_extra=(("create_level", "5"),),
        evidence=(
            "老接口实测（2026-09-24）：**type=7 + create_level=5 出图** 900x600（8.1s）；"
            "主链形态未坐实（疑似 13/28~34 家族，语义待证）"
        ),
        notes="legacy-only：需 BAIDU_LEGACY=fallback/prefer；主链不参与",
    ),
}

#: 站点上**存在但本服务刻意不注册**的 toolType（每一条写清「差什么才注册」）。
NOT_REGISTERED: dict[str, str] = {
    "toolType 1 · 去水印（另一入口）": (
        "§3 枚举：与 toolType 2 同指「去水印」，但对测试图返回「没有检测到水印」、不出图。"
        "（**老接口** type=1 已实测出图，走 `wenxin:dewatermark`。）"
    ),
    "toolType 12 · 背景替换（重复编号）": (
        "与 toolType 11 同指「背景替换」（§3）。重复注册会让同一能力有两个模型名 —— "
        "模型名侧只保留 wenxin:bgreplace。"
    ),
    "toolType 28~34 · 相关图编（重复编号）": (
        "§3：28~34 与 13 同指「相关图编」一类，语义未坐实。页面上的「AI重绘 / 相似图」"
        "已分别走老接口落地（wenxin:redraw / wenxin:similar）；主链编号的坐实待真实交互报文。"
    ),
    "toolType 6/7 · 新接口无效编号": (
        "§3 实测：新接口 6/7 返回「服务繁忙」⇒ 编号不存在。"
        "**但老接口的 6/7 = AI重绘/相似图**（已实测出图）—— 两套编号体系别混。"
    ),
}

#: 站点上存在、但本服务**刻意不暴露**的能力（设计取舍，不是"还没做"）。
DELIBERATELY_ABSENT: dict[str, str] = {
    "生成文案（toolType 16）": (
        "§3 枚举出图 ✅，但它**不产图**（产文案）—— 挂在图片端点上属语义污染。"
        "与 biz-api 的取舍一致：刻意不注册。要文案请走对话类上游。"
    ),
    "老接口的风格(14+style) / 背景替换(12+text)": (
        "老接口里这两项需要额外的语义参数（`style` / `text`），本服务的入口面没有"
        "对应字段 ⇒ 不做「无参透传」的假能力。要接需先做字段设计 + 实测取证。"
    ),
}


def all_names() -> list[str]:
    return sorted(CAPABILITIES)


def lookup(name: str) -> Capability:
    try:
        return CAPABILITIES[name]
    except KeyError as exc:
        known = "、".join(all_names())
        raise KeyError(f"未注册的模型 {name!r}；已知：{known}") from exc


def availability(cap: Capability, *, allow_unverified: bool,
                 legacy: bool = False) -> tuple[bool, str]:
    """该能力在本部署是否可用；不可用时给出**原因 + 开启方式**（空串 = 可用）。

    `legacy=True`（= `BAIDU_LEGACY != off`）：
      · legacy-only 能力 ⇒ 仅此一路（未开启即不可用）；
      · 主链未取证但有 `legacy_type` 的能力 ⇒ 同样算可用（依据是实测出图）。
    """
    if cap.legacy_only:
        if legacy and cap.legacy_type:
            return True, ""
        return False, (
            f"{cap.name} 是 legacy-only 能力（主链形态未坐实：{cap.evidence}）；"
            f"要放开请设 BAIDU_LEGACY=fallback 或 prefer（走老接口 image.baidu.com/aigc）"
        )
    if cap.verified:
        return True, ""
    if allow_unverified:
        return True, ""
    if legacy and cap.legacy_type:
        return True, ""
    return False, (
        f"{cap.name} 未端到端取证（{cap.evidence}）；"
        f"要放开请设 BAIDU_ALLOW_UNVERIFIED=1（自担，{cap.notes}）"
        + ("；或设 BAIDU_LEGACY=fallback 走老接口（该能力有已验证的老接口映射）"
           if cap.legacy_type else "")
    )


def available(*, allow_unverified: bool, legacy: bool = False) -> dict[str, Capability]:
    """本部署当前**真正可调用**的能力（= /v1/models 的依据）。"""
    return {
        k: v for k, v in CAPABILITIES.items()
        if availability(v, allow_unverified=allow_unverified, legacy=legacy)[0]
    }
