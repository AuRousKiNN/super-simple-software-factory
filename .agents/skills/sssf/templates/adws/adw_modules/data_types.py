"""Concrete data types for the SSSF ADW system.

RULE (four-param rule): any function that takes more than 4 parameters takes
ONE of these objects instead. AgentCall and PhaseParams are the pattern.

Every agent call declares a concrete output type — an EnvelopeBase subclass —
that its final JSON response is parsed against. No untyped handoffs.
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

PhaseKind = Literal["engineer", "agent", "code"]
PhaseStatus = Literal["queued", "running", "success", "fail"]


# ── Phases ────────────────────────────────────────────────────────────────────

class PhaseParams(BaseModel):
    """Everything run.phase() needs. Passed as one object, never loose params."""

    name: str                       # short id, unique within the run: "plan", "build"
    kind: PhaseKind                 # which lane the block renders in
    owner: str                      # engineer's name, "git", or an agent name from config
    description: str                # REQUIRED: what this phase does and why — see below
    retries: int = 0                # agent phases: gate-failure retries via continue

    @field_validator("description")
    @classmethod
    def _description_must_be_earned(cls, value: str, info: ValidationInfo) -> str:
        """A phase name identifies; a description explains. Both are required.

        The description is the only sentence the trace, the console, and the
        phase block in the UI ever show about intent — everything else is ids,
        statuses, and timings. `commit_plan: "Commit the plan"` tells a reader
        nothing they could not already see, so an echo is rejected the same way
        a blank one is. This is a construction-time error on purpose: it fires
        before the phase opens, not after a run is already in the trace.
        """
        text = " ".join(value.split())
        name = str(info.data.get("name", "?"))
        if not text:
            raise ValueError(
                f"phase {name!r}: description is required — one sentence on what this "
                f"phase does and why. It is what the trace and the UI show.")
        if text.rstrip(".").casefold() == name.replace("_", " ").casefold():
            raise ValueError(
                f"phase {name!r}: description {text!r} only restates the phase name — "
                f"say what it does and why instead.")
        return text


class Phase(BaseModel):
    """The persisted phase record — PhaseParams plus lifecycle."""

    phase_id: str
    adw_id: str
    seq: int
    params: PhaseParams
    status: PhaseStatus = "fail"    # success must be earned
    attempt: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Envelopes (agent output types) ───────────────────────────────────────────

class EnvelopeBase(BaseModel):
    """Base of every agent's final JSON response. Output types extend this."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "fail"]
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    notes_for_next_agent: str = ""


class GenericOutput(EnvelopeBase):
    pass


class PlanOutput(EnvelopeBase):
    spec_path: str = ""

    # Subject for committing the PLAN — the spec file the planner wrote, not the
    # implementation it describes. Each agent's commit_message covers its own
    # work product, so a chain that commits per step never reuses one agent's
    # words for another agent's diff.
    commit_message: str = ""


class DecomposeOutput(EnvelopeBase):
    ticket_set_path: str = ""
    outcome: Literal["ready", "needs_spec_revision", "needs_decision", "artifact_error"]
    commit_message: str = ""

    @model_validator(mode="after")
    def consistent_outcome(self):
        if (self.status == "success") != (self.outcome == "ready"):
            raise ValueError("success requires ready; other outcomes require fail")
        if self.status == "success" and not self.ticket_set_path:
            raise ValueError("ready requires ticket_set_path")
        return self


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SpecWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["spec"] = "spec"
    spec: ArtifactRef


class TicketWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["ticket"] = "ticket"
    spec: ArtifactRef
    ticket_set: ArtifactRef
    ticket: ArtifactRef
    index: ArtifactRef
    ticket_id: str
    definition_sha256: str
    dependency_evidence: list[ArtifactRef] = Field(default_factory=list)


class DecompositionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: ArtifactRef
    output_dir: str


class AcceptanceRecord(BaseModel):
    """Host-issued evidence with its observed baseline and applicability."""
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    accepted: Literal[True] = True
    adw_id: str
    ticket_id: str
    definition_sha256: str
    baseline: str
    checks: list[ArtifactRef] = Field(min_length=1)
    reviews: list[ArtifactRef] = Field(min_length=1)
    manual_validation: list[ArtifactRef] = Field(default_factory=list)
    applicability: str = Field(min_length=1)


class BuildOutput(EnvelopeBase):
    commit_message: str = ""        # consumed by the git commit phase


class ScoutFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    note: str = ""


class ScoutOutput(EnvelopeBase):
    findings: list[ScoutFinding] = Field(default_factory=list)


class EvidenceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Keep the model inside the SDK schema subset; the coverage gate checks
    # exact host-supplied paths/hashes and nonblank reasons.
    path: str
    sha256: str
    verdict: Literal["applicable", "stale", "uncertain"]
    reason: str


class EvidenceScoutOutput(ScoutOutput):
    """One bounded scout investigation; stale/uncertain evidence stops the ADW."""
    assessments: list[EvidenceAssessment]


class ReviewFinding(BaseModel):
    """One thing the request (or plan) asked for, and whether it is there."""

    model_config = ConfigDict(extra="forbid")

    requirement: str                # the ask, in the requester's words
    met: bool
    evidence: str = ""              # where it lives, or what is missing


ReviewBlockerKind = Literal[
    "implementation", "test_implementation", "check_execution", "spec_conflict",
    "ticket_conflict", "environment", "manual_validation", "external_regression",
    "protocol_issue",
]
ReviewOwner = Literal["builder", "quality", "planner", "decomposer", "environment", "human", "external"]


class ReviewBlocker(BaseModel):
    """One independently actionable gap; semantic consistency is checked by a gate."""
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: ReviewBlockerKind
    owner: ReviewOwner
    description: str
    basis: list[str]
    trigger: str
    consequence: str
    evidence: list[str]
    closure: str
    handoff: str
    checks: list[str] = Field(default_factory=list)
    preconditions: str = ""
    steps: list[str] = Field(default_factory=list)
    pass_criteria: str = ""
    affected_tickets: list[str] = Field(default_factory=list)
    invalidated_evidence: list[str] = Field(default_factory=list)


class ReviewObligation(BaseModel):
    """Mandatory verification owned by this review, including required manual work."""
    model_config = ConfigDict(extra="forbid")
    id: str
    description: str
    satisfied: bool
    evidence: list[str] = Field(default_factory=list)


class ReviewOutput(EnvelopeBase):
    """A completed review can reject the target without failing its execution."""
    approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    blocking: list[ReviewBlocker] = Field(default_factory=list)
    required_verification: list[ReviewObligation] = Field(default_factory=list)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "repair", "verify", "handoff"]
    reason: str
    owners: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class ReviewReceipt(BaseModel):
    """Host-owned review snapshot, used as the explicit recheck source."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    adw_id: str
    prompt: str
    work_item: SpecWorkItem | TicketWorkItem | None = Field(default=None, discriminator="kind")
    baseline: str
    tree_sha256: str
    tree_files: dict[str, str]
    build_base: str
    review: ReviewOutput
    decision: ReviewDecision
    mandatory_checks: list[str] = Field(default_factory=list)
    reports: dict[str, str] = Field(default_factory=dict)


class RecheckEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: ArtifactRef
    resolves: list[str] = Field(min_length=1)
    applicability: str = Field(min_length=1)


class RecheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_review: ArtifactRef
    baseline: str = Field(min_length=1)
    evidence: list[RecheckEvidence] = Field(default_factory=list)
    dependency_evidence: list[ArtifactRef] | None = None
    checks: list[str] = Field(default_factory=list)


class PlanningTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec_dir: str | None = None
    spec: str | None = None

    @model_validator(mode="after")
    def exclusive(self):
        if bool(self.spec_dir) == bool(self.spec):
            raise ValueError("provide exactly one of --spec-dir or --spec")
        return self


class DocumentDraftOutput(EnvelopeBase):
    """Agent-authored bodies, never a publication or acceptance receipt."""
    execution_draft_path: str
    overview_draft_path: str
    documented_files: list[str] = Field(default_factory=list)
    commit_message: str = ""


class DocumentOutput(EnvelopeBase):
    """Host-published execution report and cumulative overview."""
    spec_path: str
    document_path: str
    overview_path: str
    documented_files: list[str] = Field(default_factory=list)
    commit_message: str = ""


# ── Deterministic quality blocks ─────────────────────────────────────────────

QualityArea = Literal["frontend", "backend"]
QualityOperation = Literal["lint", "typecheck", "build"]


class QualityCheckSpec(BaseModel):
    """One deterministic quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    argv: list[str]
    timeout_seconds: int = 120


