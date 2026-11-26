"""codex-worktree：给 Codex 建、列、删独立工作台（git worktree + 独立分支）。

工作台放在 <主仓库所在目录>/<仓库名>.worktrees/<名字>，分支为 codex/<名字>。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from . import common as c

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codex-worktree", description="给 Codex 建、列、删独立工作台。")
    sub = p.add_subparsers(dest="action", required=True)

    cr = sub.add_parser("create", help="从当前提交建一个新工作台和分支 codex/<名字>")
    cr.add_argument("repo", help="主仓库目录")
    cr.add_argument("name", help="工作台名字：小写字母、数字、短横线")
    cr.add_argument("--base", default="HEAD", help="从哪个提交或分支建（默认当前 HEAD）")
    cr.add_argument("--json", action="store_true")

    rm = sub.add_parser("remove", help="删除工作台；分支已合并才删分支，除非 --force")
    rm.add_argument("repo")
    rm.add_argument("name")
    rm.add_argument("--force", action="store_true", help="工作台有未提交改动或分支未合并时也强制删除")
    rm.add_argument("--json", action="store_true")

    ls = sub.add_parser("list", help="列出这个仓库的 Codex 工作台")
    ls.add_argument("repo")
    ls.add_argument("--json", action="store_true")
    return p


def main_repo_root(repo: Path) -> Path:
    """即使传入的是某个工作台，也返回主仓库目录。"""
    common_dir = Path(c.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    return common_dir.parent


def worktree_path(root: Path, name: str) -> Path:
    return root.parent / f"{root.name}.worktrees" / name


def ignored_files(path: Path) -> list[str]:
    """工作台里被 git 忽略、删工作台时会一起丢掉的文件。依赖和缓存目录（node_modules、.venv 等）能重装，不算。"""
    out = c.git(path, "status", "--porcelain=v1", "--ignored=matching", "--untracked-files=normal")
    found = []
    for line in out.splitlines():
        if not line.startswith("!! "):
            continue
        rel = line[3:]
        if rel.rstrip("/").split("/")[0] in c._SKIP_DIRS:
            continue
        found.append(rel)
    return found


def list_worktrees(root: Path) -> list[dict]:
    items, cur = [], {}
    for line in c.git(root, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur.get("branch", "").startswith("refs/heads/codex/"):
                items.append({"path": cur["worktree"], "branch": cur["branch"].removeprefix("refs/heads/")})
            cur = {}
        elif " " in line:
            k, v = line.split(" ", 1)
            cur[k] = v
    return items


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir() or not c.is_git_repo(repo):
        print(f"codex-worktree：不是 git 仓库：{repo}")
        return c.EXIT_USAGE
    root = main_repo_root(repo)

    if args.action == "list":
        items = list_worktrees(root)
        report = "\n".join(f"- {x['branch']}　{x['path']}" for x in items) or "（没有 Codex 工作台）"
        c.emit(report, {"worktrees": items}, args.json)
        return c.EXIT_OK

    if not NAME_RE.match(args.name):
        print("codex-worktree：名字只能用小写字母、数字和短横线，且以字母或数字开头")
        return c.EXIT_USAGE
    path, branch = worktree_path(root, args.name), f"codex/{args.name}"

    if args.action == "create":
        if path.exists():
            print(f"codex-worktree：工作台已存在：{path}")
            return c.EXIT_USAGE
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            c.git(root, "worktree", "add", "-b", branch, str(path), args.base)
        except RuntimeError as e:
            print(f"codex-worktree：{e}")
            return c.EXIT_FAILED
        result = {
            "status": "CREATED",
            "path": str(path),
            "branch": branch,
            "base": c.git(path, "rev-parse", "HEAD").strip(),
        }
        report = (
            f"# 已建工作台\n\n- 路径：{path}\n- 分支：{branch}\n- 起点提交：{result['base']}\n\n"
            "注意：工作台里没有依赖（node_modules、虚拟环境等），派活前先在这里装好依赖；"
            "未提交的改动不会带过来。"
        )
        c.emit(report, result, args.json)
        return c.EXIT_OK

    # remove
    if not path.exists():
        print(f"codex-worktree：工作台不存在：{path}")
        return c.EXIT_USAGE
    if not args.force and c.git_status_entries(path):
        print("codex-worktree：工作台里还有未提交的改动，确认不要了再加 --force")
        return c.EXIT_USAGE
    ignored = ignored_files(path)
    if not args.force and ignored:
        print(
            "codex-worktree：工作台里有被 git 忽略的文件，删除后无法从 git 恢复，确认不要了再加 --force：\n  "
            + "\n  ".join(ignored[:10])
        )
        return c.EXIT_USAGE
    try:
        c.git(root, "worktree", "remove", *(["--force"] if args.force else []), str(path))
    except RuntimeError as e:
        print(f"codex-worktree：{e}")
        return c.EXIT_FAILED
    deleted = c.git(root, "branch", "-D" if args.force else "-d", branch, check=False)
    branch_deleted = bool(deleted.strip())
    try:
        path.parent.rmdir()  # 最后一个工作台删掉后，不留空的 <仓库名>.worktrees 目录
    except OSError:
        pass
    result = {"status": "REMOVED", "path": str(path), "branch": branch, "branch_deleted": branch_deleted}
    report = f"# 已删除工作台\n\n- 路径：{path}\n- 分支 {branch}：" + (
        "已删除" if branch_deleted else "保留（还没合并进当前分支；确认不要了用 --force）"
    )
    c.emit(report, result, args.json)
    return c.EXIT_OK
