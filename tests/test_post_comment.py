import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import post_comment  # noqa: E402


class PostCommentTests(unittest.TestCase):
    def test_render_body_includes_review_and_checkout_command(self):
        manifest = {
            "dependencies": [
                {
                    "slug": "dep-1",
                    "name": "dep",
                    "old_version": "1.0.0",
                    "new_version": "1.0.1",
                    "local_checkout": {"command": "git clone https://example.com/dep\n"},
                }
            ],
            "skipped": [
                {"name": "other", "old_version": None, "new_version": "0.1.0", "reason": "added dependency"}
            ],
        }
        results = {
            "dependencies": [
                {
                    "slug": "dep-1",
                    "status": "uploaded",
                    "review_url": "https://reviews.example/r/abc",
                    "severity": "low",
                    "audit_summary": "No critical issues found.",
                }
            ]
        }
        body = post_comment.render_body(manifest, results, include_marker=True)
        self.assertIn(post_comment.MARKER, body)
        self.assertIn("[Open review](https://reviews.example/r/abc)", body)
        self.assertIn("No critical issues found.", body)
        self.assertIn("git clone https://example.com/dep", body)
        self.assertIn("added dependency", body)

    def test_load_json_falls_back_on_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-results.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(post_comment.load_json(path, {"dependencies": []}), {"dependencies": []})

    def test_render_body_formats_added_dependency_without_none(self):
        manifest = {
            "dependencies": [
                {
                    "slug": "dep-1",
                    "name": "dep",
                    "change_kind": "added",
                    "change_label": "added 1.0.1",
                    "old_version": None,
                    "new_version": "1.0.1",
                    "local_checkout": {"command": "curl https://example.com/dep\n"},
                }
            ],
            "skipped": [],
        }
        results = {"dependencies": [{"slug": "dep-1", "status": "uploaded", "severity": "none"}]}

        body = post_comment.render_body(manifest, results, include_marker=False)

        self.assertIn("| dep | added 1.0.1 | none | uploaded | Not uploaded |", body)
        self.assertIn("**dep added 1.0.1**", body)
        self.assertNotIn("None", body)

    def test_render_body_formats_source_migration(self):
        manifest = {
            "dependencies": [
                {
                    "slug": "dep-1",
                    "name": "dep",
                    "change_kind": "source-migration",
                    "old_version": "1.0.0",
                    "new_version": "1.0.1",
                    "old_source_kind": "git",
                    "new_source_kind": "crates.io",
                    "local_checkout": {"command": "curl https://example.com/dep\n"},
                }
            ],
            "skipped": [],
        }
        results = {"dependencies": [{"slug": "dep-1", "status": "uploaded", "severity": "low"}]}

        body = post_comment.render_body(manifest, results, include_marker=False)

        self.assertIn("1.0.0 (git) -> 1.0.1 (crates.io)", body)


if __name__ == "__main__":
    unittest.main()
