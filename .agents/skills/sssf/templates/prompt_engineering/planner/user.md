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

Write one self-contained plan at `<context_handoff_dir>/plan.md`. State its
readiness at the top, then organize the following information to fit the request;
merge sections for small tasks and include conditional detail only when relevant:

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
  their impact. For a revision, identify the prior plan and affected obligations.

Compare the plan with the original request before handing it off: all requested
behavior must be represented, public semantics must be settled for a ready plan,
and future validation must remain clearly distinct from completed evidence.

## Save and hand off

1. Save the plan even when it is blocked, clearly identifying the missing decision
   or input. Keep the known requirements and useful findings available for recovery.
2. Archive an identical copy under `specs/`. Inspect that directory before choosing
   a name; if it does not exist, create it. Use `specs/<adw_id>_<slug>.md`, where
   `<adw_id>` is the session directory name in `context_handoff_dir` and `<slug>`
   briefly describes the work. If occupied, choose `_v2`, `_v3`, and so on until
   the name is free. Preserve every existing archived plan, including prior plans
   from the same session. Copy the file rather than generating its contents again.
3. Confirm both files exist, contain the same plan, and match its readiness. Emit
   the report below with the actual paths. If writing fails, declare only artifacts
   that were actually saved and report the failure.

## Report

Respond with ONLY valid JSON matching `PlanOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<the planned outcome, or the concrete blocker when status is fail>",
  "artifacts": ["<context_handoff_dir>/plan.md", "specs/<adw_id>_<slug>.md"],
  "spec_path": "specs/<adw_id>_<slug>.md",
  "commit_message": "<one-line subject for THIS PLAN DOCUMENT, following repository conventions>",
  "notes_for_next_agent": "<implementation handoff, or the decision/input needed to unblock planning>"
}
```

Use `status: "success"` only for an implementable plan whose two copies were
saved. Use `status: "fail"` for unresolved blockers or artifact failures; a saved
blocked plan is still a failed planning phase. A successful plan describes future
implementation and verification, not proof that either has already happened.

`spec_path` is the authoritative archive, a repository-relative POSIX path.
Use repository-relative POSIX paths in all structured artifact fields.