class QualityCheckResult(BaseModel):
    """Captured evidence from one quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    command: str
    returncode: int
    passed: bool
    duration_seconds: float
    output_artifact: str
    input_fingerprint: str = ""
    applicable: bool = True
    # The tail of stdout+stderr, verbatim and unparsed. A failure has to travel
    # back to the builder as an envelope, and the builder cannot open a log file
    # it was never handed — so the evidence rides along. Deliberately raw: every
    # runner formats failures differently and a generic parser would be
    # confidently wrong. The full log is always at output_artifact.
    output_tail: str = ""


class QualityResult(BaseModel):
    """Aggregate result from a quality block: every check it ran, and the verdict."""

    passed: bool
    checks: list[QualityCheckResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


# ── Change capture (git diff, deterministic) ─────────────────────────────────

class ChangeCapture(BaseModel):
    """Everything documentation.capture() needs. One object, never loose params."""

    base: str = "main"              # the ref the work is measured against
    max_diff_lines: int = 2000      # the diff artifact is truncated past this
    include_untracked: bool = True  # a brand-new file is part of the change


class BaseRef(BaseModel):
    """The commit a change is measured from, and why that one.

    `reason` is the line the trace shows. A diff is only as trustworthy as the
    thing it was taken against, so the ADW records that choice instead of
    leaving the reader to infer it.
    """

    ref: str                        # what was asked for: "main", or a pinned sha
    commit: str                     # the commit actually diffed against
    reason: str = ""

    @property
    def label(self) -> str:
        """Display form — a named ref as itself, a pinned raw sha shortened."""
        if len(self.ref) == 40 and all(c in "0123456789abcdef" for c in self.ref):
            return self.ref[:7]
        return self.ref


class ChangeSet(BaseModel):
    """What changed since the base commit — pure git facts, no judgement."""

    base: BaseRef
    files: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""                  # `git diff --stat` output, verbatim
    diff_path: str = ""             # the full diff, written into context_handoff/
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not (self.files or self.untracked)


class ChangesOutput(EnvelopeBase):
    """A ChangeSet shaped as an envelope so an agent can be handed it directly.

    Same adapter idea as VerifyOutput: code computes the diff, the documenter
    consumes it through the one door every agent handoff uses.
    """

    base: str = ""                  # "<ref> @ <commit> — <reason>"
    changed_files: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""
    diff_path: str = ""             # read this for the full diff


class VerifyOutput(EnvelopeBase):
    """A deterministic result, shaped as an envelope so an agent can consume it.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door a tester agent's report used to —
    the ADW script is the only thing that knows the difference.
    """

    passed: bool = False
    failures: list[str] = Field(default_factory=list)


class DocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_item: SpecWorkItem | TicketWorkItem = Field(discriminator="kind")
    purpose: str = Field(min_length=1)
    changes: ChangesOutput | None = None
    checks: list[QualityCheckResult] = Field(default_factory=list)
    review: ReviewOutput | None = None
    review_receipt: ArtifactRef | None = None
    evidence: list[ArtifactRef] = Field(default_factory=list)
    record_requested: bool = True
    session_only: bool = False


class DocumentContext(DocumentRequest):
    review_applicable: bool | None = None
    spec_key: str
    revision: int
    overview: ArtifactRef
    history: list[ArtifactRef] = Field(default_factory=list)
    baseline: str
    snapshot: str
    observed_at: str
    execution_id: str
    invocation_dir: str
    execution_draft_path: str
    overview_draft_path: str
    document_path: str
    overview_path: str


# ── Agent calls ──────────────────────────────────────────────────────────────

class GateCheck(BaseModel):
    """One thing a gate looked at, and what it found.

    `note` is the evidence — "exists, 2.1KB", "exit 0", "not in the diff". On a
    failed check it doubles as the reason, so it is what the agent is told.
    """

    item: str                       # what was checked: a path, a command, a test
    ok: bool
    note: str = ""


class GateReport(BaseModel):
    """What every gate returns: the checks it ran. Violations are derived.

    Authoring stays a one-liner per item — `report.check(...)` appends and
    returns self, so a gate is a loop and a return.
    """

    checks: list[GateCheck] = Field(default_factory=list)

    def check(self, item: str, ok: bool, note: str = "") -> "GateReport":
        self.checks.append(GateCheck(item=item, ok=ok, note=note))
        return self

    @property
    def violations(self) -> list[str]:
        return [f"{c.item}: {c.note or 'failed'}" for c in self.checks if not c.ok]

    @property
    def passed(self) -> bool:
        return not self.violations


class AgentCall(BaseModel):
    """One agent invocation: prompt in, typed envelope out, gates verified."""

    model_config = {"arbitrary_types_allowed": True}

    output_type: Type[EnvelopeBase]
    prompt: str
    previous: Optional[EnvelopeBase] = None
    work_item: SpecWorkItem | TicketWorkItem | None = Field(default=None, discriminator="kind")
    decomposition: DecompositionInput | None = None
    planning_target: PlanningTarget | None = None
    document_context: DocumentContext | None = None
    gates: list[Callable] = Field(default_factory=list)   # gate(envelope, run) -> list[str]


# ── Config ───────────────────────────────────────────────────────────────────

class StrictConfigModel(BaseModel):
    """Configuration objects reject misspellings instead of ignoring them."""

    model_config = ConfigDict(extra="forbid")


ReasoningEffort = Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
]


class PromptEngineering(StrictConfigModel):
    system: str                     # path to system.md
    user: str                       # path to user.md


class SubagentConfig(StrictConfigModel):
    enabled: bool = False
    max_concurrent: int = Field(default=6, ge=1, le=6)
    role: str = Field(default="sssf_recon", pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    config_file: str = ".codex/agents/sssf_recon.toml"


class AgentConfig(StrictConfigModel):
    name: str
    coding_agent: Literal["codex"] = "codex"
    model: str = "gpt-5.6-terra"
    thinking: ReasoningEffort = "medium"
    color: str = ""                 # hex swatch for this agent's lane in the UI
    purpose: str = ""
    prompt_engineering: PromptEngineering
    subagents: SubagentConfig = Field(default_factory=SubagentConfig)
    # What this agent may MODIFY in the repo, enforced in code after every call
    # (see adw_modules/permissions.py). Codex sandbox constrains execution; this
    # separate contract constrains which modifications a phase may leave behind.
    #   None  -> unrestricted, except the roster-wide `protected_files` paths
    #   []    -> read-only: may modify nothing tracked
    #   [...] -> only these. A trailing "/" means a directory prefix; a "*"
    #            makes it a glob; anything else is an exact path.
    writes: Optional[list[str]] = None


class ConfigDefaults(StrictConfigModel):
    coding_agent: Literal["codex"] = "codex"
    model: str = "gpt-5.6-terra"
    thinking: ReasoningEffort = "medium"
    color: str = ""
    subagents: SubagentConfig = Field(default_factory=SubagentConfig)
    writes: Optional[list[str]] = None
    # Off-limits to every agent that has not named them in its own `writes`.
    # The factory's own code is the default: an agent must not be able to edit
    # the machinery that decides whether its work passed.
    protected_files: list[str] = Field(default_factory=lambda: [
        "adws/adw_modules/", "adws/adw_sssf_config/", "adws/adw-*.py", "adws/spec_artifacts_cli.py",
    ])
    data_dir: str = "adws/adw_data"


class CodexRuntimeConfig(StrictConfigModel):
    auth: Literal["cli", "api_key"] = "cli"
    approval_policy: Literal["never"] = "never"
    turn_timeout_s: int = Field(default=900, ge=1)
    startup_timeout_s: int = Field(default=30, ge=1)
    shutdown_grace_s: int = Field(default=10, ge=1)
    command_network_access: bool = False


class ObservabilityConfig(StrictConfigModel):
    db: str = "adws/adw_data/sssf.db"
    poll_ms: int = 500


class SSSFConfig(StrictConfigModel):
    schema_version: Literal[2]
    defaults: ConfigDefaults = Field(default_factory=ConfigDefaults)
    codex: CodexRuntimeConfig = Field(default_factory=CodexRuntimeConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    agents: list[AgentConfig] = Field(default_factory=list)


# ── Tracing ──────────────────────────────────────────────────────────────────

class EventRecord(BaseModel):
    """One traced event, always logged against adw_id + phase."""

    adw_id: str
    phase_id: str = ""
    type: str                       # phase_start | agent_start | tool_call | handoff | gate_pass | gate_fail | log | agent_end | phase_end | error
    name: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_id: str = ""
    tokens: Optional[int] = None
    # Spans: set both when an event covers real elapsed time (a tool call), so
    # the UI lays it out on a time axis without parsing payload JSON. Left unset,
    # the tracer stamps started_at with the moment the event was recorded.
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


class UsageBreakdown(BaseModel):
    """SSSF's Codex usage contract.

    Cached input is already included in input_tokens; reasoning is already
    included in output_tokens. Neither is added to total_tokens a second time.
    """

    usage_schema_version: Literal[2] = 2
    input_tokens: int = 0
    cached_input_tokens: int = 0
    uncached_input_tokens: Optional[int] = None
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    cost: Optional[float] = None
    cost_kind: Literal["reported", "estimated", "unknown"] = "unknown"

    def merge(self, other: "UsageBreakdown") -> None:
        """Add another call's usage — a phase that retries spends more than once."""
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.total_tokens += other.total_tokens
        if self.uncached_input_tokens is not None and other.uncached_input_tokens is not None:
            self.uncached_input_tokens += other.uncached_input_tokens
        else:
            self.uncached_input_tokens = None
        if self.cost is not None and other.cost is not None:
            self.cost += other.cost
            if self.cost_kind != other.cost_kind:
                self.cost_kind = "estimated"
        else:
            self.cost = None
            self.cost_kind = "unknown"


