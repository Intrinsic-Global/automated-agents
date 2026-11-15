#!/usr/bin/env python3
"""Prepare and retain exact-range evidence for the Symphony integration reviewer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.request


HEX40 = re.compile(r"^[0-9a-f]{40}$")
ISSUE_ID = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
DECISION_FIELDS = {
    "schema", "status", "source_commit", "target_commit", "source_tree", "target_tree",
    "reviewed_commits", "reviewer", "issues", "findings",
}
STAGING_FIELDS = {
    "schema", "status", "source_commit", "target_commit", "source_tree", "target_tree",
    "included_commits", "issues", "validation_evidence",
}
MANIFEST_FIELDS = {
    "schema", "issue", "repo", "source_commit", "source_tree", "target_ref", "target_commit",
    "target_tree", "reviewed_commits", "issues", "staging_record", "staging_sha256",
}
VALIDATION_FIELDS = {"command", "status", "tree", "ref", "sha256"}


class ReviewError(Exception):
    pass


def command(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["jj", "--ignore-working-copy", "-R", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ReviewError(result.stderr.strip() or f"jj command failed: {args[0]}")
    return result.stdout.strip()


def resolve(repo: Path, revision: str) -> str:
    value = command(repo, "log", "--no-graph", "-r", revision, "-T", "commit_id")
    if not HEX40.fullmatch(value):
        raise ReviewError(f"revision must resolve to one exact commit: {revision}")
    return value


def tree(repo: Path, commit: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{commit}^{{tree}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode or not HEX40.fullmatch(value):
        raise ReviewError(f"cannot resolve Git tree for {commit}")
    return value


def exact_range(repo: Path, target: str, source: str) -> list[str]:
    membership = command(repo, "log", "--no-graph", "-r", f"({target}) & ancestors({source})", "-T", "commit_id")
    if membership != target:
        raise ReviewError("candidate is not a descendant of the integration target")
    values = command(
        repo, "log", "--no-graph", "--reversed", "-r", f"{target}..{source}", "-T", 'commit_id ++ "\\n"'
    ).splitlines()
    if not values or any(not HEX40.fullmatch(value) for value in values) or len(values) != len(set(values)):
        raise ReviewError("candidate range is empty or JJ returned malformed commit identities")
    return values


def load_manifest(review_root: Path, issue: str, repo: Path) -> dict:
    path = review_root / "manifests" / f"{issue}.json"
    if path.is_symlink() or not path.is_file():
        raise ReviewError(f"prepared review manifest is missing or unsafe: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError("prepared review manifest is unreadable JSON") from exc
    if (not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS
            or manifest.get("schema") != "symphony.integration-review-manifest.v1"):
        raise ReviewError("prepared review manifest has an unsupported schema")
    if manifest.get("issue") != issue or manifest.get("repo") != str(repo):
        raise ReviewError("prepared review manifest does not match this issue and repository")
    source = resolve(repo, manifest.get("source_commit", ""))
    target = resolve(repo, manifest.get("target_ref", "integration"))
    if source != manifest.get("source_commit") or target != manifest.get("target_commit"):
        raise ReviewError("candidate or integration target changed after review preparation")
    if tree(repo, source) != manifest.get("source_tree") or tree(repo, target) != manifest.get("target_tree"):
        raise ReviewError("candidate or integration tree changed after review preparation")
    if exact_range(repo, target, source) != manifest.get("reviewed_commits"):
        raise ReviewError("candidate commit range changed after review preparation")
    staging_path = Path(manifest.get("staging_record", "")).expanduser()
    if staging_path.is_symlink() or not staging_path.is_file():
        raise ReviewError("aggregate staging qualification record is missing or unsafe")
    if hashlib.sha256(staging_path.read_bytes()).hexdigest() != manifest.get("staging_sha256"):
        raise ReviewError("aggregate staging qualification record changed after review preparation")
    validate_staging_record(staging_path, manifest)
    return manifest


def validate_staging_record(path: Path, manifest: dict) -> tuple[dict, list[dict]]:
    try:
        stage = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError("aggregate staging qualification record is unreadable JSON") from exc
    if not isinstance(stage, dict) or set(stage) != STAGING_FIELDS:
        raise ReviewError("aggregate staging qualification fields do not match the v1 contract")
    expected = {
        "source_commit": manifest["source_commit"],
        "target_commit": manifest["target_commit"],
        "source_tree": manifest["source_tree"],
        "target_tree": manifest["target_tree"],
        "included_commits": manifest["reviewed_commits"],
        "issues": manifest["issues"],
    }
    if stage.get("schema") != "prf-dev.staging-admission.v1" or stage.get("status") != "admitted":
        raise ReviewError("aggregate staging qualification is not admitted")
    if any(stage.get(key) != value for key, value in expected.items()):
        raise ReviewError("aggregate staging qualification does not bind the exact candidate and issue range")
    repo_root = Path(manifest["repo"]).resolve()
    try:
        path.resolve().relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise ReviewError("aggregate staging qualification evidence must be stored outside the repository")
    executions = stage.get("validation_evidence")
    if not isinstance(executions, list) or not executions:
        raise ReviewError("aggregate staging qualification lacks validation execution receipts")
    normalized = []
    for execution in executions:
        if (not isinstance(execution, dict) or set(execution) != VALIDATION_FIELDS
                or execution.get("status") != "passed" or execution.get("tree") != manifest["source_tree"]
                or not isinstance(execution.get("command"), str) or not execution["command"]
                or not isinstance(execution.get("ref"), str) or not execution["ref"]
                or not re.fullmatch(r"^[0-9a-f]{64}$", str(execution.get("sha256", "")))):
            raise ReviewError("validation execution receipt does not match the exact qualified tree")
        evidence_path = Path(execution["ref"]).expanduser()
        if not evidence_path.is_absolute():
            evidence_path = path.parent / evidence_path
        if evidence_path.is_symlink() or not evidence_path.is_file():
            raise ReviewError("validation execution evidence is missing or unsafe")
        evidence_path = evidence_path.resolve()
        try:
            evidence_path.relative_to(repo_root)
        except ValueError:
            pass
        else:
            raise ReviewError("validation evidence must be stored outside the repository")
        digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        if digest != execution["sha256"]:
            raise ReviewError("validation execution evidence digest mismatch")
        normalized.append({**execution, "ref": str(evidence_path)})
    return stage, normalized


def write_exclusive(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        raise ReviewError(f"refusing to overwrite an existing or symlinked evidence file: {path}")
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ReviewError(f"refusing to overwrite existing evidence: {path}") from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def default_review_root() -> Path:
    code_root = Path(os.environ.get("CODE_ROOT", Path.home() / "Code")).expanduser()
    return Path(os.environ.get("SYMPHONY_INTEGRATION_REVIEW_DIR", code_root / ".symphony-integration-reviews"))


def prepare(args: argparse.Namespace) -> dict:
    if not ISSUE_ID.fullmatch(args.issue):
        raise ReviewError("issue must be a Linear identifier such as PRO-123")
    if not args.issues or len(set(args.issues)) != len(args.issues) or any(not ISSUE_ID.fullmatch(item) for item in args.issues):
        raise ReviewError("the assembled candidate needs a non-empty list of distinct Linear issue IDs")
    repo = Path(args.repo).expanduser().resolve(strict=True)
    source = resolve(repo, args.source_commit)
    target = resolve(repo, args.target_ref)
    commits = exact_range(repo, target, source)
    staging_path = args.staging_record.expanduser()
    if staging_path.is_symlink() or not staging_path.is_file():
        raise ReviewError("aggregate staging qualification record must be a regular file")
    staging_path = staging_path.resolve()
    manifest = {
        "schema": "symphony.integration-review-manifest.v1",
        "issue": args.issue,
        "repo": str(repo),
        "source_commit": source,
        "source_tree": tree(repo, source),
        "target_ref": args.target_ref,
        "target_commit": target,
        "target_tree": tree(repo, target),
        "reviewed_commits": commits,
        "issues": args.issues,
        "staging_record": str(staging_path),
        "staging_sha256": hashlib.sha256(staging_path.read_bytes()).hexdigest(),
    }
    validate_staging_record(staging_path, manifest)
    path = args.review_root / "manifests" / f"{args.issue}.json"
    write_exclusive(path, manifest)
    return {"status": "prepared", "manifest": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def context(args: argparse.Namespace) -> dict:
    manifest = load_manifest(args.review_root, args.issue, Path(args.repo).expanduser().resolve(strict=True))
    prior_decision = args.review_root / "decisions" / f"{args.issue}.json"
    prior_record = args.review_root / "records" / f"{args.issue}.json"
    if prior_decision.exists() or prior_record.exists():
        raise ReviewError("this integration review already has a retained decision; do not spend another turn")
    context_doc = {
        "schema": manifest["schema"],
        "issue": manifest["issue"],
        "source_commit": manifest["source_commit"],
        "source_tree": manifest["source_tree"],
        "target_ref": manifest["target_ref"],
        "target_commit": manifest["target_commit"],
        "target_tree": manifest["target_tree"],
        "reviewed_commits": manifest["reviewed_commits"],
        "issues": manifest["issues"],
        "qualified_tree": manifest["source_tree"],
        "staging_qualification_sha256": manifest["staging_sha256"],
        "diff_command": (
            f"jj --ignore-working-copy -R {shlex.quote(manifest['repo'])} diff "
            f'--from {manifest["target_commit"]} --to {manifest["source_commit"]}'
        ),
    }
    output = args.workspace / "review-context.json"
    if output.is_symlink():
        raise ReviewError("review context output cannot be a symlink")
    output.write_text(json.dumps(context_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "ready", "context": str(output)}


def record(args: argparse.Namespace) -> dict:
    repo = Path(args.repo).expanduser().resolve(strict=True)
    manifest = load_manifest(args.review_root, args.issue, repo)
    decision_path = args.decision.expanduser()
    if decision_path.is_symlink() or not decision_path.is_file():
        raise ReviewError("review decision must be a regular file")
    try:
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError("review decision is unreadable JSON") from exc
    if not isinstance(decision, dict) or set(decision) != DECISION_FIELDS:
        raise ReviewError("review decision fields do not match the exact-range decision contract")
    expected = {
        "source_commit": manifest["source_commit"],
        "target_commit": manifest["target_commit"],
        "source_tree": manifest["source_tree"],
        "target_tree": manifest["target_tree"],
        "reviewed_commits": manifest["reviewed_commits"],
        "issues": manifest["issues"],
    }
    if any(decision.get(key) != value for key, value in expected.items()):
        raise ReviewError("review decision does not bind the prepared candidate, target, trees, range, and issues")
    if decision.get("schema") != "symphony.integration-review-decision.v1":
        raise ReviewError("unsupported review decision schema")
    if decision.get("status") not in {"approved", "changes_requested"}:
        raise ReviewError("review status must be approved or changes_requested")
    reviewer = os.environ.get("SYMPHONY_REVIEWER_ID", "").strip()
    if not reviewer or decision.get("reviewer") != reviewer:
        raise ReviewError("reviewer identity does not match SYMPHONY_REVIEWER_ID")
    if not isinstance(decision.get("findings"), list):
        raise ReviewError("review findings must be a list, including when empty")
    raw_path = args.review_root / "decisions" / f"{args.issue}.json"
    write_exclusive(raw_path, decision)
    if decision.get("status") != "approved":
        result = {
            "status": "not-approved",
            "decision_status": decision["status"],
            "decision_record": str(raw_path),
        }
        try:
            result["linear_status"] = transition_review_issue_to_state(args.issue, "Todo")
        except (OSError, ReviewError, json.JSONDecodeError) as exc:
            result["linear_status"] = "blocked"
            result["linear_reconciliation"] = str(exc)
        return result
    review_record = {
        "schema": "prf-dev.integration-review.v1",
        "status": "approved",
        "source_commit": manifest["source_commit"],
        "target_commit": manifest["target_commit"],
        "source_tree": manifest["source_tree"],
        "target_tree": manifest["target_tree"],
        "reviewed_commits": manifest["reviewed_commits"],
        "reviewer": reviewer,
        "issues": manifest["issues"],
    }
    path = args.review_root / "records" / f"{args.issue}.json"
    write_exclusive(path, review_record)
    result = {
        "status": "approved",
        "review_record": str(path),
        "review_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "decision_record": str(raw_path),
        "decision_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
    }
    promotion_result = promote(args, manifest, path, raw_path)
    result.update(promotion_result)
    target_state = "Done" if promotion_result.get("promotion_status") == "promoted" else "Todo"
    if promotion_result.get("promotion_status") in {"promoted", "blocked"}:
        try:
            result["linear_status"] = transition_review_issue_to_state(args.issue, target_state)
        except (OSError, ReviewError, json.JSONDecodeError) as exc:
            result["linear_status"] = "blocked"
            result["linear_reconciliation"] = str(exc)
    return result


def transition_review_issue_to_state(identifier: str, target_state: str) -> str:
    token = os.environ.get("LINEAR_API_KEY", "").strip()
    if not token:
        raise ReviewError("LINEAR_API_KEY is unavailable for post-promotion issue reconciliation")

    def graphql(query: str, variables: dict) -> dict:
        request = urllib.request.Request(
            "https://api.linear.app/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Content-Type": "application/json", "Authorization": token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        if result.get("errors"):
            raise ReviewError("Linear GraphQL rejected post-promotion state reconciliation")
        return result["data"]

    issue = graphql(
        "query($id:String!){issue(id:$id){id identifier state{name} team{states{nodes{id name}}}}}",
        {"id": identifier},
    ).get("issue")
    if not isinstance(issue, dict) or issue.get("identifier") != identifier:
        raise ReviewError("Linear integration-review issue lookup did not match its identifier")
    state_name = (issue.get("state") or {}).get("name")
    if state_name != "In Review":
        raise ReviewError(f"integration-review issue changed state after promotion: {state_name}")
    states = ((issue.get("team") or {}).get("states") or {}).get("nodes") or []
    target_states = [state for state in states if state.get("name") == target_state]
    if len(target_states) != 1:
        raise ReviewError(f"Linear team does not expose one unique {target_state} state")
    updated = graphql(
        "mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success issue{state{name}}}}",
        {"id": issue["id"], "input": {"stateId": target_states[0]["id"]}},
    ).get("issueUpdate") or {}
    if not updated.get("success") or (updated.get("issue") or {}).get("state", {}).get("name") != target_state:
        raise ReviewError(f"Linear did not confirm the integration-review issue transition to {target_state}")
    return target_state.lower()


def promote(args: argparse.Namespace, manifest: dict, review_path: Path, decision_path: Path) -> dict:
    repo_root = Path(manifest["repo"])
    prf_dev_root = Path(os.environ.get(
        "PRF_DEV_ROOT", Path(__file__).resolve().parents[3] / "prf-dev"
    )).expanduser().resolve(strict=True)
    sys.path.insert(0, str(prf_dev_root / "src"))
    try:
        from prf_dev.integration import JJRepository, IntegrationError, preflight, promote as promote_integration, verify
    except ImportError as exc:
        raise ReviewError("prf-dev integration gate is unavailable") from exc

    stage_path = Path(manifest["staging_record"])
    stage, executions = validate_staging_record(stage_path, manifest)
    review_bytes = review_path.read_bytes()
    stage_bytes = stage_path.read_bytes()
    admission = {
        "schema": "prf-dev.integration-admission.v2",
        "status": "admitted",
        "source_commit": manifest["source_commit"],
        "source_tree": manifest["source_tree"],
        "target_commit": manifest["target_commit"],
        "target_tree": manifest["target_tree"],
        "reviewed_commits": manifest["reviewed_commits"],
        "review": {
            "status": "approved",
            "candidate_commit": manifest["source_commit"],
            "reviewer": os.environ["SYMPHONY_REVIEWER_ID"],
            "reviewed_commits": manifest["reviewed_commits"],
            "ref": str(review_path.resolve()),
            "sha256": hashlib.sha256(review_bytes).hexdigest(),
        },
        "staging": {
            "status": "admitted",
            "source_commit": manifest["source_commit"],
            "target_commit": manifest["target_commit"],
            "source_tree": manifest["source_tree"],
            "target_tree": manifest["target_tree"],
            "included_commits": manifest["reviewed_commits"],
            "ref": str(stage_path),
            "sha256": hashlib.sha256(stage_bytes).hexdigest(),
        },
        "qualification": {
            "status": "passed",
            "commit": manifest["source_commit"],
            "tree": manifest["source_tree"],
            "tests": executions,
        },
        "issues": manifest["issues"],
        "evidence": [
            {"ref": str(review_path.resolve()), "sha256": hashlib.sha256(review_bytes).hexdigest()},
            {"ref": str(stage_path), "sha256": hashlib.sha256(stage_bytes).hexdigest()},
            {"ref": str(decision_path.resolve()), "sha256": hashlib.sha256(decision_path.read_bytes()).hexdigest()},
            *[{"ref": item["ref"], "sha256": item["sha256"]} for item in executions],
        ],
    }
    admission_path = args.review_root / "admissions" / f"{args.issue}.json"
    write_exclusive(admission_path, admission)
    promotion_root = args.review_root / "promotions"
    preflight_path = promotion_root / f"{args.issue}-preflight.json"
    promotion_path = promotion_root / f"{args.issue}-promotion.json"
    staging_refs = ("agent-c-staging", "symphony-staging")
    gate_repo = JJRepository(repo_root)
    try:
        report = preflight(
            gate_repo,
            manifest["source_commit"],
            manifest["target_ref"],
            admission_path,
            staging_refs=staging_refs,
        )
        write_exclusive(preflight_path, report)
        if report.get("status") != "ready":
            return {"promotion_status": "blocked", "preflight_report": str(preflight_path),
                    "blockers": report.get("blockers", [])}
        promoted = promote_integration(
            gate_repo,
            manifest["source_commit"],
            manifest["target_ref"],
            admission_path,
            preflight_path,
            report["report_sha256"],
            promotion_path,
            staging_refs=staging_refs,
        )
        verification = verify(gate_repo, promotion_path)
    except (IntegrationError, OSError) as exc:
        return {"promotion_status": "blocked", "admission_record": str(admission_path),
                "preflight_report": str(preflight_path), "promotion_record": str(promotion_path),
                "reason": str(exc)}
    return {"promotion_status": promoted.get("status"), "admission_record": str(admission_path),
            "preflight_report": str(preflight_path), "promotion_record": str(promotion_path),
            "verification": verification}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="pin a candidate range for independent review")
    prepare_parser.add_argument("--repo", type=Path, required=True)
    prepare_parser.add_argument("--issue", required=True, help="Linear integration review issue identifier")
    prepare_parser.add_argument("--source-commit", required=True)
    prepare_parser.add_argument("--target-ref", default="integration")
    prepare_parser.add_argument("--issues", nargs="+", required=True, help="Linear issues included in the candidate")
    prepare_parser.add_argument("--staging-record", type=Path, required=True,
                                help="exact-tree aggregate prf-dev.staging-admission.v1 record with test receipts")
    prepare_parser.add_argument("--review-root", type=Path, default=default_review_root())
    prepare_parser.set_defaults(run=prepare)

    for name in ("context", "record"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--repo", type=Path, required=True)
        sub.add_argument("--issue", required=True)
        sub.add_argument("--review-root", type=Path, default=default_review_root())
        if name == "context":
            sub.add_argument("--workspace", type=Path, default=Path.cwd())
            sub.set_defaults(run=context)
        else:
            sub.add_argument("--decision", type=Path, required=True)
            sub.set_defaults(run=record)

    args = parser.parse_args()
    if args.review_root is None:
        parser.error("--review-root or SYMPHONY_INTEGRATION_REVIEW_DIR is required")
    args.review_root = args.review_root.expanduser().resolve()
    try:
        print(json.dumps(args.run(args), indent=2, sort_keys=True))
        return 0
    except (OSError, ReviewError) as exc:
        result = {"status": "blocked", "reason": str(exc)}
        if args.command == "record":
            try:
                result["linear_status"] = transition_review_issue_to_state(args.issue, "Todo")
            except (OSError, ReviewError, json.JSONDecodeError) as state_exc:
                result["linear_reconciliation"] = str(state_exc)
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
