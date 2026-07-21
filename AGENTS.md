# Pullwise Worker Agent Notes

## Problem Solving Discipline

When resolving failures or regressions, do not default to adding diagnostic
patches or surface-level workarounds first. Identify the root cause from the
current code and available evidence, then fix that root cause. Add diagnostics
only when they directly support root-cause isolation or make a verified fix
safer to operate.

## Agent-First Clean-Break Refactor Policy

The user has explicitly chosen a clean-break Agent-First refactor. For any
Agent-First architecture, schema, protocol, runtime, control-plane, data-model,
or UI work, do not preserve legacy compatibility.
This section overrides later rules that describe a legacy surface as a future
target or require old/new coexistence. Those later sections are current-state
evidence only until the coordinated cutover removes them.

- Implement one clean current contract across Worker, Server, and Web. Do not
  add or retain adapters, shims, dual reads/writes, legacy fallbacks, protocol
  downgrade/negotiation, compatibility modes, shadow fallback paths, or feature
  flags whose purpose is old/new coexistence.
- Do not add migrations or backfills solely to preserve pre-refactor data or old
  wire/storage shapes. During internal development, prefer a clean schema/data
  reset and coordinated cutover unless the user explicitly authorizes retention
  in a later instruction.
- Treat `review-worker-protocol/v1`, legacy worker/result/outbox/DTO surfaces,
  compatibility baselines and fixtures, and legacy mapping prose as deletion
  inventory, not target contracts. Do not refresh them or let them constrain the
  new design. The cutover change must remove or rewrite old tests, CI gates,
  documentation, and code that enforce compatibility.
- Current legacy production code may remain only until its replacement is
  end-to-end verified and the applicable decision gates permit cutover. Do not
  route new features through it or promote shadow code as a second production
  path; delete the obsolete surface in the coordinated cutover.
- Preserve security, authority, durability, idempotency, audit, and user-visible
  correctness. No compatibility is not permission to weaken those invariants.
  If a still-active decision's frozen options assume compatibility, obtain an
  explicit option-anchored custom user resolution and update every controlled
  unit before implementation; never silently infer the choice.
- Legacy here means pre-Agent-First product protocol, storage, data, DTO, and
  runtime surfaces. It does not waive current security fail-closed behavior,
  deterministic model-output normalization, or safe deployment rollback.
- Previously resolved legacy-scoped decision records remain immutable history.
  Record an ordered explicit supersession before implementing a contradictory
  production scope; do not rewrite or silently ignore the old resolution.
- Any exception requires later explicit user authorization naming the exact
  surface, bounded duration, owner, and removal condition. Existing prose, CI,
  tests, or current code do not authorize an exception.

## Module And File Size Discipline

Keep handwritten production source, tests, and maintained scripts small enough
for an agent or reviewer to understand without loading an unrelated subsystem.

- A new handwritten file should contain at most 400 physical lines. A file in
  the 401-600 range requires a written cohesion reason in the change evidence;
  a new handwritten file must not exceed 600 physical lines. Count ordinary
  newline-delimited lines, including comments and blank lines. Do not game the
  limits by minifying code, combining unrelated statements, or deleting useful
  tests and documentation.
- Split by cohesive domain responsibility, state/data ownership, protocol or
  side-effect boundary, and independently testable behavior. Modules must expose
  narrow interfaces and have one-way dependencies where practical. Tests should
  split by contract, behavior, or failure mode rather than arbitrary line ranges.
- Do not create arbitrary numbered fragments, wildcard-import aggregators, thin
  pass-through modules, shared mutable-global seams, or circular imports merely
  to satisfy the line limit. In particular, existing `_main_part_XX` and
  `import *` compatibility structure is not a template for new modules.
- Existing files above 600 lines are grandfathered only for narrow maintenance.
  Freeze their line-count baseline before implementation. A small cohesive fix
  may remain in place, but the file must not gain a new responsibility; any
  growth above its baseline must be explicitly justified with an extraction
  plan. A change adding a new capability or more than 100 net physical lines
  must extract the work into focused modules, leaving only composition or a
  compatibility seam in the oversized file. A reduced baseline never rises.
- Exceptions are limited to generated files that have a small checked-in
  generator, vendored third-party code, frozen canonical fixtures/snapshots/
  benchmark data, and framework-mandated atomic migrations or registries whose
  business logic has already been extracted. “Temporary”, “tests are repetitive”,
  and “refactoring is difficult” are not exceptions.
- Completion evidence for every implementation slice must include line counts
  for all added or modified files, the frozen baseline and responsibility impact
  for each oversized legacy file touched, and any exception with its reason,
  considered split seam, owner, and removal condition. An undocumented exception
  fails completion.

## Strict V1 Current-State Removal Baseline

`contracts/agent-first/legacy-v1-contract-baseline.json` is frozen evidence of
the current Server/Web `review-worker-protocol/v1` surface and a deletion
inventory. D27 forbids treating it as a target contract or compatibility
commitment. The temporary `check` command may expose accidental current-state
drift until coordinated cutover; it must not constrain the clean current design
or authorize Agent Kernel production implementation.

- Run `python scripts/verify_agent_first_contract_baseline.py check
  --workspace-root ..` from the Worker repository before relying on the
  snapshot. Exit `0` is compatible, `1` is a deterministic incompatibility,
  and `2` is indeterminate.
- Repository directories and executable probe commands belong to the fixed
  verifier catalog. The manifest may reference their IDs but must not add a
  path, command, test node, cwd, timeout, or shell fragment.
- A fixed probe that reports zero executed tests is indeterminate
  (`insufficient_tests`), including Python `unittest` runtimes that return a
  nonzero no-tests exit code. A nonzero run with actual test evidence remains a
  deterministic failure.
- Contract text hashes use strict UTF-8 with CRLF/CR normalized to LF. Git HEAD
  and unlisted paths are informational. Blocking canonical-fixture drift fails.
  Broad watched-file drift is compatible with a warning only while every
  blocking fixture matches and all linked fixed probes pass; otherwise it is
  indeterminate.
- Do not hand-edit the generated Appendix A block between its markers. Render
  it from the manifest and keep it byte-for-byte synchronized.
- Worker-only CI must check out the Server repository as a sibling at the exact
  frozen baseline commit before collecting the cross-repository wire fixture
  tests. Install that sibling's declared Python dependencies; never skip the
  tests merely because a clean Worker checkout lacks `pullwise_server`.
- The baseline-refresh `candidate` path is retired. Do not recreate it, update
  frozen hashes or fixtures to accept drift, or establish a replacement legacy
  compatibility baseline. A mismatch is current-state evidence to investigate
  and ultimately remove at coordinated cutover, not a request to preserve the
  old surface.
- `contracts/agent-first/fixtures/review-worker-protocol-v1.json` is a frozen
  canonical-fixture exception at 449 physical lines, owned by the Worker
  compatibility owner. It stays atomic because one blocking digest and one
  Server bridge verify the coordinated register/lease/heartbeat/event/artifact/
  terminal-result pack. The considered split is control-plane versus terminal
  result cases; split only when a contract-package generator can produce and
  verify both without partial refresh. Remove the pack when strict v1 retires.
- `contracts/agent-first/legacy-v1-contract-baseline.json` is an atomic
  machine-registry exception at 952 physical lines, owned by the Worker
  compatibility owner and validated by the small manifest module. The
  considered split is repositories/registries/surfaces/tests; retain one file
  until a contract-package generator can assemble deterministic includes while
  preserving one schema validation and Appendix render. Replace this exception
  when that generator becomes the contract source.

### D27 Legacy Absence Ratchet

`contracts/agent-first/legacy-removal-inventory.json` is the machine deletion
inventory bound to resolved D27 option `clean_break_no_legacy` and digest
`f3ef27ad6318d4da20d4750cdde9387b66045f1708a909b57aba1c6e48ec2b0e`.
It freezes 10 high-signal legacy literals and their per-path occurrence
ceilings across 81 current Server/Web/Worker paths, binds all 28 semantic
surfaces from the frozen strict-v1 baseline, and permits exactly 13 audited
control/history evidence exclusions. Catalog digest
`a3f4a6b32eb66dd33e0855743bbbcdc024c2ce760f4aaf56683f495a7312be68` is
the immutable expansion ceiling: observed legacy may shrink, but do not add a
surface, raise an occurrence ceiling, or broaden an exclusion to make the
ratchet pass. This remains a deletion inventory, not a compatibility baseline.

- Run `python scripts/verify_agent_first_legacy_absence.py --workspace-root ..`
  for the current CI gate. Exit `0` reports inventoried legacy without blocking
  while no new occurrence exists, exit `1` reports an unregistered legacy
  occurrence or a registered signature above its frozen ceiling, and exit `2`
  is indeterminate input or environment evidence.
- `--require-absent` is the final-cutover mode: every high-signal inventory
  surface and every frozen baseline semantic surface must be absent or it exits
  `1`. Keep it tested but do not add it to required CI until the coordinated
  switch is authorized.
- Current CI checks out both frozen Server and Web siblings and runs only the
  default ratchet. The retired candidate-refresh path remains forbidden.
- The CLI requires the fixed production inventory ID and catalog digest in every
  workspace. Inventory paths are strict contained POSIX-relative paths. Reads
  reject symlinks, reparse points, non-regular files, unsafe Git paths, changed
  descriptors, and descriptor targets that differ from the validated lexical
  path. Keep first-read inventory and D27 register bytes in the observation
  snapshot and exact-byte recheck them before exit. Whole-file exclusions remain
  limited to the decision register, generated view, and inventory; bounded D27
  marker exclusions remain exact, unique, ordered, and non-overlapping.

## Agent-First Slice 0 Evidence

`contracts/agent-first/worker-slice-0-baseline.json` is the machine source for
the current Worker module, 30-entry phase/progress registry, and file-size
evidence. This baseline describes the current implementation only; it neither
assigns future Agent Kernel ownership nor authorizes production implementation.

- Run `python scripts/agent_first_slice0_baseline.py check --repo-root .` from
  the Worker repository. Exit `0` is compatible, `1` is deterministic drift,
  and `2` is indeterminate.
- Keep the generated `docs/agent-first-worker-current-code-map.md` block
  synchronized with the machine source after canonical LF normalization.
- Exact HEAD identity and the dirty bit are informational. Reachability of the
  fixed ratchet genesis `904165f3bed784faaa209ca80e33214c7b07f909`, the exact
  phase registry, anchors, and every Git-tracked regular file over 400 physical
  lines that matches the fixed handwritten code/test/maintenance suffix, name,
  or extensionless-executable catalog are blocking evidence. CI must retain
  full Git history.
- Any current line-count drift or unregistered file over 400 lines fails. A
  lower count must refresh the baseline downward. The genesis path set is
  closed: no new trigger path may be grandfathered, and removing a path after
  it reaches 400 or fewer lines retires it from later re-entry. The gate reads
  every committed blob revision for each genesis path, so a source-only
  historical reduction also lowers the floor or retires the path.

## Agent-First Specification Decision Gate

contracts/agent-first/spec-decision-register.json is the only machine source
for Agent-First specification decision state. The generated human view is
docs/agent-first-worker-spec-decision-register.md. Recommendations in either
artifact are non-normative: current code, existing prose, silence, and Agent
inference never select an option or authorize production implementation.

