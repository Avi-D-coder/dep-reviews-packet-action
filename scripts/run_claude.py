#!/usr/bin/env python3
"""Run Claude Code CLI to audit prepared dependency diffs."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional


ALLOWED_EXTRA_VALUE_FLAGS = {
    "--betas",
    "--effort",
    "--fallback-model",
    "--max-budget-usd",
    "--max-turns",
}
ALLOWED_EXTRA_BOOL_FLAGS = {
    "--include-partial-messages",
}


def main() -> int:
    model = os.environ.get("INPUT_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    action_path = Path(os.environ.get("GITHUB_ACTION_PATH", ".")).resolve()
    skill_root = action_path / ".claude" / "skills"
    manifest = load_manifest(Path(".dep-review-work") / "manifest.json")
    key_file = Path(required_env("INPUT_ANTHROPIC_API_KEY_FILE"))
    claude_home = create_claude_auth_home(key_file)
    try:
        env = claude_child_env(os.environ, claude_home)
        result_paths = []
        for dependency in manifest.get("dependencies", []):
            result_path = dependency_result_path(dependency)
            initialize_result_file(dependency, result_path)
            conversation_cwd = dependency_conversation_cwd(dependency)
            prompt = audit_prompt(dependency, result_path, skill_root)
            cmd = build_command(
                model,
                prompt,
                dependency,
                result_path,
                skill_root,
                key_file,
                claude_home,
                os.environ.get("INPUT_CLAUDE_ARGS", ""),
            )
            print(
                f"Starting separate Claude Code conversation for "
                f"{dependency.get('name')} {dependency.get('change_label')}",
                flush=True,
            )
            subprocess.run(cmd, check=True, env=env, cwd=conversation_cwd)
            result_paths.append(result_path)
        merge_results(manifest, result_paths)
    finally:
        shutil.rmtree(claude_home, ignore_errors=True)
    return 0


def build_command(
    model: str,
    prompt: str,
    dependency: dict[str, Any],
    result_path: Path,
    skill_root: Path,
    key_file: Path,
    claude_home: Path,
    claude_args: str = "",
) -> list[str]:
    dep_dir = result_path.parent
    add_dirs = [dep_dir, skill_root]
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--setting-sources",
        "user",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--tools",
        "Read,Grep,Glob,LS,Edit,Write",
        "--add-dir",
        *[str(directory) for directory in add_dirs],
        "--max-turns",
        "25",
        "--permission-mode",
        "dontAsk",
        "--verbose",
        "--output-format",
        "stream-json",
        "--allowedTools",
        *allowed_tools_for_dependency(dependency, result_path, skill_root),
        "--disallowedTools",
        *disallowed_tools_for_secrets(key_file, claude_home),
    ]
    cmd.extend(extra_args(claude_args))
    return cmd


def allowed_tools_for_dependency(
    dependency: dict[str, Any],
    result_path: Path,
    skill_root: Path,
) -> list[str]:
    repo_path = Path(dependency["repo_path"])
    diff_path = Path(dependency["diff_path"])
    hunks_path = Path(dependency["hunks_path"])
    packet_path = Path(dependency["packet_path"])
    dep_dir = packet_path.parent
    return [
        f"Read({permission_path(repo_path)}/**)",
        f"Read({permission_path(diff_path)})",
        f"Read({permission_path(hunks_path)})",
        f"Read({permission_path(packet_path)})",
        f"Read({permission_path(result_path)})",
        f"Read({permission_path(skill_root)}/**)",
        "Grep",
        "Glob",
        "LS",
        f"Edit({permission_path(packet_path)})",
        f"Edit({permission_path(result_path)})",
        f"Write({permission_path(packet_path)})",
        f"Write({permission_path(result_path)})",
    ]


def disallowed_tools_for_secrets(key_file: Path, claude_home: Path) -> list[str]:
    return [
        "Bash",
        "WebFetch",
        "WebSearch",
        "Task",
        f"Read({permission_path(key_file)})",
        f"Read({permission_path(key_file.parent)}/**)",
        f"Read({permission_path(claude_home)}/**)",
        "Read(//proc/**)",
    ]


def permission_path(path: Path) -> str:
    absolute = path.expanduser().resolve(strict=False)
    return "//" + str(absolute).lstrip("/")


def dependency_conversation_cwd(dependency: dict[str, Any]) -> Path:
    cwd = Path(dependency["packet_path"]).parent / "claude-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


def audit_prompt(dependency: dict[str, Any], result_path: Path, skill_root: Path) -> str:
    dependency_json = json.dumps(dependency, indent=2, sort_keys=True)
    return f"""\
You are auditing exactly one third-party Cargo dependency source change prepared by this action.

This is a fresh Claude Code conversation for this dependency only. You are running from a neutral working directory, not from dependency-controlled project config. Use the absolute paths in the dependency entry.

Read the Reviews packet guidance from this file before editing the packet:
- {skill_root / "writing-review-packets" / "SKILL.md"}

