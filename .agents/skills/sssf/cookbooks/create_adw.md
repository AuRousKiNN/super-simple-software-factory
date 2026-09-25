# Create ADW

Workflow filenames use hyphens: `adw-review-docs.py`. Runtime module and Python
identifier names retain underscores. Prefer the nine shipped workflows; ticket
is an input mode of build/recheck, not another entrypoint.

## Choose the chain

| Need | Entry or module |
|---|---|
| Inspect a repository | adw-scout |
| Plan or revise a specification | adw-plan |
| Decompose an existing specification | adw-decompose |
| Plan and decompose | adw-plan-decompose |
| Deliver an existing spec/ticket or direct request | adw-build |
| Plan and then deliver | adw-simple-sdlc |
| Execute quality commands | adw-quality |
| Reassess a review with current evidence | adw-recheck |
| Run one configured agent | adw-prompt |

Do not recreate build-only, build-test, build-review or plan-build shortcuts.
A chain containing builder enters `delivery.execute`: required checks, independent
review, bounded repairs, documentation, commit and final acceptance all belong to
that shared pipeline. Add pre-build discovery/planning phases when needed, not a
second implementation of the acceptance algorithm.

## Generate a thin entry

```bash
uv run .agents/skills/sssf/scripts/make_adw.py --name inspect-deliver --agents scout,builder
```

This writes `adws/adw-inspect-deliver.py`. Underscores supplied in `--name` normalize
to hyphens. Path separators and invalid names are rejected. The generator does not
create roster entries or prompts. Required reviewer/documenter roles are added
automatically for builder chains, which preflight quality before business turns.
Explicit reviewer/documenter suffixes after builder are absorbed by shared delivery;
other post-builder custom phases are rejected because they could invalidate proof.

Generated phases still need meaningful descriptions and task-specific prompt
context. Known commands are code phases, not agent calls. Agents are for reading,
implementation and judgment. Each agent call uses a concrete EnvelopeBase subtype.

## Shared delivery interface

```python
from adw_modules import delivery, git_helper, tickets

# Validate roster, create a run, and record the request first.
delivery.preflight(run)
item = tickets.spec_work_item(run.repo_root, "specs/example/spec.md")
return delivery.execute(run, delivery.DeliveryRequest(
    prompt="Deliver the bound specification.", work_item=item,
    build_base=git_helper.rev("HEAD")))
```

Use `tickets.ticket_work_item` for an explicit ticket and current dependency
acceptance. Every repair/review keeps that same item; feedback belongs in previous.
For direct requests, `delivery.resolve_input` creates a host-owned request spec at
`specs/request-<adw_id>/spec.md` so documentation has a durable target.

Planning requires exactly one of `--spec-dir` or `--spec` and passes PlanningTarget
to planner. Decomposition consumes planner's authoritative spec_path. A generated
planner/decomposer/builder chain requires explicit `--ticket-id` selection; ticket
order never selects a target implicitly. Definitions and prerequisite evidence
must meet the normal clean-baseline requirements before implementation.

## Runtime contracts

- Pin `openai-codex==0.155.1` in every executable entry.
- Declare and validate all REQUIRED_AGENTS before business turns.
- Use unique phase names and substantive descriptions; owner names select roster roles.
- Code phases execute deterministic commands and record their results.
- Keep parsing, lifecycle, reusable checks and acceptance policy in adw_modules.
- Functions needing more than four parameters take one typed request object.
- Every terminal business route reaches run.finish with an explicit verdict.
- Documenter is a stage, never a standalone workflow.
- Ticket acceptance is host-issued after finish, never authored by an agent.

Run deterministic tests for the changed branch and inspect generated `--help`.
Do not launch a paid agent workflow merely to check that an entry compiles.