- Run the canonical structural/provenance check from the Worker repository:

    python scripts/agent_first_decision_register.py check --repo-root .

  Exit 0 means valid_pending or ready, exit 1 means blocked or invalid, and
  exit 2 means observation was indeterminate.
- Before starting an implementation slice, add --require-slice S2 through S8
  as appropriate. Every applicable pending decision required by that slice
  blocks it, even when another dependency is not yet ready to ask.
- After recording an explicit owner resolution, regenerate the human view:

    python scripts/agent_first_decision_register.py sync-document --repo-root .

- Ask only active_decision_id. A resolution requires an option-anchored decision
  text, user/architecture_owner/operator authority, date, evidence references,
  and its canonical digest. A custom resolution activates conditional follow-up
  questions fail-closed instead of inferring a branch.
- The reviewed D1-D26 question, option, recommendation, dependency, slice, and
  normative-unit definitions are digest-frozen. Resolved records are immutable
  through full Git history. Reversal requires a new, ordered, resolved decision
  that explicitly supersedes the unchanged prior record. Supersession never
  reactivates an old question ID; newly applicable follow-up questions require
  new ordered decision records.
- Controlled normative units use exact decision-id and resolution-digest
  markers. A resolved applicable unit must be referenced on every check;
  unknown, pending, malformed, stale, or unscoped references fail.
- In the generated decision view, the option selected by a resolution must be
  labelled as selected. The non-normative recommendation/not-selected label
  applies only to a recommended option that the resolution did not select.
- The current resolved set is D1 and D3-D27; D2 is the only stored pending
  record but is inactive under D1. There is no active question: the register is
  `ready` with 26 resolved records and zero applicable pending records, and
  S2-S8 have no pending-decision blocker.
  D20 remains bound to
  custom `new_gate_immediate_authority` digest
  `3701e29aac3b42c5f88743cc21ea49cafe685d0d2c4b8ab0ec8ff5619dad023a`; D21 is
  bound to custom `server_claim_bound_mode` digest
  `ddfd221626d5677def6472f59e6fa002c56fd1f6ca6602188ebb7c23735a0282`; D23 is
  bound to recommended `server_owned_package` digest `cecd60a0f27d18240d3222eb6aa117dc588b06ba3f9581c83af3d292dd4254e2`.
  D23 means one Server-published current package with exact Worker/Web pins, not legacy coexistence or negotiation.
  D24 is bound to custom `new_tasks_only` digest
  `8e9b8ee728dabd8e8f07e3b6ce8057a6e3e11707d07bbaf4e5d1e67f7dfc3806`:
  its audited Server-side acceptance/TaskRecord-creation barrier admits only
  post-cutover tasks, isolates every pre-cutover task as non-executable, forbids
  legacy migration or coexistence, and permits rollback only to an exact-pinned
  build implementing the same current package, schema, storage semantics, and contract.
  D25 is bound to recommended `immutable_receipt_mutable_binding` digest
  `03564c29030767d552a5759828970f30ed10c11bbd46c42c51f16a08c3e2f2d0`:
  upload/transport receipts stay immutable, while a separate Server-owned binding
  index performs the one-time CAS to the exact transport-envelope digest.
  D26 is bound to recommended `roadmap_separate_designs` digest
  `ce8a907836b3b8209f12f7c48f66878e9534d7cac667532c2899f3d74c86602f`:
  unclosed long-range versions remain roadmap until each receives a separate
  complete implementation design before work starts.
  D22 is bound to custom `absolute_plus_baseline` digest
  `94ec57c0b72801dc37d8a7de08b16cc78b8ffc8bdb69b39f0eb0b56cf80d6e96`:
  pre-result signed benchmark/policy inputs, a reproducible three-state CI
  report, and a post-result release attestation use separated owners and bind
  the exact current package, candidate, dataset, oracle, runtime, thresholds,
  baseline, and canary plan. Every applicable absolute and stable-baseline
  relative gate must pass; missing, stale, invalid, incomparable, or
  undersampled evidence is indeterminate and blocks release. Bootstrap may
  waive only the nonexistent relative baseline, never an absolute gate, and
  becomes the first baseline only after the signed offline gate and capacity-only
  canary pass. The D24 barrier precedes production canary intake; rollback stays
  on the same current contract or stops/fences intake, never restores an old
  protocol, QA authority, task population, or compatibility baseline.
  The recommendation directive remains bounded by D27.

The generated Markdown view is a generated-file size exception owned by the
Worker specification owner. It stays atomic because it is one ordered decision
packet produced by the small checked-in renderer. The considered split is
summary versus per-decision detail; remove the exception when a deterministic
include publisher can preserve one order, one digest gate, and byte-exact
verification.

`contracts/agent-first/spec-decision-register.json` is an atomic
machine-registry exception at 454 physical lines, owned by the Worker
specification owner. It stays atomic because one ordered frozen
question/definition/resolution packet enforces question order behind one
structural-validation and immutable-history boundary. The considered split is
definitions versus resolutions; remove the exception when a deterministic
include/assembler preserves the frozen definition digest, ordering, schema
validation, and immutable-history checks.

## Agent Kernel Slice 1 Storage Contracts

`contracts/agent-task/v1/schema-registry.json`, the digest-bound schemas it
lists, and their golden valid/invalid fixtures are one contract surface. Update
them atomically. Registry loading must reject digest drift, symlinks,
non-regular files, unsupported schema keywords, and unresolved references.

- Validate a document against its registered schema and semantic invariants
  before canonicalization or CAS persistence. Pullwise JCS Profile 1 rejects
  floats, non-safe integers, invalid UTF-8, non-NFC strings, non-ASCII object
  keys, and duplicate JSON keys; never silently normalize an invalid value.
- CAS publication order is staged bytes, file `fsync`, independent digest and
  size verification, no-clobber atomic publish, parent-directory `fsync`, then
  the SQLite object/binding transaction. Stored objects are private `0600`,
  regular, single-link files. Corruption or artifact rebinding fails closed;
  never repair either silently. A verified read must open with `O_NOFOLLOW` and
  return bytes from the same descriptor whose `fstat`, size, and digest passed.
  During a no-clobber race, an observed two-link staging transition may already
  have converged to one link; retry full verification under a bounded monotonic
  deadline, but never accept more than two links or commit before convergence.
- Slice 1 persistence is shadow-only. It must not publish a terminal result,
  replace the legacy v1 authority, or expose an Agent Kernel task runner before
  the applicable decision and later-slice gates pass.
- Run CAS orphan collection only while the Worker is idle and only with a
  positive integer age threshold. Verify every database-indexed object before
  deleting staging files or objects: a database row without durable bytes
  remains an error; a durable unbound object may be collected after the threshold.
- The design names `transport-receipt/v1` and
  `transport-abandonment-record/v1` without defining their field contracts.
  Record this as `SPEC_GAP`; do not invent or register either schema until its
  normative shape, identity, and idempotency rules are explicitly resolved.
- Verify Agent Kernel package data by building a wheel without dependencies,
  installing it into an isolated virtual environment, and loading the default
  registry from outside the source tree. The smoke check must also validate the
  installed schema/fixture inventory and a SQLite/CAS round trip; inspecting a
  wheel archive alone is not sufficient evidence that runtime discovery works.
  Keep `MANIFEST.in`, `setup.py` data files, and `pyproject.toml` synchronized:
  Ubuntu 22.04 setuptools 59.6 does not consume the current pyproject data-file
  declaration on the supported fallback build path.

## Agent Kernel Slice 2 Control State

The typed Task/Attempt reducers are the sole lifecycle edge registry. Keep the
Cartesian state/event and state/state tests synchronized with that registry;
every unlisted edge fails with `STATE_TRANSITION_INVALID`, and terminal Tasks
fail with `TASK_ALREADY_TERMINAL`.

- Task control events use one `BEGIN IMMEDIATE` transaction for version CAS,
  Attempt action, terminal publication row, and append-only event. Exact
  idempotency retries return the original event version; a reused key with any
  other task/type/payload fails `IDEMPOTENCY_CONFLICT`; SQLite also enforces the
  event idempotency key globally. Task acceptance idempotency binds `scan_id`,
  and owner-incarnation idempotency binds the event timestamp. A newly accepted
  FINALIZING terminalization fact always advances exactly once; only the
  selected reason/outcome fields depend on `terminal_outcome_changed`.
- D5 resolves `task_version` to one increment per newly applied Task control
  event transaction. Every such transaction must advance exactly once even
  when it changes several fields or records a new FINALIZING terminalization
  fact without changing the selected outcome; exact idempotency retries reuse
  the original version, while rejected or rolled-back transactions do not
  advance. Attempt-only transitions, ordinary Observation appends, and logs do
  not change Task version unless a Task control transaction freezes their
  pointer. Keep the `mvp-state-semantics` unit bound to D5 digest
  `859647945022b9d62bca4c6cf16b290c48e4e9bdb2f10700a40553194748b74a`.
- D6 resolves `attempt.claimed` to one `BEGIN IMMEDIATE` Task control event
  transaction. After validating claim guards, it atomically advances native
  and owner epochs, creates one `LEASED` Attempt bound to one exact `STARTING`
  Owner incarnation/session, updates current pointers, appends one event under
  one idempotency key, and advances `task_version` exactly once. No commit may
  expose only one side; an exact retry returns the original complete result,
  while any conflict or rollback creates neither. Keep the
  `mvp-state-semantics` unit bound to D6 digest
  `e1ad16c135ae5f0880123becdd640bf685c0f201b44dd941830590b0b39174d8`.
  The current two-transaction Slice 2 shadow scaffold is not this production
  contract; do not promote or refactor it until the S4 decision gate passes.
- D7 resolves recovery timing to persisted elapsed consumption only. Raw
  monotonic values are never recovery authority across process or boot
  boundaries. On restart, rebase a new process-local monotonic origin from the
  immutable `absolute_deadline_at` and durably ratchet persisted consumption
  before restoring execution, so wall-clock jumps or restarts never increase
  remaining budget or extend the deadline. Keep the `mvp-state-semantics` unit
  bound to D7 digest
  `5d7916e9389c0203185fb7e2e64be49df0ea52557d875f661f5d0180e093f5ea`.
  The current bare `budget_entries.monotonic_ms` schema and SQLite rows are
  shadow scaffolding; migrations 1-3 and runtime behavior remain unchanged
  until the S4 decision gate passes.
- D8 resolves outer-lease loss as a Task/Attempt layering boundary. Lease loss
  alone must leave an executing or finalizing Task in `ACTIVE` or `FINALIZING`
  with every terminal/result field unchanged. The authoritative loss transition
  is a newly applied D5 Task control transaction: it advances `task_version`
  exactly once while atomically fencing the exact predecessor transport/native
  Attempt and its actor owner/session, rejecting all later predecessor writes or
  publication, and recording transport abandonment as separate non-result
  evidence rather than a Task terminal kind. An exact idempotent replay reuses
  the original event version and does not advance `task_version` again. A
  successor may take over only after an explicit recovery eligibility
  predicate passes and with a fresh fence/epoch; do not infer that predicate.
  D9 now makes the internal TaskResult CAS the sole semantic terminal
  linearization point, and D10 requires one exhaustive global safety-first
  matrix to select the outcome. Until that complete matrix is frozen, no
  candidate precedence is implementation authority. Keep the
  `mvp-state-semantics` and `post-closure` units bound to
  D8 digest
  `e895f73c3a0962937cbab61b4c8037f9ccba9daa6e6de89d5004005dd830b98a`.
  The current Slice 2 `outer_lease.fenced` Task-terminalization behavior is a
  pre-D8 shadow scaffold, not a production contract; do not promote it or
  change runtime, schemas, or migrations 1-3 until the S4 decision gate passes.
