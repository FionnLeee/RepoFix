"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, ArrowRight, Check, CircleAlert, Code2, ExternalLink, FileDiff, GitBranch, ShieldCheck } from "lucide-react";
import styles from "../../showcase.module.css";

type TestSide = { output?: string; report?: { tests?: number; failures?: number; errors?: number; skipped?: number } } | null;
type DemoDetail = {
  kind: "deterministic-demo-run";
  id: string;
  baselineId: string;
  title: string;
  task: string;
  status: string;
  createdAt: string;
  updatedAt: string;
  commit: string;
  allowedPaths: string[];
  workerId: string | null;
  milestones: { type: string; createdAt: string }[];
  patch: string;
  patchTruncated: boolean;
  changedFiles: string[];
  review: { rounds: number; roundsDetail: { round: number; status: string; summary?: string; findings: number }[]; dispositions: { id: number; file: string; line: number; outcome: string }[] };
  verification: { passed: boolean | null; patchReplayed: boolean; baseline: TestSide; candidate: TestSide };
};

const milestoneLabels: Record<string, string> = {
  QUEUED: "任务已入队", RUNNING: "Worker 开始执行", REPOSITORY_READY: "仓库快照已固定",
  REVIEW_REQUESTED: "请求确定性评审", REVIEW_COMPLETED: "评审已返回",
  CANDIDATE: "候选补丁已生成", SUCCEEDED: "执行完成", FAILED: "执行失败",
  CANCELLED: "已取消", INTERRUPTED: "执行中断",
};

function TestCard({ title, side, tone }: { title: string; side: TestSide; tone: "before" | "after" }) {
  const report = side?.report;
  const failures = (report?.failures ?? 0) + (report?.errors ?? 0);
  return <article className={`${styles.testCard} ${tone === "after" ? styles.after : ""}`}>
    <div className={styles.testCardHead}><span>{title}</span><strong>{report ? `${report.tests ?? "?"} 项测试` : "等待验收"}</strong></div>
    <div className={styles.testScore}>{report ? (failures ? `${failures} 项失败` : "全部通过") : "—"}</div>
    <p>{tone === "before" ? "相同的独立测试先在原始版本运行，确认缺陷确实存在。" : "补丁重新应用到干净副本后，再运行同一组测试。"}</p>
    {side?.output && <details><summary>查看测试日志</summary><pre>{side.output}</pre></details>}
  </article>;
}

