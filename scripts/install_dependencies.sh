#!/usr/bin/env bash
set -euo pipefail

mode="${1:-runtime}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
constraints_file="$repo_root/constraints.txt"

case "$mode" in
  runtime)
    pip_args=(-r "$repo_root/requirements.txt" -c "$constraints_file")
    ;;
  test)
    pip_args=(-r "$repo_root/requirements.txt" -r "$repo_root/requirements-test.txt" -c "$constraints_file")
    ;;
  quality)
    pip_args=(-r "$repo_root/requirements.txt" -r "$repo_root/requirements-test.txt" -r "$repo_root/requirements-quality.txt" -c "$constraints_file")
    ;;
  *)
    echo "Usage: $0 {runtime|test|quality}" >&2
    exit 2
    ;;
esac

pip_pin="$(python - "$constraints_file" <<'PY'
from pathlib import Path
import sys

for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw_line.split("#", 1)[0].strip()
    if line.lower().startswith("pip=="):
        print(line)
        break
PY
)"
if [ -z "$pip_pin" ]; then
  echo "constraints.txt must pin pip" >&2
  exit 1
fi

current_pip_version="$(python - <<'PY'
from importlib import metadata

print(metadata.version("pip"))
PY
)"
if [ "$current_pip_version" != "${pip_pin#pip==}" ]; then
  python -m pip install --upgrade "$pip_pin"
fi

python -m pip install "${pip_args[@]}"
