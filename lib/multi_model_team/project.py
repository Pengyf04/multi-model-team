"""项目级配置：<仓库>/.multi-model-team/project.toml（随项目提交）。

示例：

    [checks]
    commands = ["npm run typecheck", "npm test"]
    timeout = "10m"

    [[guards]]
    name = "money-no-float"
    pattern = 'parseFloat\\('
    files = ["src/**/*.ts"]
    exclude = ["src/legacy/**"]
    message = "金额一律用整数分，改用 toCents()"
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

CONFIG_DIR = ".multi-model-team"
CONFIG_FILE = "project.toml"
BASELINE_FILE = "guard-baseline.json"


class ProjectConfigError(Exception):
    pass


def config_path(repo: Path) -> Path:
    return repo / CONFIG_DIR / CONFIG_FILE


def baseline_path(repo: Path) -> Path:
    return repo / CONFIG_DIR / BASELINE_FILE


def load(repo: Path) -> dict:
    path = config_path(repo)
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ProjectConfigError(f"{path} 格式错误：{e}") from e


def glob_to_regex(pattern: str) -> re.Pattern:
    """把 src/**/*.ts 这类写法转成正则（** 跨目录，* 不跨目录）。"""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")
