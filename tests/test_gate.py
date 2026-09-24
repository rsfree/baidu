"""闸门用例：RateGate（串行 + 最小间隔 + 滑窗 + 排队上限）与 RiskWindow。

时间源全部注入（`clock=` / `at=`）—— 用例不睡真实时间，断言是确定性的。
"""

from __future__ import annotations

import pytest

from app.gate import GateBusy, RateGate, RiskWindow
from tests.helpers import arun


def _gate(min_interval: float = 4.0, per_minute: int = 8) -> tuple[RateGate, dict]:
    clock = {"t": 100.0}
    return RateGate(min_interval, per_minute, clock=lambda: clock["t"]), clock


# ---------------------------------------------------------------- RateGate


def test_first_acquire_never_waits():
    gate, _ = _gate()
    assert gate.wait_needed() == 0.0
    assert arun(gate.acquire(10.0)) == 0.0
    gate.release()
    assert gate.snapshot()["started"] == 1


def test_min_interval_counts_from_last_start():
    gate, clock = _gate(min_interval=4.0)
    arun(gate.acquire(10.0))
    gate.release()
    clock["t"] = 101.5
    assert gate.wait_needed() == pytest.approx(2.5)
    clock["t"] = 104.0
    assert gate.wait_needed() == pytest.approx(0.0)


def test_busy_when_needed_wait_exceeds_budget():
    gate, clock = _gate(min_interval=90.0)
    arun(gate.acquire(10.0))
    gate.release()
    with pytest.raises(GateBusy) as ei:
        arun(gate.acquire(1.0))
    assert ei.value.wait == pytest.approx(90.0)
    assert gate.snapshot()["rejected"] == 1
    # 拒绝路径不能泄漏名额：时钟走过最小间隔后应能立刻拿到
    clock["t"] = 200.0
    assert arun(gate.acquire(0.0)) == pytest.approx(0.0)
    gate.release()


def test_sliding_window_cap_blocks_after_per_minute():
    gate, clock = _gate(min_interval=0.0, per_minute=2)
    for _ in range(2):
        arun(gate.acquire(10.0))
        gate.release()
        clock["t"] += 0.001
    clock["t"] = 101.0
    assert gate.wait_needed() == pytest.approx(59.0, abs=0.01)   # 等第 1 次滑出 60s 窗
    with pytest.raises(GateBusy):
        arun(gate.acquire(1.0))
    # 窗口滑出后恢复（两次都已超过 60s）
    clock["t"] = 161.0
    assert gate.wait_needed() == 0.0


def test_release_makes_slot_available_again():
    gate, _ = _gate(min_interval=0.0)
    arun(gate.acquire(1.0))
    assert gate.snapshot()["in_flight"] == 1
    gate.release()
    assert gate.snapshot()["in_flight"] == 0
    assert arun(gate.acquire(0.0)) == 0.0
    gate.release()


# ---------------------------------------------------------------- RiskWindow


def test_risk_window_trip_remaining_and_snapshot():
    w = RiskWindow(600.0)
    assert not w.open(at=0.0)
    assert w.trip(at=100.0) == 600.0
    assert w.open(at=200.0)
    assert w.remaining(at=200.0) == pytest.approx(500.0)
    assert not w.open(at=700.0)
    snap = w.snapshot(at=200.0)
    assert snap == {"cooling": True, "remaining_s": 500.0, "cooldown_s": 600.0}


def test_risk_window_zero_cooldown_never_opens():
    w = RiskWindow(0.0)
    assert w.trip(at=1.0) == 0.0
    assert not w.open(at=1.0)
    assert w.snapshot(at=1.0)["cooling"] is False


def test_risk_window_clear():
    w = RiskWindow(60.0)
    w.trip(at=0.0)
    assert w.open(at=1.0)
    w.clear()
    assert not w.open(at=1.0)
