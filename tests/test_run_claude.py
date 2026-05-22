import json
import os
import stat
import sys
import tempfile
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_claude  # noqa: E402


class RunClaudeTests(unittest.TestCase):
    def test_build_command_uses_narrow_tools_and_streams_verbose_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dependency, result_path = dependency_fixture(root, "itoa")
            key_file = root / "secret" / "anthropic-key"
            key_file.parent.mkdir()
            key_file.write_text("secret", encoding="utf-8")
            claude_home = root / "claude-home"
            skill_root = root / "action" / ".claude" / "skills"

            cmd = run_claude.build_command(
                "sonnet",
                "prompt",
                dependency,
                result_path,
                skill_root,
                key_file,
                claude_home,
            )

            self.assertEqual(cmd[:3], ["claude", "-p", "prompt"])
            self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
            self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
            self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Grep,Glob,LS,Edit,Write")
            self.assertIn("--no-session-persistence", cmd)
            self.assertIn("--disable-slash-commands", cmd)
            self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
            self.assertIn("--verbose", cmd)
            self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
            self.assertNotIn("Read", cmd)
            allowed = cmd[cmd.index("--allowedTools") : cmd.index("--disallowedTools")]
            self.assertNotIn("Bash", allowed)
            self.assertIn("Bash", cmd[cmd.index("--disallowedTools") :])
            self.assertIn("Grep", cmd)
            self.assertIn(f"Read({run_claude.permission_path(root / 'itoa' / 'repo')}/**)", cmd)
            self.assertIn(f"Read({run_claude.permission_path(skill_root)}/**)", cmd)
            self.assertIn(f"Read({run_claude.permission_path(result_path)})", cmd)
            self.assertIn(f"Edit({run_claude.permission_path(root / 'itoa' / 'packet.md')})", cmd)
            self.assertIn(f"Write({run_claude.permission_path(root / 'itoa' / 'packet.md')})", cmd)
            self.assertIn(f"Edit({run_claude.permission_path(result_path)})", cmd)
            self.assertIn(f"Write({run_claude.permission_path(result_path)})", cmd)
            self.assertIn(f"Read({run_claude.permission_path(key_file.parent)}/**)", cmd)

    def test_build_command_rejects_security_sensitive_extra_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dependency, result_path = dependency_fixture(root, "itoa")
            key_file = root / "secret" / "anthropic-key"
            key_file.parent.mkdir()
            key_file.write_text("secret", encoding="utf-8")

            with self.assertRaises(SystemExit):
                run_claude.build_command(
                    "sonnet",
                    "prompt",
                    dependency,
                    result_path,
                    root / "skills",
                    key_file,
                    root / "claude-home",
                    "--permission-mode bypassPermissions",
                )

    def test_build_command_allows_limited_extra_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dependency, result_path = dependency_fixture(root, "itoa")
            key_file = root / "secret" / "anthropic-key"
            key_file.parent.mkdir()
            key_file.write_text("secret", encoding="utf-8")

            cmd = run_claude.build_command(
                "sonnet",
                "prompt",
                dependency,
                result_path,
                root / "skills",
                key_file,
                root / "claude-home",
                "--max-turns 5 --effort high --include-partial-messages",
            )

            self.assertEqual(cmd[-5:], ["--max-turns", "5", "--effort", "high", "--include-partial-messages"])

    def test_audit_prompt_is_limited_to_one_dependency_and_no_upload(self):
        dependency = {
            "slug": "itoa-1-0-0-to-1-0-1",
            "name": "itoa",
            "change_label": "1.0.0 -> 1.0.1",
            "repo_path": "/work/.dep-review-work/deps/itoa/repo",
            "diff_path": "/work/.dep-review-work/deps/itoa/diff.patch",
            "hunks_path": "/work/.dep-review-work/deps/itoa/hunks.json",
            "packet_path": "/work/.dep-review-work/deps/itoa/packet.md",
        }

        prompt = run_claude.audit_prompt(
            dependency,
            Path("/work/.dep-review-work/deps/itoa/result.json"),
            Path("/action/.claude/skills"),
        )

        self.assertIn("fresh Claude Code conversation for this dependency only", prompt)
        self.assertIn("neutral working directory", prompt)
        self.assertIn("/action/.claude/skills/writing-review-packets/SKILL.md", prompt)
        self.assertIn('"slug": "itoa-1-0-0-to-1-0-1"', prompt)
        self.assertIn("Read, Grep, Glob, and LS only", prompt)
        self.assertIn("full contents of your security audit", prompt)
        self.assertIn("limitations", prompt)
        self.assertIn("Do not run reviews push", prompt)
        self.assertIn('"status": "packet-ready"', prompt)
        self.assertNotIn("For each dependency", prompt)

    def test_dependency_result_path_is_next_to_packet(self):
        dependency = {"packet_path": "/work/.dep-review-work/deps/itoa/packet.md"}

        self.assertEqual(
            run_claude.dependency_result_path(dependency),
            Path("/work/.dep-review-work/deps/itoa/result.json"),
        )

    def test_initialize_result_file_creates_failed_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "dep" / "result.json"

            run_claude.initialize_result_file({"slug": "dep"}, result_path)

            data = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(data["dependencies"][0]["slug"], "dep")
            self.assertEqual(data["dependencies"][0]["status"], "failed")
            self.assertEqual(data["dependencies"][0]["severity"], "unknown")

    def test_merge_results_combines_per_dependency_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dep_a = root / "a" / "result.json"
            dep_b = root / "b" / "result.json"
            dep_a.parent.mkdir()
            dep_b.parent.mkdir()
            dep_a.write_text(
                '{"dependencies":[{"slug":"a","status":"packet-ready","review_url":""}]}',
                encoding="utf-8",
            )
            dep_b.write_text(
                '{"dependencies":[{"slug":"b","status":"failed","review_url":""}]}',
                encoding="utf-8",
            )

            results_path = root / "results.json"
            run_claude.merge_results({"results_path": str(results_path)}, [dep_a, dep_b])
            data = json.loads(results_path.read_text(encoding="utf-8"))

            self.assertEqual([item["slug"] for item in data["dependencies"]], ["a", "b"])

    def test_create_claude_auth_home_uses_helper_and_denies_secret_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_runner_temp = os.environ.get("RUNNER_TEMP")
            os.environ["RUNNER_TEMP"] = str(root)
            key_file = root / "keys" / "anthropic"
            key_file.parent.mkdir()
            key_file.write_text("secret", encoding="utf-8")

            try:
                claude_home = run_claude.create_claude_auth_home(key_file)
            finally:
                if original_runner_temp is None:
                    os.environ.pop("RUNNER_TEMP", None)
                else:
                    os.environ["RUNNER_TEMP"] = original_runner_temp
            settings = json.loads((claude_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            helper = claude_home / ".claude" / "api-key-helper.sh"

            self.assertEqual(settings["apiKeyHelper"], str(helper))
            self.assertEqual(settings["permissions"]["defaultMode"], "dontAsk")
            self.assertEqual(settings["permissions"]["disableBypassPermissionsMode"], "disable")
            self.assertIn(f"Read({run_claude.permission_path(key_file.parent)}/**)", settings["permissions"]["deny"])
            self.assertIn("Bash", settings["permissions"]["deny"])
            self.assertEqual(stat.S_IMODE(helper.stat().st_mode), 0o700)
            self.assertIn(str(key_file), helper.read_text(encoding="utf-8"))

    def test_claude_child_env_removes_secrets_and_uses_temp_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / "claude-home"
            env = run_claude.claude_child_env(
                {
                    "PATH": "/bin",
                    "INPUT_ANTHROPIC_API_KEY": "secret",
                    "ANTHROPIC_API_KEY": "secret",
                    "REVIEWS_API_KEY": "secret",
                    "GITHUB_TOKEN": "secret",
                    "HTTPS_PROXY": "http://proxy",
                },
                claude_home,
            )

            self.assertEqual(env["HOME"], str(claude_home))
            self.assertEqual(env["PATH"], "/bin")
            self.assertEqual(env["HTTPS_PROXY"], "http://proxy")
            self.assertNotIn("INPUT_ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("REVIEWS_API_KEY", env)
            self.assertNotIn("GITHUB_TOKEN", env)

    def test_main_starts_one_restricted_claude_process_per_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_file = root / "keys" / "anthropic"
            key_file.parent.mkdir()
            key_file.write_text("secret", encoding="utf-8")
            dep_a, result_a = dependency_fixture(root, "a")
            dep_b, result_b = dependency_fixture(root, "b")
            manifest = {
                "results_path": str(root / "results.json"),
                "dependencies": [dep_a, dep_b],
            }
            calls = []

            def fake_run(cmd, check, env, cwd):
                calls.append((cmd, check, env, cwd))
                result_path = result_a if Path(cwd).parent.name == "a" else result_b
                self.assertTrue(result_path.is_file())
                placeholder = json.loads(result_path.read_text(encoding="utf-8"))
                self.assertEqual(placeholder["dependencies"][0]["status"], "failed")
                result_path.write_text(
                    json.dumps(
                        {
                            "dependencies": [
                                {
                                    "slug": Path(cwd).parent.name,
                                    "status": "packet-ready",
                                    "review_url": "",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

            original_load_manifest = run_claude.load_manifest
            original_subprocess_run = run_claude.subprocess.run
            original_environ = dict(run_claude.os.environ)
            try:
                run_claude.load_manifest = lambda path: manifest
                run_claude.subprocess.run = fake_run
                run_claude.os.environ.clear()
                run_claude.os.environ.update(
                    {
                        "INPUT_ANTHROPIC_API_KEY_FILE": str(key_file),
                        "INPUT_CLAUDE_MODEL": "sonnet",
                        "GITHUB_ACTION_PATH": str(root / "action"),
                        "RUNNER_TEMP": str(root),
                    }
                )

                self.assertEqual(run_claude.main(), 0)
            finally:
                run_claude.load_manifest = original_load_manifest
                run_claude.subprocess.run = original_subprocess_run
                run_claude.os.environ.clear()
                run_claude.os.environ.update(original_environ)

            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][3], Path(dep_a["packet_path"]).parent / "claude-cwd")
            self.assertEqual(calls[1][3], Path(dep_b["packet_path"]).parent / "claude-cwd")
            self.assertNotIn("ANTHROPIC_API_KEY", calls[0][2])
            self.assertNotIn("INPUT_ANTHROPIC_API_KEY_FILE", calls[0][2])
            self.assertIn("--disallowedTools", calls[0][0])
            self.assertIn("Bash", calls[0][0])
            self.assertEqual(calls[0][0][calls[0][0].index("--setting-sources") + 1], "user")
            results = json.loads((root / "results.json").read_text(encoding="utf-8"))
            self.assertEqual([item["slug"] for item in results["dependencies"]], ["a", "b"])


def dependency_fixture(root: Path, slug: str) -> tuple[dict[str, str], Path]:
    dep_dir = root / slug
    repo = dep_dir / "repo"
    repo.mkdir(parents=True)
    (repo / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n", encoding="utf-8")
    for name in ["diff.patch", "hunks.json", "packet.md"]:
        (dep_dir / name).write_text(name + "\n", encoding="utf-8")
    result_path = dep_dir / "result.json"
    dependency = {
        "slug": slug,
        "name": slug,
        "change_label": "1.0.0 -> 1.0.1",
        "repo_path": str(repo),
        "diff_path": str(dep_dir / "diff.patch"),
        "hunks_path": str(dep_dir / "hunks.json"),
        "packet_path": str(dep_dir / "packet.md"),
    }
    return dependency, result_path


if __name__ == "__main__":
    unittest.main()
