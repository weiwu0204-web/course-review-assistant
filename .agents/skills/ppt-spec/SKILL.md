---
name: ppt-spec
description: Draft and refine specs for PPT Reviewer changes involving public behavior, document revisions, account isolation, storage, or task scheduling. Use a short change note for a reversible cosmetic edit.
---

Read the project [docs index](../../../docs/README.md) and current product and architecture notes before making assumptions.

For an affected user journey, write `spec.md` (what changes, what stays compatible, observable acceptance), `design.md` (API, data ownership, failures, migration and rollback), and `tasks.md` (small tasks and verification). For example, for courseware updates specify which revision a queued job sees, when a revision becomes visible, and what happens on a failed or competing publish. Check sibling flows such as downloads and retries against the same ownership and revision rules. Leave product choices that need review explicit.
