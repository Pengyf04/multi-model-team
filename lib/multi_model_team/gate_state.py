"""推送前关卡的本地状态：放在 <git 公共目录>/multi-model-team/，不进仓库。

- review-pass/<tree>.json：这份代码（以 git tree 标识）审查通过的记录，含审查范围的起点 base。
- rounds/<远端>_<目标分支>.json：该推送目标被挡的轮数、上一轮结果和审查基准；基准变了就从第 1 轮重审。
- gate.log：每次放行、拦截的记录。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
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


def commit_tree(repo: Path, commit: str) -> str | None:
    res = subprocess.run(["git", "rev-parse", f"{commit}^{{tree}}"], cwd=repo, capture_output=True, text=True)
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
    """这份代码审过、且审查范围正好覆盖本次推送时返回记录。

    全量审查记录覆盖任何推送；差异审查记录只在审查起点与推送起点是同一提交时复用。
    只凭"审查起点是推送起点的祖先"不够：中间可能有提交修复后又被回退，净差异里看不到。"""
    path = state_dir(repo) / "review-pass" / f"{tree}.json"
    if not path.exists():
        return None
    rec = json.loads(path.read_text(encoding="utf-8"))
    if rec["base"] == EMPTY_BASE:
        return rec
    return rec if push_base is not None and rec["base"] == push_base else None


def revoke_pass(repo: Path, tree: str) -> bool:
    """同一份代码后来被判定有挡上线问题：撤销它的审查通过记录，推送时不能再复用。"""
    path = state_dir(repo) / "review-pass" / f"{tree}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def _branch_file(repo: Path, branch: str) -> Path:
    """branch 是推送目标键（远端地址 + 完整目标引用）。文件名带哈希，避免 release/api 与 release_api 撞名。"""
    d = state_dir(repo) / "rounds"
    d.mkdir(exist_ok=True)
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", branch)[-60:]
    digest = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:16]
    return d / f"{readable}-{digest}.json"


def load_rounds(repo: Path, branch: str) -> dict | None:
    p = _branch_file(repo, branch)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_rounds(repo: Path, branch: str, data: dict) -> None:
    c.write_json(_branch_file(repo, branch), {**data, "target": branch})


def clear_rounds(repo: Path, branch: str | None = None) -> int:
    d = state_dir(repo) / "rounds"
    if not d.exists():
        return 0
    targets = list(d.glob("*.json"))
    if branch:
        exact = _branch_file(repo, branch)

        def wanted(p: Path) -> bool:
            if p == exact:
                return True
            try:
                target = json.loads(p.read_text(encoding="utf-8")).get("target", "")
            except (OSError, json.JSONDecodeError):
                return False
            return target == branch or target.endswith(f" refs/heads/{branch}")

        targets = [p for p in targets if wanted(p)]
    n = 0
    for p in targets:
        if p.exists():
            p.unlink()
            n += 1
    return n


def log(repo: Path, message: str) -> None:
    with open(state_dir(repo) / "gate.log", "a", encoding="utf-8") as f:
        f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')}  {message}\n")
