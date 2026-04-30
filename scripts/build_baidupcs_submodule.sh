#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SUBMODULE_DIR="$REPO_ROOT/vendor/BaiduPCS-Py"
CIPHER_DIR="$SUBMODULE_DIR/baidupcs_py/common"
PYX_FILE="$CIPHER_DIR/simple_cipher.pyx"

if [[ ! -d "$SUBMODULE_DIR/baidupcs_py" ]]; then
  echo "BaiduPCS-Py 子模块不存在，请先执行: git submodule update --init --recursive" >&2
  exit 1
fi

needs_build=1
for so_file in "$CIPHER_DIR"/simple_cipher*.so; do
  if [[ -e "$so_file" && "$so_file" -nt "$PYX_FILE" ]]; then
    needs_build=0
    break
  fi
done

if [[ "$needs_build" -eq 1 ]]; then
  echo "构建 BaiduPCS-Py Cython 扩展..."
  (cd "$SUBMODULE_DIR" && python build.py build_ext --inplace)
fi
