"""接线门禁：配置旋钮全被读、gunicorn/容器入口可解析、网络门禁自证。

这一层防的是「**看起来在工作**」的缺陷：没人读的旋钮（假配置）、
写成模块对象的 gunicorn 目标（单测全绿、一起就炸）、
以及"测试零出网"这句承诺其实没生效。
"""

from __future__ import annotations

import pathlib
import re
import socket

import pytest

from app.config import Settings
from app.main import create_app
from tests.helpers import settings

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 允许「被读」的四种前缀：main/lifespan 用 `s.`、`__main__` 里用 `_s.`、
#: service 用 `settings.`、client 用 `self._s.`
_USE_RE = re.compile(r"\b(?:s|_s|settings|conf|self\._s)\.([A-Z][A-Z0-9_]{2,})\b")


def _src(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_network_guard_is_alive():
    """门禁自证：**证明拦得住**（否则 conftest 的 fixture 是死代码，全仓绿而无意义）。"""
    s = socket.socket()
    with pytest.raises(AssertionError):
        s.connect(("127.0.0.1", 9))
    s.close()


def test_config_knobs_are_all_read():
    """每个 Settings 字段都必须在 app/ 里被真的读到（config.py 自身除外）。"""
    fields = set(Settings.model_fields)
    used: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        if path.name == "config.py":
            continue
        used |= set(_USE_RE.findall(path.read_text(encoding="utf-8")))
    missing = sorted(fields - used)
    assert not missing, f"没人读的配置旋钮（等于假配置，删掉或接上）：{missing}"


def test_env_example_documents_every_knob():
    """.env.example 是字段说明的权威来源：每个旋钮都要有 BAIDU_<字段> 的身影。"""
    text = _src(".env.example")
    missing = [f for f in Settings.model_fields if f"BAIDU_{f}" not in text]
    assert not missing, f".env.example 缺这些旋钮的说明：{missing}"


def test_gunicorn_target_uses_factory_call():
    src = _src("gunicorn_conf.py")
    assert 'app.main:create_app()' in src
    assert '"app.main:app"' not in src and "'app.main:app'" not in src


def test_dockerfile_cmd_matches_factory_and_port():
    src = _src("Dockerfile")
    assert "app.main:create_app()" in src
    assert f"0.0.0.0:{Settings.model_fields['PORT'].default}" in src


def test_main_module_entry_is_in_main_py():
    """`python -m app.main` 跑的是 app/main.py —— 入口必须写在这里（写进 __main__.py 会静默退出 0）。"""
    src = _src("app/main.py")
    assert 'if __name__ == "__main__":' in src
    assert "factory=True" in src


def test_app_factory_and_media_dir_created(tmp_path):
    media = tmp_path / "m"
    app = create_app(settings(MEDIA_DIR=str(media)))
    assert app.title == "baidu-service"
    # StaticFiles 在挂载时就要求目录存在 ⇒ 目录必须是 create_app 自己建的
    assert media.is_dir()


# --------------------------------------------------- 离线自检/冒烟的"零触网"门禁


def test_offline_probe_exports_all_point_at_local_mock():
    """`probe.py` 的离线配置：**每一个上游出口**都必须指向本地假上游。

    2026-09-24 实际踩过：`_settings_offline` 漏了 `LEGACY_BASE` ⇒ probe 的 loop 阶段
    把 erase/redraw/replace/similar 打到了**真实** `image.baidu.com`（离线自检的本意是零触网）。
    这类"漏一个出口"的缺陷只有逐个字段钉死才防得住。
    """
    from scripts.probe import _settings_offline  # noqa: PLC0415

    s = _settings_offline()
    for field in ("BASE_URL", "BOS_HOST", "CDN_BASE", "LEGACY_BASE"):
        value = getattr(s, field)
        assert value.startswith("http://127.0.0.1:"), f"{field}={value!r} 指向了非本地地址"


def test_smoke_script_pins_legacy_base_to_mock():
    """冒烟脚本必须把老接口基址也指到假上游（同上一条缺陷的脚本版）。"""
    src = _src("scripts/smoke.sh")
    assert 'BAIDU_LEGACY_BASE="http://127.0.0.1:${MOCK_PORT}"' in src
    assert "BAIDU_LEGACY=fallback" in src
