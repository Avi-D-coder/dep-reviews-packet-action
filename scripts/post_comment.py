#!/usr/bin/env python3
"""Publish a sticky PR comment and step summary for dependency review results."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


MARKER = "<!-- dep-reviews-packet-action -->"


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.workdir) / "manifest.json"
    results_path = Path(args.workdir) / "results.json"
    manifest = load_json(manifest_path, default_manifest())
    results = load_json(results_path, {"dependencies": []})

    body = render_body(manifest, results, include_marker=True)
    summary = render_body(manifest, results, include_marker=False)
    write_step_summary(summary)

    exit_code = 0
    try:
        comment_url = maybe_upsert_comment(args, body)
    except RuntimeError as exc:
        print(f"Error: failed to create or update PR comment: {exc}")
        comment_url = ""
        exit_code = 1

    review_urls = [
        item.get("review_url", "")
        for item in result_items(manifest, results)
        if item.get("review_url")
    ]
    write_outputs(
        {
            "review-urls": json.dumps(review_urls),
            "comment-url": comment_url,
            "results-json": str(results_path),
        }
    )
    return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=".dep-review-work")
    parser.add_argument("--github-token", default=os.environ.get("INPUT_GITHUB_TOKEN", ""))
    parser.add_argument(
        "--comment-on-pr",
        default=os.environ.get("INPUT_COMMENT_ON_PR", "true").lower() == "true",
        action=argparse.BooleanOptionalAction,
    )
    parser.add_argument("--pr-number", default=os.environ.get("INPUT_PR_NUMBER", ""))
    return parser.parse_args()


def default_manifest() -> dict[str, Any]:
    return {"dependencies": [], "skipped": [], "artifacts": []}


def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return fallback
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        print(f"Warning: could not parse {path}: {exc}")
        return fallback


def result_items(manifest: dict[str, Any], results: dict[str, Any]) -> list[dict[str, Any]]:
    by_slug = {item.get("slug"): item for item in results.get("dependencies", [])}
    items = []
    for dep in manifest.get("dependencies", []):
        result = by_slug.get(dep.get("slug"), {})
        merged = {**dep, **result}
        if not merged.get("status"):
            merged["status"] = "missing-results"
        if not merged.get("severity"):
            merged["severity"] = "unknown"
        if not merged.get("audit_summary"):
            merged["audit_summary"] = "Claude did not write a result for this dependency."
        items.append(merged)
    return items


def render_body(manifest: dict[str, Any], results: dict[str, Any], include_marker: bool) -> str:
    items = result_items(manifest, results)
    lines = []
    if include_marker:
        lines.append(MARKER)
    lines.append("## Cargo dependency review packets")
    lines.append("")

    if not items:
        lines.append("No external Cargo dependency upgrades were prepared for Reviews.")
    else:
        lines.extend(
            [
                "| Dependency | Change | Severity | Status | Reviews packet |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in items:
            review = item.get("review_url")
            review_cell = f"[Open review]({review})" if review else "Not uploaded"
            lines.append(
                "| {name} | {change} | {severity} | {status} | {review} |".format(
                    name=escape_table(item.get("name", "")),
                    change=escape_table(format_change(item)),
                    severity=escape_table(item.get("severity", "unknown")),
                    status=escape_table(item.get("status", "unknown")),
                    review=review_cell,
                )
            )

        lines.append("")
        lines.append("### Audit summaries")
        lines.append("")
        for item in items:
            lines.append(
                f"- **{format_dependency_heading(item)}**: "
                f"{item.get('audit_summary')}"
            )

        lines.append("")
        lines.append("<details>")
        lines.append("<summary>Local dependency checkout commands</summary>")
        lines.append("")
        for item in items:
            command = item.get("local_checkout", {}).get("command", "")
            if not command:
                continue
            lines.append(f"#### {item.get('name')} {item.get('new_version')}")
            lines.append("")
            lines.append("```sh")
            lines.append(command.rstrip())
            lines.append("```")
            lines.append("")
        lines.append("</details>")

    skipped = manifest.get("skipped", [])
    if skipped:
        lines.append("")
        lines.append("### Skipped package changes")
        lines.append("")
        lines.append("| Package | Change | Reason |")
        lines.append("| --- | --- | --- |")
        for item in skipped:
            old = item.get("old_version") or "-"
            new = item.get("new_version") or "-"
            lines.append(
                "| {name} | `{old}` -> `{new}` | {reason} |".format(
                    name=escape_table(item.get("name", "")),
                    old=escape_table(old),
                    new=escape_table(new),
                    reason=escape_table(item.get("reason", "")),
                )
            )

    lines.append("")
    return "\n".join(lines)


def escape_table(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def format_change(item: dict[str, Any]) -> str:
    label = item.get("change_label")
    if label:
        return str(label)

    change_kind = item.get("change_kind")
    new_version = item.get("new_version") or "-"
    if change_kind == "added":
        return f"added {new_version}"

    old_version = item.get("old_version") or "-"
    if change_kind == "source-migration":
        old_kind = item.get("old_source_kind") or "unknown"
        new_kind = item.get("new_source_kind") or item.get("source_kind") or "unknown"
        return f"{old_version} ({old_kind}) -> {new_version} ({new_kind})"

    return f"{old_version} -> {new_version}"


def format_dependency_heading(item: dict[str, Any]) -> str:
    name = item.get("name", "")
    return f"{name} {format_change(item)}".strip()


def write_step_summary(body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(body)


def pull_request_number() -> Optional[int]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).is_file():
        return None
    with open(event_path, "r", encoding="utf-8") as fh:
        event = json.load(fh)
    if "pull_request" in event:
        return int(event["pull_request"]["number"])
    if "issue" in event and event["issue"].get("pull_request"):
        return int(event["issue"]["number"])
    return None


def maybe_upsert_comment(args: argparse.Namespace, body: str) -> str:
    if not args.comment_on_pr:
        print("PR comments disabled.")
        return ""
    if not args.github_token:
        raise RuntimeError("GitHub token missing while PR comments are enabled")

    pr_number = explicit_pr_number(args.pr_number) or pull_request_number()
    if not pr_number:
        print("No pull request context found; skipped PR comment.")
        return ""

    comment_url = upsert_comment(args.github_token, pr_number, body)
    print(f"Updated PR comment: {comment_url}")
    return comment_url


def explicit_pr_number(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None
    try:
        number = int(value)
    except ValueError as exc:
        raise RuntimeError(f"invalid PR number: {value}") from exc
    if number <= 0:
        raise RuntimeError(f"invalid PR number: {value}")
    return number


def upsert_comment(token: str, pr_number: int, body: str) -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise RuntimeError("GITHUB_REPOSITORY is not set")
    api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    comments_url = f"{api_base}/repos/{repo}/issues/{pr_number}/comments"

    existing = find_existing_comment(token, comments_url)
    if existing:
        response = request_json(token, existing["url"], method="PATCH", data={"body": body})
    else:
        response = request_json(token, comments_url, method="POST", data={"body": body})
    return str(response.get("html_url", ""))


def find_existing_comment(token: str, comments_url: str) -> Optional[dict[str, Any]]:
    page = 1
    while True:
        url = f"{comments_url}?per_page=100&page={page}"
        comments = request_json(token, url)
        if not comments:
            return None
        for comment in comments:
            if MARKER in comment.get("body", ""):
                return comment
        page += 1


def request_json(
    token: str,
    url: str,
    method: str = "GET",
    data: Optional[dict[str, Any]] = None,
) -> Any:
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "dep-reviews-packet-action",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed: {exc.code} {detail}") from exc
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def write_outputs(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write(f"{key}={value}\n")


if __name__ == "__main__":
    raise SystemExit(main())
