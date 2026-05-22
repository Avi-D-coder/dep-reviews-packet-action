# Cargo Dependency Reviews GitHub Action

This action audits external Cargo dependency upgrades from `Cargo.lock`. For each changed dependency it materializes the old and new source, creates a synthetic git diff, asks Claude Code to perform a security audit and write a Reviews packet, uploads the packet to Reviews, and posts a sticky PR comment with review links and local checkout commands.

## Usage

```yaml
name: Dependency reviews

on:
  pull_request:
    paths:
      - Cargo.lock

permissions:
  contents: read
  pull-requests: read
  issues: write

jobs:
  dep-reviews:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0

      - uses: Avi-D-coder/dep-reviews-packet-action@v1
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          reviews-api-key: ${{ secrets.REVIEWS_API_KEY }}
          github-token: ${{ github.token }}
```

Use normal `pull_request` events for trusted contexts. Do not run this action with repository secrets on untrusted fork code via `pull_request_target`.

## Secrets

Configure these as repository secrets in the repository that runs the workflow:

| Secret name | Value |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude Code. |
| `REVIEWS_API_KEY` | Reviews API token for uploading packets. |

In GitHub, open the target repository, then go to **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**. Add each secret with the exact names above.

Do not create a `GITHUB_TOKEN` secret. GitHub automatically provides `${{ github.token }}` for each workflow run, and the workflow passes it to `github-token`.

For testing, open PRs from branches in the same repository. GitHub does not pass repository secrets to `pull_request` workflows from forks, except for the built-in `GITHUB_TOKEN`.

## PR comments

When this action runs from a `pull_request` event, it creates or updates one sticky PR comment by default. The comment is marked with `<!-- dep-reviews-packet-action -->`, so later runs update the existing comment instead of adding a new one.

Commenting requires:

- `comment-on-pr` left as `true`.
- `github-token` set, usually to `${{ github.token }}`.
- Workflow permission `issues: write`, because GitHub PR comments are issue comments.

Outside a PR event, the action does not create a comment. It still writes the same report to `$GITHUB_STEP_SUMMARY`.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `anthropic-api-key` | yes | | Anthropic API key for `anthropics/claude-code-action@v1`. |
| `reviews-api-key` | yes | | Reviews API key. |
| `reviews-server-url` | no | `https://reviews-dev.fly.dev` | Reviews server URL. |
| `github-token` | yes | | Token used to create or update the PR comment. |
| `lockfile` | no | `Cargo.lock` | Lockfile path relative to the repo root. |
| `base-ref` | no | PR base SHA or `HEAD~1` | Base ref used to read the old lockfile. |
| `head-ref` | no | PR head SHA or `HEAD` | Head ref used to read the new lockfile. |
| `comment-on-pr` | no | `true` | Whether to create or update a sticky PR comment. |
| `reviews-cli-version` | no | `0.0.1-alpha.0` | Reviews CLI release version. |
| `claude-args` | no | | Extra Claude Code CLI arguments. |

## Outputs

| Output | Description |
| --- | --- |
| `review-urls` | JSON array of uploaded Reviews packet URLs. |
| `comment-url` | URL of the sticky PR comment, or an empty string when no PR comment was written. |
| `results-json` | Workspace path to the machine-readable audit results JSON file. |

## Behavior

- Pairs upgrades by package name plus normalized external source.
- Audits dependency source migrations, including crates.io to git and git to crates.io changes.
- Audits newly added external dependencies as an empty baseline to new source diff.
- Skips local/path dependencies, removals, and ambiguous multi-version changes in v1.
- Downloads crates.io archives and verifies `Cargo.lock` checksums.
- Clones git dependencies and checks out the exact lockfile revision.
- Falls back to `cargo vendor --locked --versioned-dirs` for other Cargo source styles.
- Installs the Reviews CLI from the requested `cli-v*` release asset and verifies it against release checksums.
- Does not run dependency build scripts, tests, examples, or arbitrary dependency code.
- Uploads new dependency source tarballs as workflow artifacts for local reproduction fallback.

The PR comment includes a table of dependency updates, Reviews packet links, audit summaries, skipped changes, and copyable commands for pulling down the full new dependency source locally.
