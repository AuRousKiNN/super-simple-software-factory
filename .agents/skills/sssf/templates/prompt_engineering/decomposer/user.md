# Decomposition task

{{prompt}}

## Bound source and output directory

{{decomposition}}

## Previous feedback

{{previous_envelope}}

## Handoff directory

{{context_handoff_dir}}

Read the bound source spec, preserve its bytes, investigate relevant repository
facts, and write the following inside output_dir. All structured paths must be
repository-relative POSIX paths. Use the bound revision for the set.

`ticket-set.md`:
```markdown
---
schema_version: 1
revision: 1
source_spec: specs/example.md
tickets:
  - specs/example.tickets/r1/tickets/TICKET-QUERY.md
---
# Ticket set
## Integration obligations
Root AC and cross-ticket outcomes, required checks and manual validation.
## Coordination
Shared modifications and coordination requirements, or explicitly none.
## Rationale and revision impact
Delivery boundaries, evidence gaps, and affected tickets/evidence.
```

`tickets/TICKET-QUERY.md` (one file per ticket):
```markdown
---
schema_version: 1
id: TICKET-QUERY
revision: 1
kind: behavior
profile: standard
blocked_by: []
requirements: [REQ-01]
---
# Query behavior
## Behavior and scope
Existing capabilities, the increment, and explicit scope.
## Contracts and prerequisites
Root CONTRACT references, true blocker capabilities and environment premises.
## Acceptance criteria
| AC | Observable outcome | Evidence responsibility |
|---|---|---|
| AC-QUERY-01 | Concrete observable result | Sufficient evidence |
## Verification and risks
Required checks, regression protection, reusable evidence and residual risks.
```

Prefer kind behavior/refactor/integration and profile standard/critical. Use stable
IDs and explain real blocked_by prerequisites. The examples are writing guidance;
Markdown headings, AC presentation, identifiers and frontmatter schemas are not
machine-validated. The index reads source_spec/tickets from set metadata and
id/blocked_by plus descriptive metadata from tickets. Keep those references usable.

## Report

Emit ONLY JSON matching DecomposeOutput, listing all actually saved artifacts:
```json
{
  "status": "success",
  "summary": "<deliveries and dependencies, or specific blocker>",
  "artifacts": ["<output_dir>/ticket-set.md", "<output_dir>/tickets/TICKET-QUERY.md"],
  "ticket_set_path": "<output_dir>/ticket-set.md",
  "outcome": "ready",
  "commit_message": "<summary of these planning artifacts following repository conventions>",
  "notes_for_next_agent": "<implementation, revision or decision handoff>"
}
```

success pairs only with ready. For needs_spec_revision, needs_decision or
artifact_error use fail. ticket_set_path may be empty if nothing was saved.
