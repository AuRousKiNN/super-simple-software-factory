# Execution Record Task

## Request
{{prompt}}

## Bound document context
{{document_context}}

Read all required references. Write the execution report body to
`document_context.execution_draft_path` and updated cumulative overview body to
`document_context.overview_draft_path`. The report covers target and revision,
actual work, fulfillment, checks and review, unresolved obligations, blockers and
handoff. The overview explains current capabilities, implementation and validation
separately, evidence applicability, missing work and next steps. Preserve the known
state of requirements outside this invocation's scope. The runtime owns navigation.

Return only JSON matching `DocumentDraftOutput`:

```json
{
  "status": "success",
  "summary": "<execution observation and cumulative update>",
  "execution_draft_path": "<document_context.execution_draft_path>",
  "overview_draft_path": "<document_context.overview_draft_path>",
  "documented_files": ["<an actual changed file, if any>"],
  "artifacts": ["<document_context.execution_draft_path>", "<document_context.overview_draft_path>"],
  "commit_message": "<repository-conventional subject for these documents>",
  "notes_for_next_agent": "<remaining evidence, blockers and continuation context>"
}
```

Artifacts list only existing drafts. All structured paths are repository-relative
POSIX paths. The host publishes formal `DocumentOutput` after validation.