- An actor fence binds task/deletion version, lease/transport epoch, current
  Attempt/native epoch, stable owner ID, owner epoch, and the exact live owner
  session. A mismatch fails closed with the stable fence code; do not infer
  freshness from only one epoch or from an Agent-provided identity.
- The legacy active-run marker, terminal outbox, and success receipt remain
  current runtime state only until coordinated replacement; they are not the
  target Agent-First semantic authority after D9. The Agent Kernel bridge is a
  read-only one-slot shadow projection enabled by
  `PULLWISE_AGENT_KERNEL_SHADOW_ENABLED`; disabling it constructs the unchanged
  legacy worker. It must never create a local queue/prefetch slot, publish a
  second result, or treat a stale outbox as pending after an exact bound success
  receipt has won. An ACTIVE observation freezes the complete
  job/run/lease/attempt identity, and persisted-run recovery must immediately
  refresh the shadow projection from the recovered marker/outbox. In the
  current-only replacement, a TaskResult CAS commits the immutable semantic
  outcome; Server ACK is only a recoverable transport projection and cannot
  create, replace, or rewrite it.
- D11-D17 resolve the S3/S4 design choices to a Worker-owned partial-delivery
  manifest, immutable generation supersession, prepublication cancel plus
  postpublication addendum, a separate bundle-integrity manifest, separate
  Gate-predicate and stable-error registries, removal of the Q0 success path,
  and a versioned concern/coverage table. Under D9, D12 repair may create only
  a new transport projection/envelope generation; it never rewrites a CASed
  TaskResultCore or outcome. D13 addenda likewise never mutate the result.
- D18 makes the existing root coordinator the sole current logical Task Owner
  at coordinated cutover, with coordinator identity separated from owner
  incarnation; D27 forbids a parallel second controller or old-protocol path.
  D19 keeps that Owner incarnation live through fanout and reserves its fixed
  agent/session slot alongside reviewers and verifiers.
- D20 makes the new Gate the sole production authority after coordinated cutover;
  D21 makes Agent Kernel authority intrinsic to that sole current contract, not a
  selectable mode. Server claim/grant immutably binds contract identity/version,
  job/run scope, and authorization; Worker only validates/executes and fails closed.
  Config/deployment/job cannot change tracks; authorization loss stops, fences, or rejects.
  Staged same-contract rollout and earlier same-contract build rollback remain allowed, but fallback, downgrade, or different-authority tracks do not.
- D23 makes the Server repository the sole cross-end current-package source; Worker/Web exact-pin it and may not redefine its schemas.
- D24 makes the audited Server-side Task acceptance/TaskRecord creation barrier
  the cutover linearization point. Pause intake before the barrier; every
  pre-cutover Task must first reach an authoritative terminal state or
  tombstone/delete disposition, or lose authorization and be stopped, fenced,
  or rejected into non-executable isolation. Stop/fence/reject does not invent
  Task terminalization or TaskResult, and no pre-cutover Task or late legacy
  lease/event/result/replay may execute or re-enter after the barrier.
- Do not migrate, backfill, dual-read/write, negotiate, or add compatibility for
  pre-Agent-First Tasks or data. Safe rollback is limited to an exact-pinned
  prior build with the same current package identity/version/digest, TaskRecord
  schema, storage semantics, and Agent-First contract; it must not reopen old
  tasks, data shapes, protocols, or entry points.
- D25 keeps each upload/transport receipt byte-immutable and content-addressed.
  A separate Server-owned mutable binding/index may CAS exactly once from
  unbound to one exact `transport_envelope_digest`; it cannot rebind, clear, or
  mutate the receipt. `task_result_core_digest` and `transport_envelope_digest`
  are distinct DAG identities. The binding/ACK is transport metadata and never
  replaces D9's internal TaskResult CAS or rewrites outcome; D23's Server-owned
  package defines both digest algorithms, binding schema, and crash fixtures.
- D26 classifies unclosed long-range versions as roadmap, not executable
  specification or current DoD. Each version requires a separate complete
  implementation design and independent decision before work starts. Roadmap
  prose cannot authorize legacy coexistence, runtime multi-major negotiation,
  downgrade, old-Web compatibility, or a second production track; future
  current-contract evolution still requires a coordinated cutover.
- SQLite migration 2 upgrades a Slice 1 database in place and transactionally
  adds event digest, terminalization reason, and complete Attempt control
  fields. Preserve migration 1 bytes/digest; crash before migration commit must
  leave a valid v1 database that cleanly upgrades once on restart. Migration 3
  adds the global task-event idempotency index without changing migrations 1/2;
  duplicate v2 data or a commit crash must fail closed and leave v2 intact.

## Agent-First MVP Capability Boundary

D3 resolves the MVP capability ceiling to R1. Every MVP profile may execute
only R0/R1; reject R2/R3/R4 before dispatch without creating an approval wait,
capability grant, tool invocation, or Effect Ledger row. A policy-denied audit
Observation may record the rejection, but no external effect may begin. Keep
Worker control transport separate from Agent capability risk; it is not an R2
grant. The controlled MVP contract/state units must reference D3 resolution
digest `0126d5ee3329c0f954e88e08979e8f0883086b3846315e2904cd7d323b97b07a`.

### S3a Internal Read Tracer Boundary

The current S3a work is package-independent safety infrastructure only, not a
production Agent-First runner or a completed S3 slice.

- agent_kernel_source_state.py contains only internal typed identity facts and
  pure diffs. agent_kernel_source_scan.py owns Ubuntu descriptor-rooted
  traversal; the focused Windows fallback exists for development tests. Worker
  must not add versioned schema tags, construct current manifests, or treat
  these internal shapes as a substitute for D23's exact-pinned Server package.
- Pullwise source selection has fixed .git and .codex-review exclusions.
  Caller-selected exclusions, ephemeral patterns, and raw gitlink mappings fail
  closed. Verified gitlinks come only from an exact 40-hex Git revision with
  replace objects and lazy fetch disabled; the catalog is bound to the checkout
  device/inode and the scanner-opened root. Production composition must require
  Git 2.45 or newer and a materializer-enforced single-writer catalog/scan
  window. The Windows path scanner remains development-only.
- agent_kernel_gateway.py is only the fixed-order orchestration kernel. Journal
  begin must atomically revalidate the authority ticket and bind one opaque
  dispatch capability consumed by the dispatcher and every settlement path.
  Cancellation cleans prepared resources without creating a false settlement.
  Its
  codec, authority, policy, budget, dispatch journal, and execution committer
  are injected current-only boundaries. Do not satisfy them with Slice 2 shadow
  state, legacy Task rows, bare budget_entries.monotonic_ms, or the legacy-FK
  observations table.
- agent_kernel_r0_read.py prepares one internal R0 source read by binding a
  full SourceState snapshot to a held regular-file descriptor. The dispatcher
  receives no unresolved path, shell, network client, approval channel, or
  secret handle. Excluded roots fail before leaf open, reads are capped at the
  policy/expected extent plus one byte, and every pre-dispatch loss path closes
  the descriptor. A fresh post-dispatch snapshot with any diff withholds the
  normal result.
- This tracer deliberately does not construct an Observation, dispatch-intent
  contract, ContentRef, or durable idempotency result. Those require the
  exact-pinned current package plus a current-only journal/CAS transaction
  boundary. The existing shadow database cannot fill that role.

## Agent-First Legacy Policy History

D4's field-by-field legacy policy mapping is immutable history superseded by
D27. Do not implement its S3 legacy-mapping manifest, Adapter constants,
fallback inputs, or contract-pack compatibility work, and do not bind current
implementation units to the D4 digest. Carry forward only the security
invariant: authority-bearing current-contract inputs fail closed when absent or
invalid, and Worker time, environment, repository text, or Agent output cannot
invent authority. The current clean-break scope is bound to D27 digest
`f3ef27ad6318d4da20d4750cdde9387b66045f1708a909b57aba1c6e48ec2b0e`.

## Worker Host Platform

Pullwise worker installs target Ubuntu 22.04 hosts. Worker runtime, doctor,
update, restart, uninstall, and cleanup changes may assume Linux/systemd
behavior available on Ubuntu 22.04, including `useradd`, `chown`, `chmod`,
`sudo`/`runuser`, logrotate, and systemd unit management. Do not add macOS or
Windows worker installer behavior.

## Worker Provider Isolation

Each worker instance owns its own Codex runtime state. Do not let a
worker use global Codex binaries, global auth, root auth, or another
worker instance's config.

- Provider commands must resolve to absolute paths inside the current worker
  `worker_root`, for example:
  - `$worker_root/.local/bin/codex`
  - `$worker_root/.codex/bin/codex`
- Review execution and Codex quota refresh must enforce this at runtime before
  starting the worker-scoped Codex SDK client; do not fall back to `codex` from `PATH` or any global
  Codex binary.
- Provider subprocesses must run with instance-scoped environment values:
  - `HOME=$worker_root`
  - `USERPROFILE=$worker_root`
  - `CODEX_HOME=$worker_root/codex-home`
  - `XDG_CONFIG_HOME=$worker_root/.config`
  - `XDG_CACHE_HOME=$worker_root/.cache`
  - `XDG_DATA_HOME=$worker_root/.local/share`
  - `PATH` with this worker's `$worker_root/.venv/bin`, `$worker_root/.local/bin`,
    `$worker_root/.codex/bin`, `$worker_root/codex-home/bin` before the base service path
- Do not inherit global provider credentials such as root `HOME`,
  root `CODEX_HOME`, or global API-key based readiness when checking or running
  provider work.
- `doctor`, provider readiness checks, provider review execution, and semantic
  fallback execution must all use the same instance-scoped provider environment.
- A fresh install followed by no manual action and then `doctor` must report the
  same provider readiness state as the installer reported. In particular,
  `doctor` must not become ready by seeing auth/config from root, a global CLI
  install, or another worker.

Multiple workers on the same server are supported only if each worker uses its
own `worker_root` for Codex binaries, config, cache, and auth state.

## Codex Review Worker Architecture

`../codex_full_repo_review_worker_spec_v1_2_FULL_SELF_CONTAINED.md` is the
source of truth for worker review behavior. The current worker is the
`review-worker-protocol/v1` Codex full-repository review worker; do not
reintroduce alternate review pipelines, per-task CLI review flows, local job
queues, or worker-side prefetch compatibility.

Hard invariants:

- One worker instance may process at most one active job at a time.
- The worker must not maintain `pending_jobs`, `prefetched_jobs`, `next_job`, or
  any local job queue.
- The worker must call `POST /v1/workers/register` during startup with v1
  capability, isolation, platform, and one-slot/no-prefetch metadata before it
  enters the heartbeat/lease loop.
- The worker may call `POST /v1/workers/{worker_id}/lease` only when
  `active_job == null`, state is `idle`, and the local queue depth is zero.
  The request must include `review-worker-protocol/v1`, `active_jobs = 0`,
  `available_job_slots = 1`, `maintains_local_queue = false`,
  `local_queue_depth = 0`, and required capabilities for full repo scan, Codex
  App Server, isolated Codex home, progress events, cancellation, and intent
  test validation.
