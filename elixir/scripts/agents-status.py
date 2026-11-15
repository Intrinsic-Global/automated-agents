#!/usr/bin/env python3
"""Generate a compact status.md from Symphony's per-lane logs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path


LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)) "
    r"(?P<level>trace|debug|info|warning|error): (?P<message>.*)$"
)
ISSUE_RE = re.compile(r"\bissue_identifier=(?P<issue>[A-Z][A-Z0-9]+-\d+)\b")
REASONS_RE = re.compile(r"\breasons=(\[[^\]]*\])")
ACTIVE_WINDOW = timedelta(minutes=15)


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    default_logs = Path(os.environ.get("SYMPHONY_LOGS_ROOT", "~/Code/.symphony-logs")).expanduser()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-root", type=Path, default=default_logs)
    parser.add_argument("--output", type=Path, default=repo_root / "status.md")
    parser.add_argument("--json", action="store_true", help="print the status records as JSON")
    return parser.parse_args()


def log_files(lane_dir: Path) -> list[Path]:
    log_dir = lane_dir / "log"
    if not log_dir.is_dir():
        return []
    return sorted(
        (path for path in log_dir.glob("symphony.log*") if path.is_file() and path.suffix not in {".idx", ".siz"}),
        key=lambda path: path.name,
    )


def parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def collect_events(log_root: Path) -> list[dict[str, str | datetime | None]]:
    events: list[dict[str, str | datetime | None]] = []
    if not log_root.is_dir():
        return events

    for lane_dir in sorted(path for path in log_root.iterdir() if path.is_dir()):
        for path in log_files(lane_dir):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                match = LINE_RE.match(line)
                if not match:
                    continue
                message = match.group("message")
                timestamp = parse_timestamp(match.group("timestamp"))
                issue_match = ISSUE_RE.search(message)
                issue = issue_match.group("issue") if issue_match else None
                event_type = None
                detail = ""
                if "Starting agent run for " in message:
                    event_type = "run_started"
                elif "Completed agent run for " in message:
                    event_type = "run_completed"
                elif "Agent run failed for " in message:
                    event_type = "run_failed"
                    detail = message.partition(": ")[2]
                elif "Retrying issue_id=" in message:
                    event_type = "retrying"
                    detail = message.partition(" (attempt ")[0].rsplit(" in ", 1)[-1]
                elif "Skipping ineligible issue " in message:
                    event_type = "ineligible"
                    status_match = re.search(r"\bstatus=([^ ]+)", message)
                    reasons_match = REASONS_RE.search(message)
                    detail = status_match.group(1) if status_match else "ineligible"
                    if reasons_match:
                        detail += ": " + reasons_match.group(1).strip("[]")
                elif "Dispatch skipped " in message:
                    event_type = "dispatch_skipped"
                    reason_match = re.search(r"\breason_code=([^ ]+)", message)
                    detail = reason_match.group(1) if reason_match else "dispatch skipped"
                elif "Dispatching issue to agent:" in message:
                    event_type = "dispatched"
                elif match.group("level") == "error":
                    event_type = "error"
                    detail = message

                if event_type:
                    events.append(
                        {
                            "lane": lane_dir.name,
                            "timestamp": timestamp,
                            "timestamp_text": utc_text(timestamp),
                            "issue": issue,
                            "type": event_type,
                            "detail": detail,
                            "source": str(path),
                        }
                    )
    return events


def age_text(timestamp: datetime | None, now: datetime) -> str:
    if timestamp is None:
        return "unknown"
    seconds = max(0, int((now - timestamp).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def summarize_lanes(events: list[dict[str, str | datetime | None]]) -> list[dict[str, str | None]]:
    now = datetime.now(timezone.utc)
    lanes = sorted({str(event["lane"]) for event in events})
    summaries = []
    for lane in lanes:
        lane_events = [event for event in events if event["lane"] == lane]
        lane_events.sort(key=lambda event: event["timestamp"] or datetime.min.replace(tzinfo=timezone.utc))
        starts = [event for event in lane_events if event["type"] == "run_started"]
        latest_start = starts[-1] if starts else None
        if latest_start:
            issue = latest_start["issue"]
            related = [event for event in lane_events if event["timestamp"] >= latest_start["timestamp"] and event["issue"] == issue]
            run_events = [event for event in related if event["type"] in {"run_completed", "run_failed", "retrying"}]
            if run_events:
                terminal = run_events[-1]
                outcome = {"run_completed": "completed", "run_failed": "failed", "retrying": "retrying"}[str(terminal["type"])]
                detail = str(terminal["detail"] or ("agent invocation completed" if terminal["type"] == "run_completed" else "retry scheduled" if terminal["type"] == "retrying" else "agent invocation failed"))
            elif latest_start["timestamp"] and now - latest_start["timestamp"] <= ACTIVE_WINDOW:
                outcome, detail = "no end logged (recent)", "no completion or failure event after start"
            else:
                outcome, detail = "incomplete (stale)", "no completion or failure event; run is older than 15 minutes"
            timestamp = latest_start["timestamp_text"]
        else:
            issue = None
            timestamp = None
            outcome = "no invocation recorded"
            detail = "latest event is shown under dispatch activity"

        latest_observation = lane_events[-1]
        activity = latest_observation["timestamp_text"]
        recent = [event for event in lane_events if event["timestamp"] and now - event["timestamp"] <= ACTIVE_WINDOW]
        recent_dispatch = [event for event in recent if event["type"] in {"dispatched", "dispatch_skipped", "ineligible", "retrying", "run_started"}]
        dispatch_summary = "no dispatch events in last 15m"
        if recent_dispatch:
            counts: dict[str, int] = {}
            issue_examples: dict[str, list[str]] = {}
            for event in recent_dispatch:
                label = str(event["type"])
                if event["type"] in {"dispatch_skipped", "ineligible"} and event["detail"]:
                    label += ": " + str(event["detail"])
                counts[label] = counts.get(label, 0) + 1
                if event["issue"]:
                    issue_list = issue_examples.setdefault(label, [])
                    if str(event["issue"]) not in issue_list:
                        issue_list.append(str(event["issue"]))
            top_reasons = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]
            reason_text = []
            for label, count in top_reasons:
                issues = issue_examples.get(label, [])
                sample = ",".join(issues[-4:])
                reason_text.append(f"{label} ×{count}" + (f" [{sample}]" if sample else ""))
            dispatch_summary = f"{len(recent_dispatch)} scan events; " + "; ".join(reason_text)
        freshness = "recent" if recent else "stale"

        summaries.append(
            {
                "agent": lane,
                "last_run": str(timestamp) if timestamp else None,
                "last_run_issue": str(issue) if issue else None,
                "outcome": outcome,
                "detail": detail.replace("\n", " ")[:180],
                "latest_issue": str(latest_observation["issue"]) if latest_observation["issue"] else None,
                "latest_event": str(latest_observation["type"]),
                "latest_detail": str(latest_observation["detail"] or latest_observation["type"]).replace("\n", " ")[:180],
                "last_activity": str(activity),
                "activity_age": age_text(latest_observation["timestamp"], now),
                "freshness": freshness,
                "dispatch_summary": dispatch_summary,
            }
        )
    return summaries


def markdown(records: list[dict[str, str | None]], logs_root: Path) -> str:
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lines = [
        "# Symphony agent status",
        "",
        f"Generated: {generated}  ",
        f"Logs: `{logs_root}`",
        "",
        "One row per agent lane. Historical run outcomes are separate from dispatch scans in the last 15 minutes; scans are not agent invocations. Stale log events are not evidence that an agent is currently running.",
        "",
        "| Agent | Last run (UTC) | Run issue | Last run outcome | Dispatch activity (15m) | Last activity (UTC) | Age | Freshness |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    if records:
        for record in records:
            values = [record["agent"], record["last_run"] or "—", record["last_run_issue"] or "—", record["outcome"] + " · " + str(record["detail"]), record["dispatch_summary"], record["last_activity"] or "—", record["activity_age"], record["freshness"]]
            lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in values) + " |")
    else:
        lines.append("| — | — | — | no logs found | — | — | — | unknown |")
    lines.extend(["", "Refresh with `./scripts/agents-status`.", ""])
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    logs_root = args.logs_root.expanduser().resolve()
    records = summarize_lanes(collect_events(logs_root))
    if args.json:
        print(json.dumps({"logs_root": str(logs_root), "agents": records}, indent=2))
        return 0

    output = args.output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    content = markdown(records, logs_root)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as handle:
            handle.write(content)
            temp_path = Path(handle.name)
        temp_path.replace(output)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    print(f"Updated {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
