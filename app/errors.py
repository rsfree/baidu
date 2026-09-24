"""错误分类体系。

分工与兄弟服务一致：

- `UpstreamError` 是**上游**错误的容器（`kind` 决定对外 HTTP 状态，映射表只此一处）；
- `ApiError` 是本服务对外的错误信封（我们自己的报文，不载荷上游实现细节）。

wenxin 上游错误的特殊性（见 docs/UPSTREAM.md §7）：

1. **风控挑战（昆仑）不是限流**。它是风险评分下发的验证挑战，**重试会加重标记** ⇒
   单独归类为 `risk_control`（429，报文里明确"不要重试，去浏览器过一次验证"）。
   与「配额/限流」（429，可退避重试）是两回事，混用会指挥调用方做反事。
2. **`tokenFail` 的文案完全不提参数**（只说「出了点小问题」）⇒ 归为 `auth`（503，
   部署侧的凭据问题），并在 message 里点出两个已知成因（lid 用错 / token 过期）。
3. 上游**没有业务错误码**：判成败靠 SSE 帧内容（hints / 无图），不是 HTTP 状态。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ApiError",
    "UpstreamError",
    "RiskControlError",
    "AuthError",
    "UpstreamParamError",
    "UpstreamTimeout",
    "UpstreamUnavailableError",
    "UPSTREAM_KIND_STATUS",
]


class UpstreamError(RuntimeError):
    """上游错误基类。`detail` 会原样进对外错误信封（**不得含凭据**）。"""

    kind = "upstream"

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        retry_after: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.retry_after = retry_after
        self.detail = detail or {}

    def __str__(self) -> str:
        bits = [self.message]
        if self.http_status is not None:
            bits.append(f"HTTP {self.http_status}")
        return " ".join(bits)


class RiskControlError(UpstreamError):
    """命中昆仑风控验证挑战（SSE 首帧 `chatHitKunlun`）。

    🔴 **不可重试**：持续施压很可能延长风控标记。正确处置是
    「在真实浏览器打开文心页面过一次验证框」+ 等冷却窗自然过期。
    服务层收到它会把进程内冷却窗打开（`BAIDU_COOLDOWN`），窗内零上游请求。
    """

    kind = "risk_control"


class AuthError(UpstreamError):
    """凭据层面的失败（`tokenFail` / 首页取不到凭据 / STS 拒绝）。

    这几乎总是**部署侧**的问题（cookie 失效、上游改版、lid 用错），不是调用方
    参数写错了 ⇒ 对外 503 而不是 401/400（与 wuli「上游 401 ⇒ 本服务 503」同源）。
    """

    kind = "auth"


class UpstreamParamError(UpstreamError):
    """上游拒绝了请求形状（如 HTTP 400 / Bad_Request 一类）。走到这里通常意味着
    上游行为变化或本服务的请求构造回归 —— 正常参数问题在本地校验就挡住了。"""

    kind = "param"


class UpstreamTimeout(UpstreamError):
    kind = "timeout"


class UpstreamUnavailableError(UpstreamError):
    """其余上游失败：非 200、非 JSON、**静默型失败**（无 hint 也没出图）。"""

    kind = "upstream"


#: 上游 kind → 对外状态码。**这一张表就是「归属」的全部答案**。
#:
#: `risk_control` 与「配额类 429」语义不同，此处刻意只映射风控；
#: `auth` ⇒ 503（部署问题），与 wuli 处理上游 401 的口径一致。
UPSTREAM_KIND_STATUS: dict[str, int] = {
    "risk_control": 429,
    "auth": 503,
    "param": 400,
    "timeout": 504,
    "upstream": 502,
}


class ApiError(RuntimeError):
    """对外错误信封（我们自己的报文，不载荷上游实现细节）。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.extra:
            body["error"].update(self.extra)
        return body
