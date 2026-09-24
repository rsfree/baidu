FROM python:3.13-slim

# 版本号由发版 workflow 注入（--build-arg APP_VERSION=<version>）；
# 镜像 label 与 /healthz 的自报版本必须一致（CI 冒烟会断言）。
ARG APP_VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="baidu" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.source="https://github.com/rsfree/baidu"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY gunicorn_conf.py ./

# 非 root 运行。MEDIA_DIR 默认 var/media：目录要在镜像里就属于运行用户，
# 否则调用方要 url 时保存结果会失败（那是**部署问题**，不该等运行期才发现）。
RUN useradd --create-home --uid 10001 baidu \
    && mkdir -p /srv/var/media \
    && chown -R baidu:baidu /srv
USER baidu

EXPOSE 8700

# 🔴 目标必须是工厂调用形态（括号不能省）：
# 写成模块级对象会 `Failed to find attribute`，而单测全绿。
# ⚠️ 只跑 1 个 worker：速率闸门与昆仑冷却窗都是进程内状态（见 gunicorn_conf.py）。
CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:create_app()", "-b", "0.0.0.0:8700", "-w", "1"]