class RuntimeErrorInfo(BaseModel):
    kind: Literal[
        "authentication", "model_unavailable", "unsupported_effort",
        "thread_unavailable", "approval_required", "timeout", "interrupted",
        "runtime", "outcome_unknown", "subagent_policy",
    ]
    message: str


class RuntimeCapabilities(BaseModel):
    sdk_version: str
    runtime_version: str
    model: str
    effort: ReasoningEffort


class AgentRunRequest(BaseModel):
    role: str
    cwd: str
    model: str
    effort: ReasoningEffort
    developer_instructions: str
    prompt: str
    output_schema: dict[str, Any]
    raw_output_path: str
    thread_id: Optional[str] = None
    subagents: SubagentConfig = Field(default_factory=SubagentConfig)
    # Thread totals persisted after the previous turn. Used only when a runtime
    # omits the per-turn `last` usage and exposes cumulative counters instead.
    usage_baseline: Optional[UsageBreakdown] = None


SubagentStatus = Literal[
    "running", "completed", "interrupted", "failed", "shutdown", "not_found",
    "unknown",
]
ChildUsageAttribution = Literal["not_applicable", "separate", "unknown"]


class SubagentRun(BaseModel):
    """One child thread observed during a parent Codex turn."""

    thread_id: str
    parent_thread_id: str = ""
    parent_turn_id: str = ""
    role: str = ""
    agent_path: str = ""
    status: SubagentStatus = "unknown"
    task: str = ""
    model: Optional[str] = None
    reasoning_effort: Optional[ReasoningEffort] = None
    result: str = ""
    error: str = ""
    usage: Optional[UsageBreakdown] = None


