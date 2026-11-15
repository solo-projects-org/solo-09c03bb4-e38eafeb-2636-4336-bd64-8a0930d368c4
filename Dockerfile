# DICOM CT 取数流水线 API 镜像
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# gosu 用于入口脚本降权；非 root 运行应用
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY simulator ./simulator
COPY demo ./demo
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh && chown -R appuser:appuser /srv

# 入口以 root 处理卷属主后通过 gosu 降权为 appuser
USER root

EXPOSE 8000 11113
ENTRYPOINT ["./entrypoint.sh"]
