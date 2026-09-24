"""`/llms.txt` 的门禁：**说明书不许漂移成假信息**。

三条硬约束：
1. 能力表 / 风格表 / 计数 **必须从注册表派生**（对账，不靠人肉同步）；
2. 错误码**必须真的存在于源码**（防"文档里有、代码里没有"的幽灵码）；
3. 端点是**免鉴权**的（LLM/Agent 要能直接读）。
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import UPSTREAM_KIND_STATUS
from app.llms_txt import render
from app.main import create_app
from app.models import CAPABILITIES, available

APP_SRC = "\n".join(p.read_text(encoding="utf-8")
                    for p in pathlib.Path(__file__).resolve().parent.parent.joinpath("app").glob("**/*.py"))


def _client(tmp_path, **env) -> TestClient:
    settings = Settings(_env_file=None, MEDIA_DIR=str(tmp_path / "media"), COOKIE="BAIDUID=x",
                        **env)
    return TestClient(create_app(settings))


def test_llms_txt_is_public_markdown(tmp_path):
    with _client(tmp_path) as c:
        r = c.get("/llms.txt")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/markdown"), r.headers["content-type"]
    body = r.text
    assert body.startswith("# baidu-service"), body[:80]
    # 有鉴权说明，且明确点出免鉴权面
    assert "Authorization: Bearer" in body
    assert "/healthz" in body and "/readyz" in body and "/v1/models" in body
    # 干跑（零成本预演）必须写清
    assert "dry_run" in body and "X-Avm-Dry-Run" in body


def test_llms_txt_derive_from_registry(tmp_path):
    """能力表逐条出现；计数与注册表对账；风格表 17 项齐全。"""
    with _client(tmp_path, LEGACY="fallback") as c:
        body = c.get("/llms.txt").text
    for name in CAPABILITIES:
        assert f"`{name}`" in body, f"{name} 未出现在 /llms.txt"
    registered = re.search(r"本服务注册 \*\*(\d+) 项能力\*\*", body)
    usable = re.search(r"本部署当前可调用 \*\*(\d+) 项\*\*", body)
    assert registered and int(registered.group(1)) == len(CAPABILITIES)
    assert usable and int(usable.group(1)) == len(available(allow_unverified=False, legacy=True))
    restyle = CAPABILITIES["wenxin:restyle"]
    for sid, label in restyle.style_table:
        assert f"`{sid}`" in body and label in body, f"风格 {sid}/{label} 未出现"


def test_llms_txt_counts_track_settings(tmp_path):
    """可用性随部署开关变化（legacy off ⇒ 可用数下降、文案随之变化）。"""
    with _client(tmp_path) as c:            # LEGACY 默认 off
        no_legacy = c.get("/llms.txt").text
    with _client(tmp_path, LEGACY="fallback") as c:
        with_legacy = c.get("/llms.txt").text
    n_off = int(re.search(r"本部署当前可调用 \*\*(\d+) 项\*\*", no_legacy).group(1))
    n_on = int(re.search(r"本部署当前可调用 \*\*(\d+) 项\*\*", with_legacy).group(1))
    assert n_off < n_on, "关掉老接口后可用能力必须变少（不许两份文案都说满）"
    assert "BAIDU_LEGACY=off" in no_legacy and "BAIDU_LEGACY=fallback" in with_legacy


def test_llms_txt_error_codes_exist_in_source():
    """幽灵码门禁：说明里列的每个 code 都必须在 app/ 源码里出现。"""
    txt = render(Settings(_env_file=None))
    codes = set(re.findall(r"^\| `([a-z_]+)` \| \d{3} \|", txt, flags=re.MULTILINE))
    assert codes, "错误码表没解析到"
    missing = sorted(c for c in codes if c not in APP_SRC)
    assert not missing, f"/llms.txt 里出现源码中不存在的错误码：{missing}"


def test_llms_txt_mentions_upstream_kinds_from_source():
    """上游归因词表同样回源码核对（kind 由 UPSTREAM_KIND_STATUS 决定）。"""
    txt = render(Settings(_env_file=None))
    for kind in UPSTREAM_KIND_STATUS:
        assert f"`{kind}`" in txt, f"上游归因 {kind} 未写进说明书"


@pytest.mark.parametrize("cap_with_mask", ["wenxin:erase", "wenxin:replace", "wenxin:bgreplace"])
def test_llms_txt_mask_requirement_is_accurate(tmp_path, cap_with_mask):
    with _client(tmp_path, LEGACY="fallback") as c:
        body = c.get("/llms.txt").text
    row = next(ln for ln in body.splitlines() if f"`{cap_with_mask}`" in ln and ln.startswith("|"))
    assert "`mask`" in row, row
