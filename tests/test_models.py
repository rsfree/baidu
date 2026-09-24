"""能力注册表不变量（防止改表时悄悄改坏「谁能做什么」）。"""

from __future__ import annotations

import pytest

from app.models import (
    CAPABILITIES,
    DELIBERATELY_ABSENT,
    EXPAND_RATIOS,
    NOT_REGISTERED,
    available,
    availability,
    lookup,
)

#: 主链「曾经」端到端跑通的 toolType 全集（§3 枚举实证；**不重复**）
_MAIN_TOOL_TYPES = ["2", "3", "4", "5", "8", "9", "10", "11",
                    "14", "15", "17", "20", "21", "22", "23", "24", "27"]

#: legacy-only（`tool_type=None`，页面有、主链形态未坐实）
_LEGACY_ONLY = {"wenxin:redraw", "wenxin:similar"}

#: 默认门禁（未取证）——8 项，含 2026-09-24 新判定的编辑器/agent 形态两条
_GATED = {"wenxin:dewatermark", "wenxin:erase", "wenxin:replace",
          "wenxin:redraw", "wenxin:similar", "wenxin:bgreplace"}


def test_registry_shape_and_tool_types():
    assert len(CAPABILITIES) == 19
    assert all(n.startswith("wenxin:") for n in CAPABILITIES), "对外命名空间必须是 wenxin:*"
    main = sorted((c.tool_type for c in CAPABILITIES.values() if c.tool_type), key=int)
    assert len(set(main)) == 17, "同一 toolType 不得注册两个模型名（重复编号见 NOT_REGISTERED）"
    assert all(t.isdigit() for t in main)
    assert main == _MAIN_TOOL_TYPES
    assert {n for n, c in CAPABILITIES.items() if c.legacy_only} == _LEGACY_ONLY, \
        "`tool_type=None` 的恰好是 legacy-only 那两项"
    assert all(c.legacy_type for n, c in CAPABILITIES.items() if c.legacy_only), \
        "legacy-only 若无 legacy_type 就是死条目"


def test_gated_set_is_exactly_the_documented_one():
    """未取证的能力集合 = 门禁对象；多一个/少一个都说明有人改了结论而没改这里。

    6 项：去水印（编辑器件形态）＋ 消除/局部替换/背景替换（主链遮罩未逆向，走老接口）＋
    AI重绘/相似图（legacy-only）—— **全都有老接口映射** ⇒ 开兜底后 19/19 全可用。
    ⚠️ 换风格**已攻克**（2026-09-24：抓到站点真实报文 ⇒ sa=workspace_piccreate_hfg + style/text + TEXT(标签) 出图）。
    """
    gated = {n for n, c in CAPABILITIES.items() if not c.verified}
    assert gated == _GATED


def test_every_capability_has_evidence_and_title():
    for cap in CAPABILITIES.values():
        assert cap.evidence, f"{cap.name} 缺取证出处"
        assert cap.title and cap.title.strip()


def test_mask_and_prompt_semantics_are_registered():
    """遮罩 / prompt / 工具入口码的登记口径（均为实测所得）。"""
    assert {n for n, c in CAPABILITIES.items() if c.requires_mask} == \
        {"wenxin:erase", "wenxin:replace", "wenxin:bgreplace"}
    assert {n for n, c in CAPABILITIES.items() if c.legacy_uses_prompt} == \
        {"wenxin:replace", "wenxin:bgreplace"}
    assert dict(lookup("wenxin:redraw").legacy_extra) == {"create_level": "2"}
    assert dict(lookup("wenxin:similar").legacy_extra) == {"create_level": "5"}
    # 工具入口形状（站点 UI 实测的 enter_type）——目前只在背景替换/换风格上
    assert {n: c.entry_type for n, c in CAPABILITIES.items() if c.entry_type} == {
        "wenxin:restyle": "pic_picfunc_14"}   # 背景替换改走老接口 12，不再需要入口码
    assert {n for n, c in CAPABILITIES.items() if c.needs_style} == {"wenxin:restyle"}
    assert len(lookup("wenxin:restyle").style_table) == 17
    assert lookup("wenxin:restyle").workspace_sa == "workspace_piccreate_hfg"


