#!/usr/bin/env python3
"""Run Codex CLI to audit prepared dependency diffs."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional


ALLOWED_EXTRA_VALUE_FLAGS = {
    "--color",
}
ALLOWED_EXTRA_BOOL_FLAGS = {
    "--strict-config",
}
SHELL_ENV_EXCLUDES = [
    "*KEY*",
    "*SECRET*",
    "*TOKEN*",
    "CODEX_*",
    "GITHUB_*",
    "INPUT_*",
    "OPENAI_*",
    "REVIEWS_*",
]
PERMISSIONS_PROFILE_NAME = "dep-review"
PERMISSIONS_PROFILE_ID = "dep-review-dependency"
RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dependencies": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "status": {"type": "string", "enum": ["packet-ready", "failed"]},
                    "review_url": {"type": "string"},
                    "patchset_number": {"type": ["integer", "null"]},
                    "severity": {
                        "type": "string",
                        "enum": ["none", "low", "medium", "high", "critical", "unknown"],
                    },
                    "audit_summary": {"type": "string"},
                },
                "required": [
                    "slug",
                    "status",
                    "review_url",
                    "patchset_number",
                    "severity",
                    "audit_summary",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["dependencies"],
    "additionalProperties": False,
}


def main() -> int:
    model = os.environ.get("INPUT_CODEX_MODEL", "").strip()
    action_path = Path(os.environ.get("GITHUB_ACTION_PATH", ".")).resolve()
    skill_root = select_skill_root(action_path)
    manifest = load_manifest(Path(".dep-review-work") / "manifest.json")
    key_file = Path(required_env("INPUT_OPENAI_API_KEY_FILE"))
    codex_home = create_codex_home()
    try:
        env = codex_child_env(os.environ, codex_home, key_file)
        result_paths = []
        for dependency in manifest.get("dependencies", []):
            result_path = dependency_result_path(dependency)
            initialize_result_file(dependency, result_path)
            try:
                run_dependency(
                    dependency,
                    result_path,
                    skill_root,
                    model,
                    env,
                    os.environ.get("INPUT_CODEX_ARGS", ""),
                )
            except Exception as exc:  # noqa: BLE001 - dependency failures are reported per item.
                write_dependency_result(
                    result_path,
                    failed_dependency_result(dependency, f"Codex runner failed: {trim_exception(exc)}"),
                )
            result_paths.append(result_path)
        merge_results(manifest, result_paths)
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)
    return 0


def run_dependency(
    dependency: dict[str, Any],
    result_path: Path,
    skill_root: Path,
    model: str,
    env: dict[str, str],
    codex_args: str = "",
) -> None:
    dep_dir = dependency_workspace(dependency)
    dep_dir.mkdir(parents=True, exist_ok=True)
    guidance_path = copy_packet_guidance(skill_root, dep_dir)
    schema_path = dep_dir / "codex-result.schema.json"
    final_message_path = dep_dir / "codex-final-result.json"
    write_json(schema_path, RESULT_SCHEMA)
    if final_message_path.exists():
        final_message_path.unlink()
    write_dependency_permissions_profile(Path(env["CODEX_HOME"]), dep_dir, Path(dependency["packet_path"]), final_message_path)

    snapshot_before = snapshot_tree(dep_dir)
    prompt = audit_prompt(dependency, guidance_path)
    cmd = build_command(model, prompt, dep_dir, schema_path, final_message_path, codex_args)

    print(
        f"Starting separate Codex CLI run for {dependency.get('name')} {dependency.get('change_label')}",
        flush=True,
    )
    try:
        subprocess.run(cmd, check=True, env=env, cwd=dep_dir)
        result = parse_final_result(final_message_path, dependency)
    except (OSError, subprocess.CalledProcessError) as exc:
        result = failed_dependency_result(dependency, f"Codex CLI failed: {trim_exception(exc)}")

    packet_path = Path(dependency["packet_path"])
    if not packet_path.is_file():
        result = failed_dependency_result(dependency, "Codex did not leave packet_path as a file.")

    changed = unexpected_workspace_changes(
        snapshot_before,
        snapshot_tree(dep_dir),
        {
            relative_to(dep_dir, packet_path),
            relative_to(dep_dir, final_message_path),
        },
    )
    if changed:
        result = failed_dependency_result(
            dependency,
            f"Codex changed unauthorized workspace files: {format_paths(changed)}",
        )

    write_dependency_result(result_path, result)


def build_command(
    model: str,
    prompt: str,
    dep_dir: Path,
    schema_path: Path,
    final_message_path: Path,
    codex_args: str = "",
) -> list[str]:
    cmd = [
        "codex",
        "exec",
        "--cd",
        str(dep_dir),
        "--skip-git-repo-check",
        "--ephemeral",
        "--json",
        "--profile",
        PERMISSIONS_PROFILE_NAME,
        "--sandbox",
        "workspace-write",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(final_message_path),
        "--ignore-rules",
        "--disable",
        "hooks",
        "--disable",
        "multi_agent",
        *codex_config_overrides(),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(extra_args(codex_args))
    cmd.append(prompt)
    return cmd


def codex_config_overrides() -> list[str]:
    return [
        "--config",
        'approval_policy="never"',
        "--config",
        'sandbox_mode="workspace-write"',
        "--config",
        f"default_permissions={toml_string(PERMISSIONS_PROFILE_ID)}",
        "--config",
        'web_search="disabled"',
        "--config",
        "project_root_markers=[]",
        "--config",
        "sandbox_workspace_write.writable_roots=[]",
        "--config",
        "sandbox_workspace_write.network_access=false",
        "--config",
        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "--config",
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "--config",
        'shell_environment_policy.inherit="none"',
        "--config",
        "shell_environment_policy.ignore_default_excludes=false",
        "--config",
        f"shell_environment_policy.exclude={toml_array(SHELL_ENV_EXCLUDES)}",
        "--config",
        f"shell_environment_policy.set={toml_inline_table(shell_env_set())}",
    ]


def create_codex_home() -> Path:
    runner_temp = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    home = Path(tempfile.mkdtemp(prefix="dep-reviews-codex-home-", dir=runner_temp))
    (home / "config.toml").write_text(codex_config_text(), encoding="utf-8")
    return home


def codex_config_text() -> str:
    return "\n".join(
        [
            'approval_policy = "never"',
            'sandbox_mode = "workspace-write"',
            'web_search = "disabled"',
            "project_root_markers = []",
            "",
            "[sandbox_workspace_write]",
            "writable_roots = []",
            "network_access = false",
            "exclude_tmpdir_env_var = true",
            "exclude_slash_tmp = true",
            "",
            "[features]",
            "hooks = false",
            "multi_agent = false",
            "",
            "[shell_environment_policy]",
            'inherit = "none"',
            "ignore_default_excludes = false",
            f"exclude = {toml_array(SHELL_ENV_EXCLUDES)}",
            f"set = {toml_inline_table(shell_env_set())}",
            "",
        ]
    )


def write_dependency_permissions_profile(
    codex_home: Path,
    dep_dir: Path,
    packet_path: Path,
    final_message_path: Path,
) -> Path:
    codex_home.mkdir(parents=True, exist_ok=True)
    profile_path = codex_home / f"{PERMISSIONS_PROFILE_NAME}.config.toml"
    dep_root = dep_dir.resolve(strict=False)
    packet_rel = relative_to(dep_dir, packet_path)
    final_message_rel = relative_to(dep_dir, final_message_path)
    profile_path.write_text(
        "\n".join(
            [
                f"default_permissions = {toml_string(PERMISSIONS_PROFILE_ID)}",
                "",
                f"[permissions.{PERMISSIONS_PROFILE_ID}.workspace_roots]",
                f"{toml_string(str(dep_root))} = true",
                "",
                f"[permissions.{PERMISSIONS_PROFILE_ID}.filesystem]",
                '":minimal" = "read"',
                "",
                f"[permissions.{PERMISSIONS_PROFILE_ID}.filesystem.\":workspace_roots\"]",
                '"." = "read"',
                f"{toml_string(packet_rel)} = \"write\"",
                f"{toml_string(final_message_rel)} = \"write\"",
                "",
                f"[permissions.{PERMISSIONS_PROFILE_ID}.network]",
                "enabled = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return profile_path


def shell_env_set() -> dict[str, str]:
    temp_dir = os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR") or tempfile.gettempdir()
    return {
        "HOME": temp_dir,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TEMP": temp_dir,
        "TMP": temp_dir,
        "TMPDIR": temp_dir,
    }


def toml_array(values: list[str]) -> str:
    return "[" + ", ".join(toml_string(value) for value in values) + "]"


def toml_inline_table(values: dict[str, str]) -> str:
    return "{ " + ", ".join(f"{key} = {toml_string(value)}" for key, value in values.items()) + " }"


def toml_string(value: str) -> str:
    return json.dumps(value)


def codex_child_env(base_env: dict[str, str], codex_home: Path, key_file: Path) -> dict[str, str]:
    if not key_file.is_file():
        raise SystemExit(f"INPUT_OPENAI_API_KEY_FILE does not exist: {key_file}")
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
    env["CODEX_HOME"] = str(codex_home)
    env["HOME"] = str(codex_home)
    env["CODEX_API_KEY"] = key_file.read_text(encoding="utf-8")
    env.setdefault("RUST_LOG", "error")
    return env


def select_skill_root(action_path: Path) -> Path:
    candidate = action_path / ".codex" / "skills"
    if (candidate / "writing-review-packets" / "SKILL.md").is_file():
        return candidate
    raise SystemExit("Could not find vendored writing-review-packets skill.")


def copy_packet_guidance(skill_root: Path, dep_dir: Path) -> Path:
    source = skill_root / "writing-review-packets" / "SKILL.md"
    if not source.is_file():
        raise SystemExit(f"Packet guidance does not exist: {source}")
    guidance_path = dep_dir / "packet-guidance.md"
    guidance_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return guidance_path


def audit_prompt(dependency: dict[str, Any], guidance_path: Path) -> str:
    dependency_json = json.dumps(dependency, indent=2, sort_keys=True)
    return f"""\