- A busy, cancelling, or finishing worker must heartbeat with zero available job
  slots through `POST /v1/workers/{worker_id}/heartbeat` and must not claim
  another job. The heartbeat payload must use the fixed v1 shape:
  `protocol_version`, `status`, `active_run_id`, `concurrency`,
  `codex_app_server`, and active-run `progress`; do not make legacy
  `running_jobs`/`active_job_ids` the worker-facing protocol. Idle heartbeats
  must report `active_jobs = 0` and `available_job_slots = 1`; active heartbeats
  must report `active_jobs = 1`, `available_job_slots = 0`, and a progress
  snapshot whose `run_id` matches `active_run_id`.
- The worker HTTP client must require the fixed v1 heartbeat shape directly.
  Do not translate legacy `running_jobs`, `active_job_ids`, or partial heartbeat
  inputs into v1 payloads.
- Each worker instance owns an isolated `WORKER_ROOT`, lock file, `CODEX_HOME`,
  `CODEX_SQLITE_HOME`, Codex auth/config/log/session/cache directories,
  workspace root, artifact root, and worker log.
- Multiple workers on one host must not share Codex config, auth, sqlite state,
  SDK client process, Codex sockets, workspaces, artifacts, service user runtime, or
  mutable lifecycle files.
- Worker runtime targets Linux/Ubuntu 22.04 only. Do not add Windows or macOS
  worker runtime behavior.

Codex execution rules:

- Use the OpenAI Codex Python SDK (`openai-codex`) for worker automation; do not add new hand-written app-server JSON-RPC clients. Managed workers must refresh OpenAI's official standalone CLI under the current `worker_root` (default release `latest`) and pass its absolute `PULLWISE_CODEX_COMMAND` as `CodexConfig.codex_bin`, while keeping `cwd` and `env` instance-scoped. The SDK-bundled CLI is only a compatibility fallback when no managed command is configured.
- For the `openai-codex` SDK approval mode, use `ApprovalMode.deny_all` when Pullwise wants no escalations; current SDKs expose `deny_all`/`auto_review`, not `ApprovalMode.never`.
- Worker Python package dependencies such as `pullwise-worker`, `openai-codex`, `openai-codex-cli-bin`, and transitive runtime packages must run from the worker instance venv under `$worker_root/.venv`; do not rely on global/system Python packages or console scripts for worker execution.
- Keep `openai-codex` pinned to the SDK version validated by the runtime
  contract tests. SDK upgrades are explicit compatibility changes and must
  include the optional real Codex integration check before release.
- Installer/update code must accept only an HTTPS Codex installer URL without credentials or fragments, a `latest` or validated semantic release, and an absolute `.../codex` command contained by `worker_root`. Download through a secure temporary file, install as the worker service user, probe `codex --version`, then migrate env state; failed updates must leave the prior command usable.
- Persist `codex-runtime.json` and include it in debug bundles so the worker version, Python SDK version, SDK-bundled CLI version, configured CLI path/version, and runtime mode are available when model compatibility fails.
- The Python SDK owns Codex runtime/app-server lifecycle for worker automation. Do not reintroduce worker-managed app-server process lifetime knobs such as `PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS` or `PULLWISE_CODEX_APP_SERVER_MAX_TURNS`.
- Use one instance-scoped Codex App Server per worker through the SDK runtime; prefer stdio transport or
  a worker-unique Unix socket.
- Do not use `codex exec` for review phases and do not launch one Codex runtime
  process per reviewer/subtask.
- Each run has one root coordinator Codex thread. Root semantic phases remain
  sequential. `reviewer_fanout` is the only bounded-turn exception: use the
  server-owned `reviewerConcurrency` policy (`1..2`) to run independent
  reviewer threads inside the same worker-owned SDK/App Server, never a second
  Codex process. Do not run concurrent turns on the root thread.
- Keep one worker-owned `CODEX_HOME` and auth store per worker identity. Never
  copy or share `auth.json` across worker roots. In-process reviewer turns must
  reuse the same App Server/AuthManager, and event, quota, progress, and
  execution-artifact mutations must remain serialized by the worker.
- Start the Codex SDK client with the worker-owned `CODEX_HOME` and
  `CODEX_SQLITE_HOME`, then initialize the JSON-RPC connection before turns.
- After root thread initialization, store the `thread_id` on the active job and
  include it as `codex_app_server.active_thread_id` in busy heartbeats until the
  job reaches a terminal state. Idle heartbeats must keep `active_thread_id`
  null.
- Capture Codex events to `codex-events.jsonl` and treat completion/error events
  as authoritative for terminal handling.
- Codex App Server `error` notifications are turn-scoped. Keep waiting when
  `willRetry = true`; when `willRetry = false`, terminate that turn immediately
  and preserve current camelCase `codexErrorInfo` values such as
  `usageLimitExceeded` when mapping the worker's stable public error code.
- A timed-out or cancelled SDK call may still own a background reader or late
  thread/turn. Mark that App Server runtime unhealthy and close it during job
  cleanup; never reuse it for another turn or run. Serialize SDK lifecycle
  transitions and atomically detach the runtime before bounded close so late
  archive cleanup and job cleanup cannot close the same App Server twice.
- Implement a fixed approval handler even when approval policy is `never`:
  deny every file-change approval plus Python/helper and project-test command
  execution. A Codex thread starts read-only. Every writable semantic/reviewer
  turn must use a fresh worker-owned `model-turns/<run>/<turn>` cwd outside the
  source repository, with no additional writable roots; source, prior evidence,
  prompts, schemas, tools, configuration, run-state, and logs remain read-only.
  After the turn, publish only that phase's explicit allowlisted outputs.
- Keep generated global Codex instructions and per-phase prompts consistent
  with that external writable directory. Refresh the Worker-owned global
  instructions during isolation preparation so upgrades cannot retain stale
  `.codex-review` write guidance.
- Treat every model-turn workspace as hostile. Snapshot/publish only bounded
  regular declared files with both per-file and aggregate byte limits, reject
  symlinks and non-regular files, and ignore undeclared files. Directory
  outputs are exact mirrors, safe executable intent harnesses retain only
  owner execute permission, and declared outputs publish as one journaled
  transaction: prebuild every replacement, back up every prior destination,
  roll the whole set back on failure, and recover an interrupted transaction
  during persisted-run startup and before the next publish. Durably sync
  prepared files, the journal, and affected parent directories on POSIX before
  advancing transaction state. Every stage is removed after its turn; cleanup
  failure must not replace an active turn/cancellation error, but must be
  recorded and must remain fatal after an otherwise successful turn. Reviewer
  assignment output uses the same bounded snapshot boundary plus one
  thread-safe run-level aggregate byte budget before worker-owned publication.
  Worker/SDK JSONL appends and JSON reads must also
  reject symlinks and non-regular files and use non-blocking opens so
  model-authored FIFOs cannot hang the worker or become confused-deputy access
  to provider credentials or other worker state.
- Once a terminal result is accepted and the active marker/slot is cleared, a
  retryable final idle-heartbeat failure must be logged and deferred to the
  continuous control-plane loop. It must not escape `run_job()` and terminate
  the daemon; permanent HTTP errors and programming errors still fail closed.

## Whole-Scan ETA Contract

- A running scan ETA always estimates the remaining time for the whole scan,
  not the current phase. Queued jobs have no execution ETA, and terminal jobs
  expose actual elapsed duration instead of retaining a forecast.
- The worker is the sole ETA authority. Base forecasts only on monotonic timing
  observations from the current run and its remaining hard deadline; do not use
  cross-run history, repository-size heuristics, cache state, or server/web
  guesses. Report `estimating` until current-run evidence is sufficient, then
  either an `available` lower/upper interval or an explicit `unavailable` state.
- Model remaining work as a dependency graph scheduled over explicit resources.
  Pipeline work is serial; reviewer assignments use the current configured and
  effective reviewer concurrency for any supported positive value, including
  values other than 1 or 2. Account for active residual work and parallel
  long-tail effects rather than summing every reviewer duration.
- A retry or semantic/JSON repair is new work in the current-run graph and must
  rewire downstream dependencies. Runtime concurrency reductions must affect
  the next snapshot immediately without rewriting completed observations.
- Include sanitized ETA snapshots in running progress events and heartbeats.
  Clear ETA at every terminal boundary, and never expose Codex thread ids in
  public progress or ETA payloads.

## Adaptive Repository Scan Rules

`../auto-adjust-plan.md` defines the current adaptive scan upgrade. Keep these rules in force for worker changes in this area:

- Keep the full-repository pipeline fixed. Do not change `PIPELINE_PHASES`, create repo-type-specific pipelines, let adapters skip core phases, or let adapters decide terminal status, QA gates, upload/result envelopes, or final finding confidence.
- `repo-profile.json` is an optional, mechanical, best-effort side artifact produced from inventory/file-tree evidence only. It must not call Codex, depend on semantic artifacts, fail `inventory_repository`, enter `PHASE_JSON_OUTPUTS`, or enter `REQUIRED_COMPLETED_ARTIFACT_FILES`.
- Repo profile generation and helper scripts must use the Python standard library only and remain Python 3.10 compatible. Do not add `tomli`, `PyYAML`, dependency-audit parsing, package installs, or external scan services.
- Repo profile test-framework detection must use mechanical evidence. Infer
  `pytest` only from explicit pytest config/dependencies/files such as
  `pytest.ini`, `conftest.py`, `[tool.pytest]`/requirements text, or
  package scripts; do not classify a Python project as pytest merely because
  tests live under `tests/` or files start with `test_`.
