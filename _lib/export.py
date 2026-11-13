"""导出：把拆解好的要求按“一条要求一行”填进 ②，样本ID 填进 ③。
两种方式：生成新文件（复制评测体系 Excel，原文件不动）；追加到导出文件夹里已有的评测记录（先备份，再接着往下写）。"""
import copy
import os
import re
import shutil
import time
import warnings

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from rules import NO_DIM

warnings.filterwarnings("ignore", message="Data Validation extension")
FIRST, LAST = 5, 2000          # ③表公式统计②表第 5–2000 行
TASK_LIST = '"文生图,图片编辑,文生视频,图生视频,参考生视频,视频编辑,视频延长"'


class ExportError(Exception):
    pass


def _sheet(wb, prefix):
    for ws in wb.worksheets:
        if ws.title.startswith(prefix):
            return ws
    raise ExportError(f"评测体系文件里找不到以「{prefix}」开头的 Sheet")


def sample_ids(it):
    return [f"{it.get('prefix') or it['code']}-{k:02d}" for k in range(1, int(it.get("n_samples") or 1) + 1)]


def check_item(it, lib):
    """导出前检查，返回 (错误, 提醒)。有错误就不导出"""
    errs, warns, code = [], [], it["code"]
    if not (it.get("prompt") or "").strip():
        errs.append(f"{code}：提示词是空的")
    if not it.get("requirements"):
        errs.append(f"{code}：还没有任何要求（先自动拆解，或手动加行）")
    for i, r in enumerate(it.get("requirements") or [], 1):
        tag, d = f"{code} R{i:02d}", r.get("dimension")
        if not str(r.get("text") or "").strip():
            errs.append(f"{tag}：要求内容是空的")
        if r.get("level") not in ("关键", "次要"):
            errs.append(f"{tag}：级别只能是“关键”或“次要”")
        if not d:
            errs.append(f"{tag}：没选维度")
        elif d == NO_DIM:
            warns.append(f"{tag}：没有对应维度，评测时直接判满足/不满足")
        elif d not in lib:
            errs.append(f"{tag}：维度「{d}」既不在维度库，也不是自定义维度")
    if it.get("status") != "已确认":
        warns.append(f"{code}：还没标记为“已确认”")
    if not any(f["on"] for f in it.get("fixed") or []):
        warns.append(f"{code}：没勾任何固定加评项，③表会显示“缺固定加评项”")
    return errs, warns


def _rows(it):
    prompt = it["prompt"] + (f"\n【反向提示词】{it['negative']}" if it.get("negative") else "")
    lines = [(f"R{i:02d}", q["text"] + (f"\n【判定要点】{q['note']}" if q.get("note") else ""), q["level"], q["dimension"])
             for i, q in enumerate(it["requirements"], 1)]
    lines += [("固定", "（每次都评）", "固定加评", f["dim"]) for f in it.get("fixed") or [] if f["on"]]
    lines += [("固定", "（满足条件才评）", "固定加评", c["dim"]) for c in it.get("conditional") or [] if c["on"]]
    rows = []
    for sid in sample_ids(it):
        for k, (no, text, level, dim) in enumerate(lines):
            rows.append([sid, it["task"] if k == 0 else None, prompt if k == 0 else None, no, text, level, dim] + [None] * 6)
    return rows


