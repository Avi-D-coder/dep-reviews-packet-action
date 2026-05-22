---
name: writing-review-packets
description: Create or revise Reviews packet markdown for a code diff, including section strategy, prose, hunk references, validation, and pushing packet revisions with the Reviews CLI.
---

# Writing Review Packets

Use this skill when drafting packet markdown for `reviews push --packet`. A good packet is a reviewer map: it groups related hunks, explains why to look, and avoids rephrasing the diff line by line.

## Packet Markdown Format

Use one top-level title and `##` packet sections:

```markdown
# Packet Title

Short overview of what the review packet covers.

## Section Title

One or two sentences orienting the reviewer.

@hunk path/to/file.ex#1
```

Rules:

- Start with exactly one `#` title.
- Use `##` for review packet sections.
- Prose between hunk refs is allowed and encouraged.
- Hunk refs use `@hunk path#N`.
- Paths and hunk numbers must match the diff being pushed.

## Writing Method

1. Inspect the diff with `git diff` or the intended CLI range.
2. Identify logical review areas.
3. Put a 1-2 sentence summary at the top of each `##` section.
4. Add terse hunk explainers when they help the reader know what to look for.
5. Avoid hunk explainers that merely restate the diff.

## Hunk Selection

- Cover every changed line exactly once.
- Use full hunk refs for small cohesive changes.
- If one large hunk contains multiple topics, split surrounding prose instead of duplicating hunk refs.
- Keep generated files, lockfiles, and mechanical output out of the packet unless they need review.

## Push Workflow

Use the CLI from the git checkout being reviewed:

```bash
reviews push --packet /path/to/packet.md
reviews push --update <slug> --packet /path/to/packet.md
reviews push --range HEAD~1..HEAD --packet /path/to/packet.md
```

If validation fails with an uncovered changed line, add or adjust hunk refs until the packet covers the diff.
