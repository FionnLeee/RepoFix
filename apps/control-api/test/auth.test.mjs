import assert from "node:assert/strict";
import test from "node:test";
import { matchesWorkerToken } from "../src/auth.ts";

test("worker endpoints accept only the configured bearer token", () => {
  assert.equal(matchesWorkerToken("Bearer test-secret", "test-secret"), true);
  for (const supplied of [undefined, "", "Basic test-secret", "Bearer wrong", "Bearer test-secreu"])
    assert.equal(matchesWorkerToken(supplied, "test-secret"), false);
  assert.equal(matchesWorkerToken("Bearer undefined", undefined), false);
  assert.equal(matchesWorkerToken("Bearer ", ""), false);
});

test("a Unicode header with matching character length fails without throwing", () => {
  assert.equal("Bearer éééé".length, "Bearer abcd".length);
  assert.equal(matchesWorkerToken("Bearer éééé", "abcd"), false);
  assert.equal(matchesWorkerToken("Bearer 😀😀", "abcd"), false);
});
