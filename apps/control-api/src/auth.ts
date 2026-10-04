import { timingSafeEqual } from "node:crypto";

export function matchesWorkerToken(value: string | undefined, token: string | undefined): boolean {
  if (!value || !token) return false;
  const supplied = Buffer.from(value, "utf8");
  const expected = Buffer.from(`Bearer ${token}`, "utf8");
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}