- If profile generation fails, keep `inventory.json`, log `repo_profile_skipped` to `worker.log.jsonl`, and continue the phase.
- Treat adapters as strategy providers only. They may provide signals, fallback risk rules, skip patterns, grouping hints, prompt emphasis, and intent-test preferences; they must not become a scheduler or artifact-contract owner.
- Risk tier priority is `hard skip/generated/binary > Codex explicit semantic route > Codex explicit default_depth`. Broad semantic routes must not promote generated, vendor, cache, minified, binary, or lock-file source into review bundles. Every other inventory path must match an agent route or the agent's explicit default; Worker code must not infer tiers from path keywords, repository profiles, suffixes, or risk hints, and incomplete routing must enter semantic repair then fail if still incomplete.
- Do not overwrite `risk-routing.json` with deterministic fallback routes. Put merged/explanatory downstream routing in optional run-local artifacts such as `effective-risk-routing.json` when implemented.
- Keep `bundle-plan.json` at `bundle-plan/v1` and `coverage.json` at `coverage/v1`. Conservative grouping may use path/name/entrypoint/test affinity only; do not build dependency graphs or call graphs for grouping.
- Keep reviewer ids limited to `security`, `correctness`, `test_gap`, and `correctness_lite` unless a future migration updates fanout, validation, clustering, reporting, QA, and backward compatibility together.
- Adaptive prompt context may be appended only when `repo-profile.json` is valid. It must not request extra required artifacts, change required outputs, or introduce reviewer ids.
- Intent command policy remains the first gate and must not be loosened. Runnable preflight may only skip unsafe/not-runnable commands with explicit reasons; it must not permit installs, `npx`, network calls, provider initialization, external scanners, or dependency setup.
- Intent-test preflight must verify the command executable before inspecting a package script, then verify that package-local script runners such as `node_modules/.bin/vitest` exist. Missing executables are `dependency_missing` evidence and must be skipped before subprocess launch. A Linux sandbox setup failure is `environment_error`; never retry the generated test command unsandboxed.
- When normalizing generated intent-test commands, rewrite only the bare `python` token to `python3`. Preserve explicit interpreter paths such as `sys.executable`, because executable preflight treats an existing absolute path as authoritative and CI runners may not expose `python3` on `PATH`.
- A generated intent-test failure is evidence only. It must not automatically classify a finding as `confirmed_bug` or increase confidence before the allowed failure-analysis classification step.
- Intent execution is capability-driven. Repository descriptors and baseline runtime probes are evidence only; they must not become a language/framework command-template matrix. Codex owns execution_candidates and may revise command, cwd, runtime, or harness, while the Worker owns generic containment, denied-operation, executable, required-path, timeout, and sandbox checks.
- Write agentic execution evidence under intent/: execution-capabilities.json, intent-test-preflight.json, intent-test-runtime-diagnostics.json, intent-test-execution-history.json, and validation-workspace-integrity.json. Preflight and runtime repair are optional bounded Codex turns; canonical policy defaults each to one attempt and may allow only 0 through 3.
- Before and after every intent-test process and after every execution-repair turn, compare validation-workspace repository files with inventory.json hashes. A mutation blocks or invalidates execution even when the process exits 0; a repair that mutates source is rejected and the prior result remains authoritative. Existing repository tests require explicit reuse_existing and byte equality; generated records must not claim existing source files.
- Runtime repair may target mechanically identified harness, import, executable, or dependency failures only. Explicit nonrepairable preflight results and assertion/product signals must not be retried. Rerun only candidate test IDs and retain all attempts without replacing prior passed evidence.
- Intent-test subprocesses must use UTF-8 replacement decoding plus isolated writable home/tmp/runtime caches. Preserve executable shim basenames when recording candidates because resolving multicall shims can change semantics. Do not expose user package credentials or dependency caches, and do not turn arbitrary-project support into dependency installation or network bootstrap.
- Worker-side adaptation may tilt internal emphasis within server-owned job policy, but must not invent or raise repository limits, wall-time limits, token budgets, model policy, or reasoning effort.
Review pipeline rules:

- Full repository scan only; this is not a diff or PR review.
- Do not install third-party dependencies or call external scan/lint/review
  tools such as Semgrep, SonarQube, CodeQL, reviewdog, or MegaLinter.
- Helper scripts must use Python 3 standard library only, write only under
  `.codex-review/runs/**`, and perform mechanical tasks only.
- Codex performs semantic judgment. Helper scripts must not decide whether a
  finding is real, severe, exploitable, or worth fixing.
- Security findings must demonstrate an end-to-end attacker-controlled path through the actual producer and its validation/containment before claiming exploitability. A dangerous-looking consumer or URL sink alone is defense-in-depth evidence, not proof of a high/critical issue; merge a test-gap observation into the same finding when it shares the contract, sink, and fix.
- Reviewer JSON validation must reject malformed reviewer outputs and use a
  Codex repair turn before retrying validation; never silently default missing
  schemas or `findings` arrays into verified reviewer artifacts. The phase
  error artifact is `json-errors.json`.
- Required phases follow the v1.2 spec: prepare workspace, start app server,
  initialize, auth check, bootstrap helper scripts, inventory, token budget,
  repo map, risk routing, bundle planning/packing, reviewer fanout, reviewer
  JSON validation, location validation, clustering/voting, intent-test
  validation, intent mining, intent-test planning, validation workspace
  preparation, intent-test writing, intent-test running, intent-test failure
  analysis, validator disproof, final report JSON, markdown render, QA gate,
  hash artifacts, upload artifacts, submit envelope, and cleanup active job.
- Codex semantic phases must use phase-specific prompts/templates that name the
  role, inputs, required output files, schema discipline, and safety rules for
  that phase. Do not send generic `Phase: <name>` prompts for repo mapping,
  risk routing, reviewer fanout, clustering, intent mining/planning/writing,
  test failure analysis, validator disproof, or final report generation.
- Semantic phases and semantic output repair require an initialized Codex
  App Server and root thread. Do not satisfy a semantic phase by writing local
  fallback artifacts when the SDK client or thread is missing.
- A phase may emit `phase_completed` only after its required output files or
  directories exist and local validation passes. Schema-bound phase outputs must
  be parseable JSON objects with the expected `schema_version`; hash-artifact
  completion requires an `artifact-manifest/v1` object with an `items` list.
- Intent-driven tests are allowed only for selected P0/P1 high-value candidate
  findings. Codex-authored generated test source must be staged and published
  only to the run-local `intent/generated-tests/**` tree. The Worker must copy it into the disposable
  validation workspace before execution; Codex may not write or execute there
  directly. Tests must not install dependencies or use network, and must execute
  with `cwd` inside the disposable validation repo.
  Local phase validation must enforce the v1.2 intent artifact contract:
  `intent-map.json` has `bundle_id` and `behavioral_contracts`, every planned,
  generated, raw, and analyzed test has a unique `test_id`, plan/source/result
  `linked_finding_ids` reference IDs present in `clusters.json`, generated test
  files exist, non-skipped raw runs include a command plus existing stdout and
  stderr log artifacts, and final classifications use only the allowed enum:
  `confirmed_bug`, `plausible_bug`, `test_oracle_wrong`, `test_harness_error`,
  `environment_error`, `flaky_or_nondeterministic`, `dependency_missing`,
  `unclear_requirement`, `passed_no_bug_reproduced`, and
  `skipped_not_runnable`.
  When `intent_test_validation.enabled` is false in the canonical job policy,
  the worker must skip the intent child phases without Codex turns or local test
  execution after writing the parent intent validation config artifact.
  Execute generated test files, not plan rows: one generated file linked to
  multiple plan target ids runs once and preserves those ids in
  `target_test_ids`. Top-level command arrays must be mapped against the full
  executable generated-test set before `max_tests_per_run` truncation; infer
  declared Python `unittest` sources with `python -m unittest`, never pytest.
  The worker must record stdout/stderr under `intent/test-output/`, include
  those logs in artifact manifests with unique artifact ids, and report
  skipped/error/timeout cases as degraded intent-test evidence, not as direct
  job failure. Failing generated tests must be classified before they influence
  confidence. If Codex does not materialize `intent-test-results.json`, the
  fallback must preserve raw project test runs using only the
  `intent-test-result/v1` classification enum, with `confidence = 0.0` and no
  positive finding confidence impact.
- Intent artifact repair must handle existing malformed Codex outputs, not only
  missing files. Keep the strict validators strict, but normalize common model
  shape variants at the repair/fallback boundary before retrying validation; for
  example, `generated_tests` may arrive as a string path list that must become
  object entries with `test_id`, `path`, and `artifact_refs`, and analyzed
  results may arrive with `outcome`/`raw_status` instead of canonical
  `status`/`classification`/`confidence`/`evidence` fields. Skipped, blocked,
  timeout, or environment-limited intent tests are degraded evidence and must
  not fail the whole repository scan after they can be represented in
  `intent-test-result/v1`.
- Intent writer output must use `intent-test-source/v1` with every executable
  record in top-level `generated_tests`; recover known aliases such as
  `generated_test_files` and `created_test_files`, but require each canonical
  record to retain `path`, `command`, and `target_test_ids`. Generated test
  oracles must be grounded in repository instructions/contracts. A Python
  `unittest` entry point must not expose imported repository `TestCase`
  subclasses at module scope where `unittest.main()` can discover unrelated
  suites. Generated-Python preflight rejects module-scope `from` imports of
  test-like objects from project test modules as `test_harness_error`; import
  the module and access any reused helper through that module instead.
- Semantic artifact repair may normalize an existing malformed agent-written
  phase output when a known schema alias can be converted without inventing
  missing semantic content. Worker-owned mechanical outputs, including the
  bootstrap helper summary, must not enter the semantic repair path.
- Report repair must normalize `recommended_fix`/`recommended_action`/
  `remediation` into `recommendation`; accept singular `location`, plural
  `affected_locations`, and top-level path/line aliases; and swap reversed
  line bounds before strict validation. Location verification must resolve the
  same aliases and finding-id variants instead of silently emitting an empty
  result.
- Intent test result repair must classify missing local test runners or missing
  project dependencies, such as exit code 127 or `vitest: not found`, as
  `dependency_missing` with zero confidence and no finding confidence impact.
  Do not leave these as `unclear_requirement` or let model-provided confidence
  promote the linked finding.
- QA must fail completed runs when intent-test validation is enabled and
  `intent-test-results.json` is missing unless an explicit skipped reason is
  recorded in the intent validation, planning, source, or raw run artifacts.
- Required completed artifacts are `report.md`, `report.agent.json`,
  `coverage.json`, `token-budget.json`, `qa.json`, `artifact-manifest.json`,
  `codex-events.jsonl`, `worker.log.jsonl`, and `progress.log.jsonl`.
- Optional v1 artifact catalog entries must be preserved when present,
  including `raw_reviewer_output`, `verified_reviewer_output`,
  `intent_test_output`, and the intent planning/source/result artifacts.
- Materialize `location-verification.json` as an optional `validation_result` artifact with its own stable artifact id. Any artifact name declared by `report.agent.json.artifacts`, `raw_artifact_refs`, or finding `validation_sources` becomes conditionally required: it must be a regular top-level run output and an exact artifact-manifest name before completed-run QA can pass.
- Artifact manifest/upload entries must include stable v1 metadata:
  `artifact_id`, supported `kind`, `name`, `media_type`, `schema_id`,
  `schema_version = v1`, `encoding = utf-8`, `compression = none`, `required`,
  `storage`, `sha256`, and `size_bytes`.
- Intent source repair must run before strict validation is retried for
  `intent_test_writing`. Preserve strict validation, but normalize common
  Codex output variants such as generated test objects with `test_file`,
  `filename`, `created_files`, or materialized generated-test files but no
  canonical `path`.
- Required artifact upload failures must remain terminal for completed uploads.
  Optional artifact upload failures, especially large debug/log artifacts that
  hit server/proxy body limits, should be recorded as artifact-manifest warnings
  and must not prevent result envelope submission when required artifacts were
  uploaded.
- For completed runs, the terminal result envelope must reuse the exact
  artifact manifest that was uploaded before result submission. Do not refresh
  log/debug-bundle hashes while building the completed result envelope; final
  log uploads after accepted result submission are best-effort replacements and
  must not change the manifest used for terminal result validation.
- The worker persists `uploaded-artifact-manifest.json` as the upload-success
  snapshot. Result envelopes must merge this snapshot over the mutable
  `artifact-manifest.json` by `artifact_id`, because `artifact-manifest.json`
  can be rewritten by late logs, debug-bundle refresh, terminal snapshots, or
  local mutation after artifact upload.
- Artifact IDs in one manifest must be unique, and upload must reject
  duplicates before posting any artifact, because artifact upload idempotency is
  keyed by `run_id + artifact_id`.
- Artifact upload must reject manifest names that resolve outside the artifact
  directory before reading or posting any file.
