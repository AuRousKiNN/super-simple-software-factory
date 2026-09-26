# Update ADW

Modify an existing ADW chain — add phases, add gates, add a bounded fix loop.

## Preserve the planning target

For every chain containing a planner, including all `adw-plan*` scripts, keep
`--spec-dir` and `--spec` in a required mutually exclusive CLI group. Construct
`PlanningTarget` and pass it as `AgentCall.planning_target` to the planner.
Keep both new-spec and revision examples in the script docstring up to date.
For plan → decomposition, pass the returned `plan.spec_path` to
`tickets.decompose`; never derive another target from the prompt or session ID.
See [launch target selection](run_adw.md#choose-the-specification-target).

## Add a phase

Insert a `with run.phase(...)` block where it belongs in the sequence. Pick the right `kind`: `agent` for a `ph.call(...)`, `code` for a deterministic step, `engineer` for a human touchpoint. If the new phase names an agent not already in `REQUIRED_AGENTS`, add it there too — otherwise validation passes and the run dies mid-flight instead of at startup.

```python
    with run.phase(PhaseParams(name="scout", kind="agent", owner="scout",
                               description="Locate the code the request touches")) as ph:
        found = ph.call(AgentCall(output_type=ScoutOutput, prompt=prompt))
```

Phase `name` must be unique within the run — that is what the UI keys blocks on. In a loop, suffix it (`f"test_{i}"`).

`description` is **required**, and `PhaseParams` rejects both a blank one and one that merely restates the name. It is the single line of intent the trace, the console, and the UI phase block show, so write what the phase does and why — `"Land the code only now: green suite, approved review"`, not `"Commit build"`.

A code phase executes known operations and logs evidence. For delivery, use
`adw_modules/delivery.py`: document the verified result before committing it, then
confirm that commit hooks and finish projection did not change implementation
content. Use `git_helper.commit_paths` with explicit paths; never introduce an
unchecked `commit_all` shortcut after builder.

## Remove a phase

Delete the block, drop any now-unused agent from `REQUIRED_AGENTS`, and re-thread the chain: whatever the removed phase produced was probably somebody's `previous=`. Point that call at the surviving upstream envelope.

## Add gates

Gates are callables over the finished envelope — `gate(envelope, run) -> GateReport`, recording one `check(item, ok, note)` per thing they looked at, with violations derived from the failed ones. Compose them per call:

```python
    document = spec_artifacts.document(run, DocumentRequest(
        work_item=work_item, purpose="Explain this execution", changes=changes_output,
        checks=list(results.values()), review=review, evidence=[review_receipt_ref]))
    spec_artifacts.prepare_finish(run, document)
```

On violations the runtime does **not** restart the agent. It sends the violation list as a new turn on the **same Codex thread**, bounded by that phase's `retries`. Every gate result is traced to the `gate_results` table. Exhausting the retries raises `GateFailure` and fails the phase.

Gate claims, not guesses: declared artifacts exist and are non-empty, declared JSON parses, declared test commands pass. Changed paths are not an agent claim at all: use `changes.capture(...)` in a code phase, which combines Git's tracked diff with untracked files. Never hardcode counts — express quantity as a property of the declared list ("at least one artifact", "ALL declared paths valid"). Plan quality and code taste are not gateable; that is a reviewer agent or a human. New reusable gates go in `adw_modules/gates.py` (`update_modules.md`).

## Add a bounded fix loop

The shared `delivery.execute` loop allows three builder repairs and two supplemental
verification rounds. Initial checks and rechecks after a repair run every required
and previously executed check. A reviewer classifies failures and Python chooses
repair, verify, handoff or approval. A repair always invalidates prior evidence.

Configure `quality.check_specs()` and explicit `not_applicable_checks()` reasons
first. `quality.required_checks()` rejects placeholders before any business turn;
test cannot be excluded. A code phase running a red suite has executed correctly,
but the shared acceptance decision cannot approve failed, missing or stale checks.

Keep the loop in the shared module so adw-build, adw-simple-sdlc and generated
builder chains retain identical delivery guarantees. Ordinary delivery checks successful
current-definition prerequisite receipts and starts builder directly. It does not run
an evidence scout or freeze referenced implementation files. Retained session reports
and receipts stay immutable. Explicit recheck still uses `evidence_freshness.inspect`
before checks/reviewer to assess the evidence supplied for that recheck.

Shared delivery also owns [explicit recovery](../references/delivery-recovery.md).
Retain its checkpoints when extending the chain: invalidate completed builder
state before a repair, preserve consumed budgets and prior review obligations,
and validate retained stage inputs/evidence before reuse or acceptance. Do not add automatic retry in entrypoints
or cache approved verdicts across attempts.

Three distinctions worth keeping straight:

- **Gate retries vs. JSON retries.** `retries` buys extra *gate*-correction rounds. Malformed final JSON is handled separately and always — `JSON_FIX_ATTEMPTS` in `adw_modules/agents.py` (2 by default) re-prompts the same session for a valid object even on a phase with `retries=0`. Raising the phase's `retries` does not buy more JSON attempts, and vice versa.
- **Phase retries vs. fix loops.** `retries=N` on `PhaseParams` buys gate-correction turns on the same thread. A fix loop is a *chain* of phases repeated — different operations and new envelopes each pass.
- **The test phase succeeds when it runs and reports correctly.** A failing suite does not fail that phase; it fails the run, checked at the end. The runner did its job; the code didn't.

## Keep scripts thin

An ADW is sequencing and acceptance — nothing else. The moment you are writing parsing, subprocess handling, retry mechanics, or a reusable predicate inside `adw-*.py`, it belongs in `adw_modules/`. See `update_modules.md`.

## Route review verdicts

Use `review_routing.decide` (or `acceptance_decision` when code checks are required)
with explicit remaining repair/verification budgets and `quality.configured_checks()`.
Persist each decision with `review_routing.save` inside a code phase. End handoffs
with `run.finish(accepted=False, reason=decision.reason)`. Never infer repairability
from `approved=false`, nor run commands supplied in reviewer prose. Include
`verdict_consistent` and `obligations_retained` gates on repeated reviews.

See [Reviewer routing and recheck](../references/reviewer-routing.md) for the
structured contract, explicit evidence-only entry point and planning handoffs.
