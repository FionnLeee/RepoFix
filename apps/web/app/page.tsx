"use client";
import { useEffect, useState } from "react";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
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
  Square,
  Terminal,
  Workflow,
} from "lucide-react";
import { Button } from "../components/button";
type Event = {
  id: number;
  type: string;
  data: Record<string, unknown>;
  createdAt: string;
};
type Run = {
  id: string;
  mode: string;
  task: string;
  status: string;
  workerId: string | null;
  createdAt: string;
  events: Event[];
  result?: {
    patch?: string;
    verification?: { output?: string; passed?: boolean };
    model_calls?: number;
    artifact_path?: string;
    error?: string;
    usage?: { input_tokens: number; output_tokens: number };
  };
};
const labels: Record<string, string> = {
  QUEUED: "等待执行",
  RUNNING: "执行中",
  VERIFYING: "独立验收",
  SUCCEEDED: "已通过",
  FAILED: "未通过",
  CANCELLED: "已取消",
  INTERRUPTED: "执行中断",
  MODEL_CALL: "选择下一步",
  TOOL_RESULT: "执行工具",
  CANDIDATE: "生成候选补丁",
  CANCEL_REQUESTED: "已请求取消",
  HEARTBEAT: "执行保持连接",
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
    [busy, setBusy] = useState(false);
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
  useEffect(() => {
    if (!id) return;
    const stream = new EventSource(`/api/runs/${id}/stream`);
    stream.onmessage = () => {
      void qc.invalidateQueries({ queryKey: ["run", id] });
      void qc.invalidateQueries({ queryKey: ["runs"] });
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
  const run = detail.data,
    events = run?.events?.filter((e) => e.type !== "HEARTBEAT") || [];
  const active = run && ["QUEUED", "RUNNING", "VERIFYING"].includes(run.status);
  return (
    <div className="shell">
      <aside className="rail">
        <a className="brand" href="/">
          <span className="brand-mark">
            <Code2 size={21} />
          </span>
          <span>
            RepoPilot<small>代码任务工作台</small>
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
                修复折扣计算
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
            工作空间 <ChevronRight size={13} /> pricing-example
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
            <span className="phase">首轮部署 · M0 / M1</span>
          </div>
          <div className="task-brief">
            <div className="task-icon">
              <FileDiff size={23} />
            </div>
            <div className="brief-copy">
              <div className="repo-tag">
                pricing-example <span>Python</span>
              </div>
              <h2>修复百分比折扣与输入边界</h2>
              <p>
                将 20%
                折扣正确应用于订单总额，保留两位小数，并拒绝超出范围的折扣。
              </p>
            </div>
            <div className="task-actions">
              <Button
                disabled={busy}
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
          <p className="scope-note">
            首版使用固定示例仓库。演示采用预设动作验证运行链路；真实模型自行定位与修改，结果分别记录。
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
                    {labels[run.status] || run.status}
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
              <div className="tabs" role="tablist">
                {[
                  ["trace", "执行轨迹"],
                  ["patch", "候选补丁"],
                  ["tests", "验收结果"],
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
                  </button>
                ))}
              </div>
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
                      <dd>干净副本 + 固定测试</dd>
                      <dt>模型调用</dt>
                      <dd>{run.result?.model_calls ?? "完成后汇总"}</dd>
                    </dl>
                    <div className="fact-note">
                      当前版本提供执行与验收基线。上下文压缩、记忆、权限审批及
                      SWE-bench 接入按最终方案继续推进。
                    </div>
                  </aside>
                </div>
              )}
              {tab === "patch" && (
                <div className="result-view">
                  <h3>
                    <FileDiff size={17} />
                    源码差异
                  </h3>
                  {run.result?.patch ? (
                    <>
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
                      <pre className="diff">
                        {run.result.patch.split("\n").map((line, i) => (
                          <div
                            key={i}
                            className={
                              line.startsWith("+")
                                ? "added"
                                : line.startsWith("-")
                                  ? "removed"
                                  : ""
                            }
                          >
                            {line || " "}
                          </div>
                        ))}
                      </pre>
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
                    仅将候选 pricing.py
                    放入新环境，使用固定的开发与边界测试验证。
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
            <span>RepoPilot · 首轮可运行基线</span>
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
