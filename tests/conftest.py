"""pytest 装置。

🔴 一条铁律：**所有用例零真实上游请求**。
`_no_real_network` 直接把 `socket.connect` 钉死 —— 任何漏掉的真实连接会当场炸，
而不是"悄悄打了一次上游、消耗风控评分"。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 刻意**不**在这里设任何 BAIDU_* 环境变量：pydantic-settings 的 `_env_file=None`
# 只关掉 .env 文件，**环境变量照样生效** —— 设了就会污染全部用例。
# 需要钉值的用例请用 `helpers.settings(...)` 逐项显式传参。


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """任何真实出网都当场失败（DNS 不在拦截范围，但 connect 一定拦）。"""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "测试禁止真实网络连接（tests/conftest.py::_no_real_network）；"
            "请用 httpx.MockTransport 或假 fetch"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