- Artifact upload must reject any manifest-listed artifact whose file is missing
  before posting any artifact; optional manifest entries must not be silently
  skipped once listed.
- Artifact storage URLs must exactly reference the active artifact run directory:
  `/v1/review-runs/<run_id>/artifacts/<artifact_id>`.
- A terminal debug bundle's `debug-summary.json` status and error must match
  the actual terminal envelope (`failed`, `cancelled`, or
  `partial_completed` included), never a default completed value.
- `artifact-manifest.json` must use `artifact-manifest/v1` and its `run_id`
  must match the active artifact directory/run before QA or upload can pass.
- Post run progress through `POST /v1/review-runs/{run_id}/events`, upload
  artifacts through `POST /v1/review-runs/{run_id}/artifacts`, and upload
  artifacts before submitting the terminal result envelope through
  `POST /v1/review-runs/{run_id}/result`.
- Every posted progress event must include `protocol_version`, `run_id`,
  `worker_id`, positive monotonic `sequence`, `timestamp`, supported
  `event_type`, `phase`, `severity`, `message`, and `progress` with
  `overall_percent`, `current_phase_percent`, `status`, and the worker-owned
  ordered `steps` snapshot for the full flow this worker is executing.
- Active heartbeat `progress` snapshots must include the v1 counter set
  (`source_like_files_*`, `bundles_*`, `reviewer_runs_*`,
  `intent_tests_*`, `validator_candidates_*`, and `artifacts_*`) plus
  `active_unit`, even when counters are zero.
- Terminal progress must reconcile artifact-backed source, bundle, intent-test,
  validator, and artifact counters immediately before envelope/debug-bundle
  creation; do not preserve placeholder zeros after the corresponding artifacts
  exist, and never report a completed count above its total.
- The worker is the source of truth for jobscan detail flow shape. Keep phase
  definitions, ordering, labels, and step counts on the worker side, report them
  through progress events and heartbeat snapshots, and do not rely on web or
  server code to recreate this worker's pipeline. Future workers may report a
  different flow; their reported steps must remain internally consistent with
  their own events.
- Long-running phases must post `progress_updated` events, not only
  `phase_started`/`phase_completed`. `reviewer_fanout` progress data must
  include `reviewer_runs_total` and `reviewer_runs_completed`;
  `intent_test_validation` progress data must include `intent_tests_total`,
  `intent_tests_written`, and `intent_tests_run`; `upload_artifacts` progress
  data must include `artifacts_total` and `artifacts_uploaded` and update after
  each successfully uploaded artifact.
- Reviewer progress counts logical `(bundle_id, reviewer_id)` assignments, not reviewer-output files. Grouped outputs must declare every covered bundle through `bundles_reviewed`; validation must reject missing planned assignments and unexpected assignments before a run can complete.
- Once the terminal result request has been submitted, do not post non-terminal cleanup phase events. Only the terminal event matching the submitted status may follow result acceptance.
- A heartbeat response that stops accepting the active job while its terminal
  result request is in flight or already accepted is terminal-commit
  acknowledgement, not a new cancellation. Do not emit `server_cancelled` in
  that window; clear the guard if result submission fails so genuine later
  cancellation remains observable.
- V1 `cancel_run` commands must mark the active job `cancelling`, keep
  available job slots at zero, emit exactly one `run_cancel_requested` event
  before the terminal `run_cancelled` event, interrupt the active Codex turn,
  and still submit a valid cancelled result envelope with partial artifacts
  when possible.
- Unrepaired QA gate failure must not be submitted as a completed run. It must
  emit `phase_failed` for `qa_gate`, then `qa_failed`, then
  `run_partial_completed`, upload terminal artifacts, and submit a
  `partial_completed` result envelope.
- Failed, cancelled, and partial-completed jobs must still submit valid
  terminal envelopes when possible, including required `qa`, `worker_log`, and
  either `error_report` or partial `report.agent` artifacts.

Plan policy:

- Subscription plan policy still controls the model, timeout, repository limits,
  and core reasoning effort.
- Review-phase complexity limits are global server system configuration, not
  subscription entitlements. Require claimed jobs to provide
  `review_request.policy.max_bundles` and `max_reviewer_assignments`, normalize
  them for internal use, enforce both after exact bundle rendering and before any
  reviewer thread starts, and fail explicitly rather than truncating paths,
  tiers, bundles, or reviewer coverage.
- Core semantic phases use the plan reasoning effort. Non-core phases use the
  same model with medium reasoning effort.

## Job Slot And Upload Discipline

Each worker instance has exactly one job execution slot. It does not maintain a
local job queue and must claim a new server-side job only after the current job
has finished. The only job slot must not be occupied by avoidable job-level
retry sleep or cleanup IO.

- After local manifest/upload-snapshot validation, durably write the exact
  terminal payload to the run's `terminal-result-outbox.json` before the first
  HTTP request. Attempt once inside `run_job()`; retry only transport failures,
  HTTP 408/429, and server 5xx from the outer control-plane loop. Permanent HTTP
  failures and local validation/integrity failures stay blocked for operator
  diagnosis. Missing, malformed, checksum-invalid, or identity-invalid outbox
  evidence must fail closed and keep the slot occupied. This one-record
  terminal WAL is not a local job queue.
- A server cancellation may supersede a prepared non-cancelled terminal WAL
  only on HTTP 409 with `JOB_CANCELLATION_AUTHORITATIVE` and exact job, run,
  attempt, job-status, and accepted-result-status bindings. First durably
  publish a checksummed immutable supersession journal and exact original-WAL
  archive, then materialize/upload unique required cancellation artifacts and
  replace the active WAL with generation 2 `cancelled`. Restart must resume
  from the journal or generation-2 WAL without ever resending the superseded
  result. Ordinary 409 conflicts remain blocked.
- Publish write-once journal/archive files from a fully written and fsynced
  sibling temporary file through an atomic no-clobber link, then fsync the
  directory. A crash must expose either no final file or the complete final
  bytes, never a truncated immutable record.
- Result upload payloads should use gzip compression for large JSON. Keep server
  gzip JSON support and worker compression thresholds aligned.
- Do not add unbounded job-level `time.sleep()` retry loops to `run_job()` or other code
  that holds the only job execution slot.
- Cleanup should run only when the worker is idle or on a low-priority
  background path. Do not run checkout/log cleanup before heartbeat/claim in the
  hot loop.
- Persist the active slot under the instance runtime directory before checkout
  starts. On restart, restore unfinished active/result-submission state before
  the first heartbeat or lease attempt, then replay a valid retryable terminal
  outbox after heartbeat and before claim. Never claim around a recovered run.
  Clear `runtime/active-run.json` and the outbox only after the control plane
  acknowledgement is durably recorded; blocked/failed submission evidence must
  keep the slot occupied. A durable success receipt suppresses replay after an
  ambiguous cleanup crash, but workspace cleanup remains on the post-heartbeat
  idle path.
- A completed-result commit must check recorded cancellation under the same
  lock that durably prepares the terminal outbox. Cancellation that wins before
  that boundary produces a cancelled result; cancellation observed after the
  terminal WAL commit begins must not mutate the prepared outcome. Only the
  bound server-authority protocol above may preserve it as immutable audit
  evidence and create a separate cancelled generation.
- Require a positive server-supplied scan wall-time budget. Start one absolute
  monotonic deadline before checkout and pass it
  unchanged through checkout/copy/clone, Codex thread starts and turns,
  inventory, intent execution, and repair retries. Clamp local turn/test caps
  to the remaining global budget and never create a fresh global budget for a
  retry.
- Checkout Git/copy work and intent-test children must poll local cancellation
  and the shared deadline. Intent stdout/stderr stream to files rather than
  accumulating in parent memory; cancellation/deadline must terminate the child
  without a retry, while process-reap failures must not mask the lifecycle
  exception.
- Clean `worker_root/workspaces` only from the idle low-priority path after
  heartbeat/claim. Protect every workspace with unfinished persisted evidence,
  and remove only direct children of the instance-scoped workspace root.

## Checkout And Cache Discipline

Repository checkout performance depends on the worker mirror cache.

- Server/worker responsibility is fixed: the server owns job/scan state,
  repository access validation, short-lived clone token issuance, and lease
  payload fields (`clone_url`, branch, commit, `clone_token`, and
  `repositoryLimits`); the worker owns materializing that repository inside its
  isolated workspace before inventory or review phases run.
- A claimed v1 job may provide an already materialized `checkout_dir` only for
  tests or trusted local integration paths. Production workers must be able to
  clone from the server-provided `clone_url` and short-lived `clone_token` when
  no `checkout_dir` is present.
- After copy or clone, the worker must verify that the repository workspace
  contains real repository files excluding `.git` and `.codex-review`. Empty
  checkouts must fail during `prepare_workspace`, not later during semantic
  phases such as `repo_map`.
- After clone/copy and before starting the Codex App Server, the worker must
  enforce the claimed job `repositoryLimits` against the materialized checkout.
  Repository limit failures must not wait until `inventory_repository`; they
  must submit `REPOSITORY_TOO_LARGE` with `preflight.repositoryStats`,
  `preflight.repositoryLimits`, `repositoryLimitExceeded = true`, and concrete
  `repositoryLimitReasons` so scan history, audit bundles, and quota handling
  have evidence immediately.
- Repository limit preflight stats must report the full eligible checkout
  totals, not the first threshold-crossing values. For example, a 1,028-file
  checkout with `maxFiles = 200` must report `fileCount = 1028`, not `201`;
  only set `scanStoppedEarly` when the stats are actually truncated.
- Keep repository mirrors under `.pullwise-repo-cache` and protect that runtime
  directory from ordinary checkout cleanup.
- Commit-specific jobs should use shallow fetch into the mirror plus a shared
  no-checkout worktree/checkout, not a full fresh clone per job.
- Do not include clone tokens in mirror path names, logs, or persistent config.
  Token-sensitive remote URLs may be used for fetch, but persisted cache
  identity and diagnostics must be redacted.

## Review Evidence Discipline

The v1 worker reports findings through `report.agent.json` and the stable result
envelope. Findings shown to users must be grounded in concrete repository files
and line locations, include a clear failure scenario or risk, and provide an
actionable recommendation. Weak or uncertain observations belong in appendix or
internal artifacts, not as confirmed findings.

Main `report.agent.json.findings` are a mechanically validated surface. Each main finding must be backed by `validated-findings.json.validated_findings` with status `confirmed`, `plausible`, or `validated`. Report repair must demote unbacked findings into `appendix_findings` with `demoted_from_main_findings = true`, recompute `summary.overall_risk` from retained main findings, and rebuild `next_agent_tasks` only from retained main findings. QA must fail non-empty main findings when `validated-findings.json` is missing, malformed, or lacks a matching accepted validation entry.
- Preserve the accepted validator disposition across `validator_status`, stable summary counts, and `report.md`: a `plausible` finding must never be counted or rendered as confirmed. Normalize priority-style severity aliases such as `P0`-`P4` to canonical `critical`/`high`/`medium`/`low`/`info` before report, summary, and server-facing result construction.
Accepted validation status may arrive through common alias fields including
`status`, `validator_status`, `validation_status`, `classification`, or
`disposition`; keep QA/report repair binding logic aligned across those aliases.
When Codex assigns different local ids to the same finding across
`report.agent.json` and `validated-findings.json`, QA/report repair may bind by
the unique `(title, path, start_line)` fallback key even if both records have
non-matching ids. Keep this fallback unique-match-only; ambiguous fallback
matches must remain unbacked.