Audit only this dependency entry. Do not inspect or upload any other dependency and do not read .dep-review-work/manifest.json:

```json
{dependency_json}
```

Steps:
1. Read diff_path and hunks_path carefully. Do not run dependency build scripts, tests, examples, shell commands, or arbitrary dependency code.
2. Treat the diff as the primary review target. Use the full new dependency source tree at repo_path as context for changed files, referenced symbols, feature gates, build configuration, platform-specific paths, and security-sensitive call paths.
3. Perform a security review of the dependency source change with a supply-chain attack mindset. Look for malicious or suspicious behavior, backdoors, typosquat-style package/source changes, hidden code execution, credential or environment access, network/file/process I/O that could be exploited by consumers, unsafe code that could cause undefined behavior or memory unsafety, FFI, proc macros, build scripts, dependency graph changes, generated/obfuscated/minified code, parser/deserializer edge cases, crypto/auth changes, and privilege-boundary mistakes.
4. Refine the provided packet_path markdown into a useful Reviews packet. Keep every changed line covered exactly once by @hunk references. Include the full contents of your security audit in the packet itself: audit verdict, notable findings, suspicious build script or proc-macro changes, sensitive code paths reviewed, limitations, and a guided walkthrough of the diff.
5. Do not run reviews push. A trusted post-processing step uploads the packet after this Claude conversation finishes.

The result file at {result_path} already exists. Replace its contents when finished with this exact JSON shape:
{{
  "dependencies": [
    {{
      "slug": "manifest slug",
      "status": "packet-ready",
      "review_url": "",
      "patchset_number": null,
      "severity": "none|low|medium|high|critical",
      "audit_summary": "one concise sentence"
    }}
  ]
}}

If a dependency cannot be audited, include it with status "failed", an empty review_url, severity "unknown", and audit_summary explaining the failure.
"""


def load_manifest(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dependency_result_path(dependency: dict[str, Any]) -> Path:
    packet_path = Path(dependency["packet_path"])
    return packet_path.with_name("result.json")


def initialize_result_file(dependency: dict[str, Any], result_path: Path) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "slug": dependency.get("slug", ""),
                        "status": "failed",
                        "review_url": "",
                        "patchset_number": None,
                        "severity": "unknown",
                        "audit_summary": "Claude did not update the per-dependency result file.",
                    }
                ]
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def merge_results(manifest: dict[str, Any], result_paths: list[Path]) -> None:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in result_paths:
        if not path.is_file():
            continue
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for item in data.get("dependencies", []):
            slug = item.get("slug")
            if slug in seen:
                continue
            seen.add(slug)
            merged.append(item)

    results_path = Path(manifest["results_path"])
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps({"dependencies": merged}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def create_claude_auth_home(key_file: Path) -> Path:
    if not key_file.is_file():
        raise SystemExit(f"INPUT_ANTHROPIC_API_KEY_FILE does not exist: {key_file}")
    runner_temp = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    home = Path(tempfile.mkdtemp(prefix="dep-reviews-claude-home-", dir=runner_temp))
    claude_dir = home / ".claude"
    claude_dir.mkdir(mode=0o700)
    helper = claude_dir / "api-key-helper.sh"
    helper.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f"cat {shell_quote(str(key_file))}\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    settings = {
        "apiKeyHelper": str(helper),
        "cleanupPeriodDays": 1,
        "includeCoAuthoredBy": False,
        "includeGitInstructions": False,
        "permissions": {
            "defaultMode": "dontAsk",
            "disableBypassPermissionsMode": "disable",
            "deny": [
                *disallowed_tools_for_secrets(key_file, home),
            ]
        },
    }
    (claude_dir / "settings.json").write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return home


def claude_child_env(base_env: dict[str, str], claude_home: Path) -> dict[str, str]:
    keep = {
        "CI",
        "GITHUB_ACTIONS",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "RUNNER_ARCH",
        "RUNNER_OS",
        "RUNNER_TEMP",
        "SHELL",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
    }
    env = {
        key: value
        for key, value in base_env.items()
        if key in keep or key.upper().endswith("_PROXY")
    }
    env["HOME"] = str(claude_home)
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"
    return env


def extra_args(value: str) -> list[str]:
    if not value.strip():
        return []
    args = shlex.split(value)
    index = 0
    while index < len(args):
        arg = args[index]
        flag = arg.split("=", 1)[0]
        if flag in ALLOWED_EXTRA_BOOL_FLAGS:
            index += 1
            continue
        if flag in ALLOWED_EXTRA_VALUE_FLAGS:
            if "=" in arg:
                index += 1
                continue
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                raise SystemExit(f"claude-args option {flag} requires a value")
            index += 2
            continue
        raise SystemExit(f"claude-args option {flag} is not allowed in the hardened runner")
    return args


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def required_env(name: str) -> str:
    value: Optional[str] = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
