#!/bin/bash
# 本地跑一遍和 CI 相同的检查：ruff、shellcheck、单元测试 + 覆盖率。
# ruff / coverage 优先用已安装的，没有时用 uvx 临时运行。
set -euo pipefail
cd "$(dirname "$0")/.."

run_tool() {  # run_tool <工具> [参数...]
    local tool="$1"; shift
    if command -v "$tool" >/dev/null 2>&1; then "$tool" "$@"
    elif command -v uvx >/dev/null 2>&1; then uvx "$tool" "$@"
    else echo "✗ 找不到 $tool（pip install $tool 或安装 uv）" >&2; return 1
    fi
}

echo "== ruff"
run_tool ruff check .
run_tool ruff format --check .

echo "== shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck scripts/*.sh
else
    echo "  ⚠ 未安装 shellcheck，跳过"
fi

echo "== unittest + coverage"
run_tool coverage erase
run_tool coverage run -m unittest discover tests
run_tool coverage combine -q
run_tool coverage report
run_tool coverage erase
