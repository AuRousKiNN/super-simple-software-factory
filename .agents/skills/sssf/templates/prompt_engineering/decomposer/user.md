# Decomposition task

{{prompt}}

## Bound source and output directory

{{decomposition}}

## Previous feedback

{{previous_envelope}}

## Handoff directory

{{context_handoff_dir}}

Read the bound source spec, preserve its bytes, investigate relevant repository
facts, and maintain the following documents inside output_dir. Use stable paths
for the current ticket set and its tickets, and repository-relative POSIX paths
in structured fields.

Read existing documents before revising them. Give each new document revision: 1
in its YAML frontmatter. For each revision, update the corresponding file in place
and increase that document's revision by 1, including wording and ordering changes.
Unchanged documents keep their revision. Keep each document's revision count in
that document and its file path stable. The examples below show initial revisions;
fill in the current values and actual paths for this task.

`ticket-set.md`:
```markdown
---
schema_version: 1
revision: 1
source_spec: specs/example.md
tickets:
  - <output_dir>/tickets/TICKET-QUERY.md
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
IDs and explain real blocked_by prerequisites. Adapt headings and AC presentation
to make the handoff self-contained. The index reads source_spec/tickets from set
metadata and id/blocked_by plus descriptive metadata from tickets. Keep those
references usable and the set's membership consistent with the current tickets.

## Report

Emit a single JSON object matching DecomposeOutput, listing all actually saved
artifacts:
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
