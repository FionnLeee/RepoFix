import assert from "node:assert/strict";
import test from "node:test";
import { sourceFacts, terminalElapsedSeconds } from "../src/showcase-facts.ts";

const a = "a".repeat(40);
const b = "b".repeat(40);

test("historical source uses saved run facts after catalog changes or removal", () => {
  assert.deepEqual(sourceFacts({ commit: a, allowedPaths: ["old.py"] }, { commit: a }),
    { commit: a, allowedPaths: ["old.py"], sourceStatus: "confirmed" });
  assert.equal(sourceFacts({ commit: a }, null).commit, a);
  assert.equal(sourceFacts(null, { commit: a }).commit, a);
  assert.equal(sourceFacts(null, null).sourceStatus, "unknown");
  assert.deepEqual(sourceFacts({ commit: a }, { commit: b }).sourceStatus, "mismatch");
  assert.equal(sourceFacts({ commit: a }, { commit: b }).commit, null);
});

test("terminal duration is stable when run metadata later changes", () => {
  const created = new Date("2026-09-24T00:00:00Z");
  const events = [{ type: "QUEUED", createdAt: created },
    { type: "SUCCEEDED", createdAt: new Date("2026-09-24T00:00:25Z") }];
  assert.equal(terminalElapsedSeconds(created, events), 25);
  assert.equal(terminalElapsedSeconds(created, events), 25);
  assert.equal(terminalElapsedSeconds(created, events.slice(0, 1)), null);
});
