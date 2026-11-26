"""Codex 第 3 次推送审查挡回问题的回归测试：推送快照、复审范围、审查缓存、凭据脱敏、隔离副本、diff 与守卫计数。"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from test_gate import PASS_JSON, GateBase
from test_wrappers import BLOCKING, ROOT, Base, git

sys.path.insert(0, str(ROOT / "lib"))
from multi_model_team import common, gate_state  # noqa: E402


def sha(repo, rev="HEAD"):
    return subprocess.run(["git", "rev-parse", rev], cwd=repo, capture_output=True, text=True).stdout.strip()


class PushSnapshotTest(GateBase):
    def test_checkout_during_checks_blocks_push(self):
        cfg = self.repo / ".multi-model-team"
        cfg.mkdir()
        (cfg / "project.toml").write_text('[checks]\ncommands = ["git checkout -q other"]\n')
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "config")
        git(self.repo, "checkout", "-qb", "other")
        self.commit("other.txt")
        git(self.repo, "checkout", "-q", "main")
        code, out = self.push()
        self.assertNotEqual(code, 0, "自检期间切走了检出，不能审另一个提交后放行原提交")
        self.assertIn("检查期间当前检出的提交变了", out)
        self.assertEqual(self.review_calls(), [])
        self.assertIsNone(self.remote_head())

    def test_codex_review_refuses_unexpected_head(self):
        code, _, res = self.run_bin("codex-review", str(self.repo), "--all", "--expect-head", "0" * 40)
        self.assertEqual(code, 2)
        self.assertIn("不审", res.stdout)
        self.assertEqual(self.review_calls(), [])


class ReviewCacheTest(GateBase):
    def test_blocked_review_revokes_earlier_pass(self):
        self.commit("c1.txt")
        code, r, _ = self.run_bin("codex-review", str(self.repo), "--all", env={"FAKE_REVIEW_JSON": PASS_JSON})
        self.assertEqual((code, bool(r["pass_record"])), (0, True))
        code, r, _ = self.run_bin("codex-review", str(self.repo), "--all", "--deep", env={"FAKE_REVIEW_JSON": BLOCKING})
        self.assertEqual(code, 5)
        n = len(self.review_calls())
        code, out = self.push(review=BLOCKING)
        self.assertNotEqual(code, 0, "最近一次已判阻断，旧的通过记录不能放行")
        self.assertNotIn("不重复审", out)
        self.assertEqual(len(self.review_calls()), n + 1)

    def test_diff_record_only_reused_for_same_base(self):
        a = sha(self.repo)
        self.commit("b.txt")
        b = sha(self.repo)
        self.commit("h.txt")
        tree = gate_state.commit_tree(self.repo, "HEAD")
        gate_state.record_pass(self.repo, tree, a, "r.json")
        self.assertIsNotNone(gate_state.find_pass(self.repo, tree, a))
        self.assertIsNone(gate_state.find_pass(self.repo, tree, b), "审查起点是推送起点的祖先也不能复用")
        self.assertIsNone(gate_state.find_pass(self.repo, tree, None))
        gate_state.record_pass(self.repo, tree, gate_state.EMPTY_BASE, "r.json")
        self.assertIsNotNone(gate_state.find_pass(self.repo, tree, b), "全量审查记录可覆盖任何推送")


class CredentialRedactionTest(GateBase):
    def test_redact_url(self):
        self.assertEqual(common.redact_url("https://user:ghp_tok@github.com/o/r.git"), "https://github.com/o/r.git")
        self.assertEqual(common.redact_url("https://ghp_tok@github.com/o/r.git"), "https://github.com/o/r.git")
        self.assertEqual(common.redact_url("git@github.com:o/r.git"), "git@github.com:o/r.git")
        self.assertEqual(common.redact_url("origin"), "origin")

    def test_token_not_persisted_or_shown(self):
        old = sha(self.repo)
        self.commit("c1.txt")
        new = sha(self.repo)
        url = "https://user:ghp_SECRET123@example.invalid/o/r.git"
        res = subprocess.run(
            [str(ROOT / "bin/review-gate"), "hook", "origin", url],
            cwd=self.repo,
            input=f"refs/heads/main {new} refs/heads/main {old}\n",
            env={**self.env, "FAKE_REVIEW_JSON": BLOCKING},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(res.returncode, 0)
        state = self.repo / ".git/multi-model-team"
        for p in state.rglob("*"):
            if p.is_file():
                self.assertNotIn("ghp_SECRET123", p.read_text(errors="ignore") + p.name, p)
        _, _, st = self.gate("status", str(self.repo))
        self.assertIn("被挡中的推送目标：https://example.invalid/o/r.git refs/heads/main", st.stdout)
        self.assertNotIn("ghp_SECRET123", st.stdout + res.stdout + res.stderr)


class RereviewScopeTest(Base):
    def test_second_round_reviews_all_changes_between_snapshots(self):
        (self.repo / "a.txt").write_text("bug\n")
        code, r1, _ = self.run_bin("codex-review", str(self.repo), env={"FAKE_REVIEW_JSON": BLOCKING})
        self.assertEqual(code, 5)
        (self.repo / "a.txt").write_text("fixed\n")
        (self.repo / "unrelated.txt").write_text("新的独立改动\n")
        code, r2, _ = self.run_bin(
            "codex-review",
            str(self.repo),
            "--round",
            "2",
            "--previous",
            str(Path(r1["run_dir"]) / "result.json"),
            env={"FAKE_REVIEW_JSON": PASS_JSON},
        )
        self.assertEqual(code, 0)
        stdin = self.calls()[-1]["stdin"]
        self.assertIn(f"git diff {r1['reviewed_tree']} {r2['reviewed_tree']}", stdin)
        self.assertIn("有就照常记为 blocking", stdin)
        self.assertTrue(r2["pass_record"])

    def test_no_stamp_when_previous_snapshot_unknown(self):
        prev = self.tmp / "prev.json"
        prev.write_text(json.dumps({"findings": json.loads(BLOCKING)["findings"]}))
        (self.repo / "a.txt").write_text("changed\n")
        code, r, _ = self.run_bin(
            "codex-review", str(self.repo), "--round", "2", "--previous", str(prev), env={"FAKE_REVIEW_JSON": PASS_JSON}
        )
        self.assertEqual(code, 0)
        self.assertIsNone(r["pass_record"], "无法和上一轮快照比对时不能留可复用的已审标记")


class IsolateSymlinkTest(Base):
    def setUp(self):
        super().setUp()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "settings.txt").write_text("SECRET\n")

    def listed(self, target):
        code, _, res = self.run_bin("agy-ask", str(target), "问题", "--isolate", env={"FAKE_AGY_LIST": "1"})
        self.assertEqual(code, 0, res.stdout + res.stderr)
        return self.calls()[-1]["files"]

    def test_tracked_dir_replaced_by_outside_symlink(self):
        (self.repo / "conf").mkdir()
        (self.repo / "conf/settings.txt").write_text("ok\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "conf")
        (self.repo / "conf/settings.txt").unlink()
        (self.repo / "conf").rmdir()
        (self.repo / "conf").symlink_to(self.outside)
        self.assertNotIn("conf/settings.txt", self.listed(self.repo), "不能经由符号链接目录复制仓库外文件")

    def test_outside_file_symlink_dropped(self):
        (self.repo / "link.txt").symlink_to(self.outside / "settings.txt")
        (self.repo / "inner.txt").symlink_to("a.txt")
        files = self.listed(self.repo)
        self.assertNotIn("link.txt", files)
        self.assertIn("inner.txt", files)

    def test_non_git_dir_drops_outside_symlink(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text("x\n")
        (plain / "link.txt").symlink_to(self.outside / "settings.txt")
        (plain / "sub").symlink_to(self.outside)
        files = self.listed(plain)
        self.assertIn("a.txt", files)
        self.assertFalse({"link.txt", "sub/settings.txt"} & set(files))


class WorkerDiffTest(Base):
    def test_diff_includes_new_untracked_file(self):
        code, r, _ = self.run_bin("codex-worker", str(self.repo), "任务", env={"FAKE_CODEX_WRITE": "mod.py"})
        self.assertEqual(code, 0)
        diff = Path(r["diff_file"]).read_text()
        self.assertIn("mod.py", diff)
        self.assertIn("+new", diff)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True)
        self.assertIn("?? mod.py", status.stdout, "生成 diff 不能改动真正的暂存区")


class GuardSameLineTest(Base):
    def test_second_hit_on_same_line_counts(self):
        d = self.repo / ".multi-model-team"
        d.mkdir()
        (d / "project.toml").write_text(
            '[[guards]]\nname = "no-float"\npattern = \'parseFloat\\(\'\nfiles = ["*.ts"]\nmessage = "用 toCents()"\n'
        )
        (self.repo / "a.ts").write_text("parseFloat(x)\n")
        self.run_bin("guard-check", str(self.repo), "--update-baseline")
        self.assertEqual(self.run_bin("guard-check", str(self.repo))[0], 0)
        (self.repo / "a.ts").write_text("parseFloat(x) + parseFloat(y)\n")
        code, r, _ = self.run_bin("guard-check", str(self.repo))
        self.assertEqual(code, 1, "同一行新增违规也要拦")
        self.assertEqual(r["new_violations"][0]["file"], "a.ts")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
