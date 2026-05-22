import os
import sys
import tempfile
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import setup_reviews  # noqa: E402


class SetupReviewsTests(unittest.TestCase):
    def test_release_tag_for_version_uses_cli_release_tags(self):
        self.assertEqual(setup_reviews.release_tag_for_version("0.0.1-alpha.0"), "cli-v0.0.1-alpha.0")
        self.assertEqual(setup_reviews.release_tag_for_version("v0.0.1-alpha.0"), "cli-v0.0.1-alpha.0")
        self.assertEqual(setup_reviews.release_tag_for_version("cli-v0.0.1-alpha.0"), "cli-v0.0.1-alpha.0")

    def test_release_api_url_honors_repo_and_version(self):
        self.assertEqual(
            setup_reviews.release_api_url("0.0.1-alpha.0", "example/reviews"),
            "https://api.github.com/repos/example/reviews/releases/tags/cli-v0.0.1-alpha.0",
        )
        self.assertEqual(
            setup_reviews.release_api_url("", "example/reviews"),
            "https://api.github.com/repos/example/reviews/releases",
        )

    def test_select_release_assets_for_target(self):
        release = {
            "assets": [
                {
                    "name": "reviews-cli-0.0.1-alpha.0-linux-x64.tar.gz",
                    "browser_download_url": "https://example.com/reviews.tar.gz",
                },
                {"name": "checksums.txt", "browser_download_url": "https://example.com/checksums.txt"},
            ]
        }

        self.assertEqual(
            setup_reviews.select_release_assets(release, "linux-x64"),
            (
                "https://example.com/reviews.tar.gz",
                "reviews-cli-0.0.1-alpha.0-linux-x64.tar.gz",
                "https://example.com/checksums.txt",
            ),
        )

    def test_checksum_for_asset(self):
        checksums = "abc  other.tar.gz\n123  reviews-cli-0.0.1-alpha.0-linux-x64.tar.gz\n"
        self.assertEqual(
            setup_reviews.checksum_for_asset(checksums, "reviews-cli-0.0.1-alpha.0-linux-x64.tar.gz"),
            "123",
        )

    def test_platform_target(self):
        self.assertEqual(setup_reviews.platform_target("Linux", "x86_64"), "linux-x64")
        self.assertEqual(setup_reviews.platform_target("Linux", "aarch64"), "linux-arm64")
        self.assertEqual(setup_reviews.platform_target("Darwin", "arm64"), "macos-arm64")

    def test_write_outputs_writes_reviews_command_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "github-output"
            original = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(output)
            try:
                setup_reviews.write_outputs({"reviews-command": "/tmp/reviews"})
            finally:
                if original is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = original

            self.assertEqual(output.read_text(encoding="utf-8"), "reviews-command=/tmp/reviews\n")


if __name__ == "__main__":
    unittest.main()
