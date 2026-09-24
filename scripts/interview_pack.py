"""Export a small offline interview page from two explicit frozen model runs."""

import argparse
import csv
import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path

from interview_metrics import mini_metrics

CASES = (
    ("sphinx-doc__sphinx-8475", "29ac5098-0e7d-460e-b1ac-a04649b8d1f8", "成功"),
    ("django__django-11885", "091df0ef-b64c-480d-85cb-2082a9de0ac5", "失败"),
)


def esc(value):
    return html.escape(str(value), quote=True)


def make_card(row, result, diagnosis=None):
    patch = result.get("patch") or ""
    provenance = result.get("provenance") or {}
    patch_hash = hashlib.sha256(patch.encode()).hexdigest()
    if diagnosis:
        delta = diagnosis.get("diagnostic_delta") or {}
        process = (f"第 {diagnosis['first_observed_delta']['model_calls']} 次调用后的检查点首次观测到改动；"
                   f"最后过程快照 {delta.get('bytes', 0)} bytes，SHA-256 {delta.get('sha256', '未知')}。"
                   "过程快照未提交、未验收，不能当作正式补丁。") if diagnosis.get("first_observed_delta") else "未观测到有效过程改动。"
    else:
        process = "生成正式候选；下方展示补丁前 20 行。"
    excerpt = "\n".join(patch.splitlines()[:20]) if patch else "（无正式候选补丁）"
    duration = result.get("duration_seconds")
    duration_text = f"{duration:.2f} 秒 Agent 执行" if isinstance(duration, (float, int)) else "Agent 时长未知"
    return f"""<article class="card"><div class="tag">真实模型归档 · {esc(row['official_verdict'])}</div>
<h2>{esc(row['instance_id'])}</h2><p>{esc(process)}</p>
<dl><dt>Run ID</dt><dd><code>{esc(row['run_id'])}</code></dd>
<dt>固定 commit</dt><dd><code>{esc(provenance.get('commit', '未知'))}</code></dd>
<dt>模型与配置</dt><dd>{esc(provenance.get('model', '未知'))} · Reviewer {esc(row['reviewer'])} · 上限 {esc(row['call_cap'])} 次</dd>
<dt>用量与时长</dt><dd>{esc(row['model_calls'])} 次调用 · {esc(row['reported_tokens'])} 已报告 token · {esc(duration_text)}（不含 harness）</dd>
<dt>正式补丁 SHA-256</dt><dd><code>{esc(patch_hash if patch else '无正式候选')}</code></dd>
<dt>官方判定</dt><dd>{esc(row['official_verdict'])}</dd></dl>
<details><summary>正式候选片段</summary><pre>{esc(excerpt)}</pre></details></article>"""


def build(csv_path, artifact_root, diagnosis_path, recovery_path, delivery_path):
    mini_metrics(csv_path, artifact_root)
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))
    cards = []
    for issue, run_id, _ in CASES:
        matches = [row for row in rows if row["instance_id"] == issue and row["run_id"] == run_id]
        if len(matches) != 1:
            raise ValueError(f"Explicit run missing or duplicated: {issue}/{run_id}")
        result = json.loads((artifact_root / run_id / "result.json").read_text(encoding="utf-8"))
        card_diagnosis = diagnosis if run_id == diagnosis["run_id"] else None
        cards.append(make_card(matches[0], result, card_diagnosis))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    source = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    if len(recovery.get("checks", [])) != 6 or len(delivery.get("checks", [])) != 6:
        raise ValueError("Expected the frozen six-check recovery and delivery cases")
    engineering = (f"<div class=\"grid\"><article class=\"card\"><div class=\"tag\">确定性故障注入 · 6/6 检查</div>"
                   f"<h2>失联恢复</h2><p>租约失效后重新入队；第 {esc(recovery['lost_generation'])} 代失权，"
                   f"第 {esc(recovery['final_generation'])} 代从已登记检查点恢复，旧代次不能写终态。</p>"
                   f"<p>run <code>{esc(recovery['run_id'])}</code> · 生成模型调用 {esc(recovery['generative_model_calls'])}</p></article>"
                   f"<article class=\"card\"><div class=\"tag\">确定性交付检查 · 6/6 检查</div>"
                   f"<h2>目标指纹失效</h2><p>批准后目标文件变化，执行器拒绝写入；重复应用不重复写，"
                   f"混合目标状态交给人工处理。仅在临时 checkout 验证。</p>"
                   f"<p>run <code>{esc(delivery['run_id'])}</code></p></article></div>")
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RepoFix · 离线面试证据</title><style>
body{{margin:0;background:#f6f5f1;color:#17231d;font:16px/1.6 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:48px 24px}}
h1{{font-size:clamp(34px,6vw,64px);line-height:1.1;letter-spacing:-.04em}}h2{{margin:8px 0 16px}}.lead{{max-width:760px;color:#50635a}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px;margin:32px 0}}.card{{background:white;border:1px solid #dbe2da;border-radius:16px;padding:25px;box-shadow:0 10px 30px #17231d0b}}
.tag{{font-size:13px;color:#136443;font-weight:700}}dl{{display:grid;grid-template-columns:120px 1fr;gap:8px 12px}}dt{{color:#64756c}}dd{{margin:0;overflow-wrap:anywhere}}code,pre{{font:12px/1.5 ui-monospace,monospace}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf2ee;padding:14px;border-radius:8px}}.route{{background:#e4ece5;padding:22px;border-radius:14px;margin:18px 0}}
</style><main><p class="tag">REPOFIX / ARCHIVED EVIDENCE / READ ONLY</p><h1>一次成功，一次失败。</h1>
<p class="lead">本页从冻结的本地评测工件生成，可离线打开。它不表示服务当前在线，不会运行模型，也不改变官方评分。Mini 50 首轮 33/50 resolved，混合 60/100 次调用上限。</p>
<div class="grid">{''.join(cards)}</div>
<h2>工程故障与交付边界</h2>{engineering}
<section class="route"><h2>3 分钟讲述</h2><p>固定 commit → 队列与租约/代次 → 补丁与独立验收 → 人工交付指纹。用成功案例说明正式候选与官方判定，用失败案例说明“编辑过”不等于“已提交”。</p></section>
<section class="route"><h2>8 分钟追问</h2><p>Outbox 解决数据库与消息发布的双写缺口，重复投递仍由幂等认领处理；generation 阻止失联旧 Worker 写回；交付批准绑定补丁哈希与目标指纹，目标变动须重新审查。Reviewer 和上下文管理作为可选能力展示，不宣称稳定提效。</p></section>
<p class="lead">生成于 {esc(generated)}。Mini CSV SHA-256：<code>{esc(source)}</code>；恢复工件 SHA-256：<code>{esc(hashlib.sha256(recovery_path.read_bytes()).hexdigest())}</code>；交付工件 SHA-256：<code>{esc(hashlib.sha256(delivery_path.read_bytes()).hexdigest())}</code>。官方 verdict 来自冻结 manifest 的逐次索引；过程快照未经官方 harness 评分。完整原始轨迹、凭据及环境配置未打包。</p></main></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("mini-csv", "artifact-root", "diagnosis", "recovery", "delivery", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build(args.mini_csv, args.artifact_root, args.diagnosis,
                                 args.recovery, args.delivery), encoding="utf-8")
    print(f"Offline evidence: {args.output}")


if __name__ == "__main__":
    main()