def _height(ws, values, sz=10):
    lines = 1
    for c, v in enumerate(values, 1):
        if v is None:
            continue
        w = max((ws.column_dimensions[get_column_letter(c)].width or 8.43) - 1.5, 2)
        def n(part):
            return max(1, -(-int(sum(sz * (0.205 if ord(ch) > 0x2E80 else 0.115) for ch in part) * 10) // int(w * 10)))
        lines = max(lines, sum(n(p) for p in str(v).split("\n")))
    return round(lines * sz * 1.75 + 7, 1)


def _last_used(ws, cols, first=FIRST):
    used = [r for r in range(first, ws.max_row + 1) if any(ws.cell(r, c).value not in (None, "") for c in cols)]
    return max(used, default=first - 1)


def _add_customs(dl, customs, used):
    added = []
    nums = [int(v) for v in (str(dl.cell(r, 2).value or "") for r in range(4, dl.max_row + 1)) if v.isdigit()]
    no, style = max(nums, default=0), [copy.copy(dl.cell(dl.max_row, c)._style) for c in range(1, 12)]
    for cu in customs:
        if cu["name"] not in used or cu["name"] in {dl.cell(x, 3).value for x in range(4, dl.max_row + 1)}:
            continue
        no, r = no + 1, dl.max_row + 1
        lv = " | ".join(f"{k}={v}" for k, v in (cu.get("levels") or {}).items() if v)
        vals = ["自定义", str(no), cu["name"], "自定义（提示词拆解助手）", cu.get("definition", ""), cu.get("checks", ""), "人工",
                (lv + "（自定义，待校准）") if lv else "（自定义，未写等级标准）", "", "要求类（由提示词或请求参数触发）", "—"]
        for c, v in enumerate(vals, 1):
            dl.cell(r, c).value = v
            dl.cell(r, c)._style = copy.copy(style[c - 1])
        added.append(cu["name"])
    dl.auto_filter.ref = f"A3:{get_column_letter(dl.max_column)}{dl.max_row}"
    return added


def _trace_sheet(wb, items, rules_version):
    if "拆解记录" in wb.sheetnames:
        ws = wb["拆解记录"]
        r = max((x for x in range(4, ws.max_row + 1) if ws.cell(x, 1).value not in (None, "")), default=3) + 1
    else:
        ws, r = _new_trace(wb), 4
    _write_trace(ws, r, items, rules_version)


def _new_trace(wb):
    ws = wb.create_sheet("拆解记录")
    ws.sheet_properties.tabColor = "BFBFBF"
    ws["A1"] = "拆解记录：每条要求是模型给的还是人工改的（用于检查拆解质量，不参与③表计算）"
    ws["A1"].font = Font(name="微软雅黑", sz=12, b=True)
    heads = ["提示词编号", "样本ID", "任务类型", "状态", "要求编号", "要求内容", "提示词原文片段", "维度", "级别", "判定要点",
             "来源", "人工改了什么", "模型", "规则版本", "拆解时间"]
    for c, (h, w) in enumerate(zip(heads, (10, 16, 9, 8, 8, 28, 26, 16, 7, 24, 14, 12, 16, 12, 18)), 1):
        cell = ws.cell(3, c, h)
        cell.font, cell.fill = Font(name="微软雅黑", sz=10, b=True), PatternFill("solid", start_color="D9E1F2", end_color="D9E1F2")
        ws.column_dimensions[get_column_letter(c)].width = w
    return ws


def _write_trace(ws, r, items, rules_version):
    r0 = r
    for it in items:
        sids, llm = sample_ids(it), it.get("llm") or {}
        base = [it["code"], sids[0] if len(sids) == 1 else f"{sids[0]} … {sids[-1]}", it["task"], it.get("status")]
        tail = [llm.get("model", ""), llm.get("rules_version", rules_version), llm.get("time", "")]
        for i, q in enumerate(it["requirements"], 1):
            m = q.get("model") or {}
            changed = [n for k, n in (("text", "内容"), ("dimension", "维度"), ("level", "级别")) if m and m.get(k) != q.get(k)]
            src = "人工新增" if q.get("origin") != "模型" else ("模型（人工改过）" if changed else "模型")
            vals = base + [f"R{i:02d}", q["text"], q.get("source", ""), q["dimension"], q["level"], q.get("note", ""),
                           src, "、".join(changed)] + tail
            for c, v in enumerate(vals, 1):
                ws.cell(r, c, v).font = Font(name="微软雅黑", sz=10)
            r += 1
        for kind, lst in (("未拆", llm.get("not_split") or []), ("模型的疑问", llm.get("questions") or [])):
            for x in lst:
                for c, v in enumerate(base + [kind, x], 1):
                    ws.cell(r, c, v).font = Font(name="微软雅黑", sz=10, color="7F7F7F")
                r += 1
    for row in ws.iter_rows(min_row=r0, max_row=max(r - 1, r0)):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes, ws.auto_filter.ref = "B4", f"A3:O{max(r - 1, 3)}"


def list_exports(out_dir):
    """导出文件夹里可以追加的评测记录（新的在前）"""
    if not os.path.isdir(out_dir):
        return []
    fs = [f for f in os.listdir(out_dir) if f.lower().endswith(".xlsx") and not f.startswith("~$")]
    fs.sort(key=lambda f: os.path.getmtime(os.path.join(out_dir, f)), reverse=True)
    return [{"name": f, "time": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(os.path.join(out_dir, f))))} for f in fs]


