"""三个包装脚本共用的工具：配置、运行目录、子进程、git。"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 退出码约定（三个脚本一致）
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_WORKSPACE_MODIFIED = 3
EXIT_ESCALATE = 4
EXIT_BLOCKING = 5
EXIT_TIMEOUT = 124


class UsageError(Exception):
    pass


def load_config() -> dict:
    path = Path(os.environ.get("MULTI_MODEL_TEAM_CONFIG", ROOT / "config" / "models.toml"))
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    paths = cfg.setdefault("paths", {})
    if "runs_root" in paths:
        paths["runs_root"] = os.path.expanduser(paths["runs_root"])
    for key in ("codex", "agy"):
        paths[key] = resolve_executable(paths.get(key), key)
    return cfg


def resolve_executable(configured: str | None, name: str) -> str:
    """配置里的路径能执行就用它；否则（没配、路径不存在、只写了命令名）回退到 PATH 里找同名命令。
    都找不到时原样返回配置值（或命令名），让后续调用报出清楚的“找不到”错误。"""
    if configured:
        expanded = os.path.expanduser(configured)
        if os.sep in expanded and os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        found = shutil.which(os.path.basename(expanded) or name)
        if found:
            return found
        return expanded
    return shutil.which(name) or name


def prompt_text(name: str) -> str:
    return (ROOT / "prompts" / name).read_text(encoding="utf-8")


def parse_duration(text: str) -> int:
    """'90s' / '15m' / '1h' / '300' -> 秒。"""
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", str(text))
    if not m:
        raise UsageError(f"无法识别的时长：{text}（示例：90s、15m、1h）")
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def read_task(arg: str) -> str:
    task = sys.stdin.read() if arg == "-" else arg
    if not task.strip():
        raise UsageError("任务内容为空")
    return task


def project_name(path: Path) -> str:
    """git 仓库（含工作台）取主仓库名，否则取目录名。"""
    res = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path, capture_output=True, text=True
    )
    base = Path(res.stdout.strip()).parent if res.returncode == 0 else path
    return re.sub(r"[^A-Za-z0-9._-]+", "-", base.name) or "project"


def make_run_dir(cfg: dict, kind: str, run_dir: str | None, target: Path) -> Path:
    if run_dir:
        path = Path(run_dir).expanduser().resolve()
    else:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path(cfg["paths"]["runs_root"]) / f"{project_name(target)}-{stamp}-{kind}-{os.getpid()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass
class RunResult:
    returncode: int | None  # None 表示超时被杀
    timed_out: bool


def run_logged(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
    stdin_text: str | None = None,
    env: dict | None = None,
) -> RunResult:
    """运行子进程，输出落盘；超时则杀掉整个进程组。stdin 不给内容时接 /dev/null，防止子进程等输入。"""
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=out,
            stderr=err,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        try:
            proc.communicate(input=stdin_text.encode("utf-8") if stdin_text is not None else None, timeout=timeout)
            return RunResult(proc.returncode, False)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            return RunResult(None, True)


# ---------- git ----------


def git(cwd: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{res.stderr.strip()}")
    return res.stdout


def is_git_repo(path: Path) -> bool:
    res = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=path, capture_output=True, text=True)
    return res.returncode == 0 and res.stdout.strip() == "true"


def git_head(path: Path) -> str | None:
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None


def git_status_entries(path: Path) -> list[str]:
    return [line for line in git(path, "status", "--porcelain=v1", "-uall").splitlines() if line]


# ---------- 目录指纹：检测“只读”调用后目录有没有被改 ----------

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".cache"}
_MAX_FILES = 50000


def tree_fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            try:
                st = p.lstat()
            except FileNotFoundError:
                continue
            h.update(f"{p.relative_to(root)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
            count += 1
            if count >= _MAX_FILES:
                return h.hexdigest()
    if (root / ".git").exists() and is_git_repo(root):
        h.update((git_head(root) or "").encode())
    return h.hexdigest()


def emit(report: str, result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(report.rstrip())
