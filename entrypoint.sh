#!/bin/bash
# API 服务入口：
# 1) root 阶段确保数据卷归属 appuser（兼容 Docker 命名卷的 root 默认属主）
# 2) 降权为 appuser 启动 uvicorn（FastAPI + Swagger + C-STORE SCP 同进程）
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/data}"
    chown -R appuser:appuser "${DATA_DIR:-/data}"
    echo "[entrypoint] 数据卷 ${DATA_DIR:-/data} 属主已置为 appuser，降权启动"
    exec gosu appuser "$0" "$@"
fi

echo "[entrypoint] 以 $(id -un) 启动，data dir: ${DATA_DIR:-/data}"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
