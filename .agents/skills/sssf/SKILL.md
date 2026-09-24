---
name: sssf
description: Super Simple Software Factory — install and operate repeatable Codex-and-code workflows (ADWs). Use for SSSF installation, ADW creation or execution, roster/config changes, and workflow observation.
argument-hint: "[install | create adw | run adw | update config | ...]"
---

# Super Simple Software Factory

SSSF is a Codex-only software factory. Deterministic Python owns sequencing,
retries, permissions, checks, commits, and acceptance. Agents work inside named
phases and exchange typed JSON envelopes. SQLite is the durable trace.

## Startup

When invoked without a concrete request:

1. Read [cookbooks/sssf_overview.md](cookbooks/sssf_overview.md).
2. List `adws/adw_*.py` and read only each `Phases:` docstring line.
3. Present the installed ADWs as a short table and wait.

If the first message already contains a request, route it immediately. If the
factory is not installed, say so and offer the install procedure. Do not query
the trace database, inventory the repository, or diagnose old runs unless that
is the request.

## Orchestrator boundary

The orchestrator operates workflows; it does not silently perform an ADW
role's work itself.

- Do not edit `adws/adw_data/sessions/`; it is an audit record.
- Observe through `adws/adw_data/sssf.db` only when following or diagnosing a
  requested run.
- Wait silently while an ADW is running; do not send unsolicited progress
  updates or repeatedly inspect its trace. See [run_adw.md](cookbooks/run_adw.md).
- If an ADW fails, stop orchestration and report the failure to the user. Do not
  automatically repair, retry, resume, or launch a follow-up ADW.
- In the final report, state phase name, owner, status, and error plainly.
- Do not claim a workflow passed until `run.finish()` records acceptance.
- Execution records belong to business ADWs. Do not launch a standalone documenter.

## Request routing

| Request | Read and follow |
|---|---|
| install or update SSSF | [cookbooks/install.md](cookbooks/install.md) |
| create a workflow | [cookbooks/create_adw.md](cookbooks/create_adw.md) |
| modify a workflow | [cookbooks/update_adw.md](cookbooks/update_adw.md) |
| create the roster | [cookbooks/create_config.md](cookbooks/create_config.md) |
| add or tune an agent | [cookbooks/update_config.md](cookbooks/update_config.md) |
| extend runtime modules | [cookbooks/update_modules.md](cookbooks/update_modules.md) |
| prepare a workflow prompt | [cookbooks/how_to_prompt_for_the_eng.md](cookbooks/how_to_prompt_for_the_eng.md) |
| run or monitor a workflow | [cookbooks/how_to_prompt_for_the_eng.md](cookbooks/how_to_prompt_for_the_eng.md), then [cookbooks/run_adw.md](cookbooks/run_adw.md) |

Deep references: [configuration](references/config.md),
[handoffs](references/handoff.md), and
[observability](references/observability.md), and [tickets](references/tickets.md).

## Hard rules

1. Every workflow validates its required roster before creating a business
   turn.
2. Every agent call declares a concrete `EnvelopeBase` subtype. A successful
   turn's final result must validate against that type.
3. The output contract is one synchronized triad: data type, role prompt report
   example, and call-site `output_type=`.
4. Gate failures and JSON repairs use bounded turns on the same Codex thread.
   Runtime failures are not JSON retries.
5. A gate verifies a claim after work completes; it does not predict which
   paths an agent might touch.
6. A function needing more than four parameters takes one concrete data object.
7. ADW scripts stay thin; lifecycle and policy belong in `adw_modules/`.
8. Every phase description states what the phase does and why. A restated name
   is invalid.
9. A known command is a `kind="code"` phase. Agents are for reading and
   judgment, not rediscovering a deterministic command.
10. `writes` is the repository content boundary. Codex sandbox settings are a
    separate layer. Permission verification runs after success, failure,
    interruption, and cancellation.
11. Only the current report directory and `context_handoff/` are automatic
    runtime write exceptions; `data_dir` is not a blanket exception.
12. Only planner, scout and decomposer may enable child agents. Children are read-only,
    cannot recurse, cannot load this orchestrator skill, and must all terminate
    before parent acceptance.
13. Every ADW ends with `run.finish()`, optionally passing an explicit
    `accepted=` verdict and reason.
14. The active distribution has one runtime and schema version 2. Do not add a
    fallback backend, historical session importer, or silent compatibility
    path.

## Runtime version

Every shipped and generated ADW pins `openai-codex==0.155.1`. Models and
reasoning efforts are preflighted. Authentication, model, approval, timeout,
interruption, and runtime errors remain distinct outcomes. Cost and context
occupancy remain unknown when the runtime does not publish authoritative data.
