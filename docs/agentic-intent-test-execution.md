# Agentic Intent-Test Execution

## Goal

Intent validation must work across repositories whose languages, workspace
layout, test tools, and runnable commands are not known to Pullwise in advance.
Codex chooses the test strategy. The Worker remains the mechanical authority
for containment, runtime availability, sandboxing, time limits, immutable
repository source, and evidence preservation.

This design deliberately does not maintain a language/framework recipe matrix.
Known manifest names and baseline executable probes are capability signals
only. They never select the command that will run.

## Execution Flow

1. The Worker discovers nested workspaces, descriptors, available executables,
   dependency-state signals, and any commands already proposed by the Agent.
   It writes intent/execution-capabilities.json.
2. The planner proposes one or more execution_candidates per target. Each
   candidate carries an argv array, cwd, and optional exact required_paths.
3. The writer creates a faithful test or explicitly selects an unchanged
   repository test with reuse_existing: true. It may replace the planner's
   candidate when observed capabilities support a better strategy.
4. The Worker materializes generated files into the disposable validation
   repository, applies the generic command policy, resolves the executable,
   checks candidate paths, and writes intent/intent-test-preflight.json.
5. Repairable preflight failures get a bounded Codex repair turn. The Agent may
   change the test, runtime, command, cwd, import strategy, or harness, but it
   may not install dependencies, use network, modify application source, copy
   application logic into the test, or weaken the oracle.
6. Ready commands run as argv without a shell. Production Linux execution uses
   bubblewrap with a writable disposable repository, read-only trusted runtime
   roots, isolated caches/home/tmp, no network namespace, sanitized
   environment, per-test timeout, and total timeout.
7. The Worker classifies process output mechanically. Harness, import,
   executable, or dependency failures may receive one bounded runtime repair;
   assertions and other product signals do not.
8. Only repairable test IDs rerun. Passed/non-repairable results remain
   unchanged, and every attempt is retained in
   intent/intent-test-execution-history.json.

The default is one preflight repair and one runtime repair. Canonical policy
may set max_preflight_repair_attempts and max_runtime_repair_attempts from 0
through 3.

## Phase Artifact Contracts

Phase outputs fail closed before preflight. Every non-skipped plan target must
contain at least one execution candidate whose command normalizes to a
non-empty argv and whose cwd is non-empty. Every generated source record must
link to one or more non-empty target test IDs and, unless explicitly skipped,
carry a command that normalizes to a non-empty argv. Canonical fields and the
known repair aliases are accepted. Explicitly skipped records and empty target
or generated-test collections remain valid degradation states.

## Generic Command Contract

An Agent proposal is eligible when all of the following hold:

- cwd and every filesystem operand remain in the validation repository;
- the executable exists on the Worker or is a regular contained runner;
- argv identifies a test/spec/check/verification operation;
- no shell, installer, downloader, network URL, branch mutation, or denied
  command token is present;
- declared required paths exist and stay contained;
- project-specific checks, when mechanically observable, pass (for example an
  npm script's package-local runner exists).

The Worker has stricter branches for commands with well-known safety semantics
such as python -m unittest, node --test, go test, or cargo test. All other
runtimes use the generic contract above. This is a safety gate, not a template
selector.

An existing repository test can be reused only when the source record sets
reuse_existing: true (or the equivalent existing-test source kind) and its
validation-workspace bytes still equal the source checkout. A generated record
cannot silently claim an existing application or test file.

## Source Integrity

The Worker-owned `.pullwise-integrity/inventory.json`, stored outside the
repository and run artifact tree, is the immutable baseline for repository
files. A run-local `inventory.json` is evidence, not integrity authority.
Before and after every test process, and after every repair turn, the Worker
re-hashes all inventoried files in the validation repository.

- A pre-existing mutation blocks execution and is not Agent-repairable.
- A test that changes repository source is invalidated even when it exits 0.
- A repair turn that changes repository source is rejected; the prior result
  remains authoritative.
- A generated source file is allowed only when its path is declared by the
  source artifact and its bytes match the worker-authorized generated file.
- Undeclared source-like additions are rejected. Contained non-source runtime
  caches may remain outside the immutable source inventory.

The latest check is written to intent/validation-workspace-integrity.json.

## Evidence and Degradation

The following optional artifacts are uploaded with the normal review output:

- execution-capabilities.json
- intent-test-preflight.json
- intent-test-runtime-diagnostics.json
- intent-test-execution-history.json
- validation-workspace-integrity.json

A missing dependency is evidence that dynamic validation is unavailable, not
proof that a finding is false. If no faithful runnable strategy exists under
the no-install/no-network policy, the Agent records a precise skip reason and
the scan continues with degraded static evidence. Optional repair-turn timeout
or failure also degrades to the last structured evidence instead of failing the
whole scan. Cancellation still interrupts immediately.

Supporting arbitrary repositories does not imply arbitrary dependency
bootstrap. Installing or downloading project dependencies would require a
separate server-owned policy, provenance/lockfile verification, cache and
egress controls, and a new threat-model review.

## Verification Matrix

Focused tests cover nested workspaces, arbitrary contained runners, candidate
required paths, command/path/network denial, missing local package runners,
bounded repair, failed repair degradation, selective rerun, attempt history,
source-integrity rejection, and product-vs-harness classification.

Real subprocess tests exercise Python unittest, Node's built-in runner, Go,
.NET through a generic Agent proposal, and Rust where the host/CI toolchain is
available. The .NET and Go cases also verify isolated runtime cache and
environment behavior rather than inheriting user package state.
