---
name: reviews-overview
description: Explain the Reviews platform, when to use it, and how review packets and patchsets fit agent-driven review workflows.
---

# Reviews Overview

Reviews is a code-review tool for arbitrary diffs, not only GitHub pull requests. It lets an agent or developer push a diff to a shareable review URL, optionally attach a structured review packet, and iterate through revisions as patchsets.

Use Reviews when there is a concrete diff that benefits from a guided human review surface. Avoid it when there is no diff, when the change is trivial, or when the diff contains secrets or local-only artifacts that should not be uploaded.

Important terms:

- **Review**: the durable review URL and container for one line of work.
- **Patchset**: one revision of the uploaded diff within a review.
- **Review packet**: a markdown or JSON guide that describes how to review the diff.
- **Section**: a packet `##` grouping with prose and hunk references.
- **Hunk reference**: a pointer such as `@hunk path/to/file.ex#2` or a slice like `@hunk path/to/file.ex#2:L3-L18`.

Prefer producing a packet rather than uploading a raw diff alone for substantial work. The packet should tell the human what to review first, why sections exist, and where tradeoffs or risk live.

When reporting a Reviews link, include the URL and patchset number if the CLI provides one.
