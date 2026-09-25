# Scout Task

## Variables

### output_type

{{output_type}}

### work_item

{{work_item}}

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Find what `prompt` asks about. For ordinary scouting, write findings into
`context_handoff_dir`. For `EvidenceScoutOutput`, return the temporary result inline
with `artifacts: []` and do not write any report file. Emit the matching JSON below.

## Report

Respond with ONLY valid JSON matching `output_type` — no prose before or after.
For ordinary `ScoutOutput`:

```json
{
  "status": "success",
  "summary": "<one sentence on what you found>",
  "findings": [
    { "file": "src/server.ts", "note": "<why this file matters>" }
  ],
  "artifacts": ["<context_handoff_dir>/scout_findings.md"]
}
```

For `EvidenceScoutOutput`, use this report instead. Include exactly one assessment
for every artifact listed in `prompt`; verdicts do not change execution status:

```json
{
  "status": "success",
  "summary": "<brief applicability conclusion>",
  "findings": [{"file": "src/server.ts", "note": "<relevant changed or unchanged premise>"}],
  "assessments": [{
    "path": "<supplied evidence path>",
    "sha256": "<supplied hash>",
    "verdict": "applicable",
    "reason": "<why this proof still applies, is stale, or cannot be confirmed>"
  }],
  "artifacts": []
}
```