def _append_target(out_dir, append_to):
    name = os.path.basename(str(append_to))
    path = os.path.join(out_dir, name)
    if not name.lower().endswith(".xlsx") or not os.path.isfile(path):
        raise ExportError(f"导出文件夹里没有「{name}」，请重新选择要追加的文件")
    if os.path.exists(os.path.join(out_dir, "~$" + name)):
        raise ExportError(f"「{name}」正在 Excel 里打开，先保存并关掉它再追加")
    return path


def export(items, rules, customs, rules_path, out_dir, keep_examples=False, write_customs=True, file_name="", append_to=""):
    if not items:
        raise ExportError("没有选中任何提示词")
    lib = {d["name"] for d in rules["dims"]} | {c["name"] for c in customs}
    errs, warns = [], []
    for it in items:
        e, w = check_item(it, lib)
        errs, warns = errs + e, warns + w
    if errs:
        raise ExportError("导出前请先改好这些地方：\n" + "\n".join(errs[:40]))
    sids = [(sid, it) for it in items for sid in sample_ids(it)]
    dup = sorted({s for s, _ in sids if [x for x, _ in sids].count(s) > 1})
    if dup:
        raise ExportError(f"样本ID 重复：{'、'.join(dup[:10])}。请给这些提示词设不同的样本ID前缀")

    target = _append_target(out_dir, append_to) if append_to else None
    wb = openpyxl.load_workbook(target or rules_path)
    rec, summ, dl = _sheet(wb, "②"), _sheet(wb, "③"), _sheet(wb, "维度库")
    if target:   # 追加：目标文件的维度库可能是旧版本，用到的维度它得有
        have = {dl.cell(r, 3).value for r in range(4, dl.max_row + 1)} | ({c["name"] for c in customs} if write_customs else set())
        miss = sorted({q["dimension"] for it in items for q in it["requirements"] if q["dimension"] not in have and q["dimension"] != NO_DIM}
                      | {f["dim"] for it in items for f in (it.get("fixed") or []) + (it.get("conditional") or []) if f["on"] and f["dim"] not in have})
        if miss:
            raise ExportError(f"「{os.path.basename(target)}」的维度库里没有：{'、'.join(miss)}。它可能是用旧版评测体系导出的，请改为生成新文件")
        keep_examples = True   # 追加时不动文件里已有的内容
    style = [copy.copy(rec.cell(rec.max_row, c)._style) for c in range(1, 14)]
    removed = 0
    note_col = next((c for c in range(2, summ.max_column + 1) if summ.cell(4, c).value == "备注"), 11)   # v1.4 在 K 列，v1.5 在 O 列
    if not keep_examples:
        for r in range(FIRST, rec.max_row + 1):
            if str(rec.cell(r, 1).value or "").startswith("示例-"):
                for c in range(1, 14):
                    rec.cell(r, c).value, rec.cell(r, c)._style = None, copy.copy(style[c - 1])
                rec.row_dimensions[r].height, removed = None, removed + 1
        for r in range(FIRST, summ.max_row + 1):
            if str(summ.cell(r, 1).value or "").startswith("示例-"):
                summ.cell(r, 1).value = summ.cell(r, note_col).value = None
    existing = {rec.cell(r, 1).value for r in range(FIRST, rec.max_row + 1)} - {None}
    clash = [s for s, _ in sids if s in existing]
    if clash:
        where = f"「{os.path.basename(target)}」" if target else "评测体系文件"
        raise ExportError(f"{where}的②表里已有这些样本ID：{'、'.join(clash[:10])}。"
                          + ("这些提示词可能已经追加过；要再加一份请换个样本ID前缀" if target else "请换个前缀"))
    rows = [x for it in items for x in _rows(it)]
    start = _last_used(rec, range(1, 14)) + 1
    if start + len(rows) - 1 > LAST:
        raise ExportError(f"这次要写 {len(rows)} 行，超过②表上限（③表公式只统计到第 {LAST} 行），请分批导出")
    formula_rows = [r for r in range(FIRST, summ.max_row + 1) if str(summ.cell(r, 2).value or "").startswith("=")]
    s0 = _last_used(summ, (1,)) + 1
    if not formula_rows or s0 + len(sids) - 1 > max(formula_rows):
        raise ExportError(f"这次有 {len(sids)} 个样本，超过③表的公式行数（到第 {max(formula_rows, default=0)} 行），请分批导出")

    for i, vals in enumerate(rows):
        for c, v in enumerate(vals, 1):
            cell = rec.cell(start + i, c)
            cell.value, cell._style = v, copy.copy(style[c - 1])
        rec.row_dimensions[start + i].height = _height(rec, vals)
    for i, (sid, it) in enumerate(sids):
        summ.cell(s0 + i, 1).value = sid
        summ.cell(s0 + i, note_col).value = f"{it['code']}：{it['prompt'][:30]}"
    used = {q["dimension"] for it in items for q in it["requirements"]}
    added = _add_customs(dl, customs, used) if write_customs else []
    rec.data_validations.dataValidation = []
    dims_dv = f"=OFFSET('{dl.title}'!$C$4,0,0,COUNTA('{dl.title}'!$C$4:$C$1000),1)"   # 维度库增删行后下拉自动跟着变
    for col, f in (("B", TASK_LIST), ("F", '"关键,次要,固定加评"'), ("G", dims_dv),
                   ("H", '"满足,不满足,待复核"'), ("I", '"L1,L2,L3,L4,待复核"')):
        dv = DataValidation(type="list", formula1=f, allow_blank=True)
        dv.add(f"{col}{FIRST}:{col}{LAST}")
        rec.add_data_validation(dv)
    _trace_sheet(wb, items, rules.get("version", ""))
    wb.calculation.fullCalcOnLoad = True
    wb.active = wb.worksheets.index(rec)
    for ws in wb.worksheets:
        ws.sheet_view.tabSelected = ws is rec

    os.makedirs(out_dir, exist_ok=True)
    if target:
        name, path = os.path.basename(target), target
        bdir = os.path.join(out_dir, "备份")
        os.makedirs(bdir, exist_ok=True)
        backup = os.path.join(bdir, f"{os.path.splitext(name)[0]}_追加前_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        shutil.copy2(target, backup)
        wb.save(path)
        return {"path": path, "file": name, "rows": len(rows), "samples": len(sids), "warnings": warns,
                "customs_added": added, "examples_removed": 0, "appended": True, "backup": backup}
    name = re.sub(r'[\/:*?"<>|\r\n]', "", file_name or "").strip() or f"评测记录_{time.strftime('%Y%m%d_%H%M%S')}"
    name += "" if name.lower().endswith(".xlsx") else ".xlsx"
    path = os.path.join(out_dir, name)
    if os.path.abspath(path) == os.path.abspath(rules_path):
        raise ExportError("不能覆盖评测体系原文件")
    if os.path.exists(path):
        raise ExportError(f"已有同名文件：{name}，请换个文件名")
    wb.save(path)
    return {"path": path, "file": name, "rows": len(rows), "samples": len(sids), "warnings": warns,
            "customs_added": added, "examples_removed": removed, "appended": False}
