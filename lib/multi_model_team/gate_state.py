"""推送前关卡的本地状态：放在 <git 公共目录>/multi-model-team/，不进仓库。

- review-pass/<tree>.json：这份代码（以 git tree 标识）审查通过的记录，含审查范围的起点 base。
- rounds/<分支>.json：当前分支被挡的轮数和上一轮结果，供下次推送做第二轮复审。
- gate.log：每次放行、拦截的记录。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import common as c

EMPTY_BASE = "EMPTY"  # 审查范围是整个仓库


def state_dir(repo: Path) -> Path:
    common_dir = Path(c.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    d = common_dir / "multi-model-team"
    d.mkdir(parents=True, exist_ok=True)
    return d


def head_tree(repo: Path) -> str | None:
    res = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None


def worktree_tree(repo: Path) -> str:
    """工作区当前内容（含未跟踪、不含忽略的文件）对应的 tree，不改动真正的暂存区。"""
    index = Path(c.git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
    with tempfile.TemporaryDirectory() as d:
        tmp_index = Path(d) / "index"
        if index.exists():
            shutil.copy(index, tmp_index)
        env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index)}
        subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True, capture_output=True)
        return subprocess.run(
            ["git", "write-tree"], cwd=repo, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()


def record_pass(repo: Path, tree: str, base: str, result_file: str) -> Path:
    d = state_dir(repo) / "review-pass"
    d.mkdir(exist_ok=True)
    path = d / f"{tree}.json"
    c.write_json(
        path,
        {"tree": tree, "base": base, "result": result_file, "time": _dt.datetime.now().isoformat(timespec="seconds")},
    )
    return path


def find_pass(repo: Path, tree: str, push_base: str | None) -> dict | None:
    """这份代码审过且审查范围覆盖本次推送（审查起点不晚于推送起点）时返回记录。"""
    path = state_dir(repo) / "review-pass" / f"{tree}.json"
    if not path.exists():
        return None
    rec = json.loads(path.read_text(encoding="utf-8"))
    if rec["base"] == EMPTY_BASE:
        return rec
    if push_base is None:  # 本次要审整个仓库，而记录只审了一部分
        return None
    ok = (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", rec["base"], push_base], cwd=repo, capture_output=True
        ).returncode
        == 0
    )
    return rec if ok else None


def _branch_file(repo: Path, branch: str) -> Path:
    d = state_dir(repo) / "rounds"
    d.mkdir(exist_ok=True)
    return d / (re.sub(r"[^A-Za-z0-9._-]+", "_", branch) + ".json")


def load_rounds(repo: Path, branch: str) -> dict | None:
    p = _branch_file(repo, branch)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_rounds(repo: Path, branch: str, data: dict) -> None:
    c.write_json(_branch_file(repo, branch), data)


def clear_rounds(repo: Path, branch: str | None = None) -> int:
    d = state_dir(repo) / "rounds"
    if not d.exists():
        return 0
    targets = [_branch_file(repo, branch)] if branch else list(d.glob("*.json"))
    n = 0
    for p in targets:
        if p.exists():
            p.unlink()
            n += 1
    return n


def log(repo: Path, message: str) -> None:
    with open(state_dir(repo) / "gate.log", "a", encoding="utf-8") as f:
        f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')}  {message}\n")
