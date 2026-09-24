/** Facts shown for a historical run must come from its stored attempt, never today's catalog. */
export function sourceFacts(spec: unknown, provenance: unknown) {
  const saved = spec && typeof spec === "object" ? spec as Record<string, unknown> : {};
  const observed = provenance && typeof provenance === "object" ? provenance as Record<string, unknown> : {};
  const valid = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{40}$/.test(value);
  const configured = valid(saved.commit) ? saved.commit : null;
  const executed = valid(observed.commit) ? observed.commit : null;
  const allowedPaths = Array.isArray(saved.allowedPaths)
    ? saved.allowedPaths.filter((path): path is string => typeof path === "string") : [];
  if (configured && executed && configured !== executed)
    return { commit: null, allowedPaths, sourceStatus: "mismatch" as const };
  if (configured && executed)
    return { commit: configured, allowedPaths, sourceStatus: "confirmed" as const };
  if (configured || executed)
    return { commit: configured || executed, allowedPaths, sourceStatus: "stored" as const };
  return { commit: null, allowedPaths, sourceStatus: "unknown" as const };
}

/** updatedAt can change after completion; the first terminal event freezes elapsed time. */
export function terminalElapsedSeconds(createdAt: Date, events: { type: string; createdAt: Date }[]) {
  const terminal = events.find((event) => ["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"].includes(event.type));
  return terminal ? Math.max(0, Math.round((terminal.createdAt.getTime() - createdAt.getTime()) / 1000)) : null;
}
