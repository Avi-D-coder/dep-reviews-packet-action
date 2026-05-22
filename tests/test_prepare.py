import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import prepare  # noqa: E402


def run(cmd, cwd=None, env=None):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise AssertionError(f"{cmd} failed\nstdout={result.stdout}\nstderr={result.stderr}")
    return result


class PrepareTests(unittest.TestCase):
    def test_parse_lock_and_pair_registry_upgrade(self):
        old_lock = """
version = 3

[[package]]
name = "serde"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "aaa"

[[package]]
name = "local"
version = "0.1.0"
"""
        new_lock = """
version = 3

[[package]]
name = "serde"
version = "1.0.1"
source = "sparse+https://index.crates.io/"
checksum = "bbb"

[[package]]
name = "extra"
version = "0.1.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ccc"
"""
        changes, skipped = prepare.pair_changes(prepare.parse_lock(old_lock), prepare.parse_lock(new_lock))
        self.assertEqual(len(changes), 2)
        by_name = {change.new.name: change for change in changes}
        self.assertEqual(by_name["serde"].old.version, "1.0.0")
        self.assertEqual(by_name["serde"].new.version, "1.0.1")
        self.assertEqual(by_name["serde"].new.source_key, "registry:crates-io")
        self.assertEqual(by_name["serde"].change_kind, "version-update")
        self.assertIsNone(by_name["extra"].old)
        self.assertEqual(by_name["extra"].change_kind, "added")
        self.assertEqual(skipped, [])

    def test_pair_source_migration_from_crates_io_to_git(self):
        old = prepare.Package("dep", "1.0.0", "registry+https://github.com/rust-lang/crates.io-index")
        new = prepare.Package("dep", "1.0.1", "git+https://github.com/example/dep.git#abcdef")

        changes, skipped = prepare.pair_changes([old], [new])

        self.assertEqual(len(changes), 1)
        self.assertEqual(skipped, [])
        self.assertEqual(changes[0].old, old)
        self.assertEqual(changes[0].new, new)
        self.assertEqual(changes[0].change_kind, "source-migration")

    def test_pair_source_migration_from_git_to_crates_io(self):
        old = prepare.Package("dep", "1.0.0", "git+https://github.com/example/dep.git#abcdef")
        new = prepare.Package("dep", "1.0.1", "registry+https://github.com/rust-lang/crates.io-index")

        changes, skipped = prepare.pair_changes([old], [new])

        self.assertEqual(len(changes), 1)
        self.assertEqual(skipped, [])
        self.assertEqual(changes[0].old, old)
        self.assertEqual(changes[0].new, new)
        self.assertEqual(changes[0].change_kind, "source-migration")

    def test_pair_added_dependency_is_audited(self):
        new = prepare.Package("dep", "1.0.1", "registry+https://github.com/rust-lang/crates.io-index")

        changes, skipped = prepare.pair_changes([], [new])

        self.assertEqual(len(changes), 1)
        self.assertEqual(skipped, [])
        self.assertIsNone(changes[0].old)
        self.assertEqual(changes[0].new, new)
        self.assertEqual(changes[0].change_kind, "added")

    def test_pair_removed_dependency_is_report_only(self):
        old = prepare.Package("dep", "1.0.0", "registry+https://github.com/rust-lang/crates.io-index")

        changes, skipped = prepare.pair_changes([old], [])

        self.assertEqual(changes, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].reason, "removed dependency")

    def test_pair_ambiguous_unmatched_same_name_is_skipped(self):
        old = prepare.Package("dep", "1.0.0", "registry+https://github.com/rust-lang/crates.io-index")
        new_a = prepare.Package("dep", "1.0.1", "git+https://github.com/example/dep-a.git#abcdef")
        new_b = prepare.Package("dep", "2.0.0", "git+https://github.com/example/dep-b.git#abcdef")

        changes, skipped = prepare.pair_changes([old], [new_a, new_b])

        self.assertEqual(changes, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].reason, "ambiguous multi-version dependency")

    def test_git_source_normalization_ignores_locked_revision(self):
        a = "git+https://github.com/example/dep.git?branch=main#1111111"
        b = "git+https://github.com/example/dep.git?branch=main#2222222"
        self.assertEqual(prepare.normalize_source(a), prepare.normalize_source(b))
        self.assertEqual(prepare.parse_git_source(a)["rev"], "1111111")

    def test_local_checkout_command_for_crates_io_uses_checksum(self):
        package = prepare.Package(
            name="serde",
            version="1.0.1",
            source="registry+https://github.com/rust-lang/crates.io-index",
            checksum="abc123",
        )
        command = prepare.local_checkout_command(package, "serde-1.0.1", "serde.tar.gz")
        self.assertIn("curl -L", command)
        self.assertIn("sha256sum -c -", command)
        self.assertIn("abc123", command)
        self.assertIn("git init", command)
        self.assertIn("git add -A -f", command)

    def test_create_synthetic_repo_and_packet_hunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_src = tmp_path / "old"
            new_src = tmp_path / "new"
            repo = tmp_path / "repo"
            old_src.mkdir()
            new_src.mkdir()
            (old_src / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n", encoding="utf-8")
            (new_src / "lib.rs").write_text("pub fn value() -> u8 { 2 }\n", encoding="utf-8")
            change = prepare.Change(
                old=prepare.Package("dep", "1.0.0", "registry+https://github.com/rust-lang/crates.io-index"),
                new=prepare.Package("dep", "1.0.1", "registry+https://github.com/rust-lang/crates.io-index"),
                key="dep|registry:crates-io",
                change_kind="version-update",
            )
            self.assertTrue(prepare.create_synthetic_repo(change, old_src, new_src, repo))
            diff = run(["git", "diff", "HEAD~1..HEAD"], cwd=repo).stdout
            hunks = prepare.parse_diff_hunks(diff)
            self.assertEqual(hunks, [{"path": "lib.rs", "hunk_index": 1, "header": "@@ -1 +1 @@"}])
            packet = prepare.packet_skeleton(change, hunks)
            self.assertIn("@hunk lib.rs#1", packet)

    def test_create_synthetic_repo_force_adds_ignored_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_src = tmp_path / "old"
            new_src = tmp_path / "new"
            repo = tmp_path / "repo"
            old_src.mkdir()
            new_src.mkdir()
            (old_src / ".gitignore").write_text("*.secret\n", encoding="utf-8")
            (new_src / ".gitignore").write_text("*.secret\n", encoding="utf-8")
            (old_src / "payload.secret").write_text("old\n", encoding="utf-8")
            (new_src / "payload.secret").write_text("new\n", encoding="utf-8")
            change = prepare.Change(
                old=prepare.Package("dep", "1.0.0", "registry+https://github.com/rust-lang/crates.io-index"),
                new=prepare.Package("dep", "1.0.1", "registry+https://github.com/rust-lang/crates.io-index"),
                key="dep|registry:crates-io",
                change_kind="version-update",
            )

            self.assertTrue(prepare.create_synthetic_repo(change, old_src, new_src, repo))
            tracked = run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=repo).stdout.splitlines()
            self.assertIn("payload.secret", tracked)
            diff = run(["git", "diff", "HEAD~1..HEAD", "--", "payload.secret"], cwd=repo).stdout
            self.assertIn("-old", diff)
            self.assertIn("+new", diff)

    def test_create_synthetic_repo_for_added_dependency_uses_empty_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            new_src = tmp_path / "new"
            repo = tmp_path / "repo"
            new_src.mkdir()
            (new_src / "lib.rs").write_text("pub const V: u8 = 2;\n", encoding="utf-8")
            change = prepare.Change(
                old=None,
                new=prepare.Package("dep", "1.0.1", "registry+https://github.com/rust-lang/crates.io-index"),
                key="dep|added|registry:crates-io",
                change_kind="added",
            )

            self.assertTrue(prepare.create_synthetic_repo(change, tmp_path / "old", new_src, repo))
            baseline_files = run(["git", "ls-tree", "-r", "--name-only", "HEAD~1"], cwd=repo).stdout
            head_files = run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=repo).stdout.splitlines()
            diff = run(["git", "diff", "HEAD~1..HEAD"], cwd=repo).stdout
            hunks = prepare.parse_diff_hunks(diff)
            packet = prepare.packet_skeleton(change, hunks)

            self.assertEqual(baseline_files, "")
            self.assertEqual(head_files, ["lib.rs"])
            self.assertIn("@hunk lib.rs#1", packet)
            self.assertIn("dep added 1.0.1", packet)
            self.assertNotIn("None", packet)

    def test_prepare_dry_run_with_fixture_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fixture = tmp_path / "fixtures"
            repo = tmp_path / "repo"
            (fixture / "dep" / "1.0.0").mkdir(parents=True)
            (fixture / "dep" / "1.0.1").mkdir(parents=True)
            (fixture / "dep" / "1.0.0" / "lib.rs").write_text("pub const V: u8 = 1;\n", encoding="utf-8")
            (fixture / "dep" / "1.0.1" / "lib.rs").write_text("pub const V: u8 = 2;\n", encoding="utf-8")

            repo.mkdir()
            run(["git", "init", "-q", "-b", "main"], cwd=repo)
            run(["git", "config", "user.email", "t@example.invalid"], cwd=repo)
            run(["git", "config", "user.name", "T"], cwd=repo)
            run(["git", "config", "commit.gpgsign", "false"], cwd=repo)
            (repo / "Cargo.lock").write_text(lock_for("1.0.0", "old"), encoding="utf-8")
            run(["git", "add", "Cargo.lock"], cwd=repo)
            run(["git", "commit", "-q", "-m", "old"], cwd=repo)
            (repo / "Cargo.lock").write_text(lock_for("1.0.1", "new"), encoding="utf-8")
            run(["git", "add", "Cargo.lock"], cwd=repo)
            run(["git", "commit", "-q", "-m", "new"], cwd=repo)

            env = os.environ.copy()
            env["DEP_REVIEWS_FIXTURE_ROOT"] = str(fixture)
            result = run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "prepare.py"),
                    "--base-ref",
                    "HEAD~1",
                    "--head-ref",
                    "HEAD",
                ],
                cwd=repo,
                env=env,
            )
            self.assertIn("Prepared 1 dependency diff", result.stdout)
            manifest = json.loads((repo / ".dep-review-work" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["dependencies"]), 1)
            dep = manifest["dependencies"][0]
            self.assertEqual(dep["change_kind"], "version-update")
            self.assertTrue(Path(repo / dep["packet_path"]).is_file())
            self.assertTrue(Path(repo / dep["repo_path"]).is_dir())

    def test_prepare_dry_run_with_fixture_source_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fixture = tmp_path / "fixtures"
            repo = tmp_path / "repo"
            (fixture / "dep" / "1.0.0").mkdir(parents=True)
            (fixture / "dep" / "1.0.1").mkdir(parents=True)
            (fixture / "dep" / "1.0.0" / "lib.rs").write_text("pub const SOURCE: &str = \"crate\";\n", encoding="utf-8")
            (fixture / "dep" / "1.0.1" / "lib.rs").write_text("pub const SOURCE: &str = \"git\";\n", encoding="utf-8")

            repo.mkdir()
            run(["git", "init", "-q", "-b", "main"], cwd=repo)
            run(["git", "config", "user.email", "t@example.invalid"], cwd=repo)
            run(["git", "config", "user.name", "T"], cwd=repo)
            run(["git", "config", "commit.gpgsign", "false"], cwd=repo)
            (repo / "Cargo.lock").write_text(lock_for("1.0.0", "old"), encoding="utf-8")
            run(["git", "add", "Cargo.lock"], cwd=repo)
            run(["git", "commit", "-q", "-m", "old"], cwd=repo)
            (repo / "Cargo.lock").write_text(
                lock_for("1.0.1", None, 'git+https://github.com/example/dep.git#abcdef'),
                encoding="utf-8",
            )
            run(["git", "add", "Cargo.lock"], cwd=repo)
            run(["git", "commit", "-q", "-m", "new"], cwd=repo)

            env = os.environ.copy()
            env["DEP_REVIEWS_FIXTURE_ROOT"] = str(fixture)
            result = run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "prepare.py"),
                    "--base-ref",
                    "HEAD~1",
                    "--head-ref",
                    "HEAD",
                ],
                cwd=repo,
                env=env,
            )

            self.assertIn("Prepared 1 dependency diff", result.stdout)
            manifest = json.loads((repo / ".dep-review-work" / "manifest.json").read_text(encoding="utf-8"))
            dep = manifest["dependencies"][0]
            self.assertEqual(dep["change_kind"], "source-migration")
            self.assertEqual(dep["old_source_kind"], "crates.io")
            self.assertEqual(dep["new_source_kind"], "git")
            packet = Path(repo / dep["packet_path"]).read_text(encoding="utf-8")
            self.assertIn("crates.io", packet)
            self.assertIn("git", packet)

    def test_prepare_dry_run_with_fixture_added_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fixture = tmp_path / "fixtures"
            repo = tmp_path / "repo"
            (fixture / "dep" / "1.0.1").mkdir(parents=True)
            (fixture / "dep" / "1.0.1" / "lib.rs").write_text("pub const V: u8 = 2;\n", encoding="utf-8")

            repo.mkdir()
            run(["git", "init", "-q", "-b", "main"], cwd=repo)
            run(["git", "config", "user.email", "t@example.invalid"], cwd=repo)
            run(["git", "config", "user.name", "T"], cwd=repo)
            run(["git", "config", "commit.gpgsign", "false"], cwd=repo)
            (repo / "Cargo.lock").write_text("version = 3\n", encoding="utf-8")
            run(["git", "add", "Cargo.lock"], cwd=repo)
            run(["git", "commit", "-q", "-m", "old"], cwd=repo)
            (repo / "Cargo.lock").write_text(lock_for("1.0.1", "new"), encoding="utf-8")
            run(["git", "add", "Cargo.lock"], cwd=repo)
            run(["git", "commit", "-q", "-m", "new"], cwd=repo)

            env = os.environ.copy()
            env["DEP_REVIEWS_FIXTURE_ROOT"] = str(fixture)
            result = run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "prepare.py"),
                    "--base-ref",
                    "HEAD~1",
                    "--head-ref",
                    "HEAD",
                ],
                cwd=repo,
                env=env,
            )

            self.assertIn("Prepared 1 dependency diff", result.stdout)
            manifest = json.loads((repo / ".dep-review-work" / "manifest.json").read_text(encoding="utf-8"))
            dep = manifest["dependencies"][0]
            self.assertEqual(dep["change_kind"], "added")
            self.assertIsNone(dep["old_version"])
            self.assertNotIn("None", Path(repo / dep["packet_path"]).read_text(encoding="utf-8"))


def lock_for(version, checksum, source="registry+https://github.com/rust-lang/crates.io-index"):
    checksum_line = f'checksum = "{checksum}"' if checksum is not None else ""
    return f"""
version = 3

[[package]]
name = "dep"
version = "{version}"
source = "{source}"
{checksum_line}
"""


if __name__ == "__main__":
    unittest.main()
