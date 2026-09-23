# Build Task

## Variables

### prompt

{{prompt}}

### work_item

{{work_item}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

1. Establish the target from `work_item`, or from `prompt` when no work_item is
   bound. Read the referenced definitions, relevant prior artifacts, repository
   instructions, and current implementation. Treat `previous_envelope` feedback
   as work on that same target.
2. Implement the required behavior within scope, preserving public contracts and
   existing user changes. Resolve factual gaps through targeted reading; hand off
   blocking decisions or planning conflicts without rewriting the definitions.
3. Establish sufficient evidence for this work and address each supplied failure
   or finding. Distinguish builder verification from checks and manual validation
   still owned by later phases.
4. Save the implementation handoff below, check it against the actual work and
   evidence, then emit the Report JSON.

## Save and hand off

Write `<context_handoff_dir>/build.md` as the current implementation report for
this session. On repair, update it for the same target, preserving still-valid
results and outstanding obligations; replace stale claims with current facts.
Keep other roles' artifacts intact. Scale the report to the work, merging or
omitting inapplicable sections rather than filling a fixed template:

- **Target and baseline:** direct request or bound spec/ticket references,
  applicable revision/profile, scope, relevant baseline and prerequisite evidence,
  and existing user changes that affect this work. Do not invent round identities.
- **Actual changes:** what changed and why, with key file/symbol references and
  corresponding requirement, AC, or CASE identifiers when available. Include
  relevant interface, configuration, migration, or environment handoff details.
- **Verification:** inspection, executed checks, reused evidence, and their
  results. Locate tests and key assertions or inspected symbols. For execution,
  record command, working directory/filter, exit status, intended tests actually
  run, and output/log references. Explain new regression protection, retired
  tests/facilities, or why no new tests or command execution were needed. For
  integration claims, identify real components and substituted boundaries; for
  reused evidence, identify the source and current applicability.
- **Feedback:** each supplied failure/finding, its disposition, and the relevant
  repair or investigation evidence. Use existing identifiers or precise source
  references; do not invent a finding schema.
- **Remaining obligations:** unconfirmed behavior and its impact, pending ADW
  checks, Required Manual Validation with prerequisites/steps/pass criteria and
  pending reasons, and separate Optional Smoke suggestions.
- **Risks and next action:** residual risks or concrete blockers, their impact,
  and the action or decision needed from the workflow, planner, or decomposer.

Save useful findings even when blocked. If writing fails, return fail and list
only artifacts actually saved. Before success, confirm that the report exists
and describes the final implementation, that required builder work is complete,
and that pending validation is not presented as passed or accepted.

## Report

Respond with ONLY valid JSON matching `BuildOutput` — no prose before or after.
For completed implementation:

```json
{
  "status": "success",
  "summary": "<implemented outcome within the bound target or direct request>",
  "artifacts": ["<context_handoff_dir>/build.md"],
  "commit_message": "<one-line subject covering this work's actual changes, following repository conventions>",
  "notes_for_next_agent": "<verification result and remaining ADW checks, required manual validation, optional smoke, or other handoff>"
}
```

For blocked or incomplete implementation:

```json
{
  "status": "fail",
  "summary": "<concrete blocker or unmet obligation and its impact>",
  "artifacts": ["<context_handoff_dir>/build.md"],
  "commit_message": "",
  "notes_for_next_agent": "<useful completed work, evidence, and exact decision/action needed; recommend planner or decomposer when applicable>"
}
```

Use repository-relative POSIX paths in structured artifact fields, including when
context_handoff_dir was supplied as an absolute path. Declare only saved artifacts;
use an empty list if none could be saved. Keep routing and verification details in
the report and notes_for_next_agent rather than adding JSON fields. A successful
build is an implementation handoff, not an acceptance record.
