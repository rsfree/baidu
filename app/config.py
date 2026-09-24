"""配置层。

纪律（沿用 jimeng / pavo / textin）：**每个旋钮都必须有人读** —— 没人读的旋钮会让人
以为已调优。这条由 `tests/test_wiring.py::test_config_knobs_are_all_read` 兜底。

与兄弟服务的结构差别：本服务是**同步翻译层**（无任务库、无协调器），
所以这里刻意**没有** DB / QUEUE 一类旋钮 —— 登记了也没人读，那就是假配置。

命名空间：环境变量前缀 `BAIDU_`（服务名按目录叫 baidu），但**对外模型名保持
`wenxin:*`**（与 reverse-proxy/biz-api 逐字一致；同一上游不出现两套名字）。
cookie 与 biz-api 的 `WX_COOKIE` 是同一个值（百度系 cookie），部署时直接搬。
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings", "reset_settings"]


class Settings(BaseSettings):
    """运行期配置。全部字段都有默认值 ⇒ 缺 .env 也能起来（凭据缺失时请求回 503）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="BAIDU_",
    )

    # ------------------------------------------------------------ 上游（chat.baidu.com）
    BASE_URL: str = "https://chat.baidu.com"
    # BOS 桶与 CDN：转存输入图用。**只在联调/冒烟时应改**，生产保持默认。
    BOS_HOST: str = "aisearch.bj.bcebos.com"
    CDN_BASE: str = "https://aisearch.cdn.bcebos.com"
    # **必需**（就绪的唯一硬条件）。2026-09-12 A/B 对照：同参数下去掉 cookie 立刻
    # 变「抱歉，服务繁忙，请稍后再试」且不出图，带上同一份 cookie 立刻成功出图。
    # 注意「免登录直调」指不需要账号登录，**不等于不需要 cookie**。
    # ⚠️ 它是凭据：不要提交 .env、不要贴进 issue、不要在日志里回显。
    COOKIE: str = ""
    # chat_token = btoa(<token>|<md5(query)>|<ts>|<lid>)-<lid>-3 的两个变量。
    # **可选**：留空则运行时从首页 HTML 的 `aiTabFrameBaseData` 现取并缓存（TTL 540s），
    # 因为手工填的值实测只活 ≥10 分钟 —— 本就不该当配置项。
    # 显式填了就用填的（排障 / 防上游改版时钉住），此时不会自动现取。
    SESSION_TOKEN: str = ""
    # 🔴 必须是**页面级 searchframeLid**，用会话 ori_lid 会稳定吃 tokenFail
    # （报错文案只说"出了点小问题"，极易误判成风控）。
    LID: str = ""
    # 会话 ID（body.searchInfo.ori_lid）。实测复用不影响成败，留空则复用 LID。
    ORI_LID: str = ""
    # 输入图送上游的方式（见 docs/UPSTREAM.md §5）：
    #   bos    总是转存 → BOS（**默认**：多花 1~2s 换稳定，上游抓外链会失败）
    #   auto   外链直传，失败/非 URL 输入自动转存
    #   direct 永远直传（仅当输入本来就是外链时才成立；传本地字节时自动降级为转存）
    UPLOAD_MODE: str = "bos"
    # query 文本策略：ignore=用能力中文名（**唯一实证可用**）；
    # prepend=把 prompt 拼在能力名前再送（未验证，属实验开关）。
    PROMPT_MODE: str = "ignore"
    # 结果 URL 尾部带 `?x-bce-process=image/watermark,...`（AI 水印处理参数）。
    # 默认**剥离**（下载时去掉该 query 即为无水印原图）；要原样（带水印）设 0。
    STRIP_WATERMARK: bool = True
    # 转存前的图像标准化：限最长边、压到 MAX_BYTES 内、统一标准 MIME。
    # 目的是消除上游抓取外链的两类已知失败诱因（体积 / 非标 Content-Type）。
    NORMALIZE: bool = True
    MAX_SIDE: int = 2048
    MAX_BYTES: int = 2 * 1024 * 1024
    # 代取/结果下载的硬字节上限（外链输入与本服务取回上游产物共用）。
    MAX_DOWNLOAD_MB: int = 30
    # 上游 SSE 总超时：变清晰实测 5.5s、提线稿 20~25s，另加转存与结果下载 ⇒ 180s 留足。
    TIMEOUT: float = 180.0

    # ------------------------------------------------------------ 上行节奏（进程内闸门）
    # 昆仑风控是**风险评分**而非计数器（见 docs/UPSTREAM.md §7）：只能降速不触发 +
    # 命中即熔断。三个旋钮就是 docs/UPSTREAM.md §7.5 建议的「串行 + 最小间隔 + 滑动窗口」。
    MIN_INTERVAL: float = 4.0      # 两次生成请求的最小间隔（秒）
    PER_MINUTE: int = 8            # 60s 滑动窗口内的生成请求上限
    MAX_WAIT: float = 20.0         # 排队可等的上限（秒）；超过直接 429（带 Retry-After）
    COOLDOWN: float = 600.0        # 命中昆仑后的静默窗（秒）——窗内零上游请求
    # 出口代理池（逗号分隔的 http(s)/socks5h 代理 URL）。**空 = 直连**。
    # 配置后**每个上游请求新建连接**并轮换到下一个入口 —— 轮换的保证来自
    # 「每请求一个新连接」，不是代理凭据（池通常按 TCP 连接轮换出口）。
    # 📌 依据（2026-09-11/24 归因）：文心的门禁是**出口（IP）维度的风险评分** ——
    # 换到未滥用出口立即恢复；滥用一轮会把整片出口覆盖。这是本项目**唯一被证明有效**
    # 的换线手段（身份/cookie 维度已证否；伪造 XFF 未被证明有效）。
    # ⚠️ 由部署方决定是否启用：一旦用它跑量，被覆盖的是整池出口。
    PROXY_POOL: str = ""

    # ------------------------------------------------------------ 老接口兜底（image.baidu.com/aigc）
    # 老接口（百度AI图片助手）与新接口是**两条独立的生成链路**：
    #   · 新：POST chat.baidu.com/aichat/api/conversation（风险评分在 socket 侧，昆仑挑战）
    #   · 老：POST image.baidu.com/aigc/pccreate（表单 + base64 直传）→ GET /aigc/pcquery 轮询
    # 实测（2026-09-24）：老接口匿名可用、type=3（变清晰）/ type=1（去水印）均秒级出图。
    #   `LEGACY` 取值：
    #     off      不启用（默认）
    #     fallback 主链命中**风控 / 上游未产出**时，若该能力有老接口映射 ⇒ 自动改走老接口
    #              （冷却窗内也直接走老接口 —— 这才是兜底的意义）
    #     prefer   直接走老接口（主链演练/故障期用）
    LEGACY: str = "off"
    LEGACY_BASE: str = "https://image.baidu.com"
    # 老接口轮询：首查通常在 2s 内已就绪；给足上限（interval × tries ≈ 60s）
    LEGACY_POLL_INTERVAL: float = 1.5
    LEGACY_POLL_TRIES: int = 40

    # ------------------------------------------------------------ 本服务
    API_KEYS: str = ""             # 逗号分隔静态白名单；空 = 鉴权整体关闭（仅限内网）
    PORT: int = 8700
    LOG_LEVEL: str = "INFO"
    MEDIA_DIR: str = "var/media"   # response_format=url 时的落盘目录
    # 未取证能力闸门：dewatermark / erase / replace / bgreplace / redraw / similar 默认不可用
    #（前四个与 bgreplace 走老接口兜底，redraw/similar 为 legacy-only；见 app/models.py）。
    ALLOW_UNVERIFIED: bool = False

    # ------------------------------------------------------------ 可观测性（可选，可静默降级）
    LOGFIRE_TOKEN: str = ""
    OTEL_SERVICE_NAME: str = "baidu-service"
    LOGFIRE_ENVIRONMENT: str = ""
    OTEL_CAPTURE_UPSTREAM: bool = True
    OTEL_SCRUBBING: bool = False

    @field_validator("UPLOAD_MODE", "PROMPT_MODE", "LEGACY")
    @classmethod
    def _lower_mode(cls, v: str) -> str:
        return v.strip().lower()

    @property
    def ready(self) -> bool:
        """就绪判据 = **只有 cookie 必需**（token/lid 可现取，见 docs/UPSTREAM.md §4）。"""
        return bool(self.COOKIE)


_settings: Settings | None = None


def get_settings() -> Settings:
    """取全局 Settings（进程内单例）。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """丢弃缓存实例（测试用）。"""
    global _settings
    _settings = None