export default function ShowcaseRunPage() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<DemoDetail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    if (!id) return;
    try {
      const response = await fetch(`/api/showcase/runs/${encodeURIComponent(id)}`, { cache: "no-store" });
      if (!response.ok) throw new Error(response.status === 404 ? "这条记录不是内置确定性演示任务。" : "无法读取任务证据。");
      const next = (await response.json()) as DemoDetail;
      if (next.kind !== "deterministic-demo-run") throw new Error("任务证据格式不正确。");
      setRun(next);
      setError("");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const elapsed = run ? Math.max(0, Math.round((new Date(run.updatedAt).getTime() - new Date(run.createdAt).getTime()) / 1000)) : 0;
  const patchLines = run?.patch.split("\n") ?? [];

  return <main className={styles.page}><div className={styles.frame}>
    <header className={styles.header}>
      <a className={styles.brand} href="/"><span className={styles.brandIcon}><Code2 size={20} /></span><span>RepoFix <small>INTERVIEW WALKTHROUGH</small></span></a>
      <a className={styles.back} href="/showcase"><ArrowLeft size={15} /> 返回演示页</a>
    </header>
    {error && <div className={styles.error} role="alert"><CircleAlert size={16} /> {error}</div>}
    {loading && !run && <div className={styles.noRuns}>正在读取本机任务证据…</div>}
    {run && <>
      <section className={styles.detailHero}>
        <div className={styles.detailKicker}>真实执行记录 / 确定性预设动作 / #{run.id.slice(0, 8)}</div>
        <div className={styles.detailHeroRow}><div><h1>{run.title}</h1><p>{run.task}</p></div><span className={styles.detailStatus}>{run.verification.passed === true ? "独立验收通过" : run.status === "SUCCEEDED" ? "执行完成" : run.status}</span></div>
        <div className={styles.detailActions}><a href={`/?run=${run.id}`} className={styles.primaryLink}>进入完整工作台 <ExternalLink size={15} /></a><span>Coder 与 Reviewer 使用确定性预设；此页不表示真实模型修复能力。</span></div>
      </section>

      <section className={styles.detailFacts} aria-label="运行摘要">
        <div><GitBranch size={18} /><span>固定源码版本</span><strong className={styles.mono}>{run.commit.slice(0, 12)}</strong></div>
        <div><FileDiff size={18} /><span>候选补丁</span><strong>{run.changedFiles.length ? `${run.changedFiles.length} 个文件` : "未生成"}</strong></div>
        <div><ShieldCheck size={18} /><span>独立验收</span><strong>{run.verification.passed === true ? "通过" : run.verification.passed === false ? "未通过" : "未完成"}</strong></div>
        <div><span className={styles.clockGlyph}>◷</span><span>总耗时（含排队）</span><strong>{elapsed} 秒</strong></div>
      </section>

      <section className={styles.detailSection}>
        <div className={styles.sectionHeading}><span className={styles.overline}>EXECUTION TRACE</span><h2>从任务到补丁的关键节点</h2><p>时间来自本机控制端事件；完整工具轨迹可在工作台查看。</p></div>
        <div className={styles.milestoneGrid}>{run.milestones.map((event, index) => <div className={styles.milestone} key={event.type}><span className={styles.milestoneIndex}>{String(index + 1).padStart(2, "0")}</span><strong>{milestoneLabels[event.type] || event.type}</strong><time>{new Date(event.createdAt).toLocaleTimeString("zh-CN")}</time></div>)}</div>
        <div className={styles.traceFoot}>执行节点：<code>{run.workerId || "等待认领"}</code> <span>·</span> 允许修改：{run.allowedPaths.map((path) => <code key={path}>{path}</code>)}</div>
      </section>

      <section className={styles.detailSection}>
        <div className={styles.sectionHeading}><span className={styles.overline}>PATCH & REVIEW</span><h2>候选补丁与意见处置</h2><p>页面展示实际 diff；演示评审是确定性规则桩，意见只对对应候选版本有效。</p></div>
        <div className={styles.patchGrid}>
          <div className={styles.patchPanel}><div className={styles.patchHead}><span>git diff</span><span>{run.changedFiles.length} files changed</span></div>{run.patch ? <pre className={styles.patchBody}>{patchLines.map((line, index) => <span className={line.startsWith("+") && !line.startsWith("+++") ? styles.addedLine : line.startsWith("-") && !line.startsWith("---") ? styles.removedLine : line.startsWith("@@") ? styles.hunkLine : ""} key={index}>{line}{"\n"}</span>)}</pre> : <div className={styles.patchEmpty}>本次运行没有候选补丁。</div>}{run.patchTruncated && <p className={styles.truncated}>补丁较长，此处只展示前 30,000 字符；完整版本请在工作台查看。</p>}</div>
          <aside className={styles.reviewPanel}><div className={styles.reviewTitle}>Reviewer 意见闭环 <span>{run.review.rounds} 轮修订</span></div>{run.review.roundsDetail.length ? run.review.roundsDetail.map((item) => <div className={styles.reviewItem} key={item.round}><span>第 {item.round} 轮 · {item.status}</span><p>{item.summary || "无摘要"}</p><small>{item.findings} 条结构化意见</small></div>) : <p className={styles.reviewEmpty}>尚无评审记录。</p>}{run.review.dispositions.length > 0 && <div className={styles.dispositions}><strong>意见处置</strong>{run.review.dispositions.map((item) => <p key={item.id}><Check size={14} /> {item.file}:{item.line} · {item.outcome === "fixed" ? "已修复" : item.outcome}</p>)}</div>}</aside>
        </div>
      </section>

      <section className={styles.detailSection}>
        <div className={styles.sectionHeading}><span className={styles.overline}>INDEPENDENT ACCEPTANCE</span><h2>同一组测试，修复前后对照</h2><p>验收测试与预设动作分离；候选补丁在干净副本重新应用后执行。</p></div>
        <div className={styles.testGrid}><TestCard title="原始版本" side={run.verification.baseline} tone="before" /><TestCard title="候选版本" side={run.verification.candidate} tone="after" /></div>
        <div className={styles.verificationFoot}>{run.verification.patchReplayed ? <><Check size={15} /> 已在干净副本重新应用补丁</> : "尚无补丁重放结果"}<a href={`/?run=${run.id}`}>查看完整运行 <ArrowRight size={14} /></a></div>
      </section>
    </>}
    <footer className={styles.footer}><span>RepoFix / 确定性演示证据</span><span>真实模型效果见独立评测报告 · <a href="/showcase">返回演示页 <ArrowRight size={13} /></a></span></footer>
  </div></main>;
}
