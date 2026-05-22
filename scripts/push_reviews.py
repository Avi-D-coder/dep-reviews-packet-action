#!/usr/bin/env python3
"""Upload prepared Reviews packets outside the Claude Code conversation."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Optional


REVIEW_URL_RE = re.compile(r"https?://[^\s)>\"]+")
PATCHSET_RE = re.compile(r"patchset(?:\s+number)?\s*[:#]?\s*(\d+)", re.IGNORECASE)


def main() -> int:
    workdir = Path(".dep-review-work")
    manifest = load_json(workdir / "manifest.json", {"dependencies": []})
    results = load_json(workdir / "results.json", {"dependencies": []})
    updated = upload_all(manifest, results)
    write_json(workdir / "results.json", updated)
    return 0


def upload_all(manifest: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    by_slug = {item.get("slug"): item for item in results.get("dependencies", [])}
    uploaded = []
    for dependency in manifest.get("dependencies", []):
        slug = dependency.get("slug")
        item = {**dependency, **by_slug.get(slug, {})}
        if item.get("status") == "failed":
            uploaded.append(item)
            continue
        if item.get("status") != "packet-ready":
            item["status"] = "failed"
            item["review_url"] = ""
            item["patchset_number"] = None
            item["severity"] = item.get("severity") or "unknown"
            item["audit_summary"] = item.get("audit_summary") or "Claude did not mark the packet ready for upload."
            uploaded.append(item)
            continue
        uploaded.append(upload_dependency_packet(item))
    return {"dependencies": uploaded}


def upload_dependency_packet(item: dict[str, Any]) -> dict[str, Any]:
    repo_path = Path(item["repo_path"])
    packet_path = Path(item["packet_path"])
    title = f"Cargo dependency audit: {item.get('name')} {item.get('change_label')}"
    cmd = [
        "reviews",
        "push",
        "--title",
        title,
        "--description",
        "Automated dependency source audit from GitHub Actions.",
        "--range",
        "HEAD~1..HEAD",
        "--packet",
        str(packet_path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=repo_path,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        item["status"] = "failed"
        item["review_url"] = ""
        item["patchset_number"] = None
        item["severity"] = item.get("severity") or "unknown"
        output = exc.stdout if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        item["audit_summary"] = f"Reviews upload failed: {trim_output(output)}"
        return item

    output = completed.stdout or ""
    review_url = parse_review_url(output)
    if not review_url:
        item["status"] = "failed"
        item["review_url"] = ""
        item["patchset_number"] = None
        item["severity"] = item.get("severity") or "unknown"
        item["audit_summary"] = f"Reviews upload did not return a review URL: {trim_output(output)}"
        return item

    item["status"] = "uploaded"
    item["review_url"] = review_url
    item["patchset_number"] = parse_patchset_number(output)
    item["severity"] = item.get("severity") or "unknown"
    item["audit_summary"] = item.get("audit_summary") or "Packet uploaded to Reviews."
    return item


def parse_review_url(output: str) -> str:
    match = REVIEW_URL_RE.search(output)
    return match.group(0).rstrip(".,") if match else ""


def parse_patchset_number(output: str) -> Optional[int]:
    match = PATCHSET_RE.search(output)
    return int(match.group(1)) if match else None


def trim_output(output: Optional[str]) -> str:
    if not output:
        return "no output"
    return " ".join(output.split())[:500]


def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return fallback
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
