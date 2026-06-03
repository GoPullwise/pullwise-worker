# Worker Provider Chain Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add configurable Codex-first worker provider fallback to OpenCode, with explicit model and effort/variant controls.

**Architecture:** Keep the worker pull loop unchanged. Replace the single hard-coded `run_codex_review` dispatch with a provider chain resolver that defaults to the current Codex-only behavior and optionally tries OpenCode after Codex fails. Build each provider command from `WorkerConfig`, parse the same strict JSON findings schema, and return the same `(findings, summary, logs_summary)` tuple.

**Tech Stack:** Python 3.9 standard library, `unittest`, Codex CLI `codex exec`, OpenCode CLI `opencode run`.

---

### Task 1: Worker Configuration

**Files:**
- Modify: `worker/pullwise_worker/main.py`
- Test: `worker/tests/test_worker_main.py`

**Step 1: Write failing tests**

Add tests proving:
- default provider chain is `["codex"]`
- `PULLWISE_PROVIDER_CHAIN=codex,opencode` is parsed in order
- Codex model is optional and effort defaults to `xhigh`
- OpenCode command/model/variant are optional and default to `opencode`, empty model, empty variant

**Step 2: Run tests**

Run: `python -m unittest tests.test_worker_main.WorkerMainTest`

Expected: FAIL because the config fields do not exist.

**Step 3: Implement minimal config**

Add `provider_chain`, `codex_model`, `codex_reasoning_effort`, `opencode_command`, `opencode_model`, and `opencode_variant` to `WorkerConfig`.

**Step 4: Run tests**

Expected: PASS for config tests.

### Task 2: Provider Commands and Fallback

**Files:**
- Modify: `worker/pullwise_worker/main.py`
- Test: `worker/tests/test_worker_main.py`

**Step 1: Write failing tests**

Add tests proving:
- Codex command includes `--model` only when `PULLWISE_CODEX_MODEL` is set
- Codex command uses configured `model_reasoning_effort`
- OpenCode command includes `--model` and `--variant` only when configured
- `run_provider_review` tries OpenCode after a failed Codex run

**Step 2: Run tests**

Expected: FAIL because OpenCode and fallback functions do not exist.

**Step 3: Implement minimal provider helpers**

Extract Codex command execution into a provider helper, add OpenCode helper, and make `run_codex_review` delegate to the provider chain while preserving its public name for existing callers.

**Step 4: Run tests**

Expected: PASS for provider tests.

### Task 3: Docs and Deploy Defaults

**Files:**
- Modify: `worker/README.md`
- Modify: `worker/deploy/worker.env.template`

**Step 1: Update docs**

Document `PULLWISE_PROVIDER_CHAIN`, Codex model/effort defaults, OpenCode model/variant defaults, and example Codex-first fallback configuration.

**Step 2: Run tests**

Run: `python -m unittest discover -s tests -p "test_*.py"` from `worker`.

Expected: all worker tests pass.
