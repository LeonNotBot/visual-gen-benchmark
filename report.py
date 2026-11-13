# -*- coding: utf-8 -*-
"""读填好的评测记录 Excel，输出 Markdown 报告。
测试人员填完 ②表 后在 Excel 里保存，然后运行这个脚本，再把 report.md 和附件上传到 平台。

用法：
  python report.py --eval 评测记录.xlsx [--out <运行目录>] [--status <status.json>]

输出：运行目录下的 report.md
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_eval(path):
    """读评测记录：③表（样本结论）是公式，openpyxl 只能读缓存值，必须在 Excel 里保存后才有结果"""
    wb = openpyxl.load_workbook(path, data_only=True)

    def sheet(prefix):
        for ws in wb.worksheets:
            if ws.title.startswith(prefix):
                return ws
        raise SystemExit(f"评测记录里找不到以「{prefix}」开头的 Sheet，请确认文件是 v1.5 格式")

    ws2, ws3 = sheet("②"), sheet("③")

    # 找②表表头行（第 4 行是列名）
    head2 = {str(ws2.cell(4, c).value or "").strip(): c for c in range(1, ws2.max_column + 1)}
    need2 = ("样本ID", "任务类型", "级别", "要求内容", "评测维度", "要求结论", "维度等级", "问题描述 / 证据位置")
    for n in need2:
        if n not in head2:
            raise SystemExit(f"②表里找不到列「{n}」")

    # 读②表（从第 5 行开始）
    records = []
    for r in range(5, ws2.max_row + 1):
        sid = ws2.cell(r, head2["样本ID"]).value
        if not sid or str(sid).startswith("示例-"):
            continue
        records.append({n: ws2.cell(r, head2[n]).value for n in need2})

    if not records:
        raise SystemExit("②表里没有数据行（都是示例行，或者还没有填任何内容）")

    # 找③表表头行（第 4 行是列名）
    head3 = {str(ws3.cell(4, c).value or "").strip(): c for c in range(1, ws3.max_column + 1)}
    need3 = ("样本ID", "样本结论", "等级", "总分", "关键要求：没做到", "次要要求：没做到",
             "固定加评：L3", "固定加评：L4")
    for n in need3:
        if n not in head3:
            raise SystemExit(f"③表里找不到列「{n}」，可能文件没在 Excel 里保存过（公式值为空）")

    summaries = {}
    for r in range(5, ws3.max_row + 1):
        sid = ws3.cell(r, head3["样本ID"]).value
        if not sid or str(sid).startswith("示例-"):
            continue
        d = {n: ws3.cell(r, head3[n]).value for n in need3}
        if d["样本结论"] in (None, "", "无记录", "缺固定加评项", "待复核"):
            continue   # 跳过没填完的
        summaries[str(sid)] = d

    return records, summaries


def build_report(records, summaries, eval_path, status_path=None):
    """把读到的数据组装成 Markdown 报告"""
    status = {}
    if status_path and Path(status_path).exists():
        try:
            status = json.loads(Path(status_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    sids = list(summaries)
    total = len(sids)
    grade_map = {"合格": "A", "部分满足": "B/C", "不合格": "D"}

    def v(x, default="—"):
        return str(x) if x not in (None, "") else default

    # ── 总览 ──
    by_verdict = {}
    for sid, s in summaries.items():
        by_verdict.setdefault(str(s["样本结论"]), []).append(sid)
    grade_count = {}
    for sid, s in summaries.items():
        grade_count[str(s.get("等级") or "—")] = grade_count.get(str(s.get("等级") or "—"), 0) + 1
    pass_count = len(by_verdict.get("合格", []))
    partial_count = len(by_verdict.get("部分满足", []))
    fail_count = len(by_verdict.get("不合格", []))
    pending_count = total - pass_count - partial_count - fail_count

    scores = [float(s["总分"]) for s in summaries.values() if s.get("总分") not in (None, "")]
    avg_score = f"{sum(scores) / len(scores):.1f}" if scores else "—"

    lines = [
        "# 图片/视频生成质量评测报告",
        "",
        f"> 评测记录：`{Path(eval_path).name}`  |  生成时间：{ts()}",
        "",
        "## 一、总览",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 样本总数 | {total} |",
        f"| 合格（A 级） | {pass_count} 条（{pass_count*100//total if total else 0}%）|",
        f"| 部分满足（B/C 级） | {partial_count} 条 |",
        f"| 不合格（D 级） | {fail_count} 条 |",
        f"| 待复核或未填 | {pending_count} 条 |",
        f"| 平均总分 | {avg_score} |",
        "",
        "**等级分布**：" + "、".join(f"{k} 级 {v} 条" for k, v in sorted(grade_count.items())) if grade_count else "",
        "",
    ]

    # ── 逐样本 ──
    lines += ["## 二、逐样本结果", ""]
    lines += ["| 样本ID | 结论 | 等级 | 总分 | 没做到的关键要求 | 固定加评问题 |",
              "|---|---|---|---|---|---|"]
    for sid in sids:
        s = summaries[sid]
        missed = v(s["关键要求：没做到"], "0")
        fixed_issue = []
        if s.get("固定加评：L3"): fixed_issue.append(f"L3×{s['固定加评：L3']}")
        if s.get("固定加评：L4"): fixed_issue.append(f"L4×{s['固定加评：L4']}")
        lines.append(f"| {sid} | {v(s['样本结论'])} | {v(s.get('等级'))} | {v(s.get('总分'))} | {missed} 条 | {' '.join(fixed_issue) or '—'} |")
    lines.append("")

    # ── 没做到的要求清单 ──
    unsatisfied = [(r["样本ID"], r["要求内容"], r["评测维度"], r.get("问题描述 / 证据位置", ""))
                   for r in records if str(r.get("要求结论") or "") == "不满足" and str(r.get("级别") or "") == "关键"]
    if unsatisfied:
        lines += ["## 三、关键要求未满足清单", ""]
        lines += ["| 样本ID | 要求内容 | 维度 | 问题描述 |", "|---|---|---|---|"]
        for sid, text, dim, desc in unsatisfied:
            lines.append(f"| {sid} | {str(text or '')[:60]} | {dim or '—'} | {str(desc or '')[:80]} |")
        lines.append("")

    # ── 低质量固定加评 ──
    low_quality = [(r["样本ID"], r["评测维度"], str(r.get("维度等级") or ""), r.get("问题描述 / 证据位置", ""))
                   for r in records if str(r.get("级别") or "") == "固定加评"
                   and str(r.get("维度等级") or "") in ("L3", "L4")]
    if low_quality:
        lines += ["## 四、质量问题清单（L3/L4）", ""]
        lines += ["| 样本ID | 维度 | 等级 | 问题描述 |", "|---|---|---|---|"]
        for sid, dim, grade, desc in low_quality:
            lines.append(f"| {sid} | {dim or '—'} | {grade} | {str(desc or '')[:80]} |")
        lines.append("")

    # ── 高频问题（按维度统计） ──
    from collections import Counter
    dim_fail = Counter(r["评测维度"] for r in records if str(r.get("要求结论") or "") == "不满足")
    if dim_fail:
        lines += ["## 五、高频失败维度（按出现次数）", ""]
        lines += ["| 维度 | 失败次数 |", "|---|---|"]
        for dim, cnt in dim_fail.most_common(10):
            lines.append(f"| {dim} | {cnt} |")
        lines.append("")

    lines += [
        "---",
        "",
        "> 分数权重初始值（见③表右侧）：关键要求权重 2，次要权重 1，要求得分占比 60%，质量得分占比 40%。",
        "> 不合格封顶 59 分，部分满足封顶 89 分。权重尚未用人工数据校准，总分仅供排序参考。",
    ]

    return "\n".join(line for line in lines if line is not None)


def main():
    ap = argparse.ArgumentParser(description="读评测记录，生成 report.md")
    ap.add_argument("--eval", required=True, help="填好的评测记录 Excel 路径")
    ap.add_argument("--out", default="", help="输出目录（默认：评测记录所在的目录）")
    ap.add_argument("--status", default="", help="status.json 路径（可选，用于补充运行信息）")
    a = ap.parse_args()

    eval_path = Path(a.eval).resolve()
    if not eval_path.exists():
        sys.exit(f"找不到评测记录：{eval_path}")
    out_dir = Path(a.out).resolve() if a.out else eval_path.parent

    print(f"读取评测记录：{eval_path.name} ……", flush=True)
    try:
        records, summaries = read_eval(eval_path)
    except SystemExit as e:
        sys.exit(str(e))

    if not summaries:
        sys.exit("③表里没有有效的样本结论（公式值为空，或都是待复核）。"
                 "请先在 Excel 里填完②表并保存，再运行本脚本。")

    print(f"读取成功：{len(summaries)} 个样本有结论，{len(records)} 行评测记录。", flush=True)
    md = build_report(records, summaries, eval_path, a.status or None)

    out = out_dir / "report.md"
    out.write_text(md, encoding="utf-8")
    print(f"✅ 报告已生成：{out}")
    print(f"   接下来把 report.md + 评测记录.xlsx 上传到 评测平台。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
