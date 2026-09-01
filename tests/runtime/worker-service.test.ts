import assert from "node:assert/strict";
import test from "node:test";

import { runWorkerOnce } from "../../src/runtime/worker-service.ts";

test("Worker service claims at most one lease and executes it once", async () => {
  let leases = 0;
  const jobs: unknown[] = [];
  const worked = await runWorkerOnce({
    client: {
      lease: async () => {
        leases += 1;
        return { lease: { run_id: "run_1" }, job: { job_id: "job_1" } };
      },
    },
    executeJob: async (job) => { jobs.push(job); },
  });
  assert.equal(worked, true);
  assert.equal(leases, 1);
  assert.deepEqual(jobs, [{ job_id: "job_1" }]);
});

test("empty lease response stays idle without execution", async () => {
  let executions = 0;
  const worked = await runWorkerOnce({
    client: { lease: async () => ({ lease: null, job: null }) },
    executeJob: async () => { executions += 1; },
  });
  assert.equal(worked, false);
  assert.equal(executions, 0);
});
