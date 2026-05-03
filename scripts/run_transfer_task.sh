#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export PYTHONUNBUFFERED=1
export PYTHONPATH="$REPO_ROOT/vendor/BaiduPCS-Py${PYTHONPATH:+:$PYTHONPATH}"

"$SCRIPT_DIR/build_baidupcs_submodule.sh"
cd "$REPO_ROOT"

if [[ "${1:-}" == "--show-network-info" ]]; then
  echo "网络环境信息:"
  curl --fail --show-error --silent --connect-timeout 5 --max-time 10 https://ipinfo.io/json || echo "无法获取IP信息"
  echo "========================================"
fi

python transfer_runner.py
