---
name: using-reviews-locally
description: Use the Reviews CLI from a checkout, configure token-backed CLI access, push reviews or patchsets, and report review URLs.
---

# Using Reviews CLI

The Reviews CLI reads `~/.config/reviews/config.toml`:

```toml
[default]
server_url = "https://reviews-dev.fly.dev"
api_token = "rev_..."
```

This action installs a `reviews` wrapper on `PATH`; use that command directly. Do not print or inspect the API token.

Create a new review:

```bash
reviews push --title "Review title" --description "Optional markdown" --range HEAD~1..HEAD --packet /path/to/packet.md
```

The CLI prints a review URL and patchset number. Capture those values and include them in `.dep-review-work/results.json`.

For dependency audits, run from the synthetic dependency repo prepared by the action. Do not run dependency build scripts, tests, examples, or arbitrary dependency code.
