"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, ArrowRight, Check, CircleAlert, Code2, ExternalLink, GitBranch, LoaderCircle, Play, ShieldCheck } from "lucide-react";
import styles from "./showcase.module.css";

type Baseline = { id: string; title: string; task: string; commit: string };
type DemoRun = {
  id: string;
  baselineId: string;
  status: string;
  createdAt: string;
  updatedAt: string;
  patchReady: boolean;
  changedFiles: number;
  acceptance: "passed" | "failed" | "pending";
};
type Showcase = { kind: "deterministic-demo"; baselines: Baseline[]; featuredRuns: DemoRun[]; recentRuns: DemoRun[] };

const statusText: Record<string, string> = {
  QUEUED: "等待 Worker",
  RUNNING: "正在执行",
  VERIFYING: "独立验收中",
  WAITING_APPROVAL: "等待审批",
  SUCCEEDED: "执行完成",
  FAILED: "执行失败",
  CANCELLED: "已取消",
  INTERRUPTED: "已中断",
};

const stages = [
  { number: "01", title: "固定问题现场", detail: "选定 Git commit、允许修改范围和独立验收测试。" },
  { number: "02", title: "异步执行", detail: "控制端写入任务，RabbitMQ 分发给 Python Worker。" },
  { number: "03", title: "隔离生成补丁", detail: "Worker 在 Docker 工作区执行，导出可查看的多文件 diff。" },
  { number: "04", title: "重新应用与验收", detail: "干净副本重新应用补丁；原始版本失败、候选版本通过才算验收。" },
];

