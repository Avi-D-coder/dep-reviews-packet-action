import json
import os
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import push_reviews  # noqa: E402


class PushReviewsTests(unittest.TestCase):
    def test_upload_all_pushes_packet_ready_results_outside_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dep_dir = root / "dep"
            repo = dep_dir / "repo"
            repo.mkdir(parents=True)
            packet = dep_dir / "packet.md"
            packet.write_text("# Packet\n", encoding="utf-8")
            manifest = {
                "dependencies": [
                    {
                        "slug": "dep",
                        "name": "dep",
                        "change_label": "1.0.0 -> 1.0.1",
                        "repo_path": str(repo),
                        "packet_path": str(packet),
                    }
                ]
            }
            results = {
                "dependencies": [
                    {
                        "slug": "dep",
                        "status": "packet-ready",
                        "severity": "low",
                        "audit_summary": "Looks small.",
                    }
                ]
            }
            calls = []

            def fake_run(cmd, cwd, check, text, stdout, stderr):
                calls.append((cmd, cwd, check, text, stdout, stderr))

                class Completed:
                    stdout = "Created review https://reviews.example/r/abc patchset 2\n"

                return Completed()

            original_run = push_reviews.subprocess.run
            try:
                push_reviews.subprocess.run = fake_run
                updated = push_reviews.upload_all(manifest, results, "/tmp/reviews")
            finally:
                push_reviews.subprocess.run = original_run

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], repo)
            self.assertEqual(calls[0][0][:2], ["/tmp/reviews", "push"])
            item = updated["dependencies"][0]
            self.assertEqual(item["status"], "uploaded")
            self.assertEqual(item["review_url"], "https://reviews.example/r/abc")
            self.assertEqual(item["patchset_number"], 2)
            self.assertEqual(item["severity"], "low")
            self.assertEqual(item["audit_summary"], "Looks small.")

    def test_upload_all_skips_failed_audits(self):
        manifest = {"dependencies": [{"slug": "dep", "repo_path": "/nope", "packet_path": "/nope"}]}
        results = {"dependencies": [{"slug": "dep", "status": "failed", "audit_summary": "audit failed"}]}

        updated = push_reviews.upload_all(manifest, results)

        self.assertEqual(updated["dependencies"][0]["status"], "failed")
        self.assertEqual(updated["dependencies"][0]["audit_summary"], "audit failed")

    def test_upload_all_does_not_upload_missing_or_unready_results(self):
        manifest = {"dependencies": [{"slug": "dep", "repo_path": "/nope", "packet_path": "/nope"}]}
        results = {"dependencies": []}

        updated = push_reviews.upload_all(manifest, results)

        self.assertEqual(updated["dependencies"][0]["status"], "failed")
        self.assertEqual(updated["dependencies"][0]["review_url"], "")
        self.assertIn("did not mark the packet ready", updated["dependencies"][0]["audit_summary"])

    def test_upload_failure_replaces_codex_summary_with_upload_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            packet = root / "packet.md"
            packet.write_text("# Packet\n", encoding="utf-8")
            item = {
                "slug": "dep",
                "name": "dep",
                "change_label": "1.0.0 -> 1.0.1",
                "repo_path": str(repo),
                "packet_path": str(packet),
                "status": "packet-ready",
                "severity": "low",
                "audit_summary": "Looks small.",
            }

            def fake_run(*args, **kwargs):
                raise subprocess.CalledProcessError(1, args[0], output="authentication failed")

            original_run = push_reviews.subprocess.run
            try:
                push_reviews.subprocess.run = fake_run
                updated = push_reviews.upload_dependency_packet(item, "/tmp/reviews")
            finally:
                push_reviews.subprocess.run = original_run

            self.assertEqual(updated["status"], "failed")
            self.assertEqual(updated["review_url"], "")
            self.assertEqual(updated["patchset_number"], None)
            self.assertIn("Reviews upload failed", updated["audit_summary"])
            self.assertIn("authentication failed", updated["audit_summary"])
            self.assertNotEqual(updated["audit_summary"], "Looks small.")

    def test_success_without_review_url_is_failed_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            packet = root / "packet.md"
            packet.write_text("# Packet\n", encoding="utf-8")
            item = {
                "slug": "dep",
                "name": "dep",
                "change_label": "1.0.0 -> 1.0.1",
                "repo_path": str(repo),
                "packet_path": str(packet),
                "status": "packet-ready",
                "severity": "none",
                "audit_summary": "Looks small.",
            }

            def fake_run(*args, **kwargs):
                class Completed:
                    stdout = "Created review without URL\n"

                return Completed()

            original_run = push_reviews.subprocess.run
            try:
                push_reviews.subprocess.run = fake_run
                updated = push_reviews.upload_dependency_packet(item, "/tmp/reviews")
            finally:
                push_reviews.subprocess.run = original_run

            self.assertEqual(updated["status"], "failed")
            self.assertEqual(updated["review_url"], "")
            self.assertEqual(updated["patchset_number"], None)
            self.assertIn("did not return a review URL", updated["audit_summary"])

    def test_main_rewrites_results_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / ".dep-review-work"
            dep_dir = workdir / "deps" / "dep"
            repo = dep_dir / "repo"
            repo.mkdir(parents=True)
            packet = dep_dir / "packet.md"
            packet.write_text("# Packet\n", encoding="utf-8")
            (workdir / "manifest.json").write_text(
                json.dumps(
                    {
                        "dependencies": [
                            {
                                "slug": "dep",
                                "name": "dep",
                                "change_label": "added 1.0.0",
                                "repo_path": str(repo),
                                "packet_path": str(packet),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (workdir / "results.json").write_text(
                '{"dependencies":[{"slug":"dep","status":"packet-ready","severity":"none"}]}',
                encoding="utf-8",
            )

            def fake_upload(item, reviews_command="reviews"):
                item["status"] = "uploaded"
                item["review_url"] = "https://reviews.example/r/dep"
                return item

            original_upload = push_reviews.upload_dependency_packet
            original_cwd = Path.cwd()
            try:
                push_reviews.upload_dependency_packet = fake_upload
                os.chdir(root)
                self.assertEqual(push_reviews.main(), 0)
            finally:
                push_reviews.upload_dependency_packet = original_upload
                os.chdir(original_cwd)

            rewritten = json.loads((workdir / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(rewritten["dependencies"][0]["review_url"], "https://reviews.example/r/dep")

    def test_reviews_command_uses_action_output_env(self):
        original = os.environ.get("INPUT_REVIEWS_COMMAND")
        os.environ["INPUT_REVIEWS_COMMAND"] = "/tmp/reviews-wrapper"
        try:
            self.assertEqual(push_reviews.reviews_command(), "/tmp/reviews-wrapper")
        finally:
            if original is None:
                os.environ.pop("INPUT_REVIEWS_COMMAND", None)
            else:
                os.environ["INPUT_REVIEWS_COMMAND"] = original

    def test_main_returns_failure_when_any_dependency_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / ".dep-review-work"
            workdir.mkdir()
            (workdir / "manifest.json").write_text(
                '{"dependencies":[{"slug":"dep","repo_path":"/nope","packet_path":"/nope"}]}',
                encoding="utf-8",
            )
            (workdir / "results.json").write_text(
                '{"dependencies":[{"slug":"dep","status":"failed","audit_summary":"audit failed"}]}',
                encoding="utf-8",
            )

            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.assertEqual(push_reviews.main(), 1)
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
