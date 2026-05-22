# Cargo Dependency Reviews GitHub Action

This action audits external Cargo dependency upgrades from `Cargo.lock`. For each changed dependency it materializes the old and new source, creates a synthetic git diff, asks Claude Code to perform a security audit and write a Reviews packet, uploads the packet from a trusted post-processing step, and posts a sticky PR comment with review links and local checkout commands.

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
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          reviews-api-key: ${{ secrets.REVIEWS_API_KEY }}
          github-token: ${{ github.token }}
          pr-number: ${{ inputs.pr }}
          base-ref: HEAD^1
          head-ref: HEAD^2
```

This is intentionally manual by default. A maintainer or repository member can run it from the Actions tab with the PR number after deciding the dependency diff is worth auditing. Do not run this action automatically on every `pull_request` or `push` unless you are comfortable spending Claude and Reviews quota on every update.

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
          GH_TOKEN: ${{ github.token }}
          PR_NUMBER: ${{ github.event.issue.number }}
        run: gh workflow run dependency-reviews.yml --repo "$GITHUB_REPOSITORY" -f pr="$PR_NUMBER"
```

The command workflow should only dispatch the manual workflow; it should not check out or audit PR code itself.

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

Outside a PR event, pass `pr-number` to create or update a PR comment. Without a PR event or `pr-number`, the action does not create a comment. It still writes the same report to `$GITHUB_STEP_SUMMARY`.

The action writes the PR comment after the audit and upload attempts even when a dependency audit or Reviews upload fails, then fails the workflow after the comment is published so maintainers get both a failing check and the detailed PR report. If PR commenting is enabled and GitHub rejects the comment update, the action fails instead of silently dropping the report.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `anthropic-api-key` | yes | | Anthropic API key for the Claude Code CLI. |
| `reviews-api-key` | yes | | Reviews API key. |
| `reviews-server-url` | no | `https://reviews-dev.fly.dev` | Reviews server URL. |
| `github-token` | yes | | Token used to create or update the PR comment. |
| `lockfile` | no | `Cargo.lock` | Lockfile path relative to the repo root. |
| `base-ref` | no | PR base SHA or `HEAD~1` | Base ref used to read the old lockfile. |
| `head-ref` | no | PR head SHA or `HEAD` | Head ref used to read the new lockfile. |
| `comment-on-pr` | no | `true` | Whether to create or update a sticky PR comment. |
| `pr-number` | no | | Pull request number to comment on when the workflow is manually triggered. |
| `reviews-cli-version` | no | `0.0.1-alpha.0` | Reviews CLI release version. |
| `claude-model` | no | `sonnet` | Claude Code model or alias. `sonnet` tracks the latest Sonnet model. |
| `claude-args` | no | | Limited extra Claude Code CLI arguments. Only `--max-turns`, `--max-budget-usd`, `--effort`, `--fallback-model`, `--betas`, and `--include-partial-messages` are accepted. |

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
- Installs and runs the Claude Code CLI directly with `--model` set from `claude-model`.
- Runs a separate Claude Code CLI conversation for each dependency and streams the verbose turn-by-turn output to the GitHub Actions log.
- Runs Claude Code from a neutral per-dependency working directory with `--setting-sources user`, so dependency-provided `.claude/settings.json` and `CLAUDE.md` files are not loaded as project configuration.
- Runs Claude Code in `dontAsk` mode with only narrow `Read`, `Grep`, `Glob`, `LS`, `Edit`, and `Write` permissions for that dependency's prepared directory and the vendored packet-writing skill.
- Denies Claude Code `Bash`, `WebFetch`, `WebSearch`, subagents, and reads of the temporary auth directory, key file directory, and `/proc`.
- Gives Claude Code its Anthropic credential through an `apiKeyHelper` in a temporary Claude home, without passing `ANTHROPIC_API_KEY` to the Claude child process.
- Installs the Reviews CLI only after Claude Code finishes, then uploads packets from a trusted script outside the Claude conversation.
- Does not run dependency build scripts, tests, examples, or arbitrary dependency code.
- Uploads new dependency source tarballs as workflow artifacts for local reproduction fallback.

The PR comment includes a table of dependency updates, Reviews packet links, audit summaries, skipped changes, and copyable commands for pulling down the full new dependency source locally.
