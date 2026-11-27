---
name: ppt-change
description: Implement and review a bounded PPT Reviewer spec or a reproducible defect, keeping the desktop baseline and documenting evidence before committing a service milestone.
---

Read [AGENTS.md](../../../AGENTS.md), the relevant spec, and the affected API and storage paths. Start from a verified commit and scope the diff to one task.

For authorization, revisions, or job-state changes, first make the acceptance test fail for the intended reason, then implement the smallest useful behavior and rerun it. Use synthetic files and a fake generator. For file moves verify existing behavior with the relevant regression tests.

Before committing, examine authorization and retry paths, database transaction boundaries, side effects, dependencies, and how to roll back the change. Report what ran against real backing services and what ran against substitutes. Keep paid model probes and external deployment separate.
