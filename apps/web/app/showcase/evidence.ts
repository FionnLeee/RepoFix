export type TestReport = { tests?: number; failures?: number; errors?: number; skipped?: number };
export type TestSide = { output?: string; report?: TestReport } | null;

export function testOutcome(side: TestSide) {
  if (!side) return { kind: "pending" as const, label: "尚未运行" };
  const report = side.report;
  if (!report || [report.tests, report.failures, report.errors, report.skipped].some(
    (value) => !Number.isInteger(value) || (value as number) < 0,
  )) return { kind: "incomplete" as const, label: "报告不完整" };
  const { tests, failures, errors, skipped } = report as Required<TestReport>;
  if (failures + errors + skipped > tests) return { kind: "incomplete" as const, label: "报告不完整" };
  if (tests === 0) return { kind: "empty" as const, label: "未发现测试" };
  if (failures + errors > 0) return { kind: "failed" as const, label: `${failures + errors} 项失败` };
  if (skipped === tests) return { kind: "skipped" as const, label: "全部跳过" };
  if (skipped > 0) return { kind: "skipped" as const, label: `${skipped} 项跳过` };
  return { kind: "passed" as const, label: "本侧测试通过" };
}

export function disposition(outcome: string) {
  switch (outcome) {
    case "fixed": return { kind: "confirmed" as const, label: "已复核修复" };
    case "rejected_with_evidence": return { kind: "confirmed" as const, label: "反驳获证据支持" };
    case "unresolved": return { kind: "warning" as const, label: "未解决" };
    case "unverified": return { kind: "unknown" as const, label: "未复核" };
    default: return { kind: "unknown" as const, label: "状态未识别" };
  }
}