export default function ShowcasePage() {
  const [data, setData] = useState<Showcase | null>(null);
  const [selected, setSelected] = useState("checkout");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [launchedId, setLaunchedId] = useState("");
  const [showRecent, setShowRecent] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/showcase", { cache: "no-store" });
      if (!response.ok) throw new Error("无法读取演示记录，请检查 API 服务。");
      const next = (await response.json()) as Showcase;
      if (next.kind !== "deterministic-demo") throw new Error("演示数据格式不正确。");
      setData(next);
      setError("");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function launch() {
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch("/api/runs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          mode: "demo", requestKey: crypto.randomUUID(), baselineId: selected,
          contextMode: "managed", reviewPolicy: "auto", reviewRounds: 2,
          reviewBudget: "shared", approvalPolicy: "auto",
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || "无法创建演示任务。");
      setLaunchedId(body.id);
      await refresh();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  const current = data?.baselines.find((item) => item.id === selected);
  const launched = data?.recentRuns.find((item) => item.id === launchedId);
  const visibleRuns = showRecent ? data?.recentRuns : data?.featuredRuns.filter((run) => run.acceptance === "passed");

  return (
    <main className={styles.page}>
      <div className={styles.frame}>
        <header className={styles.header}>
          <a className={styles.brand} href="/" aria-label="RepoFix 工作台">
            <span className={styles.brandIcon}><Code2 size={20} /></span>
            <span>RepoFix <small>INTERVIEW WALKTHROUGH</small></span>
          </a>
          <a className={styles.back} href="/"><ArrowLeft size={15} /> 返回工作台</a>
        </header>

        <section className={styles.hero} aria-labelledby="showcase-title">
          <div className={styles.heroCopy}>
            <div className={styles.kicker}><span className={styles.pulse} /> 可复现的本地演示</div>
            <h1 id="showcase-title">让一次仓库修复<br /><em>有迹可循。</em></h1>
            <p>从固定源码到候选补丁，再到独立验收。用一条真实执行记录，讲清 RepoFix 的前后端与 Worker 如何协作。</p>
            <div className={styles.heroActions}>
              <a href="#try" className={styles.primaryLink}>开始演示 <ArrowRight size={17} /></a>
              <a href="#evidence" className={styles.secondaryLink}>查看运行证据 <ArrowRight size={16} /></a>
            </div>
          </div>
          <div className={styles.heroAside} aria-label="演示边界">
            <div className={styles.asideLabel}>演示边界 / 01</div>
            <div className={styles.asideBig}>真实链路<br />预设动作</div>
            <p>代码、队列、沙箱、补丁与测试按实际链路运行。Coder 和 Reviewer 使用确定性预设，不访问收费模型；这里的通过记录不代表 SWE-bench 成绩。</p>
            <div className={styles.asideFoot}><ShieldCheck size={17} /> 不触发真实模型请求</div>
          </div>
        </section>

        <section className={styles.flow} aria-label="系统执行流程">
          <div className={styles.sectionHeading}>
            <span className={styles.overline}>HOW IT WORKS</span>
            <h2>四步看懂一次交付</h2>
            <p>面试时可沿着任务、执行、补丁、验收逐步打开对应证据。</p>
          </div>
          <div className={styles.stageGrid}>
            {stages.map((stage) => (
              <article className={styles.stage} key={stage.number}>
                <span className={styles.stageNumber}>{stage.number}</span>
                <h3>{stage.title}</h3>
                <p>{stage.detail}</p>
              </article>
            ))}
          </div>
          <div className={styles.stackLine}><span>Next.js</span><ArrowRight size={15} /><span>NestJS</span><ArrowRight size={15} /><span>RabbitMQ</span><ArrowRight size={15} /><span>Python Worker</span><ArrowRight size={15} /><span>Docker</span></div>
        </section>

        <section className={styles.trySection} id="try" aria-labelledby="try-title">
          <div className={styles.sectionHeading}>
            <span className={styles.overline}>LIVE DEMO · NO MODEL CREDITS</span>
            <h2 id="try-title">亲手跑一条完整链路</h2>
            <p>选择内置跨文件问题。预设动作只用于演示工程链路，验收仍在独立容器中执行。</p>
          </div>
          <div className={styles.tryGrid}>
            <div className={styles.picker}>
              <label htmlFor="baseline">修复任务</label>
              <select id="baseline" value={selected} onChange={(event) => setSelected(event.target.value)} disabled={loading || !data}>
                {data?.baselines.map((item) => <option value={item.id} key={item.id}>{item.title}</option>)}
              </select>
              <p className={styles.taskText}>{current?.task || (loading ? "正在加载任务集…" : "任务集暂不可用")}</p>
              <div className={styles.commit}><GitBranch size={15} /> 固定 commit <code>{current?.commit.slice(0, 12) || "—"}</code></div>
              <button className={styles.launch} type="button" disabled={!data || submitting} onClick={launch}>
                {submitting ? <LoaderCircle size={17} className={styles.spin} /> : <Play size={16} />}
                {submitting ? "正在创建任务…" : "运行确定性演示"}
                <ArrowRight size={17} />
              </button>
              {error && <div className={styles.error} role="alert"><CircleAlert size={16} /> {error}</div>}
            </div>
            <div className={styles.guide}>
              <div className={styles.guideTitle}>演示时看这三处</div>
              <div className={styles.guideItem}><span>01</span><p><strong>执行轨迹</strong>：确认任务从队列进入 Worker，并留下检查点与操作记录。</p></div>
              <div className={styles.guideItem}><span>02</span><p><strong>候选补丁</strong>：查看两个文件的实际 diff 与确定性评审意见。</p></div>
              <div className={styles.guideItem}><span>03</span><p><strong>验收结果</strong>：查看原始版本与候选版本在干净环境中的测试结果。</p></div>
              {launchedId && <a className={styles.activeRun} href={`/?run=${launchedId}`}>
                <span><strong>{launched ? statusText[launched.status] || launched.status : "任务已创建"}</strong><small>打开刚创建的运行 {launchedId.slice(0, 8)}</small></span>
                <ExternalLink size={17} />
              </a>}
            </div>
          </div>
        </section>

        <section className={styles.evidence} id="evidence" aria-labelledby="evidence-title">
          <div className={styles.evidenceHead}><div className={styles.sectionHeading}>
            <span className={styles.overline}>EXECUTION EVIDENCE</span>
            <h2 id="evidence-title">来自本机数据库的演示记录</h2>
            <p>默认展示通过独立验收的确定性基线任务；可切换查看最近运行，包括失败与取消。模型评测另见项目报告。</p>
          </div><button className={styles.filterButton} type="button" onClick={() => setShowRecent((value) => !value)} aria-pressed={showRecent}>{showRecent ? "查看验收通过" : "查看最近全部"}</button></div>
          {visibleRuns?.length ? <div className={styles.runGrid}>
            {visibleRuns.map((run) => <a className={styles.runCard} href={`/?run=${run.id}`} key={run.id}>
              <div className={styles.runTop}><span className={styles.runId}>#{run.id.slice(0, 8)}</span><span className={`${styles.runStatus} ${run.status === "SUCCEEDED" ? styles.passed : ""}`}>{statusText[run.status] || run.status}</span></div>
              <h3>{data?.baselines.find((item) => item.id === run.baselineId)?.title || run.baselineId}</h3>
              <div className={styles.runEvidence}>
                <span className={run.patchReady ? styles.positive : ""}>{run.patchReady ? <Check size={14} /> : <span className={styles.pendingDot} />} 补丁{run.patchReady ? ` · ${run.changedFiles} 文件` : "待生成"}</span>
                <span className={run.acceptance === "passed" ? styles.positive : ""}>{run.acceptance === "passed" ? <Check size={14} /> : <span className={styles.pendingDot} />} 验收{run.acceptance === "passed" ? "通过" : run.acceptance === "failed" ? "未通过" : "待完成"}</span>
              </div>
              <div className={styles.runBottom}><time>{new Date(run.createdAt).toLocaleString("zh-CN")}</time><ArrowRight size={17} /></div>
            </a>)}
          </div> : <div className={styles.noRuns}>{loading ? "正在读取运行记录…" : "尚无确定性演示记录。可在上方启动第一条任务。"}</div>}
        </section>
        <footer className={styles.footer}><span>RepoFix / 本地面试演示</span><span>模型评测结果以独立报告为准 · <a href="/">进入完整工作台 <ArrowRight size={13} /></a></span></footer>
      </div>
    </main>
  );
}