You are auditing exactly one third-party Cargo dependency source change prepared by this action.

This is a fresh Codex CLI non-interactive run for this dependency only. You are running in a dependency-only workspace. Use the absolute paths in the dependency entry, and do not inspect or upload any other dependency.

Read the Reviews packet guidance from this file before editing the packet:
- {guidance_path}

Audit only this dependency entry. Do not read .dep-review-work/manifest.json:

```json
{dependency_json}
```

Steps:
1. Read diff_path and hunks_path carefully. Do not run dependency build scripts, tests, examples, shell commands, or arbitrary dependency code.
2. Treat the diff as the primary review target. Use the full new dependency source tree at repo_path as context for changed files, referenced symbols, feature gates, build configuration, platform-specific paths, and security-sensitive call paths.
3. Perform a security review of the dependency source change with a supply-chain attack mindset. Look for malicious or suspicious behavior, backdoors, typosquat-style package/source changes, hidden code execution, credential or environment access, network/file/process I/O that could be exploited by consumers, unsafe code that could cause undefined behavior or memory unsafety, FFI, proc macros, build scripts, dependency graph changes, generated/obfuscated/minified code, parser/deserializer edge cases, crypto/auth changes, and privilege-boundary mistakes.
4. Refine only packet_path markdown into a useful Reviews packet. Keep every changed line covered exactly once by @hunk references. Include the full contents of your security audit in the packet itself: audit verdict, notable findings, suspicious build script or proc-macro changes, sensitive code paths reviewed, limitations, and a guided walkthrough of the diff.
5. Do not run reviews push. A trusted post-processing step uploads the packet after this Codex run finishes.
6. Do not edit result.json or any file other than packet_path. The trusted wrapper writes result.json from your final response.