class AgentRunResult(BaseModel):
    thread_id: str = ""
    turn_id: str = ""
    text: str = ""
    status: Literal["completed", "failed", "interrupted", "outcome_unknown"] = "failed"
    error: Optional[RuntimeErrorInfo] = None
    usage: UsageBreakdown = Field(default_factory=UsageBreakdown)
    cumulative_usage: Optional[UsageBreakdown] = None
    # Cumulative billed tokens are not context occupancy.  Keep occupancy
    # unknown unless the runtime publishes an explicit measurement.
    context_tokens: Optional[int] = None
    context_window: Optional[int] = None
    runtime_version: str = ""
    raw_event_count: int = 0
    unknown_event_count: int = 0
    subagents: list[SubagentRun] = Field(default_factory=list)
    child_usage_attribution: ChildUsageAttribution = "not_applicable"
    subagent_cleanup_forced: bool = False


class BuildInput(BaseModel):
    prompt: str = ""
    spec: str | None = None
    ticket: str | None = None
    ticket_set: str | None = None
    dependency_evidence: str | None = None

    @model_validator(mode="after")
    def exclusive_mode(self):
        if sum(bool(v) for v in (self.prompt, self.spec, self.ticket)) != 1:
            raise ValueError("provide exactly one of prompt, --spec or --ticket")
        if self.ticket_set and not self.ticket:
            raise ValueError("--ticket-set requires --ticket")
        if self.dependency_evidence and not self.ticket:
            raise ValueError("--dependency-evidence requires --ticket")
        return self


BUILTIN_OUTPUT_TYPES = {
    "planner": PlanOutput, "decomposer": DecomposeOutput, "builder": BuildOutput,
    "scout": ScoutOutput, "reviewer": ReviewOutput, "documenter": DocumentDraftOutput,
}


class TicketSelection(BaseModel):
    ticket_id: str
    dependency_evidence: str | None = None


class WorkflowOptions(BaseModel):
    """Generated workflow launch bindings, independent of previous envelopes."""
    config: str = "adws/adw_sssf_config/sssf.config.yaml"
    adw_id: str | None = None
    planning_target: PlanningTarget | None = None
    spec: str | None = None
    selection: TicketSelection | None = None


class FinishOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt: ArtifactRef | None = None
    accepts_scope: bool = False
    commit: bool = False
    session_only: bool = False
