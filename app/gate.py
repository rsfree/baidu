#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上行节奏闸门 —— 「不触发昆仑」+「命中即熔断」的进程内实现。

背景（docs/UPSTREAM.md §7）：文心的门禁是**风险评分 → 下发验证挑战**，不是计数器限流。
所以策略不是"绕"，而是三件事：

  · **降速**：串行 + 最小间隔 + 每分钟上限（`RateGate`）——评分随短时密度上升；
  · **熔断**：读到首帧 `chatHitKunlun` 立即断开、不重试（客户端层），并开冷却窗
    （`RiskWindow`），窗内**零上游请求**（持续施压会延长标记）；
  · **可观测**：两个闸门都暴露 snapshot，进 `/readyz` 与 `/stats`。

🔴 这两个都是**进程内状态**：单 worker 下有效，多 worker 会变成 N 份
（速率约束 ×N、冷却窗近似失效）⇒ `gunicorn_conf.py` 默认 1 个 worker 是架构决定。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Callable

__all__ = ["GateBusy", "RateGate", "RiskWindow"]

_WINDOW_S = 60.0


class GateBusy(RuntimeError):
    """排队会超过上限（或上游在途占满）⇒ 调用方应回 429 + Retry-After。"""

    def __init__(self, wait: float) -> None:
        super().__init__(f"排队上限触发（需再等 {wait:.1f}s）")
        self.wait = max(0.0, float(wait))


class RateGate:
    """串行 + 最小间隔 + 滑动窗口上限（三重约束，模拟"一个守规矩的客户端"）。

    - **串行**：同一时刻只允许一个生成请求在打上游（信号量 1，覆盖整个 SSE 生命周期，
      不只在建连时占位）；
    - **最小间隔**：两次请求起点至少相距 `MIN_INTERVAL` 秒；
    - **滑动窗口**：任意 60s 内请求数 ≤ `PER_MINUTE`。

    超过 `MAX_WAIT` 仍排不到 ⇒ `GateBusy`（服务层翻成 429，带 Retry-After）。
    时间源可注入（`clock=`），`wait_needed()` 是纯计算，两者都便于确定性测试。
    """

    def __init__(self, min_interval: float, per_minute: int, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._min_interval = max(0.0, float(min_interval))
        self._per_minute = max(1, int(per_minute))
        self._clock = clock
        self._sem = asyncio.Semaphore(1)
        self._last = 0.0
        self._window: deque[float] = deque()
        # ---- 记账（进 /stats；都不是凭据）----
        self.started = 0
        self.rejected = 0
        self.waited_total = 0.0

    # ---------------------------------------------------------------- 纯计算

    def wait_needed(self, now: float | None = None) -> float:
        """当前要不要等、等多久（秒）。0 = 可以立即发。"""
        t = self._clock() if now is None else now
        while self._window and t - self._window[0] > _WINDOW_S:
            self._window.popleft()
        wait = 0.0
        if self._last:
            wait = max(wait, self._min_interval - (t - self._last))
        if len(self._window) >= self._per_minute:
            wait = max(wait, _WINDOW_S - (t - self._window[0]))
        return max(0.0, wait)

    # ---------------------------------------------------------------- 取名额

    async def acquire(self, max_wait: float) -> float:
        """取到一个发送名额（**覆盖整个生成过程**，用完必须 `release()`）。

        返回实际排队秒数；超上限抛 `GateBusy`。超时/失败路径不会泄漏信号量。
        """
        t0 = self._clock()
        budget = max(0.0, float(max_wait))
        try:
            if budget > 0:
                await asyncio.wait_for(self._sem.acquire(), timeout=budget)
            else:
                if self._sem.locked():
                    raise TimeoutError
                await self._sem.acquire()
            try:
                while True:
                    wait = self.wait_needed()
                    if wait <= 0:
                        break
                    if (self._clock() - t0) + wait > budget:
                        raise GateBusy(wait)
                    await asyncio.sleep(wait)
                now = self._clock()
                self._last = now
                self._window.append(now)
                self.started += 1
                self.waited_total += now - t0
                return now - t0
            except GateBusy:
                # 排队超预算：计入 rejected 后放掉名额（两条拒绝路径的记账必须一致）
                self.rejected += 1
                self._sem.release()
                raise
            except BaseException:
                self._sem.release()
                raise
        except TimeoutError:
            self.rejected += 1
            raise GateBusy(self.wait_needed()) from None

    def release(self) -> None:
        """生成过程结束（无论成败）后释放名额。"""
        self._sem.release()

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        t = self._clock() if now is None else now
        return {
            "in_flight": 1 if self._sem.locked() else 0,
            "min_interval_s": self._min_interval,
            "per_minute": self._per_minute,
            "window_len": len(self._window),
            "wait_s": round(self.wait_needed(t), 1),
            "started": self.started,
            "rejected": self.rejected,
            "waited_total_s": round(self.waited_total, 1),
        }


class RiskWindow:
    """昆仑风控冷却窗：命中后 `COOLDOWN` 秒内零上游请求。

    时间源可注入（`at=`），便于确定性测试（与 textin 的 `QuotaWindow` 同构）。
    """

    def __init__(self, cooldown: float = 0.0) -> None:
        self._cooldown = float(cooldown)
        self._until = 0.0

    @property
    def cooldown(self) -> float:
        return self._cooldown

    def trip(self, at: float | None = None) -> float:
        """命中风控时开窗。返回窗口秒数（0 = 该配置下不开窗）。"""
        if self._cooldown <= 0:
            self._until = 0.0
            return 0.0
        now = time.monotonic() if at is None else at
        self._until = now + self._cooldown
        return self._cooldown

    def remaining(self, at: float | None = None) -> float:
        now = time.monotonic() if at is None else at
        return max(0.0, self._until - now)

    def open(self, at: float | None = None) -> bool:
        return self.remaining(at) > 0

    def clear(self) -> None:
        self._until = 0.0

    def snapshot(self, at: float | None = None) -> dict[str, Any]:
        remaining = self.remaining(at)
        return {
            "cooling": remaining > 0,
            "remaining_s": round(remaining, 1),
            "cooldown_s": self._cooldown,
        }