Your final response must match the configured JSON schema and use this shape:
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

If the dependency cannot be audited, return it with status "failed", an empty review_url, severity "unknown", and audit_summary explaining the failure.
"""


def parse_final_result(path: Path, dependency: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return failed_dependency_result(dependency, "Codex did not write a final structured result.")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return failed_dependency_result(dependency, f"Codex final result was not valid JSON: {trim_exception(exc)}")
    return normalize_result(data, dependency)


def normalize_result(data: Any, dependency: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return failed_dependency_result(dependency, "Codex final result was not a JSON object.")
    items = data.get("dependencies")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return failed_dependency_result(dependency, "Codex final result did not contain exactly one dependency.")
    item = items[0]
    expected_slug = dependency.get("slug", "")
    if item.get("slug") != expected_slug:
        return failed_dependency_result(dependency, "Codex final result slug did not match the dependency.")
    status = item.get("status")
    severity = item.get("severity")
    if status not in {"packet-ready", "failed"}:
        return failed_dependency_result(dependency, "Codex final result used an invalid status.")
    if severity not in {"none", "low", "medium", "high", "critical", "unknown"}:
        return failed_dependency_result(dependency, "Codex final result used an invalid severity.")
    audit_summary = item.get("audit_summary")
    if not isinstance(audit_summary, str) or not audit_summary.strip():
        return failed_dependency_result(dependency, "Codex final result omitted audit_summary.")
    return {
        "dependencies": [
            {
                "slug": expected_slug,
                "status": status,
                "review_url": "",
                "patchset_number": None,
                "severity": severity,
                "audit_summary": " ".join(audit_summary.split()),
            }
        ]
    }


def failed_dependency_result(dependency: dict[str, Any], audit_summary: str) -> dict[str, Any]:
    return {
        "dependencies": [
            {
                "slug": dependency.get("slug", ""),
                "status": "failed",
                "review_url": "",
                "patchset_number": None,
                "severity": "unknown",
                "audit_summary": audit_summary,
            }
        ]
    }


def load_manifest(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dependency_workspace(dependency: dict[str, Any]) -> Path:
    return Path(dependency["packet_path"]).parent


def dependency_result_path(dependency: dict[str, Any]) -> Path:
    return Path(dependency["packet_path"]).with_name("result.json")


def initialize_result_file(dependency: dict[str, Any], result_path: Path) -> None:
    write_dependency_result(
        result_path,
        failed_dependency_result(dependency, "Codex did not produce a per-dependency result."),
    )


def write_dependency_result(path: Path, result: dict[str, Any]) -> None:
    write_json(path, result)


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
    write_json(results_path, {"dependencies": merged})


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for filename in filenames:
            path = Path(dirpath) / filename
            rel = relative_to(root, path)
            try:
                if path.is_symlink():
                    snapshot[rel] = "symlink:" + os.readlink(path)
                elif path.is_file():
                    snapshot[rel] = "sha256:" + sha256_file(path)
                else:
                    snapshot[rel] = "other"
            except OSError as exc:
                snapshot[rel] = "error:" + str(exc)
    return snapshot


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unexpected_workspace_changes(
    before: dict[str, str],
    after: dict[str, str],
    allowed_paths: set[str],
) -> list[str]:
    changed = set(before) ^ set(after)
    changed.update(path for path in before.keys() & after.keys() if before[path] != after[path])
    return sorted(path for path in changed if path not in allowed_paths)


def relative_to(root: Path, path: Path) -> str:
    return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()


def format_paths(paths: list[str]) -> str:
    joined = ", ".join(paths[:8])
    if len(paths) > 8:
        joined += f", and {len(paths) - 8} more"
    return joined


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
                raise SystemExit(f"codex-args option {flag} requires a value")
            index += 2
            continue
        raise SystemExit(f"codex-args option {flag} is not allowed in the hardened runner")
    return args


def trim_exception(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return f"exit status {exc.returncode}"
    return " ".join(str(exc).split())[:500] or exc.__class__.__name__


def required_env(name: str) -> str:
    value: Optional[str] = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