Terminal result envelopes must include the stable v1 summary shape:
`overall_risk`, `result_status`, `finding_counts`, `coverage`, and
`top_findings`. Do not submit top-findings-only summaries.

Do not require derived topology artifacts for worker output. New review logic,
protocols, reports, tests, and documentation must depend on the stable envelope
and versioned artifacts.

Completed-run artifacts must be real outputs produced by the run, not
placeholders synthesized during hashing or envelope construction. The QA gate
must validate `report.md`, `report.agent.json`, `coverage.json`,
`token-budget.json`, `qa.json`, source-file hashes from inventory, intent-test
classifications and generated-test artifact refs, and the final
`artifact-manifest.json` required kinds, sizes, and SHA-256 values before
upload/result submission.

Worker lifecycle endpoints for operator commands, logs, and registry state
are not the core review protocol. Do not route new review leasing, progress,
artifact, or result behavior through `/worker/...` compatibility paths.

## Server-Controlled Agent Policy

The worker can advertise local provider capability, but review policy comes from
server-provided subscription plan agent configs attached to the claimed job.

- Treat `PULLWISE_PROVIDER_CHAIN` as local installed capability/order, not as the
  source of plan policy.
- `doctor` must load free/pro/max agent configs from the server. If they cannot
  be loaded or validated, do not silently fall back to the local provider chain.
- A claimed v1 job must include canonical `model_profile`,
  `review_request.policy`, `review_request.budget`, and `repositoryLimits`;
  reject jobs that omit required server-owned policy instead of using local
  defaults.
- Drive Codex from `model_profile.default_model`, `model_profile.*_effort`,
  `review_request.policy`, and `review_request.budget`; do not fall back to
  `agentConfig.codex` or `agentConfig.reviewWorker` for model, effort, timeout,
  deadline, or intent-test validation-limit decisions.
- Treat server-provided reasoning effort as a safe lowercase identifier, not a
  worker-owned enum. The server validates model/effort compatibility from the
  Codex model capability catalog; this lets a current worker pass newly
  catalogued efforts to the SDK without another enum update.
- Worker env templates and installers must not declare local reasoning-effort
  or turn-timeout defaults; those are server-owned claimed-job policy.
- Reject jobs whose `review_request.policy` allows source modification,
  dependency install, network access, or non-standard-library helper scripts.
- Repository size limits used by worker preflight come from the job's
  `repositoryLimits`, not from local plan assumptions.
- Older installed workers may still lack a newly supported model or effort
  until operators replace them. Do not implement worker self-upgrade, fleet
  intersection, or old-worker compatibility as part of plan capability changes
  unless that deployment work is explicitly requested.

## Readiness Semantics

Provider readiness is plan-aware and provider-specific.

- `provider_ready` means at least one provider required by the loaded plan
  configs is ready.
- `codex_ready` is the Codex login/readiness state required for accepting jobs.
- A quota/readiness failure must set `provider_ready = false` locally so the
  worker does not call lease, while the idle v1 heartbeat still keeps the
  server-required idle concurrency shape (`active_jobs = 0`,
  `available_job_slots = 1`) and carries the failure through `codex_ready`,
  `codex_quota`, `doctor_status`, and `last_error`.
- Codex quota telemetry shown to admin should represent the worker's main review model bucket, currently GPT-5.5/GPT-5.4/GPT-5 preference order; do not use Spark-specific buckets such as `GPT-5.3-Codex-Spark` or `codex_bengalfox` as the fallback main quota.
- Handle the server's `refresh_codex_quota` command whenever the worker is online, including while a review run is active. Reuse the worker's existing Codex SDK/App Server, never start a second Codex process, force `CodexQuotaMonitor.refresh()`, send a heartbeat containing the new snapshot, and only then report command success.
- Only check providers required by the loaded plan configs.
- Login/auth instructions printed by `doctor` must use the same instance-scoped
  command environment documented above.

## Instance-Scoped Files

Do not share mutable runtime files between worker instances.

- `service_home`, `checkout_root`, `log_dir`, provider home/config/cache dirs,
  and service-user commands must stay instance-scoped.
- Cleanup/update/doctor helpers must operate inside the configured worker roots.
- Avoid fixed global paths for checkouts, logs, provider state, or auth files
  unless they are only base directories containing per-worker subdirectories.
- Destructive instance cleanup must prove containment in an explicit instance
  root such as the instance `service_home`, its contained `worker_root`, or the
  canonical per-worker log/config roots. A matching worker-id basename alone is
  never ownership evidence.

## Delete Instance Cleanup

Admin Delete instance must remove the worker-host resources owned by that worker
instance, not merely let the server hide the worker from admin lists. Cleanup
must cover the instance service unit, wrapper, logrotate entry, `/etc` config,
service user when safe, `service_home` under `/var/lib/pullwise-worker`, `log_dir`
under `/var/log/pullwise-worker`, and other instance-scoped runtime files.

Do not assume Pullwise Server is installed on the same host as the worker. The
worker host needs a local lifecycle manager, watcher, supervisor, or finalizer to
own destructive cleanup and status reporting. A worker process can participate
by acknowledging an uninstall command, but durable deletion should not rely only
on the process that is deleting itself; stopped or degraded workers still need a
host-local owner that can remove resources and report failure/success.

A single host may run multiple Pullwise worker instances. Each worker instance
must have its own watcher/supervisor and must not reuse another instance's
worker process, watcher process, systemd unit, service user, env file, config
directory, `service_home`, `log_dir`, runtime directory, uninstall marker, or
provider state. Instance-specific names and paths must be derived from the safe
worker id.

The watcher is the worker-host role that monitors and controls its paired worker
instance. Treat watcher reliability as a lifecycle boundary: the watcher service
must be enabled and started before the worker service, and its systemd unit must
be ordered before the paired worker unit. The watcher may stop and remove the
worker service and instance-owned resources during lifecycle cleanup.

Watcher ownership is strictly one-to-one with a worker instance. Different
worker instances on the same host must have different watcher ids, service
names, runtime directories, env/config paths, and lifecycle markers; they must
never share a watcher service.

Once a watcher service has successfully started, do not stop, disable, remove,
or uninstall it from any non-delete path, including update, restart, cleanup,
manual/local worker uninstall, and post-watcher-start install failures. Watcher
self-removal is allowed only for an admin-initiated Delete instance lifecycle
operation, and only after the watcher has first ensured the paired worker
instance service and instance resources have been successfully uninstalled.

After local lifecycle cleanup completes, a failed terminal status request must
not make the watcher exit or repeat the destructive cleanup. Keep the completed
command in memory, retry only its terminal status report at the watcher poll
interval, and exit an uninstall watcher only after the server acknowledges
`succeeded`.

## Debug Bundle Contract

A debug bundle is not the audit bundle and must never silently fall back to the audit bundle.

- A real debug bundle combines worker-side live evidence and server-side evidence for the same scan/job/run.
- Worker-side evidence should include run-local logs, Codex SDK events, progress logs, run-state, phase outputs, terminal QA/error reports, and the worker artifact manifest. It must not include repository source files, raw API keys, unredacted environment dumps, or unrelated worker-instance state.
- Server-side evidence should include only scoped records for the same scan/job/run: scan/job/attempt/run identifiers, phase/progress/error snapshots, review-run events, artifact metadata/storage references, quota state, and relevant timestamps. It must not include full database dumps, secrets, other users' data, or unrelated scans.
- The UI must disable or omit debug bundle actions when no real debug_bundle artifact/server debug bundle endpoint exists. Do not substitute /scans/{scanId}/audit-bundle.zip as a debug zip URL.
- Tests should protect this contract: missing debugBundleUrl must not produce an audit-bundle URL, and server/worker tests must verify failed runs still expose a real debug_bundle artifact or explicit absence.
- Treat `run/bundles/**` as repository source because packed review bundles embed source text. Never include that tree in a debug bundle, even though other run-local phase outputs are diagnostic evidence.
- When auditing a debug bundle that intentionally omits `run/bundles/**`, derive the packed-bundle counter from the canonical `bundle-plan.json`; only compare individual bundle Markdown names when that source-bearing tree is actually present.

## Execution And Validation Resilience

