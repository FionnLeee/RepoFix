"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DiffEditor, loader } from "@monaco-editor/react";

export type Finding = {
  file: string;
  line: number;
  severity: string;
  finding: string;
  trigger?: string;
  evidence?: string;
  suggestion?: string;
};

type Candidate = {
  changed_files: string[];
  omitted_files: number;
  base: Record<string, string>;
  candidate: Record<string, string>;
};

// Monaco is a client-only dependency; the version is pinned so the workbench renders the same
// diff on every machine, and a load failure falls back to the recorded patch below.
loader.config({ paths: { vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.0/min/vs" } });

const severityText: Record<string, string> = { blocking: "阻断", major: "重要", minor: "次要" };

export function CandidateDiff({ runId, findings, patch }: { runId: string; findings: Finding[]; patch?: string }) {
  const [data, setData] = useState<Candidate | null>(null);
  const [selected, setSelected] = useState<string>();
  const [state, setState] = useState<"loading" | "diff" | "fallback">("loading");
  const [reveal, setReveal] = useState<{ file: string; line: number }>();
  const editorRef = useRef<any>(null);

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    fetch(`/api/runs/${runId}/candidate`, { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
      .then((candidate: Candidate) => {
        if (cancelled) return;
        setData(candidate);
        setSelected(candidate.changed_files.find((name) => name in candidate.candidate) || candidate.changed_files[0]);
        setState("diff");
      })
      .catch(() => !cancelled && setState("fallback"));
    return () => {
      cancelled = true;
    };
  }, [runId]);

  useEffect(() => {
    if (state === "diff") loader.init().catch(() => setState("fallback"));
  }, [state]);

  const shown = useMemo(
    () => findings.filter((finding) => !selected || finding.file === selected),
    [findings, selected],
  );

  // Every finding is drawn on the candidate line it names; the diff never edits anything.
  const onMount = useCallback(
    (editor: any) => {
      editorRef.current = editor;
      const modified = editor.getModifiedEditor();
      modified.createDecorationsCollection(
        shown.map((finding) => ({
          range: { startLineNumber: finding.line, startColumn: 1, endLineNumber: finding.line, endColumn: 1 },
          options: {
            isWholeLine: true,
            className: `finding-line finding-${finding.severity}`,
            glyphMarginClassName: "finding-glyph",
            glyphMarginHoverMessage: { value: `**${severityText[finding.severity] ?? finding.severity}** ${finding.finding}` },
          },
        })),
      );
      if (reveal && (reveal.file === selected || !selected)) modified.revealLineInCenter(reveal.line);
    },
    [shown, reveal, selected],
  );

  // The findings are the review's own output: they are shown whether or not the editor loaded,
  // so a missing CDN degrades the diff and never hides the review.
  const ready = state === "diff" && !!data && data.changed_files.length > 0;
  return (
    <div className="candidate-diff">
      {ready && <>
        <div className="diff-files" role="tablist">
          {data!.changed_files.map((name) => (
            <button key={name} role="tab" aria-selected={name === selected} className={name === selected ? "active" : ""}
                    onClick={() => setSelected(name)}>
              {name}
              {findings.some((finding) => finding.file === name) && <span className="finding-count">
                {findings.filter((finding) => finding.file === name).length}
              </span>}
            </button>
          ))}
        </div>
        {data!.omitted_files > 0 && <p className="empty-inline">{data!.omitted_files} 个文件超过内联查看上限，未在此展示。</p>}
        <DiffEditor
          key={selected}
          original={selected ? data!.base[selected] ?? "" : ""}
          modified={selected ? data!.candidate[selected] ?? "" : ""}
          language="python"
          theme="vs-dark"
          height="360px"
          onMount={onMount}
          options={{ readOnly: true, renderSideBySide: true, minimap: { enabled: false }, glyphMargin: true,
                     renderOverviewRuler: false, scrollBeyondLastLine: false, fontSize: 12, automaticLayout: true }}
        />
      </>}
      {state === "loading" && <p className="empty-inline">正在加载候选版本…</p>}
      {state === "diff" && data && data.changed_files.length === 0
        && <p className="empty-inline">候选版本与基准版本一致，没有差异。</p>}
      {state === "fallback" && <>
        {patch && <pre className="diff">{patch.split("\n").map((line, i) => (
          <div key={i} className={line.startsWith("+") ? "added" : line.startsWith("-") ? "removed" : ""}>{line || " "}</div>
        ))}</pre>}
        <p className="empty-inline">内联 diff 组件未加载（离线或 CDN 不可达），以上为记录的补丁文本。</p>
      </>}
      <ol className="findings">
        {shown.map((finding, index) => (
          <li key={index} className={`finding finding-${finding.severity}`}>
            <button className="finding-jump" onClick={() => {
              setReveal({ file: finding.file, line: finding.line });
              const editor = editorRef.current?.getModifiedEditor();
              editor?.revealLineInCenter(finding.line);
              editor?.setPosition({ lineNumber: finding.line, column: 1 });
            }}>
              {finding.file}:{finding.line}
            </button>
            <span className={`severity severity-${finding.severity}`}>{severityText[finding.severity] ?? finding.severity}</span>
            <p>{finding.finding}</p>
            {finding.trigger && <p className="muted">触发：{finding.trigger}</p>}
            {finding.evidence && <p className="muted">证据：{finding.evidence}</p>}
            {finding.suggestion && <p className="muted">建议：{finding.suggestion}</p>}
          </li>
        ))}
      </ol>
    </div>
  );
}
