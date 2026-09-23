"use client";
import { useEffect, useState } from "react";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { CandidateDiff, type Finding } from "./candidate-diff";
import {
  ArrowUpRight,
  Check,
  ChevronRight,
  Code2,
  FileDiff,
  FlaskConical,
  GitBranch,
  LoaderCircle,
  Play,
  ShieldAlert,
  Square,
  Terminal,
  Workflow,
} from "lucide-react";
import { Button } from "../components/button";
import { ProjectContext } from "../components/project-context";
type Event = {
  id: number;
  type: string;
  data: Record<string, unknown>;
  createdAt: string;
};
type Approval = {
  id: string;
  status: string;
  generation: number;
  action: { command?: string };
  reason: string;
  targets?: { path: string; allowed: boolean }[];
  workspaceSha256: string;
  checkpointSequence: number;
  note?: string | null;
  requestedAt: string;
  decidedAt?: string | null;
};
type Delivery = {
  id: string;
  status: string;
  targetPath: string;
  targetHead: string;
  baseCommit: string;
  patchSha256: string;
  targetFingerprint: string;
  files: { path: string; change: string; before: string | null; after: string | null }[];
  expiresAt: string;
  decidedAt?: string | null;
  receipt?: { reason?: string; files_written?: number; already_applied?: boolean } | null;
  createdAt: string;
};
const deliveryLabels: Record<string, string> = {
  PENDING: "等待你批准",
  APPROVED: "已批准，等待执行器应用",
  REJECTED: "已拒绝",
  APPLYING: "执行器正在应用",
  APPLIED: "已应用到目标",
  INVALIDATED: "已作废",
  NEEDS_ATTENTION: "需要人工处理",
};
type Run = {
  id: string;
  mode: string;
  task: string;
  status: string;
  workerId: string | null;
  evaluation?: { status: string; patchSha256: string; batchId: string } | null;
  baselineId?: string;
  contextMode?: string;
  projectId?: string;
  memoryEnabled?: boolean;
  approvalPolicy?: string;
  reviewPolicy?: string;
  recoveryAttempts?: number;
  spec?: { source: string; commit: string; subdir: string };
  approvals?: Approval[];
  createdAt: string;
  events: Event[];
  result?: {
    patch?: string;
    verification?: { output?: string; passed?: boolean | null; delegated?: string };
    model_calls?: number;
    artifact_path?: string;
    error?: string;
    usage?: { input_tokens: number; output_tokens: number };
    usage_status?: "complete" | "partial" | "unavailable";
    changed_files?: string[];
    candidate_tree_stored?: boolean;
    trace_id?: string;
    review?: {
      policy: string;
      rounds: number;
      max_rounds: number;
      unresolved_blocking: number;
      budget?: string;
      dispositions?: {
        id: number; review_round: number; file: string; line: number; severity: string; finding: string;
        coder: { status: string; note: string }; reviewer: { status: string; note: string }; outcome: string;
      }[];
      reviews: {
        round: number;
        status: string;
        summary?: string;
        findings?: Finding[];
        invalid?: { reason: string }[];
        candidate_sha256?: string;
        error?: string;
      }[];
    };
    provenance?: { commit: string; source_sha256: string; image_id: string; context_mode: string };
  };
};
const labels: Record<string, string> = {
  QUEUED: "等待执行",
  RUNNING: "执行中",
  VERIFYING: "独立验收",
  WAITING_APPROVAL: "等待审批",
  SUCCEEDED: "执行完成",
  EVALUATION_IMPORTED: "官方验收结果已导入",
  QUOTA_UNAVAILABLE: "配额不可用，模型请求已停止",
  FAILED: "未通过",
  CANCELLED: "已取消",
  INTERRUPTED: "执行中断",
  MODEL_CALL: "选择下一步",
  TOOL_RESULT: "执行工具",
  CANDIDATE: "生成候选补丁",
  CANCEL_REQUESTED: "已请求取消",
  HEARTBEAT: "执行保持连接",
  REPOSITORY_READY: "仓库快照已固定",
  CONTEXT_COMPACTED: "已压缩历史上下文",
  CHECKPOINT_SAVED: "已保存执行检查点",
  INDEX_PUBLISHED: "代码索引已发布",
  INDEX_FALLBACK: "使用当前文件检索",
  CONTEXT_ASSEMBLED: "上下文已组装",
  CONTEXT_USAGE: "上下文用量核对",
  APPROVAL_REQUESTED: "请求动作审批",
  APPROVAL_DECIDED: "审批已决定",
  APPROVAL_APPLIED: "已执行批准的动作",
  APPROVAL_WAIT: "审批未决，停在检查点",
  REQUEUED: "租约过期，重新排队",
  RECOVERED: "从检查点恢复执行",
  QUOTA_WAIT: "等待模型并发配额",
  QUOTA_FALLBACK: "Redis 不可用，未共享配额",
  REVIEW_ENABLED: "已开启代码评审",
  REVIEW_REQUESTED: "请求独立评审",
  REVIEW_COMPLETED: "评审已返回",
  REVIEW_INVALIDATED: "原评审已过期",
  REVIEW_UNRESOLVED: "修订后仍有阻断项",
  REVIEW_FAILED: "评审未能完成",
  REVISION_REQUESTED: "要求有限修订",
  REVIEW_DISPOSITIONS: "评审意见处置已记录",
  DELIVERY_REQUESTED: "登记交付到目标仓库",
  DELIVERY_APPROVED: "已批准交付",
  DELIVERY_REJECTED: "已拒绝交付",
  DELIVERY_APPLYING: "执行器开始应用补丁",
  DELIVERY_APPLIED: "补丁已应用到目标仓库",
  DELIVERY_INVALIDATED: "交付已作废",
  DELIVERY_NEEDS_ATTENTION: "交付需要人工处理",
};
async function api<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(
    `/api${path}`,
    body === undefined
      ? { cache: "no-store" }
      : {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const data = await res.json();
  if (!res.ok)
    throw new Error(
      Array.isArray(data.message)
        ? data.message.join("；")
        : data.message || "请求失败",
    );
  return data;
}
function Workspace() {
  const qc = useQueryClient(),
    [selected, setSelected] = useState<string>(),
    [tab, setTab] = useState("trace"),
    [error, setError] = useState(""),
    [taskKind, setTaskKind] = useState("checkout"),
    [contextMode, setContextMode] = useState("managed"),
    [memoryEnabled, setMemoryEnabled] = useState(true),
    [source, setSource] = useState(""),
    [commit, setCommit] = useState(""),
    [subdir, setSubdir] = useState(""),
    [taskText, setTaskText] = useState(""),
    [paths, setPaths] = useState(""),
    [approvalPolicy, setApprovalPolicy] = useState("auto"),
    [reviewPolicy, setReviewPolicy] = useState("auto"),
    [reviewRounds, setReviewRounds] = useState(2),
    [reviewBudget, setReviewBudget] = useState("extra"),
    [testCommand, setTestCommand] = useState("python -m unittest discover -v"),
    [verification, setVerification] = useState("import unittest\n\nclass Acceptance(unittest.TestCase):\n    def test_behavior(self):\n        # 替换为实际业务断言\n        self.fail('请填写独立验收测试')\n"),
    [busy, setBusy] = useState(false);
  const baselines = useQuery({
    queryKey: ["baseline-tasks"],
    queryFn: () => api<{ id: string; title: string; task: string; spec: { commit: string } }[]>("/baseline-tasks"),
  });
  useEffect(() => {
    const initial = new URLSearchParams(window.location.search).get("run");
    if (initial && /^[0-9a-f-]{36}$/.test(initial)) setSelected(initial);
  }, []);
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api<{ liveEnabled: boolean }>("/health"),
    refetchInterval: 10000,
  });
  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: () => api<Run[]>("/runs"),
    refetchInterval: 3000,
  });
  const id = selected || runs.data?.[0]?.id;
  const detail = useQuery({
    queryKey: ["run", id],
    queryFn: () => api<Run>(`/runs/${id}`),
    enabled: !!id,
    refetchInterval: 5000,
  });
  const deliveries = useQuery({
    queryKey: ["deliveries", id],
    queryFn: () => api<Delivery[]>(`/runs/${id}/deliveries`),
    enabled: !!id && detail.data?.status === "SUCCEEDED",
    refetchInterval: 5000,
  });
  useEffect(() => {
    if (!id) return;
    const stream = new EventSource(`/api/runs/${id}/stream`);
    stream.onmessage = () => {
      void qc.invalidateQueries({ queryKey: ["run", id] });
      void qc.invalidateQueries({ queryKey: ["runs"] });
      void qc.invalidateQueries({ queryKey: ["deliveries", id] });
    };
    return () => stream.close();
  }, [id, qc]);
  async function create(mode: string) {
    setBusy(true);
    setError("");
    try {
      const run = await api<Run>("/runs", {
        mode,
        requestKey: crypto.randomUUID(),
        approvalPolicy,
        ...(taskKind === "legacy" ? {} : { reviewPolicy, reviewRounds, reviewBudget }),
        ...(taskKind === "legacy" ? {} : taskKind === "custom" ? {
          task: taskText, contextMode, memoryEnabled,
          spec: { source, commit, subdir, allowedPaths: paths.split(",").map(p => p.trim()).filter(Boolean),
            testCommand, verificationFiles: { "test_acceptance.py": verification } },
        } : { baselineId: taskKind, contextMode, memoryEnabled: false }),
      });
      setSelected(run.id);
      setTab("trace");
      await qc.invalidateQueries({ queryKey: ["runs"] });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function cancel() {
    try {
      await api(`/runs/${id}/cancel`, {});
      await qc.invalidateQueries({ queryKey: ["run", id] });
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function decide(approvalId: string, decision: string) {
    try {
      await api(`/runs/${id}/approvals/${approvalId}/decide`, { decision });
      setError("");
      await qc.invalidateQueries({ queryKey: ["run", id] });
    } catch (e) {
      setError((e as Error).message);
    }
  }
  // The decision carries the patch hash and target fingerprint the card displayed, so the server
  // can refuse it if the executor registered a newer survey in the meantime.
  async function decideDelivery(delivery: Delivery, decision: string) {
    try {
      await api(`/runs/${id}/deliveries/${delivery.id}/decide`, {
        decision, patchSha256: delivery.patchSha256, targetFingerprint: delivery.targetFingerprint,
      });
      setError("");
      await qc.invalidateQueries({ queryKey: ["deliveries", id] });
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const run = detail.data,
    events = run?.events?.filter((e) => e.type !== "HEARTBEAT") || [];
  const active =
    run && ["QUEUED", "RUNNING", "VERIFYING", "WAITING_APPROVAL"].includes(run.status);
  const pending = run?.approvals?.find((a) => a.status === "PENDING");
  const accepted = run?.status === "SUCCEEDED" && !!run.spec && !!run.result?.patch &&
    (run.result.verification?.delegated ? run.evaluation?.status === "resolved" : run.result.verification?.passed === true);
  const pendingDelivery = deliveries.data?.find((d) => d.status === "PENDING");
  // A review is evidence for the candidate version it read, so one that a revision replaced is
  // shown as expired rather than as a current opinion.
  const reviewHistory = run?.result?.review?.reviews ?? [];
  const staleReviews = new Set(
    events.filter((e) => e.type === "REVIEW_INVALIDATED").map((e) => String(e.data.reviewed_sha256)),
  );
  const currentFindings = (reviewHistory.at(-1)?.findings ?? []) as Finding[];
  return (
    <div className="shell">
      <aside className="rail">
        <a className="brand" href="/">
          <span className="brand-mark">
            <Code2 size={21} />
          </span>
          <span>
            RepoFix<small>代码任务工作台</small>
          </span>
        </a>
        <div className="rail-label">工作空间</div>
        <div className="nav-current">
          <Workflow size={17} /> 任务执行 <ChevronRight size={15} />
        </div>
        <div className="rail-label recent-label">
          最近任务 <span>{runs.data?.length || 0}</span>
        </div>
        <div className="run-list">
          {runs.data?.map((r) => (
            <button
              key={r.id}
              onClick={() => setSelected(r.id)}
              className={`run-item ${id === r.id ? "selected" : ""}`}
            >
              <span className={`dot ${r.status.toLowerCase()}`} />
              <span>
                {r.baselineId || (r.spec ? "仓库修复" : "折扣示例")}
                <small>
                  {r.mode === "live" ? "真实模型" : "确定性演示"} ·{" "}
                  {new Date(r.createdAt).toLocaleTimeString("zh-CN", {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </small>
              </span>
            </button>
          ))}
        </div>
        <div className="rail-bottom">
          <GitBranch size={15} />
          <span>
            基于 mini-swe-agent<small>v2.4.6 · 固定上游版本</small>
          </span>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <span>
            工作空间 <ChevronRight size={13} /> 仓库修复与基线
          </span>
          <span className="connection">
            <i className={health.isSuccess ? "online" : ""} />
            {health.isSuccess ? "服务已连接" : "连接服务中"}
          </span>
        </header>
        <section className="workspace">
          <div className="page-heading">
            <div>
              <div className="eyebrow">任务执行</div>
              <h1>从问题到可验证的补丁</h1>
              <p>查看 Agent 的每一步操作，用独立测试检查最终结果。</p>
            </div>
            <span className="phase">固定版本 · 多文件验收</span>
          </div>
          <div className="task-brief">
            <div className="task-icon">
              <FileDiff size={23} />
            </div>
            <div className="brief-copy">
              <div className="repo-tag">
                Repository tasks <span>Python</span>
              </div>
              <h2>选择一个可复现的修复任务</h2>
              <p>
                固定仓库版本，限制修改范围，在干净副本中重新应用补丁并验收。
              </p>
            </div>
            <div className="task-actions">
              <Button
                disabled={busy || taskKind === "custom"}
                onClick={() => create("demo")}
                variant="outline"
              >
                <Play size={14} />
                运行演示
              </Button>
              <Button
                disabled={busy || !health.data?.liveEnabled}
                onClick={() => create("live")}
              >
                {busy ? (
                  <LoaderCircle size={15} className="spin" />
                ) : (
                  <ArrowUpRight size={16} />
                )}
                真实模型修复
              </Button>
            </div>
          </div>
          <div className="repository-form">
            <label>任务来源<select value={taskKind} onChange={e => setTaskKind(e.target.value)}>
              {baselines.data?.map(t => <option key={t.id} value={t.id}>{t.title}</option>)}
              <option value="custom">指定 Git 仓库</option>
              <option value="legacy">原始单文件折扣示例</option>
            </select></label>
            {taskKind !== "legacy" && <label>上下文策略<select value={contextMode} onChange={e => setContextMode(e.target.value)}>
              <option value="managed">预算、检索与结构化压缩（M2）</option>
              <option value="full">完整历史（基线）</option>
              <option value="compact">历史压缩（对照实验）</option>
            </select></label>}
            <label>动作审批<select value={approvalPolicy} onChange={e => setApprovalPolicy(e.target.value)}>
              <option value="auto">自动：越界写入需批准</option>
              <option value="strict">严格：所有文件写入都需批准</option>
            </select></label>
            {taskKind !== "legacy" && <label>代码评审<select value={reviewPolicy} onChange={e => setReviewPolicy(e.target.value)}>
              <option value="auto">开启：交付前独立评审一次</option>
              <option value="off">关闭：只由独立测试判定</option>
            </select></label>}
            {taskKind !== "legacy" && reviewPolicy === "auto" && <label>修订上限<select value={reviewRounds} onChange={e => setReviewRounds(Number(e.target.value))}>
              {[0, 1, 2, 3].map(n => <option key={n} value={n}>{n} 轮</option>)}
            </select></label>}
            {taskKind !== "legacy" && reviewPolicy === "auto" && <label>评审预算<select value={reviewBudget} onChange={e => setReviewBudget(e.target.value)}>
              <option value="extra">额外：每轮修订加 5 步</option>
              <option value="shared">共享：评审与修订占用原有步数（对照实验）</option>
            </select></label>}
            {taskKind !== "custom" && taskKind !== "legacy" && <p className="form-wide">
              {baselines.data?.find(t => t.id === taskKind)?.task}<br />
              <small>固定 commit：{baselines.data?.find(t => t.id === taskKind)?.spec.commit || "正在加载任务集"}</small>
            </p>}
            {taskKind === "custom" && <>
              {contextMode === "managed" && <label className="form-wide"><input type="checkbox" checked={memoryEnabled} onChange={e => setMemoryEnabled(e.target.checked)} /> 加载已确认且版本匹配的项目记忆</label>}
              <label>公开 GitHub URL 或 registered:仓库编号<input value={source} onChange={e => setSource(e.target.value)} placeholder="https://github.com/owner/repo" /></label>
              <label>完整 commit（40 位）<input value={commit} onChange={e => setCommit(e.target.value)} /></label>
              <label>仓库子目录（可留空）<input value={subdir} onChange={e => setSubdir(e.target.value)} /></label>
              <label>允许修改的文件（逗号分隔）<input value={paths} onChange={e => setPaths(e.target.value)} placeholder="src/calc.py,src/order.py" /></label>
              <label className="form-wide">问题说明<textarea rows={3} value={taskText} onChange={e => setTaskText(e.target.value)} /></label>
              <label className="form-wide">开发测试命令<input value={testCommand} onChange={e => setTestCommand(e.target.value)} /></label>
              <label className="form-wide">独立验收测试（Python unittest，单独保存，不提供给模型）<textarea rows={7} value={verification} onChange={e => setVerification(e.target.value)} /></label>
            </>}
          </div>
          <p className="scope-note">
            支持小型 UTF-8 Python 仓库，沙箱断网且仅含标准库。预设演示仅验证链路；三个自建基线任务不代表 SWE-bench 成绩。
          </p>
          {(error || runs.error || detail.error) && (
            <div role="alert" className="error">
              {error || (runs.error || detail.error)?.message}
            </div>
          )}
          {run ? (
            <div className="task-panel">
              <div className="panel-heading">
                <div className="run-title">
                  <span className={`status ${run.status.toLowerCase()}`}>
                    {run.evaluation ? ({resolved: "官方验收通过", unresolved: "官方验收未通过", infra_failed: "官方验收环境失败"}[run.evaluation.status] || run.evaluation.status)
                      : run.status === "SUCCEEDED" ? (run.result?.verification?.delegated ? "候选已生成，待官方验收"
                        : run.result?.verification?.passed === true ? "独立验收通过" : "执行完成") : labels[run.status] || run.status}
                  </span>
                  <span>
                    {run.mode === "live" ? "真实模型运行" : "确定性链路演示"}
                  </span>
                  <code>{run.id.slice(0, 8)}</code>
                </div>
                {active && (
                  <Button variant="outline" onClick={cancel}>
                    <Square size={12} />
                    取消任务
                  </Button>
                )}
              </div>
              {pending && (
                <div className="approval">
                  <div className="approval-head">
                    <ShieldAlert size={16} />
                    <strong>这个动作需要你批准</strong>
                  </div>
                  <code className="command">$ {pending.action?.command}</code>
                  <p>{pending.reason}</p>
                  {pending.targets?.length ? (
                    <p>
                      涉及路径：
                      {pending.targets
                        .map((t) => `${t.path}${t.allowed ? "" : "（越界）"}`)
                        .join("、")}
                    </p>
                  ) : null}
                  <p className="approval-binding">
                    绑定工作区 {pending.workspaceSha256.slice(0, 12)}… · 检查点 #
                    {pending.checkpointSequence} · 执行代次 {pending.generation}
                  </p>
                  <div className="task-actions">
                    <Button onClick={() => decide(pending.id, "approve")}>
                      <Check size={14} />
                      批准并继续
                    </Button>
                    <Button variant="outline" onClick={() => decide(pending.id, "reject")}>
                      <Square size={12} />
                      拒绝
                    </Button>
                  </div>
                  <p>
                    批准只对这一个动作和该工作区版本有效；执行会在新的执行代次从检查点恢复。
                  </p>
                </div>
              )}
              <div className="tabs" role="tablist">
                {[
                  ["trace", "执行轨迹"],
                  ["patch", "候选补丁"],
                  ["tests", "验收结果"],
                  ["delivery", "交付到仓库"],
                  ["context", "上下文"],
                  ["memory", "项目记忆"],
                ].map(([value, label]) => (
                  <button
                    role="tab"
                    aria-selected={tab === value}
                    key={value}
                    className={tab === value ? "active" : ""}
                    onClick={() => setTab(value)}
                  >
                    {label}
                    {value === "trace" && <span>{events.length}</span>}
                    {value === "delivery" && pendingDelivery && <span>1</span>}
                  </button>
                ))}
              </div>
              {tab === "delivery" && (
                <div className="result-view">
                  <h3>
                    <GitBranch size={17} />
                    交付到你的仓库
                  </h3>
                  {!accepted ? (
                    <p className="empty-inline">
                      {run.status === "SUCCEEDED"
                        ? "只有通过独立验收（或官方 harness 判定 resolved）的候选才能交付；本任务尚未满足。"
                        : "任务完成并通过独立验收后，可以把补丁应用到你自己的 checkout。"}
                    </p>
                  ) : (
                    <>
                      <p>
                        Agent 只在私有沙箱里工作，不会接触你的仓库。交付由你机器上的执行器完成：它先核对目标处于基线版本
                        <code>{run.spec!.commit.slice(0, 12)}</code>，把看到的目标指纹登记到这里；你在下面批准的正是那份补丁与那个目标版本；
                        执行器只在目标仍然一致时写入，重复运行不会重复写入。
                      </p>
                      <code className="command">
                        $ python scripts/deliver.py prepare --run {run.id} --target &lt;你的仓库根目录&gt;
                      </code>
                      {!deliveries.data?.length && <p className="empty-inline">还没有登记过交付。</p>}
                    </>
                  )}
                  {deliveries.data?.map((delivery) => (
                    <div className={delivery.status === "PENDING" ? "approval" : "memory-card"} key={delivery.id}>
                      <div className="approval-head">
                        <ShieldAlert size={16} />
                        <strong>{deliveryLabels[delivery.status] || delivery.status}</strong>
                        <span className="muted">
                          {new Date(delivery.createdAt).toLocaleTimeString("zh-CN")}
                        </span>
                      </div>
                      <p>目标：<code>{delivery.targetPath}</code></p>
                      <p>
                        目标 HEAD {delivery.targetHead.slice(0, 12)}
                        {delivery.targetHead === delivery.baseCommit
                          ? " · 与任务基线相同"
                          : ` · 与基线 ${delivery.baseCommit.slice(0, 12)} 不同，但受影响文件已核对为基线版本`}
                      </p>
                      <ul className="review-history">
                        {delivery.files.map((file) => (
                          <li key={file.path}>
                            <span>{{ added: "新增", modified: "修改", deleted: "删除" }[file.change] || file.change}</span>
                            <span className="muted">{file.path}</span>
                          </li>
                        ))}
                      </ul>
                      <p className="approval-binding">
                        补丁 {delivery.patchSha256.slice(0, 12)}… · 目标指纹 {delivery.targetFingerprint.slice(0, 12)}… ·
                        {delivery.status === "PENDING" || delivery.status === "APPROVED"
                          ? ` ${new Date(delivery.expiresAt).toLocaleTimeString("zh-CN")} 前有效`
                          : delivery.receipt?.reason
                            ? ` ${delivery.receipt.reason}`
                            : delivery.status === "APPLIED"
                              ? delivery.receipt?.already_applied ? " 目标已含候选内容，本次未写入" : ` 写入 ${delivery.receipt?.files_written ?? "?"} 个文件`
                              : ""}
                      </p>
                      {delivery.status === "PENDING" && (
                        <>
                          <div className="task-actions">
                            <Button onClick={() => decideDelivery(delivery, "approve")}>
                              <Check size={14} />
                              批准应用这份补丁
                            </Button>
                            <Button variant="outline" onClick={() => decideDelivery(delivery, "reject")}>
                              <Square size={12} />
                              拒绝
                            </Button>
                          </div>
                          <p>批准后在你的机器上运行：<code>python scripts/deliver.py apply --delivery {delivery.id}</code></p>
                        </>
                      )}
                    </div>
                  ))}
                  {pendingDelivery && run.result?.patch && (
                    <CandidateDiff runId={run.id} findings={currentFindings} patch={run.result.patch} />
                  )}
                </div>
              )}
              {tab === "trace" && (
                <div className="trace-layout">
                  <div className="timeline">
                    {events.map((event, index) => (
                      <article className="event" key={event.id}>
                        <div className="event-track">
                          <span>
                            {event.type === "SUCCEEDED" ? (
                              <Check size={13} />
                            ) : (
                              index + 1
                            )}
                          </span>
                        </div>
                        <div className="event-content">
                          <div className="event-title">
                            <strong>{labels[event.type] || event.type}</strong>
                            <time>
                              {new Date(event.createdAt).toLocaleTimeString(
                                "zh-CN",
                              )}
                            </time>
                          </div>
                          {event.data.command ? (
                            <code className="command">
                              $ {String(event.data.command)}
                            </code>
                          ) : null}
                          {event.data.output ? (
                            <details>
                              <summary>查看输出</summary>
                              <pre>{String(event.data.output)}</pre>
                            </details>
                          ) : null}
                          {event.data.reason ? (
                            <p>{String(event.data.reason)}</p>
                          ) : null}
                          {event.type === "CONTEXT_COMPACTED" && event.data.strategy !== "structured-deterministic-v1" && <p>
                            本次请求历史由 {String(event.data.before_characters)} 字符缩减到 {String(event.data.after_characters)} 字符；完整轨迹仍保留。
                          </p>}
                          {event.type === "CONTEXT_COMPACTED" && event.data.strategy === "structured-deterministic-v1" && <p>
                            已生成结构化状态摘要，当前输入估算 {String(event.data.after_tokens)} token；原始轨迹可回查。
                          </p>}
                          {event.type === "CONTEXT_ASSEMBLED" && <p>输入估算 {String(event.data.estimated_input_tokens)} / {String(event.data.input_limit)} token（保守字节估算）。</p>}
                          {event.type === "INDEX_PUBLISHED" && <p>{String(event.data.scope)} · {String(event.data.chunks)} 个代码片段。</p>}
                          {event.type === "REPOSITORY_READY" && <p>
                            固定版本：{String(event.data.commit)}
                          </p>}
                          {event.type === "CHECKPOINT_SAVED" && <p>
                            检查点 {String(event.data.sequence)} · 已保存 {String(event.data.file_count)} 个文件，
                            完成 {String(event.data.model_calls)} 次模型调用。
                            {event.data.phase === "ready" ? "工作区与执行进度已保存。" : event.data.phase === "awaiting_approval" ? "已停在安全边界，等待动作审批。" : "已记录本次执行结束时的状态。"}
                          </p>}
                          {event.type === "APPROVAL_REQUESTED" && <p>
                            动作等待批准：{String((event.data.action as { command?: string } | undefined)?.command || "")}
                          </p>}
                          {event.type === "APPROVAL_DECIDED" && <p>
                            {event.data.decision === "APPROVED" ? "已批准" : "已拒绝"}；审批绑定动作 {String(event.data.action_sha256 || "").slice(0, 12)}… 与工作区 {String(event.data.workspace_sha256 || "").slice(0, 12)}…。
                          </p>}
                          {event.type === "APPROVAL_WAIT" && <p>
                            审批尚未决定，本次执行停在检查点，等待决定后由新的执行代次继续。
                          </p>}
                          {event.type === "REQUEUED" && <p>
                            第 {String(event.data.attempt)} 次自动恢复：从执行代次 {String(event.data.from_generation)} 的检查点
                            {" "}{String(event.data.checkpoint_id || "").slice(0, 8)}… 重新排队。
                          </p>}
                          {event.type === "RECOVERED" && <p>
                            已在执行代次 {String(event.data.generation)} 从检查点 {String(event.data.checkpoint_id || "").slice(0, 8)}…
                            （第 {String(event.data.sequence)} 个安全点）恢复，沿用已计入的 {String(event.data.model_calls)} 次模型调用。
                          </p>}
                          {event.type === "QUOTA_FALLBACK" && <p>{String(event.data.effect || "")}</p>}
                          {event.type.startsWith("DELIVERY_") && <p>
                            目标 {String(event.data.target || "")}
                            {event.type === "DELIVERY_REQUESTED" && ` · 指纹 ${String(event.data.target_fingerprint || "").slice(0, 12)}… · ${(event.data.files as string[] | undefined)?.length ?? 0} 个文件`}
                            {event.type === "DELIVERY_APPLIED" && ` · 写入 ${String((event.data.receipt as { files_written?: number } | undefined)?.files_written ?? "?")} 个文件`}
                          </p>}
                          {event.type === "SUCCEEDED" && (
                            <p>候选源码已在干净环境中通过独立测试。</p>
                          )}
                          {event.type === "FAILED" && (
                            <p>
                              {String(
                                event.data.error ||
                                  "本次补丁未通过验收，查看验收结果定位原因。",
                              )}
                            </p>
                          )}
                        </div>
                      </article>
                    ))}
                    {active && (
                      <div className="waiting">
                        <LoaderCircle size={15} className="spin" />
                        等待下一条执行记录…
                      </div>
                    )}
                  </div>
                  <aside className="run-facts">
                    <h3>本次运行</h3>
                    <dl>
                      <dt>执行方式</dt>
                      <dd>
                        {run.mode === "live" ? "模型自主执行" : "预设动作"}
                      </dd>
                      <dt>执行节点</dt>
                      <dd>{run.workerId || "等待认领"}</dd>
                      <dt>执行环境</dt>
                      <dd>独立容器 · 默认断网</dd>
                      <dt>验证方式</dt>
                      <dd>{run.result?.verification?.delegated
                        ? (run.evaluation ? `官方 harness：${run.evaluation.status}` : "候选已生成，等待官方 harness 判定")
                        : "干净副本 + 固定测试"}</dd>
                      <dt>运行追踪</dt>
                      <dd>{run.result?.trace_id ? <a href={`http://localhost:16686/trace/${run.result.trace_id}`}
                        target="_blank" rel="noreferrer">{run.result.trace_id.slice(0, 16)}… ↗</a> : "未开启（未配置 OTLP）"}</dd>
                      <dt>模型调用</dt>
                      <dd>{run.result?.model_calls ?? "完成后汇总"}</dd>
                      <dt>任务</dt><dd>{run.task}</dd>
                      <dt>动作审批</dt>
                      <dd>
                        {run.approvalPolicy === "strict"
                          ? "严格：所有文件写入需批准"
                          : "自动：越界写入需批准"}
                      </dd>
                      {run.recoveryAttempts ? (
                        <>
                          <dt>自动恢复</dt>
                          <dd>已恢复 {run.recoveryAttempts} 次（上限 3 次）</dd>
                        </>
                      ) : null}
                      {run.spec && <><dt>仓库版本</dt><dd>{run.spec.source}<br />{run.spec.commit}</dd>
                        <dt>上下文策略</dt><dd>{run.contextMode === "managed" ? "预算、检索与结构化压缩" : run.contextMode === "compact" ? "历史压缩" : "完整历史"}</dd>
                        <dt>修改文件</dt><dd>{run.result?.changed_files?.join(", ") || "等待候选"}</dd>
                        <dt>输入 / 输出 token</dt><dd>{run.result?.usage
                          ? `${run.result.usage.input_tokens} / ${run.result.usage.output_tokens}${run.result.usage_status === "partial" ? "（部分调用）" : run.result.usage_status !== "complete" ? "（覆盖未核实）" : ""}`
                          : ["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"].includes(run.status) ? "未获得用量记录" : "完成后汇总"}</dd></>}
                    </dl>
                    <div className="fact-note">
                      当前版本提供可靠调度（失联自动重排队、跨代次检查点恢复、共享模型并发配额）、
                      版本绑定审批与显式记忆。SWE-bench 接入与效果评测按最终方案继续推进。
                    </div>
                  </aside>
                </div>
              )}
              {tab === "memory" && <ProjectContext key={run.id} projectId={run.projectId} runId={run.id} commit={run.spec?.commit} eventId={events.at(-1)?.id} />}
              {tab === "context" && <div className="result-view">
                <h3>上下文预算与证据</h3>
                <p>仅 managed 模式组装代码证据、项目规则和有效记忆。记忆读取：{run.memoryEnabled ? "已启用" : "已关闭"}。</p>
                {active && run.contextMode === "managed" && <Button variant="outline" onClick={async () => {
                  try { await api(`/runs/${id}/compact`, {}); setError(""); } catch (e) { setError((e as Error).message); }
                }}>请求下一步骤压缩</Button>}
                {events.filter(e => ["CONTEXT_ASSEMBLED", "CONTEXT_COMPACTED", "CONTEXT_USAGE"].includes(e.type)).map(e => <details className="memory-card" key={e.id}>
                  <summary>{labels[e.type]} · 调用 {String(e.data.call || e.data.at_call || "")}</summary>
                  <pre>{JSON.stringify(e.data, null, 2)}</pre>
                </details>)}
              </div>}
              {tab === "patch" && (
                <div className="result-view">
                  <h3>
                    <FileDiff size={17} />
                    源码差异
                  </h3>
                  {run.result?.patch ? (
                    <>
                      <div className="review-summary">
                        <span className={`badge ${reviewPolicy === "off" ? "" : "on"}`}>
                          评审{run.result.review?.policy === "off" ? "关闭" : "开启"}
                        </span>
                        <span>已完成 {run.result.review?.rounds ?? 0} 轮有限修订{run.result.review?.budget === "shared" ? "（共享预算）" : ""}</span>
                        {(run.result.review?.unresolved_blocking ?? 0) > 0 && <span className="severity severity-blocking">
                          修订上限后仍有 {run.result.review?.unresolved_blocking} 条阻断项，交由独立验收判定
                        </span>}
                      </div>
                      {reviewHistory.length > 0 && <ul className="review-history">
                        {reviewHistory.map((record) => <li key={record.round}>
                          <span>第 {record.round} 轮评审</span>
                          <span className="muted">{record.status === "failed" ? `未能完成：${record.error}` : record.summary}</span>
                          {record.status === "partial" && <span className="stale">评审不完整：部分证据未覆盖或意见无效</span>}
                          <span>{record.findings?.length ?? 0} 条意见{record.invalid?.length ? `，丢弃 ${record.invalid.length} 条无效位置` : ""}</span>
                          {record.candidate_sha256 && staleReviews.has(record.candidate_sha256)
                            && <span className="stale">已过期：候选版本此后被修订，位置仅对当时的版本有效</span>}
                          {record.findings?.map((finding, index) => (
                            <span key={index} className="muted">{finding.file}:{finding.line}</span>
                          ))}
                        </li>)}
                      </ul>}
                      {(run.result.review?.dispositions?.length ?? 0) > 0 && <ul className="review-history">
                        {run.result.review!.dispositions!.map((d) => <li key={`${d.review_round}-${d.id}`}>
                          <span>第 {d.review_round} 轮意见 {d.id} · {d.file}:{d.line}</span>
                          <span className="muted">{d.finding}</span>
                          <span>执行者：{({fixed: "称已修复", rejected: "提出反驳", unstated: "未答复"} as Record<string, string>)[d.coder.status] || d.coder.status}{d.coder.note ? `（${d.coder.note}）` : ""}</span>
                          <span className={d.outcome === "unresolved" ? "stale" : d.outcome === "unverified" ? "muted" : ""}>
                            评审：{({fixed: "已修复", rejected_with_evidence: "驳回成立", unresolved: "仍未解决", unverified: "未经复核"} as Record<string, string>)[d.outcome] || d.outcome}{d.reviewer.note ? `（${d.reviewer.note}）` : ""}
                          </span>
                        </li>)}
                      </ul>}
                      <button
                        className="download"
                        onClick={() => {
                          const url = URL.createObjectURL(
                            new Blob([run.result!.patch!], {
                              type: "text/plain",
                            }),
                          );
                          const a = document.createElement("a");
                          a.href = url;
                          a.download = `${run.id}.patch`;
                          a.click();
                          URL.revokeObjectURL(url);
                        }}
                      >
                        下载 patch ↓
                      </button>
                      <CandidateDiff runId={run.id} findings={currentFindings} patch={run.result.patch} />
                    </>
                  ) : (
                    <p className="empty-inline">
                      尚未生成补丁，执行完成后在此查看。
                    </p>
                  )}
                </div>
              )}
              {tab === "tests" && (
                <div className="result-view">
                  <h3>
                    <FlaskConical size={18} />
                    独立验收
                  </h3>
                  <p>
                    {run.spec ? "在相同镜像的独立容器中验证原始版本失败，再验证重新应用多文件补丁后的版本通过。" : "仅将候选 pricing.py 放入新环境，使用固定测试验证。"}
                  </p>
                  {run.result?.verification ? (
                    <>
                      <span
                        className={`status ${run.result.verification.passed ? "succeeded" : "failed"}`}
                      >
                        {run.result.verification.passed
                          ? "测试通过"
                          : "测试未通过"}
                      </span>
                      <pre>{run.result.verification.output}</pre>
                    </>
                  ) : (
                    <p className="empty-inline">验收尚未完成。</p>
                  )}
                  {run.result?.error && (
                    <div className="error">{run.result.error}</div>
                  )}
                </div>
              )}
            </div>
          ) : (
            <div className="empty">
              <Terminal size={30} />
              <h2>开始第一条修复任务</h2>
              <p>运行演示检查链路，或让真实模型尝试修复同一个问题。</p>
            </div>
          )}
          <footer>
            <span>RepoFix · 首轮可运行基线</span>
            <span>执行记录与补丁均来自实际运行</span>
          </footer>
        </section>
      </main>
    </div>
  );
}
export default function Page() {
  const [client] = useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: 1 } } }),
  );
  return (
    <QueryClientProvider client={client}>
      <Workspace />
    </QueryClientProvider>
  );
}
