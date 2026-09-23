# Plan Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Plan the work described in `prompt`. Use `previous_envelope` and its referenced
artifacts when relevant, reconciling them with the current request and repository.

Maintain one self-contained root spec at its authoritative `spec_path`, with a
matching handoff copy at `<context_handoff_dir>/plan.md`. Record one integer
`revision` in YAML frontmatter and state readiness near the top. Organize the
following information to fit the request; merge sections for small tasks and
include conditional detail only when relevant:

- **Goal and scope:** the complete user or caller outcome, explicit constraints,
  and meaningful exclusions.
- **Current understanding:** confirmed facts and their sources, proposed changes,
  assumptions, and any missing facts or decisions.
- **Requirements and acceptance:** observable behavior and completion criteria,
  with stable REQ, CONTRACT and AC identifiers; shared contracts defined once.
- **Prerequisites and integration:** real technical and environment prerequisites
  and the complete integrated result. Leave ticket boundaries and allocation to
  the decomposer; this root spec must also support direct implementation.
- **Verification:** sufficient evidence for the criteria, existing evidence that
  may be reused, necessary new regression protection, real mechanisms and
  replaceable external boundaries, plus required checks and environment needs.
  Include verified repository commands only when useful; leave unknown commands
  for execution-time discovery instead of presenting guesses as working commands.
- **Risks and decisions:** concrete high-impact boundaries, required manual
  validation, optional smoke checks, residual risks, and blocking questions with
  their impact. For a revision, summarize the changes and affected obligations.

Compare the plan with the original request before handing it off: all requested
behavior must be represented, public semantics must be settled for a ready plan,
and future validation must remain clearly distinct from completed evidence.

## Save and hand off

1. Resolve `spec_path` from the task or the referenced plan being revised. For a
   new plan, inspect or create `specs/` and choose an available descriptive path
   `specs/<adw_id>_<slug>.md`; `<adw_id>` is the session directory name in
   `context_handoff_dir`. Keep this path for the lifetime of the plan.
2. Create a new spec with `revision: 1`. For each revision, read the current spec,
   update the same file in place, and increase its document revision by 1. Apply
   this rule to wording and ordering changes as well as behavioral changes. An
   unchanged document keeps its current revision.
3. Save the current spec even when it is blocked, clearly identifying the missing
   decision or input. Keep known requirements and useful findings available for
   recovery. Copy its exact contents to `<context_handoff_dir>/plan.md`, updating
   the handoff file to match the current spec.
4. Confirm both files contain the same current plan, revision, and readiness. Emit
   the report below with their actual paths. If writing fails, list the artifacts
   that were successfully saved and report the failure.

## Report

Respond with a single valid JSON object matching `PlanOutput`:

```json
{
  "status": "success",
  "summary": "<the planned outcome, or the concrete blocker when status is fail>",
  "artifacts": ["<context_handoff_dir>/plan.md", "<spec_path>"],
  "spec_path": "<spec_path>",
  "commit_message": "<one-line subject for THIS PLAN DOCUMENT, following repository conventions>",
  "notes_for_next_agent": "<implementation handoff, or the decision/input needed to unblock planning>"
}
```

Use `status: "success"` only for an implementable plan whose two copies were
saved. Use `status: "fail"` for unresolved blockers or artifact failures; a saved
blocked plan is still a failed planning phase. A successful plan establishes
readiness for future implementation and verification.

`spec_path` identifies the current authoritative spec at a stable
repository-relative POSIX path.
Use repository-relative POSIX paths in all structured artifact fields.
