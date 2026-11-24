#!/bin/bash
# multi-model-team 安装：命令软链到 ~/.local/bin，skill 软链到 Claude 的 skills 目录。
# 可重复运行；已存在的同名文件如果不是本项目的软链，会停下不覆盖。
# 额外的 skills 目录（比如另一个 Claude 配置目录）用 --skills-dir 追加，可多次指定。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$HOME/.local/bin"
SKILLS_DIRS=("$HOME/.claude/skills")

while [ "$#" -gt 0 ]; do
    case "$1" in
        --skills-dir) SKILLS_DIRS+=("$2"); shift 2 ;;
        -h|--help) echo "用法：install.sh [--skills-dir 目录]..."; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

link() {  # link <源> <目标>
    local src="$1" dst="$2"
    if [ -L "$dst" ]; then
        if [ "$(readlink "$dst")" = "$src" ]; then echo "  已是最新：$dst"; return; fi
        case "$(readlink "$dst")" in
            "$ROOT"/*) rm "$dst" ;;
            *) echo "  ✗ $dst 已链接到别处（$(readlink "$dst")），未覆盖" >&2; return 1 ;;
        esac
    elif [ -e "$dst" ]; then
        echo "  ✗ $dst 已存在且不是软链，未覆盖" >&2; return 1
    fi
    ln -s "$src" "$dst"
    echo "  ✓ $dst -> $src"
}

echo "命令 → $BIN_DIR"
mkdir -p "$BIN_DIR"
for cmd in agy-ask codex-worker codex-review codex-worktree review-gate guard-check; do
    chmod +x "$ROOT/bin/$cmd"
    link "$ROOT/bin/$cmd" "$BIN_DIR/$cmd"
done

for dir in "${SKILLS_DIRS[@]}"; do
    echo "skill → $dir"
    mkdir -p "$dir"
    link "$ROOT/skill/multi-model-team" "$dir/multi-model-team"
done

mkdir -p "$HOME/.multi-model-team/runs"

echo "检查依赖"
case ":$PATH:" in *":$BIN_DIR:"*) echo "  ✓ $BIN_DIR 在 PATH 中" ;; *) echo "  ⚠ $BIN_DIR 不在 PATH 中，请加入" ;; esac
python3 -c 'import sys; assert sys.version_info >= (3, 11)' && echo "  ✓ python3 $(python3 -V | cut -d' ' -f2)" || echo "  ✗ 需要 Python 3.11+"
for tool in agy codex; do
    p=$(python3 -c "import sys; sys.path.insert(0, '$ROOT/lib'); from multi_model_team.common import load_config; print(load_config()['paths']['$tool'])")
    [ -x "$p" ] && echo "  ✓ $tool：$p" || echo "  ✗ 找不到 $tool：$p（改 config/models.toml 的 [paths]）"
done
echo "完成。新开一个 Claude Code 会话后即可使用 /multi-model-team"
