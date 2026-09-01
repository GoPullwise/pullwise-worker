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
- a Pi agent directory containing the selected provider credentials and optional
  `models.json`

Install and verify:

```bash
npm ci --ignore-scripts
npm test
npm run typecheck
```

## Configure provider accounts

Credential material stays on the Worker host. Add one metadata profile per
account or API key, then run the emitted Pi auth command. Each profile owns an
isolated Pi agent directory.

```bash
export PULLWISE_PI_PROFILE_ROOT=/var/lib/pullwise/pi-profiles
pullwise-worker profile add --id anthropic_primary --label "Anthropic primary" --provider anthropic
pullwise-worker profile add --id openai_team --label "OpenAI team" --provider openai
pullwise-worker profile list
pullwise-worker profile catalog
pullwise-worker sync
pullwise-worker watch
pullwise-worker serve
```

`profile add` never accepts a secret flag. Pi collects and stores the account or
API key inside the emitted profile-specific `PI_CODING_AGENT_DIR`. The catalog
contains only credential IDs/labels, provider IDs, auth type, and available
model metadata; it is safe to send in Worker registration and heartbeat.
`pullwise-worker sync` sends that catalog through the existing authenticated v1
registration and heartbeat routes. It requires `PULLWISE_SERVER_URL`,
`PULLWISE_WORKER_ID`, and `PULLWISE_WORKER_TOKEN` in addition to the profile
root; its stdout never contains the Worker token or provider secrets.

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
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
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
