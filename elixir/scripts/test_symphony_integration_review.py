"""Tests for exact candidate binding in the aggregate integration reviewer handoff."""

import importlib.util
import hashlib
import json
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("symphony_integration_review.py")
spec = importlib.util.spec_from_file_location("symphony_integration_review", SCRIPT)
review = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = review
spec.loader.exec_module(review)


def run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class IntegrationReviewHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.config = self.root / "config"
        self.config.mkdir()
        self.old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.config)
        run(["jj", "git", "init", "--colocate", str(self.repo)])
        run(["jj", "-R", str(self.repo), "config", "set", "--repo", "user.name", "Test Reviewer"])
        run(["jj", "-R", str(self.repo), "config", "set", "--repo", "user.email", "reviewer@example.invalid"])
        run(["jj", "--ignore-working-copy", "-R", str(self.repo), "bookmark", "create", "integration", "-r", "@"])
        (self.repo / "feature.txt").write_text("review candidate\n", encoding="utf-8")
        run(["jj", "-R", str(self.repo), "new", "-m", "candidate change"])
        self.source = review.resolve(self.repo, "@")
        self.target = review.resolve(self.repo, "integration")
        for ref in ("agent-c-staging", "symphony-staging"):
            run(["jj", "--ignore-working-copy", "-R", str(self.repo), "bookmark", "create", ref, "-r", self.source])
        self.review_root = self.root / "review-evidence"
        self.issue = "PRO-900"
        self.validation_path = self.root / "validation.log"
        self.validation_path.write_text("backstop passed\n", encoding="utf-8")

    def tearDown(self):
        if self.old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.old_xdg
        self.temp.cleanup()

    def manifest(self):
        return json_load(self.review_root / "manifests" / f"{self.issue}.json")

    def prepare(self, issues):
        stage_path = self.root / f"staging-{len(issues)}.json"
        stage_path.write_text(json.dumps({
            "schema": "prf-dev.staging-admission.v1", "status": "admitted",
            "source_commit": self.source, "target_commit": self.target,
            "source_tree": review.tree(self.repo, self.source), "target_tree": review.tree(self.repo, self.target),
            "included_commits": review.exact_range(self.repo, self.target, self.source), "issues": issues,
            "validation_evidence": [{
                "command": command, "status": "passed", "tree": review.tree(self.repo, self.source),
                "ref": str(self.validation_path), "sha256": hashlib.sha256(self.validation_path.read_bytes()).hexdigest(),
            } for command in ("bb validate", "bb backstop")],
        }), encoding="utf-8")
        return review.prepare(Namespace(
            issue=self.issue, repo=self.repo, source_commit=self.source, target_ref="integration",
            issues=issues, staging_record=stage_path, review_root=self.review_root,
        ))

    def test_prepares_context_and_records_exact_aggregate_review(self):
        prepared = self.prepare(["PRO-101", "PRO-102"])
        self.assertEqual("prepared", prepared["status"])
        manifest = self.manifest()

        workspace = self.root / "reviewer"
        workspace.mkdir()
        with patch("symphony_integration_review.subprocess.run", wraps=subprocess.run) as run_spy:
            context = review.context(Namespace(
                issue=self.issue, repo=self.repo, review_root=self.review_root, workspace=workspace,
            ))
        self.assertEqual("ready", context["status"])
        context_doc = json_load(workspace / "review-context.json")
        self.assertEqual("candidate.diff", context_doc["candidate_diff"])
        self.assertEqual("staging-admission.json", context_doc["staging_admission"])
        candidate_diff = (workspace / context_doc["candidate_diff"]).read_bytes()
        self.assertEqual(hashlib.sha256(candidate_diff).hexdigest(), context_doc["candidate_diff_sha256"])
        git_diff_calls = [call.args[0] for call in run_spy.call_args_list if "diff" in call.args[0]]
        self.assertTrue(git_diff_calls)
        self.assertFalse(any("--unified=0" in args for args in git_diff_calls))
        staging_copy = (workspace / context_doc["staging_admission"]).read_bytes()
        self.assertEqual(manifest["staging_sha256"], hashlib.sha256(staging_copy).hexdigest())

        decision = {
            "schema": "symphony.integration-review-decision.v1",
            "status": "approved",
            "source_commit": manifest["source_commit"],
            "target_commit": manifest["target_commit"],
            "source_tree": manifest["source_tree"],
            "target_tree": manifest["target_tree"],
            "reviewed_commits": manifest["reviewed_commits"],
            "reviewer": "symphony-integration-reviewer",
            "issues": manifest["issues"],
            "findings": [],
        }
        decision_path = workspace / "review-decision.json"
        decision_path.write_text(json.dumps(decision), encoding="utf-8")
        with patch.dict(os.environ, {"SYMPHONY_REVIEWER_ID": "symphony-integration-reviewer"}):
            result = review.record(Namespace(
                issue=self.issue, repo=self.repo, review_root=self.review_root, decision=decision_path,
            ))

        self.assertEqual("approved", result["status"])
        self.assertEqual("promoted", result["promotion_status"])
        record = json_load(Path(result["review_record"]))
        self.assertEqual("prf-dev.integration-review.v1", record["schema"])
        self.assertEqual(manifest["reviewed_commits"], record["reviewed_commits"])
        self.assertEqual(manifest["issues"], record["issues"])

    def test_prepare_and_context_accept_a_jj_workspace_path(self):
        workspace = self.root / "workspace"
        run(["jj", "-R", str(self.repo), "workspace", "add", "--name", "review-workspace",
             "--revision", self.source, str(workspace)])
        stage_path = self.root / "workspace-staging.json"
        source_tree = review.tree(self.repo, self.source)
        target_tree = review.tree(self.repo, self.target)
        stage_path.write_text(json.dumps({
            "schema": "prf-dev.staging-admission.v1", "status": "admitted",
            "source_commit": self.source, "target_commit": self.target,
            "source_tree": source_tree, "target_tree": target_tree,
            "included_commits": review.exact_range(self.repo, self.target, self.source),
            "issues": ["PRO-101"],
            "validation_evidence": [{
                "command": command, "status": "passed", "tree": source_tree,
                "ref": str(self.validation_path),
                "sha256": hashlib.sha256(self.validation_path.read_bytes()).hexdigest(),
            } for command in ("bb validate", "bb backstop")],
        }), encoding="utf-8")

        prepared = review.prepare(Namespace(
            issue=self.issue, repo=workspace, source_commit=self.source, target_ref="integration",
            issues=["PRO-101"], staging_record=stage_path, review_root=self.review_root,
        ))
        self.assertEqual("prepared", prepared["status"])
        self.assertEqual(str(workspace.resolve()), self.manifest()["repo"])

        reviewer = self.root / "reviewer-workspace"
        reviewer.mkdir()
        context = review.context(Namespace(
            issue=self.issue, repo=workspace, review_root=self.review_root, workspace=reviewer,
        ))
        self.assertEqual("ready", context["status"])
        self.assertTrue((reviewer / "candidate.diff").is_file())
        self.assertTrue((reviewer / "staging-admission.json").is_file())

    def test_record_rejects_decision_for_a_different_candidate(self):
        self.prepare(["PRO-101"])
        manifest = self.manifest()
        decision_path = self.root / "decision.json"

        decision_path.write_text(json.dumps({
            "schema": "symphony.integration-review-decision.v1", "status": "approved",
            "source_commit": "0" * 40, "target_commit": manifest["target_commit"],
            "source_tree": manifest["source_tree"], "target_tree": manifest["target_tree"],
            "reviewed_commits": manifest["reviewed_commits"], "reviewer": "symphony-integration-reviewer",
            "issues": manifest["issues"], "findings": [],
        }), encoding="utf-8")
        with patch.dict(os.environ, {"SYMPHONY_REVIEWER_ID": "symphony-integration-reviewer"}):
            with self.assertRaisesRegex(review.ReviewError, "does not bind"):
                review.record(Namespace(
                    issue=self.issue, repo=self.repo, review_root=self.review_root, decision=decision_path,
                ))

    def test_changes_requested_is_retained_without_approval_and_blocks_repeat_turns(self):
        self.prepare(["PRO-101"])
        manifest = self.manifest()
        decision_path = self.root / "decision.json"

        decision_path.write_text(json.dumps({
            "schema": "symphony.integration-review-decision.v1", "status": "changes_requested",
            "source_commit": manifest["source_commit"], "target_commit": manifest["target_commit"],
            "source_tree": manifest["source_tree"], "target_tree": manifest["target_tree"],
            "reviewed_commits": manifest["reviewed_commits"], "reviewer": "symphony-integration-reviewer",
            "issues": manifest["issues"], "findings": [{"finding": "fix candidate"}],
        }), encoding="utf-8")
        with patch.dict(os.environ, {"SYMPHONY_REVIEWER_ID": "symphony-integration-reviewer"}):
            result = review.record(Namespace(
                issue=self.issue, repo=self.repo, review_root=self.review_root, decision=decision_path,
            ))
        self.assertEqual("not-approved", result["status"])
        self.assertFalse((self.review_root / "records" / f"{self.issue}.json").exists())
        with self.assertRaisesRegex(review.ReviewError, "already has a retained decision"):
            review.context(Namespace(
                issue=self.issue, repo=self.repo, review_root=self.review_root, workspace=self.root,
            ))

    def test_context_blocks_when_integration_target_moves_after_preparation(self):
        self.prepare(["PRO-101"])
        run(["jj", "--ignore-working-copy", "-R", str(self.repo), "bookmark", "set", "integration", "-r", self.source])
        with self.assertRaisesRegex(review.ReviewError, "changed after review preparation"):
            review.context(Namespace(
                issue=self.issue, repo=self.repo, review_root=self.review_root, workspace=self.root,
            ))

    def test_linear_issue_moves_only_from_in_review_to_requested_state(self):
        issue = {
            "id": "linear-id",
            "identifier": self.issue,
            "state": {"name": "In Review"},
            "team": {"states": {"nodes": [{"id": "todo-id", "name": "Todo"},
                                               {"id": "done-id", "name": "Done"}]}},
        }
        responses = [
            BytesIO(json.dumps({"data": {"issue": issue}}).encode()),
            BytesIO(json.dumps({"data": {"issueUpdate": {"success": True,
                       "issue": {"state": {"name": "Done"}}}}}).encode()),
        ]
        with patch.dict(os.environ, {"LINEAR_API_KEY": "test-key"}), \
                patch.object(review.urllib.request, "urlopen", side_effect=responses):
            self.assertEqual("done", review.transition_review_issue_to_state(self.issue, "Done"))

        issue["state"] = {"name": "Canceled"}
        with patch.dict(os.environ, {"LINEAR_API_KEY": "test-key"}), \
                patch.object(review.urllib.request, "urlopen", return_value=BytesIO(
                    json.dumps({"data": {"issue": issue}}).encode()
                )):
            with self.assertRaisesRegex(review.ReviewError, "changed state"):
                review.transition_review_issue_to_state(self.issue, "Todo")


def json_load(path: Path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
