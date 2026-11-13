#!/usr/bin/env bash
# 音匠 AudioForge 启动脚本（macOS / Linux）
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python
fi
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[x] 未找到 Python 3.10+，请先安装： https://www.python.org/downloads/"
  exit 1
fi

exec "$PY" launcher.py "$@"
