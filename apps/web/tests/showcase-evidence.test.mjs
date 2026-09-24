import assert from "node:assert/strict";
import test from "node:test";
import { disposition, testOutcome } from "../app/showcase/evidence.ts";

const side = (tests, failures = 0, errors = 0, skipped = 0) =>
  ({ report: { tests, failures, errors, skipped } });

test("test cards require a complete nonempty unskipped report", () => {
  assert.equal(testOutcome(null).kind, "pending");
  assert.equal(testOutcome({ report: {} }).kind, "incomplete");
  assert.equal(testOutcome(side(0)).kind, "empty");
  assert.equal(testOutcome(side(2, 0, 0, 2)).kind, "skipped");
  assert.equal(testOutcome(side(3, 0, 0, 1)).kind, "skipped");
  assert.equal(testOutcome(side(3, 0, 1)).kind, "failed");
  assert.equal(testOutcome(side(3, 1)).kind, "failed");
  assert.equal(testOutcome(side(3)).kind, "passed");
});

test("unverified and unresolved review outcomes cannot appear confirmed", () => {
  assert.equal(disposition("fixed").kind, "confirmed");
  assert.equal(disposition("rejected_with_evidence").kind, "confirmed");
  assert.equal(disposition("unresolved").kind, "warning");
  assert.equal(disposition("unverified").kind, "unknown");
});
