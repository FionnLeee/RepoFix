"use client";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "./button";

type Memory = { id: string; content: string; scope: string; sourceRun: string; evidenceRefs: number[];
  baseCommit: string; validity: string; version: number };
async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(Array.isArray(data.message) ? data.message.join("；") : data.message || "请求失败");
  return data;
}
export function ProjectContext({ projectId, runId, commit, eventId }: {
  projectId?: string; runId: string; commit?: string; eventId?: number;
}) {
  const qc = useQueryClient();
  const [content, setContent] = useState("");
  const [scope, setScope] = useState("project");
  const [editing, setEditing] = useState<Memory>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const query = useQuery({ queryKey: ["memories", projectId], enabled: !!projectId,
    queryFn: () => request<Memory[]>(`/projects/${projectId}/memories`) });
  const indexes = useQuery({ queryKey: ["indexes", projectId], enabled: !!projectId,
    queryFn: () => request<{ id: string; commit: string; scope: string; publishedId?: string; pendingId?: string }[]>(`/projects/${projectId}/indexes`) });
  async function change(memory?: Memory, validity = "active", reconfirm = false) {
    if (!projectId || !eventId) return;
    setBusy(true); setError("");
    try {
      const target = memory || editing;
      await request(`/projects/${projectId}/memories${target ? `/${target.id}` : ""}`, {
        content: memory ? memory.content : content,
        scope: memory ? memory.scope : scope,
        sourceRun: memory && !reconfirm ? memory.sourceRun : runId,
        evidenceRefs: memory && !reconfirm ? memory.evidenceRefs : [eventId],
        ...(target ? { version: target.version } : {}), validity,
      });
      if (!memory) { setContent(""); setEditing(undefined); setScope("project"); }
      await qc.invalidateQueries({ queryKey: ["memories", projectId] });
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  function download(memory: Memory) {
    const text = `<!-- scope: ${memory.scope}; commit: ${memory.baseCommit}; source: ${memory.sourceRun}; version: ${memory.version} -->\n${memory.content}`;
    const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = `memory-${memory.id}.md`; link.click();
    URL.revokeObjectURL(url);
  }
  if (!projectId) return <div className="result-view">该历史任务尚未关联项目。创建新的仓库任务后可管理项目记忆。</div>;
  return <div className="result-view memory-view">
    <h3>项目记忆</h3>
    <p>仅显式保存的经验会进入记忆。它不能授予权限；commit 变化后需要重新确认，停用和删除在下一步上下文组装时生效。</p>
    <label>内容（Markdown）<textarea aria-label="记忆内容" value={content} onChange={e => setContent(e.target.value)} maxLength={8000} rows={5} /></label>
    <label>适用范围<input aria-label="记忆范围" value={scope} onChange={e => setScope(e.target.value)} placeholder="project 或目录路径" /></label>
    <div className="memory-actions">
      <Button disabled={busy || !content.trim() || !eventId} onClick={() => change()}>{editing ? "保存修改并重新确认" : "确认保存记忆"}</Button>
      {editing && <Button variant="outline" onClick={() => { setEditing(undefined); setContent(""); }}>取消编辑</Button>}
      <label>导入 Markdown<input type="file" accept=".md,.txt" aria-label="导入记忆" onChange={async e => {
        const file = e.target.files?.[0];
        if (!file) return;
        if (file.size > 24000) { setError("文件过大，最多 8,000 字符"); return; }
        const text = (await file.text()).replace(/^<!--[^]*?-->\s*/, "");
        if (text.length > 8000) { setError("最多 8,000 字符"); return; }
        setContent(text); setEditing(undefined);
      }} /></label>
    </div>
    <small>保存来源：当前任务 · 事件 {eventId || "待生成"}。导入后需确认保存。</small>
    {(error || query.error) && <p role="alert">{error || String(query.error)}</p>}
    {query.data?.map(memory => <article className="memory-card" key={memory.id}>
      <strong>{memory.validity === "disabled" ? "已停用" : memory.baseCommit !== commit ? "版本变化 · 待复核" : "有效"} · v{memory.version} · {memory.scope}</strong>
      <pre>{memory.content}</pre>
      <small>来源 <a href={`/?run=${memory.sourceRun}`}>{memory.sourceRun.slice(0, 8)}</a> · 事件 {memory.evidenceRefs.join(", ")} · commit {memory.baseCommit.slice(0, 12)}</small>
      <div className="memory-actions">
        <Button variant="outline" disabled={busy} onClick={() => { setEditing(memory); setContent(memory.content); setScope(memory.scope); }}>编辑</Button>
        <Button variant="outline" disabled={busy} onClick={() => change(memory, memory.validity === "active" ? "disabled" : "active")}>{memory.validity === "active" ? "停用" : "启用"}</Button>
        {memory.baseCommit !== commit && <Button variant="outline" disabled={busy} onClick={() => change(memory, "active", true)}>用本次任务重新确认</Button>}
        <Button variant="outline" onClick={() => download(memory)}>导出 Markdown</Button>
        <Button variant="outline" disabled={busy} onClick={() => change(memory, "deleted")}>删除</Button>
      </div>
    </article>)}
    {!query.data?.length && <p>暂无显式记忆。</p>}
    <h3>索引版本</h3>
    <p>只有已发布版本参与检索。构建失败或未完成时使用当前文件的字面检索。</p>
    {indexes.data?.map(index => <div className="memory-card" key={index.id}>
      <code>{index.commit.slice(0, 12)} · {index.scope === "base" ? "基准索引" : `执行修改覆盖层 · 任务 ${index.scope.slice(0, 8)}`}</code>
      <p>{index.publishedId ? `已发布：${index.publishedId}` : "尚未发布"}{index.pendingId ? " · 有待完成构建" : ""}</p>
    </div>)}
  </div>;
}