- Reserve validated reviewer payload bytes through one thread-safe fanout-run budget before publication. Reuse MAX_MODEL_OUTPUT_TOTAL_BYTES as the aggregate cap, never publish the output that crosses it, and persist the limit, reserved/published byte counts, rejected size, and diagnostic error in reviewer-execution.json.
- Keep the continuous control-plane loop alive across retryable transport failures and HTTP 408/429/5xx from registration, heartbeat, or lease by using the configured bounded exponential backoff and jitter; empty leases back off independently. Never lease after a failed heartbeat, never retry permanent HTTP failures or ordinary programming errors, and keep `once=True` to one pass with no retry sleep. Always release the worker lock even when Codex shutdown raises.
- Keep an independent active-job heartbeat/cancellation supervisor running from immediately after a job becomes active until terminal cleanup finishes, including checkout and other blocking setup work. The supervisor must not start a second Codex client while a job is active.
- Codex turn cancellation callbacks must read only the local active-job cancellation state. The independent active-job supervisor owns server polling; never issue a synchronous heartbeat from the 0.5-second Codex turn wait loop.
- A Codex turn deadline starts before `turn_start`. Both turn start and interruption RPCs must remain bounded from the worker's perspective, and a timed-out/cancelled notification consumer must not write run artifacts after `run_turn` returns.
- Give each blocking raw App Server RPC exactly one caller-visible bounded wait; do not nest a bounded `request()` inside another timeout wrapper. A raw RPC timeout permanently marks that `CodexSdkClient` instance unhealthy, blocks every later SDK operation on it, and lets the existing worker cleanup close and replace the runtime before further use.
- Classify quota-probe authentication, transport, timeout, and endpoint failures as quota unavailable, never quota exhausted. Only explicit rate-limit evidence may mark exhaustion; readiness may remain degraded without lying about the user's quota state.
- Keep `check_codex_auth` mechanical and token-free: use one bounded SDK `account(refresh_token=True)` read and validate its typed account state. Never spend a model turn on a dummy authentication prompt; the first productive semantic phase supplies the end-to-end model check.
- Treat missing SDK thread or turn identifiers as protocol failures. Scope event sinks to a run generation and turn, synchronize abandonment before timeout/cancellation returns, and invalidate old scopes plus transient thread references whenever the run sink changes or the SDK client closes.
- Treat `turn/completed` as successful only when its payload identifies the active turn and reports `status = completed`. Parse both SDK model objects and dictionary payloads, fail closed on missing ids or every other status, preserve terminal error details, and propagate notification-stream exceptions without erasing their type or leaving an empty diagnostic.
- Execute reviewer fanout as one fresh independent Codex thread and one turn per planned `(bundle_id, reviewer_id)` assignment inside the existing worker-owned App Server. Schedule assignments in deterministic plan order with server-owned concurrency `1..2`, but reduce effective concurrency to `1` from phase start when total or available memory is in the low-memory class; the coordinator alone may update progress, logs, and `reviewer-execution.json`, including the effective concurrency decision and memory evidence. Give each attempt a unique staging writable root, validate its exact assignment output, then atomically publish `raw-reviewers/<bundle>.<reviewer>.json`. Never batch logical reviewers into one turn or start another Codex runtime. On transient 429/overload capacity failures, retry once on a fresh thread after reducing effective concurrency to `1`; on a fatal failure, cancel active siblings and do not start pending assignments.
- After every reviewer attempt settles and its outcome is recorded, synchronously archive its Codex App Server thread through the SDK with a bounded timeout before starting replacement work. Archive the run's root thread during terminal cleanup as well. If archive fails, stop pending fanout, cancel active siblings, close the unhealthy App Server, and fail the run; removing only a Python reference or swallowing cleanup errors is not sufficient because loaded App Server contexts accumulate across assignments and jobs.
- Validator output must be normalized to the canonical `validated_findings`, `weak_findings`, and `disproven_findings` collections before downstream progress/report generation. Legacy collection names may be repaired, but an unknown disposition must never default to confirmed.
- Normalize reporter `appendix.weak_findings` and validator `weak_findings` into top-level `report.agent.json.appendix_findings` before building the stable envelope. Deduplicate appendix entries when any canonical/source/cluster identity overlaps, not only when whole ID sets are equal. `summary.finding_counts.weak_appendix` must preserve that canonical appendix count even though weak findings are not issue-eligible main findings.
- Treat `dependency_missing` as missing dynamic evidence, not disproof. It must not by itself demote a correctness finding whose static source, control-flow, and contract evidence still support a plausible failure scenario.
- `debug-summary.json.pipeline_diagnostics` is the compact candidate-disposition trace. Keep reviewer, location, clustering, validation, intent execution, report, semantic-repair, and blocker counts aligned with the underlying run artifacts.
- Review phase prompts must be self-contained inside the cloned repository's `.codex-review` tree. Do not assume the worker package's parent-level v1.2 specification file is present in the repository being scanned.
- Debug-bundle audit must fail noncanonical validator collections, progress counters that disagree with validator records, and intent summary counts that disagree with per-test classifications.
- Intent regression commands generated as filesystem paths must run through a compatible discovery command, and duplicate records for the same generated test file must execute once while retaining all related finding ids.
- Before sandbox execution, materialize declared generated-test files from the run-local `intent/generated-tests/**` tree (or a trusted worker-owned canonical compatibility source) into the disposable validation repository at the same relative path. Reject symlinks, traversal, and paths outside those trees; command-only execution records do not require a source path.
- Keep progress-event mutation/persistence serialized and snapshots atomically replaced. Approval-policy read commands must validate every filesystem operand and resolve containment through symlinks, not just inspect the first path.
- Worker and Codex self-updates must stage and probe a new version before activation, retain the last working version through the post-restart doctor check, and restore it on install, wrapper, watcher, restart, or doctor failure.
- Report/validator binding must recognize canonical and model-emitted identity aliases on both sides, including `finding_ids`, `source_finding_ids`, `cluster_id`, and `source_cluster_id`. QA and debug-bundle audit must also verify the reverse direction: every confirmed/plausible validator entry appears in the main report.
- A generated test written under the run-local `intent/generated-tests/**` alias may be repaired only by copying a regular contained file into the same relative path in the disposable validation workspace. Python intent-test environments must put the disposable repository root first on `PYTHONPATH` (`/workspace` inside bubblewrap) so tests never import an installed/global copy of the project.
- Every generated intent test must retain explicit `target_test_ids` linkage to its plan target. Source repair may recover a missing link only from a unique matching `ITP-*`/`ITV-*` ordinal; coverage and raw-run accounting must then use the plan ID rather than treating the generated test ID as a separate target.
- Validator reasoning must not transfer schemas between unrelated endpoints. If a candidate depends only on an uninspected external producer using an assumed payload shape, keep it weak rather than plausible or confirmed.
- Canonical semantic artifacts are strict at the phase boundary. Normalize known model aliases into `risk-routing/v1.routes`, `cluster-output/v1.clusters`, and reviewer `locations` before validation, then reject empty or malformed canonical structures instead of letting fallback routing or QA hide them.
- Record recurring model-output shape incidents as fixtures under `tests/fixtures/output_contracts/`; `scripts/check_output_contracts.py` must exercise the production validators and stay wired into CI and release checks so fixture logic cannot drift from live validation.
- Treat `review_request.output_language` as required execution context for every semantic/reviewer/repair prompt, `report.agent.json`, rendered Markdown, and QA. Natural-language content must use the requested language while schema keys, code, paths, and commands remain unchanged.
- Intent counters distinguish planned, written, attempted, process-started (`intent_tests_run`), assertion-level, and analyzed evidence. Skipped or dependency-missing records are attempted but not run; generated IDs must map back to plan target IDs without inflating logical totals.
- Semantic repair and canonicalization may normalize a semantic artifact only when Codex actually wrote that file. Repo map, risk routing, intent map/plan/source/analysis, clustering, validator, and reporter paths must never synthesize an empty/default required artifact when the agent omitted it; run the bounded Codex repair turn, then keep the phase failed if strict validation still sees no output.
- A non-empty reviewer finding is verified only when it has an identity, title, severity, numeric confidence, failure scenario, substantive evidence, impact, recommendation, false-positive risk, next-agent task, and a source location.
- Keep every reviewer-facing contract aligned on confidence: generic fanout prompts, all reviewer role templates, per-assignment prompts, and reviewer JSON repair prompts must require a JSON number in `[0,1]`, never a string or qualitative label.
- Analyzed intent results and raw process runs must have the same test IDs. Raw process status is authoritative, and a passed process must never support `confirmed_bug` or `plausible_bug`.
- Intent coverage counters count only planned test IDs reached through explicit test/target linkage; unrelated artifact IDs do not contribute to written, attempted, run, asserted, or analyzed coverage.
- Final report QA must reject source locations whose line range extends past the referenced file's actual line count.
- Plan and pack review bundles through one shared full-payload renderer. Conservatively measure the larger of character count and UTF-8 byte count, exact-fit oversized content by line then character ranges, retain mixed ranged and unranged files, and make packing reject both over-cap and underestimated payloads.
- Run `bundle_planning` as a sequential, non-core-effort Codex semantic turn. The Worker must first write canonical `bundle-planning-input.json`, including the global bundle/assignment caps and reviewer cost weights `P0=3`, `P1=2`, `P2=1`; Codex writes only `bundle-grouping.json` using `bundle-grouping/v1`. Codex owns the semantic group boundaries and must minimize both group count and weighted fanout cost while preserving cohesion. The Worker must regenerate the trusted input, require every eligible P0/P1/P2 path exactly once in a same-tier group, derive reviewer assignments itself, and compile `bundle-plan/v1` through the shared bounded renderer. Do not use a mechanical production fallback when grouping is missing or invalid; use the bounded semantic repair turn and fail the phase if it remains invalid.
- Keep `bootstrap_helper_scripts` mechanical. `prepare_workspace` already materializes the worker-owned helper, schema, and prompt tree, so this phase must only summarize and validate that deterministic tree; it must never spend a Codex turn rewriting fixed worker assets.
- Treat each Codex semantic group as a final requested bundle boundary. The Worker must not coalesce separate same-tier groups or otherwise reinterpret semantic affinity; it may only split a group when exact rendering exceeds the 25-file or rendered-size safety boundary, then enforce the final global bundle and assignment caps before fanout. Prompts must tell Codex to keep exact coverage and tiers even when a cap appears impossible; never satisfy a cap by omitting paths, changing tiers, or reducing reviewer coverage.
- Do not precompute path-component hints or invent `path_affinity`/`test_affinity` grouping reasons in Worker code. Semantic titles, affinity, and grouping reasons belong to the Codex bundle planner; Worker metadata may normalize and preserve those agent outputs but must stay limited to factual routing, rendering, reviewer mapping, and safety fields.
- Stream intent subprocess stdout/stderr to run-local files and keep only bounded diagnostic snippets in worker memory. Cancellation, execution, repair turns, and retries must share one monotonic deadline rather than resetting their wall-time budget.
- Explicit-path intent executables must be regular executable files; bare commands may use `PATH`. Probe `python -m pytest` with the selected interpreter, and classify a missing Python dependency only from an anchored runtime diagnostic, never from assertion text that happens to mention `ModuleNotFoundError`.
- Bind execution to the approved preflight's exact normalized command, canonical contained cwd, and canonical existing required paths. A symlinked, non-regular, missing, or drifted preflight snapshot must fail closed before process launch.
- Generated-test sources and their disposable destinations must use the staged run-local `intent/generated-tests/**` root or the trusted canonical compatibility root, remain regular and content-matched, and preserve the same relative path. Integrity checks validate only: they must never lazily create, rebuild, or repair the immutable baseline.
- Keep the intent-validation inventory baseline in a worker-controlled workspace path outside both the source repository `.codex-review/**` write root and the disposable validation repository. Ignore the mutable run-local `inventory.json`, compare all baseline hashes, and reject undeclared source files while allowing only verified generated-test destinations.
- Toolchain outputs must remain inside the disposable validation boundary. Put integrity-ignored build output under a worker-controlled path such as `validation_repo/.codex-review/build/**`; never redirect writes outside the validation workspace to evade integrity checks.
- Unsandboxed intent runtime homes and caches must stay under `validation_repo/.codex-review/intent-test-home/**`. Keep toolchain telemetry/cache contained but outside the repository-source integrity inventory; never reintroduce a repository-visible `.intent-test-home` tree.
- Rust intent tests must keep `CARGO_HOME` isolated while resolving an explicit or default host `RUSTUP_HOME`; expose and read-only bind that toolchain only for Rust commands.
- When `intent-test-validation.json` explicitly sets `enabled = false`, QA must not warn that `intent-test-results.json` is missing. Enabled or malformed validation configuration keeps the existing fail-closed missing-results behavior.
- Bundle-planner prompts must state the validated group shape directly: stable lowercase `group_id`, non-empty `title`, non-empty `grouping_reasons`, one P0/P1/P2 `tier`, and non-empty exact-cover `paths`.
- Clustering, validation, and reporting remain Codex semantic phases, but their prompts must direct Codex to write canonical empty/no-findings artifacts immediately, without rescanning application source, when their upstream candidate collections are empty.
- A fully completed reviewer plan whose canonical raw and verified outputs cover every assignment with `findings = []` is a clean no-findings result, not an `all_reviewer_outputs_empty` blocker. Missing, malformed, or incomplete reviewer evidence must remain diagnostic blockers.
- Terminal diagnostic refreshes must skip a re-upload only when `uploaded-artifact-manifest.json` proves the same artifact id and exact manifest item was already accepted. Missing, malformed, partial, duplicate, or metadata-mismatched snapshots must fail safe by uploading again, and final replacements must not rewrite the accepted snapshot used by the completed result envelope.
- Codex token usage is thread-cumulative telemetry. Attribute it per turn using the first `total - last` baseline, retain only monotonic snapshots, aggregate concurrent turns under their semantic phase, and publish usage only when the SDK event sink belongs to the current run; never sum raw cumulative snapshots or leak a prior run's ledger.
- Codex SDK shutdown is a bounded RPC. Clear local client references before closing and preserve the timeout failure so a wedged App Server cannot hold the worker lock indefinitely.
