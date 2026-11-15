#!/bin/sh
# 模拟 PACS 启动前准备：数据卷为空时生成虚构合成 CT，随后降权运行
set -e
DATA_DIR="${PACS_DATA_DIR:-/data/pacs}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R pacsuser:pacsuser "$(dirname "$DATA_DIR")"
    exec gosu pacsuser "$0" "$@"
fi

if [ ! -f "$DATA_DIR/studies.json" ]; then
    echo "[pacs-init] 数据卷为空，生成虚构合成 CT ..."
    python -m simulator.generate_data --out "$DATA_DIR" --counts "${STUDY_COUNTS:-8,6,5}"
else
    echo "[pacs-init] 复用已有数据：$DATA_DIR/studies.json"
fi
exec python -m simulator.pacs
