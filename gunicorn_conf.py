"""gunicorn 配置。

🔴 目标必须是**工厂调用** `app.main:create_app()`：
写成模块级对象形态（把冒号后写成 `app`）会得到 `Failed to find attribute`，
而单测全绿（用例都直接调 `create_app()`），只有真起来才炸。
tests/test_wiring.py::test_gunicorn_target_uses_factory_call 钉着这条。

⚠️ 默认 WORKERS=1，这是**架构决定**不是保守参数：
本服务有两处于**进程内**的状态 —— 速率闸门（串行 + 最小间隔 + 每分钟上限）
与昆仑风控冷却窗。gunicorn 把 workers 调到 N，这组约束就变成 N 份，
恰好踩在上游风控最敏感的维度上（多份闸门 = 并发冲高）。
吞吐靠 asyncio 并发（等上游 SSE 长耗时时事件循环去干别的），不靠多进程。
timeout 放宽到 300：上游「提线稿」实测 20~25s，外加转存与结果下载；
默认 30s 会把正常请求判成僵死 worker。
"""

from __future__ import annotations

import os

_bind = f"{os.environ.get('BAIDU_HOST', '0.0.0.0')}:{os.environ.get('BAIDU_PORT', '8700')}"
bind = _bind
workers = int(os.environ.get("BAIDU_WORKERS", "1"))
worker_class = "uvicorn_worker.UvicornWorker"
timeout = int(os.environ.get("BAIDU_GUNICORN_TIMEOUT", "300"))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("BAIDU_LOG_LEVEL", "info").lower()

# 供 `gunicorn -c gunicorn_conf.py "app.main:create_app()"` 直接使用；
# 这里把命令也写出来，免得每次从 README 里抄。
CMD = f'gunicorn -c gunicorn_conf.py "app.main:create_app()" -b {_bind} -w {workers}'

__all__ = ["bind", "workers", "worker_class", "timeout", "CMD"]
