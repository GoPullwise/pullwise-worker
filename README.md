# pullwise-worker

Pullwise Worker is a one-attempt Node.js/TypeScript supervisor around
`@earendil-works/pi-coding-agent`.

The Worker does not implement an agent loop. One accepted attempt creates one
Pi `AgentSession`, sends one review prompt, observes cumulative usage, validates
the final JSON payload, checks the external fence again, and exits.

Review judgment lives in Worker-owned text under `reviewer/`:

- `skills/pullwise-repository-review/SKILL.md` defines the review workflow;
- `references/review-method.md` defines review heuristics and evidence rules;
- `prompts/review-repository.md` defines the strict model task/output request;
- `system.md` and `context.md` define the read-only trust boundary.

Production source under `src/` is limited to Pi session construction, fixed
read-only tool selection, budget/cancellation supervision, strict input/result
validation, filesystem safety, and early/late publication fencing.

## Requirements

- Node.js `>=22.19.0` (CI uses `22.23.1`)
- npm `10.9.8`
- an assigned Pullwise Model Gateway Profile Set and a Worker control-plane
  identity (or one short-lived bootstrap credential)

Install and verify:

```bash
npm ci --ignore-scripts
npm test
npm run typecheck
```

## Reconcile the managed Gateway profile

Upstream provider credentials never enter the Worker. An administrator creates
write-only Provider Connections, publishes a Profile Set, and binds this Worker
to a Worker Pool in Pullwise Admin. The installer exchanges a short-lived
single-use bootstrap credential for the Worker control-plane token without
putting either token in a URL or process argument.

```bash
export PULLWISE_SERVER_URL=https://api.pull-wise.com
export PULLWISE_WORKER_ID=wk_example
export PULLWISE_WORKER_TOKEN='the exchanged control-plane token'
export PULLWISE_PI_PROFILE_ROOT=/var/lib/pullwise-worker/wk_example/workers/wk_example/pi-profiles
export PULLWISE_WORKER_STATE_ROOT=/var/lib/pullwise-worker/wk_example/workers/wk_example/state
pullwise-worker sync
pullwise-worker watch
pullwise-worker serve
```

`profile add|list|catalog` is retired. `sync` and `watch` fetch the current
Ed25519 manifest trust key plus the Worker-bound profile, validate it, write a
private immutable generation, and atomically replace `managed-current.json`.
The generated Pi `auth.json` contains only this Worker's short-lived Gateway
token; `models.json` contains the Worker/Profile/revision-scoped Gateway URL and
model aliases. Registration and heartbeat contain only de-secreted catalog and
desired/applied profile state.

The long-running Watcher reads `PULLWISE_WORKER_STATE_ROOT/worker-state.json`
and is the only process that sends Worker status to Server. The execution
process writes that file atomically; Admin and Web always read Server state.

`pullwise-worker serve` claims at most one v1 lease, checks out the exact commit,
runs one Pi session with the Server-selected credential/provider/model, uploads
the five existing v1 artifacts, submits the terminal envelope, then returns to
idle. It never sends heartbeats directly; the paired Watcher owns that channel.

## Run one attempt

The process reads one JSON request from stdin and writes one JSON result to
stdout. The external orchestrator owns checkout creation, the fence file, and
publication of the validated result.

```bash
export PULLWISE_PI_AGENT_DIR=/var/lib/pullwise/pi-agent
export PULLWISE_FENCE_ROOT=/var/lib/pullwise/fences
npm start < attempt.json
```

Request shape:

```json
{
  "attempt": {
    "attemptId": "4f17b7fc-80d6-4a33-9d34-3ea3b8468141",
    "workspace": "/srv/pullwise/checkouts/attempt-1",
    "provider": "pullwise-gateway",
    "model": "gpt-reviewer",
    "context": {
      "repository": "owner/repository",
      "revision": "0123456789abcdef"
    },
    "budget": {
      "wallTimeMs": 900000,
      "inputTokens": 200000,
      "outputTokens": 20000,
      "cacheReadTokens": 500000,
      "cacheWriteTokens": 200000
    }
  },
  "fence": {
    "relativePath": "attempt-1.fence",
    "expected": "lease-version-7"
  }
}
```

The fence file must be a regular, non-linked file beneath
`PULLWISE_FENCE_ROOT` containing exactly the expected value (with an optional
final LF). A stale or changed fence prevents session creation or result return.

The Pi session receives only the Worker-wrapped `repo_read`, `repo_grep`, and
`repo_ls` tools. Their filesystem operations reject paths whose lexical or real
target escapes the attempt workspace. Project extensions and project prompt
templates are disabled; the exact provider/model pair is required and model
fallback is rejected.
