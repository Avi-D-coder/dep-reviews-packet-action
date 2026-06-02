import json
import os
import sys
import tempfile
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_codex  # noqa: E402


class RunCodexTests(unittest.TestCase):
    def test_build_command_uses_codex_exec_with_required_hardening(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dep_dir = root / "itoa"
            schema_path = dep_dir / "codex-result.schema.json"
            final_path = dep_dir / "codex-final-result.json"

            cmd = run_codex.build_command("gpt-test", "prompt", dep_dir, schema_path, final_path)

            self.assertEqual(cmd[:2], ["codex", "exec"])
            self.assertEqual(cmd[cmd.index("--cd") + 1], str(dep_dir))
            self.assertIn("--skip-git-repo-check", cmd)
            self.assertIn("--ephemeral", cmd)
            self.assertIn("--json", cmd)
            self.assertEqual(cmd[cmd.index("--profile") + 1], run_codex.PERMISSIONS_PROFILE_NAME)
            self.assertEqual(cmd[cmd.index("--sandbox") + 1], "workspace-write")
            self.assertEqual(cmd[cmd.index("--output-schema") + 1], str(schema_path))
            self.assertEqual(cmd[cmd.index("--output-last-message") + 1], str(final_path))
            self.assertIn("--ignore-rules", cmd)
            self.assertIn("hooks", values_after(cmd, "--disable"))
            self.assertIn("multi_agent", values_after(cmd, "--disable"))
            self.assertIn('web_search="disabled"', values_after(cmd, "--config"))
            self.assertIn('approval_policy="never"', values_after(cmd, "--config"))
            self.assertIn('default_permissions="dep-review-dependency"', values_after(cmd, "--config"))
            self.assertIn("sandbox_workspace_write.exclude_tmpdir_env_var=true", values_after(cmd, "--config"))
            self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", values_after(cmd, "--config"))
            self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-test")
            self.assertEqual(cmd[-1], "prompt")

    def test_build_command_omits_model_when_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = run_codex.build_command(
                "",
                "prompt",
                root / "dep",
                root / "dep" / "schema.json",
                root / "dep" / "final.json",
            )

            self.assertNotIn("--model", cmd)

    def test_build_command_rejects_security_sensitive_extra_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaises(SystemExit):
                run_codex.build_command(
                    "",
                    "prompt",
                    root / "dep",
                    root / "dep" / "schema.json",
                    root / "dep" / "final.json",
                    "--sandbox danger-full-access",
                )

    def test_build_command_allows_limited_extra_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            cmd = run_codex.build_command(
                "",
                "prompt",
                root / "dep",
                root / "dep" / "schema.json",
                root / "dep" / "final.json",
                "--color never --strict-config",
            )

            self.assertEqual(cmd[-4:], ["--color", "never", "--strict-config", "prompt"])

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

        prompt = run_codex.audit_prompt(
            dependency,
            Path("/work/.dep-review-work/deps/itoa/packet-guidance.md"),
        )

        self.assertIn("fresh Codex CLI non-interactive run for this dependency only", prompt)
        self.assertIn("/work/.dep-review-work/deps/itoa/packet-guidance.md", prompt)
        self.assertIn('"slug": "itoa-1-0-0-to-1-0-1"', prompt)
        self.assertIn("Treat the diff as the primary review target", prompt)
        self.assertIn("full new dependency source tree at repo_path as context", prompt)
        self.assertIn("supply-chain attack mindset", prompt)
        self.assertIn("network/file/process I/O that could be exploited", prompt)
        self.assertIn("undefined behavior or memory unsafety", prompt)
        self.assertIn("full contents of your security audit", prompt)
        self.assertIn("limitations", prompt)
        self.assertIn("Do not run reviews push", prompt)
        self.assertIn("Do not edit result.json", prompt)
        self.assertIn('"status": "packet-ready"', prompt)
        self.assertNotIn("For each dependency", prompt)

    def test_dependency_result_path_is_next_to_packet(self):
        dependency = {"packet_path": "/work/.dep-review-work/deps/itoa/packet.md"}

        self.assertEqual(
            run_codex.dependency_result_path(dependency),
            Path("/work/.dep-review-work/deps/itoa/result.json"),
        )

    def test_initialize_result_file_creates_failed_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "dep" / "result.json"

            run_codex.initialize_result_file({"slug": "dep"}, result_path)

            data = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(data["dependencies"][0]["slug"], "dep")
            self.assertEqual(data["dependencies"][0]["status"], "failed")
            self.assertEqual(data["dependencies"][0]["severity"], "unknown")
            self.assertIn("Codex", data["dependencies"][0]["audit_summary"])

    def test_create_codex_home_writes_hardened_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_runner_temp = os.environ.get("RUNNER_TEMP")
            os.environ["RUNNER_TEMP"] = tmp
            try:
                codex_home = run_codex.create_codex_home()
            finally:
                if original_runner_temp is None:
                    os.environ.pop("RUNNER_TEMP", None)
                else:
                    os.environ["RUNNER_TEMP"] = original_runner_temp

            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('approval_policy = "never"', config)
            self.assertIn('sandbox_mode = "workspace-write"', config)
            self.assertIn('web_search = "disabled"', config)
            self.assertIn("[sandbox_workspace_write]", config)
            self.assertIn("exclude_tmpdir_env_var = true", config)
            self.assertIn("exclude_slash_tmp = true", config)
            self.assertIn("multi_agent = false", config)
            self.assertIn("CODEX_*", config)
            self.assertIn("OPENAI_*", config)

    def test_write_dependency_permissions_profile_allows_only_dep_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            dep_dir = root / "dep"
            dep_dir.mkdir()

            profile_path = run_codex.write_dependency_permissions_profile(
                codex_home,
                dep_dir,
                dep_dir / "packet.md",
                dep_dir / "codex-final-result.json",
            )

            profile = profile_path.read_text(encoding="utf-8")
            self.assertIn('default_permissions = "dep-review-dependency"', profile)
            self.assertIn("[permissions.dep-review-dependency.workspace_roots]", profile)
            self.assertIn(f'{json.dumps(str(dep_dir.resolve(strict=False)))} = true', profile)
            self.assertIn("[permissions.dep-review-dependency.filesystem]", profile)
            self.assertIn('":minimal" = "read"', profile)
            self.assertIn('[permissions.dep-review-dependency.filesystem.":workspace_roots"]', profile)
            self.assertIn('"." = "read"', profile)
            self.assertIn('"packet.md" = "write"', profile)
            self.assertIn('"codex-final-result.json" = "write"', profile)
            self.assertIn("[permissions.dep-review-dependency.network]", profile)
            self.assertIn("enabled = false", profile)

    def test_codex_child_env_sets_api_key_only_for_codex_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_file = root / "keys" / "openai"
            key_file.parent.mkdir()
            key_file.write_text("secret", encoding="utf-8")
            codex_home = root / "codex-home"

            env = run_codex.codex_child_env(
                {
                    "PATH": "/bin",
                    "INPUT_OPENAI_API_KEY": "secret",
                    "OPENAI_API_KEY": "secret",
                    "REVIEWS_API_KEY": "secret",
                    "GITHUB_TOKEN": "secret",
                    "HTTPS_PROXY": "http://proxy",
                },
                codex_home,
                key_file,
            )

            self.assertEqual(env["CODEX_HOME"], str(codex_home))
            self.assertEqual(env["HOME"], str(codex_home))
            self.assertEqual(env["PATH"], "/bin")
            self.assertEqual(env["HTTPS_PROXY"], "http://proxy")
            self.assertEqual(env["CODEX_API_KEY"], "secret")
            self.assertNotIn("INPUT_OPENAI_API_KEY", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("REVIEWS_API_KEY", env)
            self.assertNotIn("GITHUB_TOKEN", env)

    def test_run_dependency_writes_result_from_codex_final_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dependency, result_path = dependency_fixture(root, "itoa")
            skill_root = skill_fixture(root)

            def fake_run(cmd, check, env, cwd):
                self.assertTrue(check)
                self.assertEqual(cwd, Path(dependency["packet_path"]).parent)
                packet_path = Path(dependency["packet_path"])
                packet_path.write_text("# Reviewed packet\n", encoding="utf-8")
                final_path = Path(cmd[cmd.index("--output-last-message") + 1])
                final_path.write_text(
                    json.dumps(
                        {
                            "dependencies": [
                                {
                                    "slug": "itoa",
                                    "status": "packet-ready",
                                    "review_url": "ignored",
                                    "patchset_number": 99,
                                    "severity": "low",
                                    "audit_summary": " Looks fine. ",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

            original_run = run_codex.subprocess.run
            try:
                run_codex.subprocess.run = fake_run
                run_codex.run_dependency(
                    dependency,
                    result_path,
                    skill_root,
                    "",
                    {"PATH": "/bin", "CODEX_HOME": str(root / "codex-home")},
                )
            finally:
                run_codex.subprocess.run = original_run

            data = json.loads(result_path.read_text(encoding="utf-8"))
            item = data["dependencies"][0]
            self.assertEqual(item["slug"], "itoa")
            self.assertEqual(item["status"], "packet-ready")
            self.assertEqual(item["review_url"], "")
            self.assertIsNone(item["patchset_number"])
            self.assertEqual(item["severity"], "low")
            self.assertEqual(item["audit_summary"], "Looks fine.")

    def test_run_dependency_detects_unexpected_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dependency, result_path = dependency_fixture(root, "itoa")
            skill_root = skill_fixture(root)

            def fake_run(cmd, check, env, cwd):
                Path(dependency["repo_path"], "lib.rs").write_text("tampered\n", encoding="utf-8")
                final_path = Path(cmd[cmd.index("--output-last-message") + 1])
                final_path.write_text(
                    json.dumps(
                        {
                            "dependencies": [
                                {
                                    "slug": "itoa",
                                    "status": "packet-ready",
                                    "review_url": "",
                                    "patchset_number": None,
                                    "severity": "none",
                                    "audit_summary": "Done.",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

            original_run = run_codex.subprocess.run
            try:
                run_codex.subprocess.run = fake_run
                run_codex.run_dependency(
                    dependency,
                    result_path,
                    skill_root,
                    "",
                    {"PATH": "/bin", "CODEX_HOME": str(root / "codex-home")},
                )
            finally:
                run_codex.subprocess.run = original_run

            item = json.loads(result_path.read_text(encoding="utf-8"))["dependencies"][0]
            self.assertEqual(item["status"], "failed")
            self.assertIn("unauthorized workspace files", item["audit_summary"])
            self.assertIn("repo/lib.rs", item["audit_summary"])

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
            run_codex.merge_results({"results_path": str(results_path)}, [dep_a, dep_b])
            data = json.loads(results_path.read_text(encoding="utf-8"))

            self.assertEqual([item["slug"] for item in data["dependencies"]], ["a", "b"])

    def test_main_starts_one_codex_process_per_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_file = root / "keys" / "openai"
            key_file.parent.mkdir()
            key_file.write_text("secret", encoding="utf-8")
            skill_fixture(root / "action")
            dep_a, result_a = dependency_fixture(root, "a")
            dep_b, result_b = dependency_fixture(root, "b")
            manifest = {
                "results_path": str(root / "results.json"),
                "dependencies": [dep_a, dep_b],
            }
            calls = []

            def fake_run(cmd, check, env, cwd):
                calls.append((cmd, check, env, cwd))
                result_path = result_a if Path(cwd).name == "a" else result_b
                self.assertTrue(result_path.is_file())
                placeholder = json.loads(result_path.read_text(encoding="utf-8"))
                self.assertEqual(placeholder["dependencies"][0]["status"], "failed")
                final_path = Path(cmd[cmd.index("--output-last-message") + 1])
                final_path.write_text(
                    json.dumps(
                        {
                            "dependencies": [
                                {
                                    "slug": Path(cwd).name,
                                    "status": "packet-ready",
                                    "review_url": "",
                                    "patchset_number": None,
                                    "severity": "none",
                                    "audit_summary": "Done.",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

            original_load_manifest = run_codex.load_manifest
            original_run = run_codex.subprocess.run
            original_environ = dict(run_codex.os.environ)
            try:
                run_codex.load_manifest = lambda path: manifest
                run_codex.subprocess.run = fake_run
                run_codex.os.environ.clear()
                run_codex.os.environ.update(
                    {
                        "INPUT_OPENAI_API_KEY_FILE": str(key_file),
                        "INPUT_CODEX_MODEL": "",
                        "GITHUB_ACTION_PATH": str(root / "action"),
                        "RUNNER_TEMP": str(root),
                        "PATH": "/bin",
                    }
                )

                self.assertEqual(run_codex.main(), 0)
            finally:
                run_codex.load_manifest = original_load_manifest
                run_codex.subprocess.run = original_run
                run_codex.os.environ.clear()
                run_codex.os.environ.update(original_environ)

            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][3], Path(dep_a["packet_path"]).parent)
            self.assertEqual(calls[1][3], Path(dep_b["packet_path"]).parent)
            self.assertEqual(calls[0][2]["CODEX_API_KEY"], "secret")
            self.assertNotIn("OPENAI_API_KEY", calls[0][2])
            self.assertNotIn("INPUT_OPENAI_API_KEY_FILE", calls[0][2])
            self.assertEqual(calls[0][0][calls[0][0].index("--sandbox") + 1], "workspace-write")
            results = json.loads((root / "results.json").read_text(encoding="utf-8"))
            self.assertEqual([item["slug"] for item in results["dependencies"]], ["a", "b"])


def values_after(items: list[str], flag: str) -> list[str]:
    return [items[index + 1] for index, item in enumerate(items[:-1]) if item == flag]


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


def skill_fixture(root: Path) -> Path:
    skill_root = root / ".codex" / "skills"
    skill_dir = skill_root / "writing-review-packets"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Packet guidance\n", encoding="utf-8")
    return skill_root


if __name__ == "__main__":
    unittest.main()