def test_availability_gate_and_open():
    cap = lookup("wenxin:dewatermark")
    ok, reason = availability(cap, allow_unverified=False)
    assert not ok and "BAIDU_ALLOW_UNVERIFIED" in reason
    assert availability(cap, allow_unverified=True)[0]
    assert len(available(allow_unverified=False)) == 13          # 20 - 7 门禁
    assert len(available(allow_unverified=True)) == 17           # 19 - 2 legacy-only


def test_verified_caps_are_available_without_the_gate():
    for cap in CAPABILITIES.values():
        if cap.verified:
            assert availability(cap, allow_unverified=False)[0], cap.name


def test_supports_ratio_is_only_expand():
    assert {n for n, c in CAPABILITIES.items() if c.supports_ratio} == {"wenxin:expand"}
    assert len(EXPAND_RATIOS) >= 4


def test_lookup_unknown_lists_known_names():
    with pytest.raises(KeyError) as ei:
        lookup("wenxin:nope")
    assert "wenxin:clarity" in str(ei.value)


def test_absences_are_documented():
    """刻意不暴露 / 不注册的能力都必须带「差什么才注册」的说明。"""
    assert "生成文案" in " ".join(DELIBERATELY_ABSENT)
    assert any("风格" in k for k in DELIBERATELY_ABSENT)
    assert any(k.startswith("toolType 1 ") for k in NOT_REGISTERED)
    assert any("无效编号" in k for k in NOT_REGISTERED)
    assert any("28~34" in k for k in NOT_REGISTERED), "兜底编号族必须有说明"
    assert any("通用编辑器兜底" in v for v in NOT_REGISTERED.values())


# ---------------------------------------------------------------- 老接口兜底


def test_legacy_unlocks_exactly_the_mapped_capabilities():
    default = available(allow_unverified=False)
    with_legacy = available(allow_unverified=False, legacy=True)
    assert len(default) == 13
    # 13 已取证 + 6 有映射的未取证 = 19 ⇒ **开兜底后全部可用**
    assert len(with_legacy) == 19 == len(CAPABILITIES)
    assert {"wenxin:dewatermark", "wenxin:erase", "wenxin:replace",
            "wenxin:redraw", "wenxin:similar", "wenxin:bgreplace"} <= set(with_legacy)
    assert "wenxin:restyle" in with_legacy               # 主链已攻克（与老接口兜底无关）
    # 每个未取证能力都有老接口映射（无「永久门禁」残留）
    assert all(lookup(n).legacy_type for n in _GATED if not lookup(n).legacy_only)


def test_legacy_type_registry_is_evidence_based():
    """老接口映射**只登记实测出图**的 type（2026-09-24 全量矩阵）。"""
    mapped = {n: c.legacy_type for n, c in CAPABILITIES.items() if c.legacy_type}
    assert mapped == {
        "wenxin:clarity": "3", "wenxin:expand": "4", "wenxin:matting": "9",
        "wenxin:sketch": "15", "wenxin:dewatermark": "1", "wenxin:erase": "8",
        "wenxin:replace": "5", "wenxin:redraw": "6", "wenxin:similar": "7",
        "wenxin:bgreplace": "12",
    }


def test_availability_reason_mentions_legacy_only_when_mapped():
    # 所有未取证能力都有老接口映射 ⇒ 开启方式里必须提 BAIDU_LEGACY
    for name in _GATED:
        cap = lookup(name)
        assert "BAIDU_LEGACY" in availability(cap, allow_unverified=False)[1], name
    # 已取证能力不因兜底改变可用性
    assert availability(lookup("wenxin:clarity"), allow_unverified=False)[0]
    legacy_only = lookup("wenxin:redraw")
    ok, reason = availability(legacy_only, allow_unverified=False)
    assert not ok and "legacy-only" in reason
    assert availability(legacy_only, allow_unverified=False, legacy=True)[0]
