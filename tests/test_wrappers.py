"""用假 CLI 测试三个包装脚本：参数、权限、超时、结果判定。运行：python3 -m unittest discover tests"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
FAKES = ROOT / "tests" / "fakes"


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True
    )


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "a.txt").write_text("hello\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        self.log = self.tmp / "calls.jsonl"
        self.runs = self.tmp / "runs"
        cfg = self.tmp / "models.toml"
        cfg.write_text(f'''
[paths]
codex = "{FAKES / "fake_codex"}"
agy = "{FAKES / "fake_agy"}"
runs_root = "{self.runs}"
[agy_ask]
model = "gem-test"
timeout = "30s"
kill_grace = "0s"
[codex_worker]
model = "gpt-test"
effort = "xhigh"
timeout = "30s"
[codex_review]
model = "sol-test"
effort = "high"
deep_effort = "xhigh"
max_rounds = 2
timeout = "30s"
deep_path_keywords = ["auth", "pay"]
''')
        self.env = {**os.environ, "MULTI_MODEL_TEAM_CONFIG": str(cfg), "FAKE_LOG": str(self.log)}

    def run_bin(self, name, *args, env=None, stdin=None):
        res = subprocess.run(
            [str(BIN / name), *args, "--json"],
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            input=stdin,
            timeout=60,
        )
        result = json.loads(res.stdout) if res.stdout.strip().startswith("{") else None
        return res.returncode, result, res

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines()]


class AgyAskTest(Base):
    def test_readonly_command_and_prompt(self):
        code, r, _ = self.run_bin("agy-ask", str(self.repo), "出个方案", "--role", "plan")
        self.assertEqual(code, 0)
        self.assertEqual(r["status"], "SUCCESS")
        argv = self.calls()[0]["argv"]
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertFalse(settings["useG1Credits"])
        self.assertIn("write_file(*)", settings["permissions"]["deny"])
        self.assertIn("command(*)", settings["permissions"]["deny"])
        for flag in ("--sandbox", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gem-test")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("只读", prompt)
        self.assertIn("独立出一份方案", prompt)
        self.assertTrue(prompt.rstrip().endswith("出个方案"))
        self.assertTrue(Path(r["response_file"]).read_text().startswith("回答"))

    def test_task_from_stdin(self):
        code, _, _ = self.run_bin("agy-ask", str(self.repo), "-", stdin="来自标准输入的任务")
        self.assertEqual(code, 0)
        argv = self.calls()[0]["argv"]
        self.assertIn("来自标准输入的任务", argv[argv.index("-p") + 1])

    def test_workspace_modified_is_flagged(self):
        code, r, _ = self.run_bin("agy-ask", str(self.repo), "问题", env={"FAKE_AGY_WRITE": "evil.txt"})
        self.assertEqual(code, 3)
        self.assertEqual(r["status"], "WORKSPACE_MODIFIED")

    def test_isolate_runs_in_copy_and_cleans_up(self):
        code, r, _ = self.run_bin("agy-ask", str(self.repo), "问题", "--isolate")
        self.assertEqual(code, 0, r)
        cwd = self.calls()[0]["cwd"]
        self.assertNotEqual(Path(cwd).resolve(), self.repo.resolve())
        self.assertFalse(Path(cwd).exists(), "临时副本应被删除")
        response = Path(r["response_file"]).read_text()
        self.assertIn(f"见 {self.repo.resolve()}/a.txt", response)
        self.assertNotIn("agy-ask-", response)

    def test_isolate_flags_write_in_copy_but_original_untouched(self):
        code, r, _ = self.run_bin("agy-ask", str(self.repo), "问题", "--isolate", env={"FAKE_AGY_WRITE": "evil.txt"})
        self.assertEqual(code, 3)
        self.assertIn("副本", r["error"])
        self.assertFalse((self.repo / "evil.txt").exists())

    def test_isolate_ignores_concurrent_edits_to_original(self):
        # 总指挥在 Gemini 运行期间改原目录，不应被算到 Gemini 头上
        code, r, _ = self.run_bin(
            "agy-ask", str(self.repo), "问题", "--isolate", env={"FAKE_AGY_TOUCH": str(self.repo / "a.txt")}
        )
        self.assertEqual(code, 0, r)
        self.assertFalse(r["workspace_modified"])

    def test_without_isolate_concurrent_edit_hint(self):
        code, r, _ = self.run_bin("agy-ask", str(self.repo), "问题", env={"FAKE_AGY_TOUCH": str(self.repo / "a.txt")})
        self.assertEqual(code, 3)
        self.assertIn("--isolate", r["error"])

    def test_resume_passes_conversation_and_raw_task(self):
        self.run_bin("agy-ask", str(self.repo), "追问", "--resume", "conv-9")
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-9")
        self.assertEqual(argv[argv.index("-p") + 1], "追问")

    def test_agy_error(self):
        code, r, _ = self.run_bin("agy-ask", str(self.repo), "问题", env={"FAKE_AGY_STATUS": "ERROR"})
        self.assertEqual(code, 1)
        self.assertEqual(r["error"], "boom")

    def test_timeout_kills(self):
        code, r, _ = self.run_bin("agy-ask", str(self.repo), "问题", "--timeout", "1s", env={"FAKE_SLEEP": "20"})
        self.assertEqual(code, 124)
        self.assertEqual(r["status"], "TIMEOUT")

    def test_missing_dir(self):
        code, _, res = self.run_bin("agy-ask", str(self.tmp / "nope"), "问题")
        self.assertEqual(code, 2)
        self.assertIn("目录不存在", res.stdout)


class CodexWorkerTest(Base):
    def test_refuses_dirty_worktree(self):
        (self.repo / "a.txt").write_text("dirty\n")
        code, _, res = self.run_bin("codex-worker", str(self.repo), "任务")
        self.assertEqual(code, 2)
        self.assertIn("未提交", res.stdout)
        self.assertEqual(self.calls(), [])

    def test_sandboxed_command_and_report(self):
        code, r, _ = self.run_bin(
            "codex-worker",
            str(self.repo),
            "写个模块",
            env={"FAKE_CODEX_WRITE": "mod.py", "FAKE_CODEX_CMD": "pytest -q"},
        )
        self.assertEqual(code, 0, r)
        call = self.calls()[0]
        argv = call["argv"]
        self.assertEqual(argv[:2], ["exec", "--ignore-user-config"])
        self.assertIn('sandbox_mode="workspace-write"', argv)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)
        self.assertIn('model_reasoning_effort="xhigh"', argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-test")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[-1], "-")
        self.assertIn("不 git commit", call["stdin"])
        self.assertTrue(call["stdin"].rstrip().endswith("写个模块"))
        self.assertEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        self.assertEqual(r["thread_id"], "th-1")
        self.assertIn("?? mod.py", r["changed_files"])
        self.assertEqual(r["commands"], [{"command": "pytest -q", "exit_code": 0}])
        self.assertEqual(r["warnings"], [])

    def test_warns_when_no_command_ran(self):
        _, r, _ = self.run_bin("codex-worker", str(self.repo), "任务")
        self.assertTrue(any("没有执行任何命令" in w for w in r["warnings"]))

    def test_warns_on_failed_command_and_commit(self):
        _, r, _ = self.run_bin(
            "codex-worker",
            str(self.repo),
            "任务",
            env={
                "FAKE_CODEX_WRITE": "x.py",
                "FAKE_CODEX_COMMIT": "1",
                "FAKE_CODEX_CMD": "pytest",
                "FAKE_CODEX_CMD_EXIT": "1",
            },
        )
        text = " ".join(r["warnings"])
        self.assertIn("提交历史", text)
        self.assertIn("非零退出码", text)
        self.assertIn("x.py", Path(r["diff_file"]).read_text())

    def test_resume_allows_dirty_and_uses_resume(self):
        (self.repo / "a.txt").write_text("dirty\n")
        code, r, _ = self.run_bin("codex-worker", str(self.repo), "追加", "--resume", "th-7")
        self.assertEqual(code, 0, r)
        call = self.calls()[0]
        self.assertEqual(call["argv"][:3], ["exec", "resume", "--ignore-user-config"])
        self.assertEqual(call["argv"][-2:], ["th-7", "-"])
        self.assertIn("继续之前的任务", call["stdin"])
        self.assertEqual(r["thread_id"], "th-7")

    def test_timeout(self):
        code, r, _ = self.run_bin("codex-worker", str(self.repo), "任务", "--timeout", "1s", env={"FAKE_SLEEP": "20"})
        self.assertEqual(code, 124)
        self.assertEqual(r["status"], "TIMEOUT")

    def test_not_a_repo(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        code, _, _ = self.run_bin("codex-worker", str(plain), "任务")
        self.assertEqual(code, 2)


BLOCKING = json.dumps(
    {
        "summary": "有严重问题",
        "findings": [
            {
                "severity": "blocking",
                "title": "金额算错",
                "file": "pay.py",
                "line": 3,
                "how_it_happens": "折扣为 0 时除以零",
                "suggestion": "判空",
            },
            {
                "severity": "advisory",
                "title": "命名",
                "file": "a.txt",
                "line": None,
                "how_it_happens": "",
                "suggestion": "改名",
            },
        ],
    }
)


class CodexReviewTest(Base):
    def change(self, name="a.txt"):
        (self.repo / name).write_text("changed\n")

    def test_no_changes_skips_codex(self):
        code, r, _ = self.run_bin("codex-review", str(self.repo))
        self.assertEqual(code, 0)
        self.assertEqual(r["status"], "PASS")
        self.assertEqual(self.calls(), [])

    def test_blocking_exit_code_and_readonly(self):
        self.change()
        code, r, _ = self.run_bin("codex-review", str(self.repo), env={"FAKE_REVIEW_JSON": BLOCKING})
        self.assertEqual(code, 5)
        self.assertEqual((r["status"], r["blocking"], r["advisory"]), ("BLOCKED", 1, 1))
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[:2], ["exec", "--ignore-user-config"])
        self.assertNotIn("review", argv)
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertNotIn("--uncommitted", argv)  # 与自定义要求冲突，范围写进要求里
        stdin = self.calls()[0]["stdin"]
        self.assertIn("git diff HEAD", stdin)
        self.assertIn("- a.txt", stdin)
        self.assertIn("--output-schema", argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertIn("金额算错", (Path(r["run_dir"]) / "review.md").read_text())

    def test_blocking_without_trigger_path_is_downgraded(self):
        self.change()
        review = json.dumps(
            {
                "summary": "s",
                "findings": [
                    {
                        "severity": "blocking",
                        "title": "可能有问题",
                        "file": "a.txt",
                        "line": 1,
                        "how_it_happens": " ",
                        "suggestion": "看看",
                    }
                ],
            }
        )
        code, r, _ = self.run_bin("codex-review", str(self.repo), env={"FAKE_REVIEW_JSON": review})
        self.assertEqual(code, 0)
        self.assertEqual(r["findings"][0]["severity"], "advisory")

    def test_fenced_json_is_parsed(self):
        self.change()
        fenced = "```json\n" + json.dumps({"summary": "ok", "findings": []}) + "\n```"
        code, r, _ = self.run_bin("codex-review", str(self.repo), env={"FAKE_REVIEW_JSON": fenced})
        self.assertEqual((code, r["status"]), (0, "PASS"))

    def test_unparseable_review_fails(self):
        self.change()
        code, r, _ = self.run_bin("codex-review", str(self.repo), env={"FAKE_REVIEW_JSON": "不是JSON"})
        self.assertEqual((code, r["status"]), (1, "FAILED"))

    def test_sensitive_path_triggers_deep(self):
        (self.repo / "auth").mkdir()
        (self.repo / "auth" / "login.py").write_text("x\n")
        _, r, _ = self.run_bin("codex-review", str(self.repo))
        self.assertTrue(r["deep"])
        self.assertIn('model_reasoning_effort="xhigh"', self.calls()[0]["argv"])

    def test_round_over_limit_escalates(self):
        self.change()
        code, r, _ = self.run_bin("codex-review", str(self.repo), "--round", "3")
        self.assertEqual((code, r["status"]), (4, "ESCALATE"))
        self.assertEqual(self.calls(), [])

    def test_second_round_includes_previous_blocking(self):
        self.change()
        _, first, _ = self.run_bin("codex-review", str(self.repo), env={"FAKE_REVIEW_JSON": BLOCKING})
        prev = Path(first["run_dir"]) / "result.json"
        self.run_bin("codex-review", str(self.repo), "--round", "2", "--previous", str(prev))
        stdin = self.calls()[1]["stdin"]
        self.assertIn("金额算错", stdin)
        self.assertNotIn("命名", stdin)

    def test_base_branch(self):
        git(self.repo, "checkout", "-qb", "feature")
        self.change("pay.py")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "feat")
        _, r, _ = self.run_bin("codex-review", str(self.repo), "--base", "main")
        self.assertEqual(r["files"], ["pay.py"])
        self.assertIn("git diff main...", self.calls()[0]["stdin"])
        self.assertNotIn("--base", self.calls()[0]["argv"])
        self.assertTrue(r["deep"])


class CodexWorktreeTest(Base):
    def wt_path(self, name):
        return self.tmp / "repo.worktrees" / name

    def test_create_list_remove_merged(self):
        code, r, _ = self.run_bin("codex-worktree", "create", str(self.repo), "export")
        self.assertEqual(code, 0)
        path = self.wt_path("export")
        self.assertEqual(Path(r["path"]).resolve(), path.resolve())
        self.assertEqual(r["branch"], "codex/export")
        self.assertTrue((path / "a.txt").exists())
        _, lst, _ = self.run_bin("codex-worktree", "list", str(self.repo))
        self.assertEqual([x["branch"] for x in lst["worktrees"]], ["codex/export"])
        code, r, _ = self.run_bin("codex-worktree", "remove", str(self.repo), "export")
        self.assertEqual(code, 0)
        self.assertTrue(r["branch_deleted"])  # 没有新提交，算已合并
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists(), "最后一个工作台删掉后不应留空目录")

    def test_remove_keeps_parent_when_others_remain(self):
        self.run_bin("codex-worktree", "create", str(self.repo), "a1")
        self.run_bin("codex-worktree", "create", str(self.repo), "a2")
        self.run_bin("codex-worktree", "remove", str(self.repo), "a1")
        self.assertTrue(self.wt_path("a2").exists())

    def test_create_from_inside_worktree_uses_main_repo(self):
        self.run_bin("codex-worktree", "create", str(self.repo), "one")
        _, r, _ = self.run_bin("codex-worktree", "create", str(self.wt_path("one")), "two")
        self.assertEqual(Path(r["path"]).resolve(), self.wt_path("two").resolve())

    def test_remove_refuses_dirty_and_unmerged_branch_kept(self):
        self.run_bin("codex-worktree", "create", str(self.repo), "job")
        path = self.wt_path("job")
        (path / "new.py").write_text("x\n")
        code, _, res = self.run_bin("codex-worktree", "remove", str(self.repo), "job")
        self.assertEqual(code, 2)
        self.assertIn("未提交", res.stdout)
        git(path, "add", ".")
        git(path, "commit", "-qm", "work")
        code, r, _ = self.run_bin("codex-worktree", "remove", str(self.repo), "job")
        self.assertEqual(code, 0)
        self.assertFalse(r["branch_deleted"])  # 未合并的分支保留

    def test_bad_name_and_duplicate(self):
        code, _, _ = self.run_bin("codex-worktree", "create", str(self.repo), "Bad Name")
        self.assertEqual(code, 2)
        self.run_bin("codex-worktree", "create", str(self.repo), "dup")
        code, _, _ = self.run_bin("codex-worktree", "create", str(self.repo), "dup")
        self.assertEqual(code, 2)


class RunDirTest(Base):
    def test_default_run_dir_named_after_main_repo(self):
        self.run_bin("codex-worktree", "create", str(self.repo), "w1")
        _, r, _ = self.run_bin("codex-worker", str(self.tmp / "repo.worktrees" / "w1"), "任务")
        self.assertTrue(Path(r["run_dir"]).name.startswith("repo-"), r["run_dir"])
        self.assertEqual(Path(r["run_dir"]).parent, self.runs)


if __name__ == "__main__":
    unittest.main()


class ReviewSafetyTest(Base):
    """codex-review 第 1 轮审查挡回问题的回归测试。"""

    def test_stale_result_not_reused_when_codex_fails(self):
        run_dir = self.tmp / "reused-run"
        (self.repo / "a.txt").write_text("changed\n")
        code, r, _ = self.run_bin("codex-review", str(self.repo), "--run-dir", str(run_dir))
        self.assertEqual((code, r["status"]), (0, "PASS"))
        (self.repo / "a.txt").write_text("changed again\n")
        env = {"FAKE_REVIEW_EXIT": "1", "FAKE_REVIEW_NO_WRITE": "1"}
        code, r, _ = self.run_bin("codex-review", str(self.repo), "--run-dir", str(run_dir), env=env)
        self.assertEqual((code, r["status"]), (1, "FAILED"))
        self.assertIsNone(r["pass_record"])

    def test_nonzero_exit_is_not_pass_even_with_output(self):
        (self.repo / "a.txt").write_text("changed\n")
        code, r, _ = self.run_bin("codex-review", str(self.repo), env={"FAKE_REVIEW_EXIT": "2"})
        self.assertEqual((code, r["status"]), (1, "FAILED"))
        self.assertIsNone(r["pass_record"])

    def test_all_with_staged_deletion_lists_head_and_leaves_no_stamp(self):
        git(self.repo, "rm", "-q", "a.txt")
        code, r, _ = self.run_bin("codex-review", str(self.repo), "--all")
        self.assertEqual(code, 0)
        self.assertIn("a.txt", r["files"])
        self.assertIsNone(r["pass_record"], "工作区与 HEAD 不一致时不登记已审")

    def test_all_clean_leaves_stamp(self):
        code, r, _ = self.run_bin("codex-review", str(self.repo), "--all")
        self.assertEqual(code, 0)
        self.assertTrue(r["pass_record"])

    def test_merge_commit_is_reviewed_against_first_parent(self):
        git(self.repo, "switch", "-q", "-c", "side")
        (self.repo / "side.txt").write_text("side\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "side")
        git(self.repo, "switch", "-q", "main")
        (self.repo / "main.txt").write_text("main\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", "side")
        code, r, _ = self.run_bin("codex-review", str(self.repo), "--commit", "HEAD")
        self.assertEqual(code, 0)
        self.assertEqual(r["files"], ["side.txt"])
        self.assertEqual(len(self.calls()), 1, "合并提交必须交给 Codex 审查")
        self.assertIn("第一父提交", self.calls()[0]["stdin"])

    def test_run_dir_is_private(self):
        (self.repo / "a.txt").write_text("changed\n")
        _, r, _ = self.run_bin("codex-review", str(self.repo))
        self.assertEqual(Path(r["run_dir"]).stat().st_mode & 0o777, 0o700)
        reused = self.tmp / "shared-run"
        reused.mkdir(mode=0o755)
        reused.chmod(0o755)
        self.run_bin("codex-review", str(self.repo), "--run-dir", str(reused))
        self.assertEqual(reused.stat().st_mode & 0o777, 0o700)

    def test_foreign_run_dir_is_refused(self):
        foreign = Path("/") if os.getuid() != 0 else None
        if foreign is None:
            self.skipTest("root 拥有所有目录")
        (self.repo / "a.txt").write_text("changed\n")
        code, _, res = self.run_bin("codex-review", str(self.repo), "--run-dir", str(foreign))
        self.assertEqual(code, 2)
        self.assertIn("不属于当前用户", res.stdout)
        self.assertEqual(self.calls(), [])

    def test_committed_mode_does_not_write_worktree_objects(self):
        (self.repo / "secret.txt").write_text("local-only-secret-xyz\n")
        blob = subprocess.run(
            ["git", "hash-object", "secret.txt"], cwd=self.repo, capture_output=True, text=True
        ).stdout.strip()
        self.run_bin("codex-review", str(self.repo), "--all")
        res = subprocess.run(["git", "cat-file", "-e", blob], cwd=self.repo, capture_output=True)
        self.assertNotEqual(res.returncode, 0, "未提交的文件内容不应写进对象库")


class IsolateSubmoduleTest(Base):
    def test_isolate_copies_checked_out_submodule(self):
        sub = self.tmp / "sub"
        sub.mkdir()
        (sub / "lib.txt").write_text("sub\n")
        git(sub, "init", "-q", "-b", "main")
        git(sub, "add", ".")
        git(sub, "commit", "-qm", "sub")
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub")
        git(self.repo, "commit", "-qm", "add submodule")
        code, r, res = self.run_bin("agy-ask", str(self.repo), "问题", "--isolate")
        self.assertEqual(code, 0, res.stdout + res.stderr)
        self.assertFalse(Path(self.calls()[0]["cwd"]).exists(), "临时副本应被删除")

    def test_isolate_skips_files_ignored_by_submodule(self):
        sub = self.tmp / "sub2"
        sub.mkdir()
        (sub / "lib.txt").write_text("sub\n")
        (sub / ".gitignore").write_text(".env\n")
        git(sub, "init", "-q", "-b", "main")
        git(sub, "add", ".")
        git(sub, "commit", "-qm", "sub")
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub")
        git(self.repo, "commit", "-qm", "add submodule")
        (self.repo / "vendor/sub/.env").write_text("TOKEN=secret\n")
        (self.repo / "vendor/sub/new.txt").write_text("untracked ok\n")
        env = {"FAKE_AGY_LIST": "1"}
        code, r, res = self.run_bin("agy-ask", str(self.repo), "问题", "--isolate", env=env)
        self.assertEqual(code, 0, res.stdout + res.stderr)
        listed = self.calls()[0].get("files", [])
        self.assertIn("vendor/sub/lib.txt", listed)
        self.assertIn("vendor/sub/new.txt", listed)
        self.assertNotIn("vendor/sub/.env", listed, "被子模块忽略的凭据文件不能进入副本")
