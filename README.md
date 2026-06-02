# Cargo Dependency Reviews GitHub Action

This action audits external Cargo dependency upgrades from `Cargo.lock`. For each changed dependency it materializes the old and new source, creates a synthetic git diff, asks Codex CLI to perform a security audit and write a Reviews packet, uploads the packet from a trusted post-processing step, and posts a sticky PR comment with review links and local checkout commands.

## Usage

```yaml
name: Dependency reviews

on:
  workflow_dispatch:
    inputs:
      pr:
        description: Pull request number to audit
        required: true
        type: number

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
          ref: refs/pull/${{ inputs.pr }}/merge
          fetch-depth: 0

      - uses: Avi-D-coder/dep-reviews-packet-action@v1
        with:
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          reviews-api-key: ${{ secrets.REVIEWS_API_KEY }}
          github-token: ${{ github.token }}
          pr-number: ${{ inputs.pr }}
          base-ref: HEAD^1
          head-ref: HEAD^2
```

This is intentionally manual by default. A maintainer or repository member can run it from the Actions tab with the PR number after deciding the dependency diff is worth auditing. Do not run this action automatically on every `pull_request` or `push` unless you are comfortable spending Codex and Reviews quota on every update.

Avoid `pull_request_target` for this workflow. It is easy to accidentally combine repository secrets with untrusted PR content.

## Optional PR slash command

Teams that want an easier PR flow can add a small command workflow that dispatches the manual workflow when a maintainer comments `/dep-review`:

```yaml
name: Request dependency review

on:
  issue_comment:
    types: [created]

permissions:
  actions: write
  contents: read
  issues: read
  pull-requests: read

jobs:
  dispatch:
    if: >
      github.event.issue.pull_request &&
      github.event.comment.body == '/dep-review' &&
      contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association)
    runs-on: ubuntu-latest
    steps:
      - env:
          DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}
          GH_TOKEN: ${{ github.token }}
          PR_NUMBER: ${{ github.event.issue.number }}
        run: gh workflow run dependency-reviews.yml --repo "$GITHUB_REPOSITORY" --ref "$DEFAULT_BRANCH" -f pr="$PR_NUMBER"
```

The explicit ref and `contents: read` permission let `gh` resolve and dispatch the workflow while still keeping the command workflow limited to dispatching the manual workflow. The command workflow should not check out or audit PR code itself.

## Secrets

Configure these as repository secrets in the repository that runs the workflow:

| Secret name | Value |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API key for Codex CLI. |
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

Outside a PR event, pass `pr-number` to create or update a PR comment. Without a PR event or `pr-number`, the action does not create a comment. It still writes the same report to `$GITHUB_STEP_SUMMARY`.

The action writes the PR comment after the audit and upload attempts even when a dependency audit or Reviews upload fails, then fails the workflow after the comment is published so maintainers get both a failing check and the detailed PR report. If PR commenting is enabled and GitHub rejects the comment update, the action fails instead of silently dropping the report.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `openai-api-key` | yes | | OpenAI API key for Codex CLI. |
| `reviews-api-key` | yes | | Reviews API key. |
| `reviews-server-url` | no | `https://reviews-dev.fly.dev` | Reviews server URL. |
| `github-token` | yes | | Token used to create or update the PR comment. |
| `lockfile` | no | `Cargo.lock` | Lockfile path relative to the repo root. |
| `base-ref` | no | PR base SHA or `HEAD~1` | Base ref used to read the old lockfile. |
| `head-ref` | no | PR head SHA or `HEAD` | Head ref used to read the new lockfile. |
| `comment-on-pr` | no | `true` | Whether to create or update a sticky PR comment. |
| `pr-number` | no | | Pull request number to comment on when the workflow is manually triggered. |
| `reviews-cli-version` | no | `0.0.1-alpha.0` | Reviews CLI release version. |
| `codex-model` | no | | Codex CLI model. Leave empty to use the Codex CLI recommended default. |
| `codex-args` | no | | Limited extra Codex CLI arguments. Only `--color` and `--strict-config` are accepted. |

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
- Installs and runs Codex CLI directly with `codex exec`; `codex-model` is passed only when set.
- Runs a separate Codex CLI non-interactive run for each dependency and streams JSONL events to the GitHub Actions log.
- Runs Codex from a neutral per-dependency working directory with project root discovery disabled, web search disabled, hooks disabled, subagents disabled, approval policy set to `never`, sandbox set to `workspace-write`, and a custom permissions profile that reads the prepared dependency directory but only writes the packet markdown and final structured output.
- Copies the vendored packet-writing guidance into the prepared dependency directory before each Codex run, so Codex does not need access to action-owned guidance outside that workspace.
- Gives Codex the OpenAI credential only as `CODEX_API_KEY` on the single `codex exec` subprocess; the wrapper removes `OPENAI_API_KEY`, Reviews secrets, GitHub tokens, and action inputs from the child environment.
- Hashes the prepared dependency workspace before and after Codex runs and marks the dependency failed if Codex changes anything except the packet markdown and final structured output.
- Installs the Reviews CLI only after Codex finishes, then uploads packets from a trusted script outside the Codex run.
- Does not run dependency build scripts, tests, examples, or arbitrary dependency code.
- Uploads new dependency source tarballs as workflow artifacts for local reproduction fallback.

The PR comment includes a table of dependency updates, Reviews packet links, audit summaries, skipped changes, and copyable commands for pulling down the full new dependency source locally.
