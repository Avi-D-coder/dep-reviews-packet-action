import os
import sys
import tempfile
import unittest
import urllib.parse
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

    def test_maybe_upsert_comment_raises_on_comment_error(self):
        class Args:
            comment_on_pr = True
            github_token = "token"
            pr_number = ""

        original_pull_request_number = post_comment.pull_request_number
        original_upsert_comment = post_comment.upsert_comment
        try:
            post_comment.pull_request_number = lambda: 1

            def fail_upsert(token, pr_number, body):
                raise RuntimeError("GitHub API request failed: 403")

            post_comment.upsert_comment = fail_upsert
            with self.assertRaises(RuntimeError):
                post_comment.maybe_upsert_comment(Args(), "body")
        finally:
            post_comment.pull_request_number = original_pull_request_number
            post_comment.upsert_comment = original_upsert_comment

    def test_maybe_upsert_comment_uses_explicit_pr_number(self):
        class Args:
            comment_on_pr = True
            github_token = "token"
            pr_number = "42"

        original_pull_request_number = post_comment.pull_request_number
        original_upsert_comment = post_comment.upsert_comment
        seen = {}
        try:
            post_comment.pull_request_number = lambda: None

            def upsert(token, pr_number, body):
                seen["pr_number"] = pr_number
                return "https://github.example/comment"

            post_comment.upsert_comment = upsert
            self.assertEqual(post_comment.maybe_upsert_comment(Args(), "body"), "https://github.example/comment")
        finally:
            post_comment.pull_request_number = original_pull_request_number
            post_comment.upsert_comment = original_upsert_comment

        self.assertEqual(seen["pr_number"], 42)

    def test_find_existing_comment_returns_latest_marker(self):
        pages = {
            1: [
                {"body": post_comment.MARKER, "url": "https://api.github.example/comments/old"},
                {"body": "ordinary comment", "url": "https://api.github.example/comments/other"},
            ],
            2: [{"body": post_comment.MARKER, "url": "https://api.github.example/comments/new"}],
            3: [],
        }

        original_request_json = post_comment.request_json
        try:
            def fake_request_json(token, url, method="GET", data=None):
                page = int(url.rsplit("page=", 1)[1])
                return pages[page]

            post_comment.request_json = fake_request_json

            comment = post_comment.find_existing_comment("token", "https://api.github.example/comments")
        finally:
            post_comment.request_json = original_request_json

        self.assertEqual(comment["url"], "https://api.github.example/comments/new")

    def test_upsert_comment_creates_new_comment_when_marker_update_is_forbidden(self):
        calls = []

        original_request_json = post_comment.request_json
        original_repository = os.environ.get("GITHUB_REPOSITORY")
        try:
            os.environ["GITHUB_REPOSITORY"] = "owner/repo"

            def fake_request_json(token, url, method="GET", data=None):
                calls.append((method, url, data))
                if method == "GET":
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                    if query.get("page") == ["1"]:
                        return [{"body": post_comment.MARKER, "url": "https://api.github.example/comments/old"}]
                    return []
                if method == "PATCH":
                    raise RuntimeError("GitHub API request failed: 403 forbidden")
                if method == "POST":
                    return {"html_url": "https://github.example/comments/new"}
                raise AssertionError(f"unexpected method: {method}")

            post_comment.request_json = fake_request_json

            comment_url = post_comment.upsert_comment("token", 7, "body")
        finally:
            post_comment.request_json = original_request_json
            if original_repository is None:
                os.environ.pop("GITHUB_REPOSITORY", None)
            else:
                os.environ["GITHUB_REPOSITORY"] = original_repository

        self.assertEqual(comment_url, "https://github.example/comments/new")
        self.assertEqual([call[0] for call in calls], ["GET", "GET", "PATCH", "POST"])

    def test_maybe_upsert_comment_requires_token_when_enabled(self):
        class Args:
            comment_on_pr = True
            github_token = ""
            pr_number = "42"

        with self.assertRaises(RuntimeError):
            post_comment.maybe_upsert_comment(Args(), "body")


if __name__ == "__main__":
    unittest.main()
