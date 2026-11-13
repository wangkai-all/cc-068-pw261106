#!/usr/bin/env bash
# 便捷启动本地工时整理服务（无第三方依赖）。
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8765}"
echo "启动本地工时整理服务: http://127.0.0.1:${PORT}"
exec python3 -m backend.server --port "${PORT}"
