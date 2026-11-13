"""从评测体系 Excel 读取拆解规则：维度库 + ①表 A–E 部分。Excel 改了，这里自动跟着变（按文件修改时间重新读取）。"""
import hashlib
import json
import os
import re
import threading
import warnings

import openpyxl

warnings.filterwarnings("ignore", message="Data Validation extension")
IMAGE_TASKS = ("文生图", "图片编辑")
NO_DIM = "暂无对应维度"


class RulesError(Exception):
    pass


def _s(v):
    return "" if v is None else str(v).strip()


def _find_sheet(wb, prefix):
    for ws in wb.worksheets:
        if ws.title.startswith(prefix):
            return ws
    raise RulesError(f"Excel 里找不到以「{prefix}」开头的 Sheet")


def _section(ws, letter):
    """①表中「A.」「B.」…某一部分标题之后、下一部分标题之前的非空行"""
    start = next((r for r in range(1, ws.max_row + 1) if _s(ws.cell(r, 1).value).startswith(f"{letter}.")), None)
    if start is None:
        raise RulesError(f"「{ws.title}」里找不到 {letter} 部分")
    rows = []
    for r in range(start + 1, ws.max_row + 1):
        if re.match(r"^[A-Z]\.", _s(ws.cell(r, 1).value)):
            break
        vals = [_s(ws.cell(r, c).value) for c in range(1, 6)]
        if any(vals):
            rows.append(vals)
    return rows


def dim_names_in(text):
    """'主体还原度（主体）/背景还原度（场景元素）' → ['主体还原度', '背景还原度']"""
    out = []
    for part in re.split(r"[/／；;\n]", text or ""):
        base = re.split(r"[（(]", part)[0].strip().lstrip("+").strip()
        if base and base not in ("—", "同左", NO_DIM):
            out.append(base)
    return out


def _read_dims(ws):
    hdr = next((r for r in range(1, 10) if _s(ws.cell(r, 3).value) == "维度名"), None)
    if hdr is None:
        raise RulesError("维度库里找不到“维度名”表头")
    heads = {_s(ws.cell(hdr, c).value): c for c in range(1, ws.max_column + 1)}
    want = {"module": "模块", "no": "序号", "name": "维度名", "source": "Benchmark", "definition": "定义",
            "checks": "检查项", "tools": "判定工具", "levels": "等级映射", "example": "示例",
            "usage": "单样本流程", "inputs": "除提示词"}
    cols = {k: next((c for h, c in heads.items() if h.startswith(p)), None) for k, p in want.items()}
    dims = []
    for r in range(hdr + 1, ws.max_row + 1):
        name = _s(ws.cell(r, cols["name"]).value)
        if name:
            d = {k: (_s(ws.cell(r, c).value) if c else "") for k, c in cols.items()}
            d["row"] = r
            dims.append(d)
    return dims


def _read_fixed(rows):
    """C 部分：任务类型 → 每次都评 / 满足条件才评 / 条件说明。“同文生视频的全部必评项”展开成具体维度"""
    raw = {}
    for task, fixed, cond, when, _ in rows[1:]:
        if task:
            raw[task] = {"fixed": fixed, "conditional": cond, "condition": when}
    def expand(task, seen=()):
        items = []
        for line in raw[task]["fixed"].split("\n"):
            line = line.strip().lstrip("+").strip()
            m = re.match(r"^同(.+?)的全部必评项", line)
            if m and m.group(1) in raw and m.group(1) not in seen:
                items += expand(m.group(1), seen + (task,))
            elif line and not line.endswith("：") and not m:
                items += dim_names_in(line)
        return list(dict.fromkeys(items))
    return {t: {"fixed": expand(t), "conditional": dim_names_in(v["conditional"]), "condition": v["condition"]}
            for t, v in raw.items()}


def load_rules(path):
    """读取整份规则。返回的 dict 可直接转 JSON 给前端，也用来拼大模型提示词"""
    if not os.path.exists(path):
        raise RulesError(f"找不到评测体系文件：{path}")
    wb = openpyxl.load_workbook(path, data_only=True)
    dl, rt = _find_sheet(wb, "维度库"), _find_sheet(wb, "①")
    dims = _read_dims(dl)
    names = {d["name"] for d in dims}

    levels = {r[0]: r[1] for r in _section(rt, "A") if r[0]}
    route, group = [], ""
    for r in _section(rt, "B")[1:]:
        if r[0] and not any(r[1:]):
            group = r[0]
            continue
        route.append({"group": group, "element": r[0], "image": r[1], "video": r[2], "level": r[3], "note": r[4]})
    fixed = _read_fixed(_section(rt, "C"))
    rules = [{"title": r[0], "text": r[1]} for r in _section(rt, "D") if r[0]]

    e = _section(rt, "E")
    example = {"prompt": next((r[1] for r in e if r[0] == "原提示词"), ""), "items": [], "notes": []}
    for r in e:
        m = re.match(r"^(R\d+)\s+(.+)$", r[0])
        if m:
            example["items"].append({"id": m.group(1), "text": m.group(2), "dim": r[1], "level": r[3], "note": r[4]})
        elif r[0].startswith(("没拆的部分", "如果")):
            example["notes"].append(r[0])

    problems = []
    for x in route:
        for k in ("image", "video"):
            problems += [f"①B「{x['element']}」引用了维度库中没有的「{n}」" for n in dim_names_in(x[k]) if n not in names]
    for t, v in fixed.items():
        problems += [f"①C「{t}」引用了维度库中没有的「{n}」" for n in v["fixed"] + v["conditional"] if n not in names]
    mx = next((ws for ws in wb.worksheets if ws.title.startswith("对照矩阵")), None)
    if mx is not None:   # 验证点用到的核心 / 辅助维度（删维度时最容易漏改的地方）
        hdr = next((r for r in range(1, 10) if any(_s(c.value).startswith("核心") for c in mx[r])), None)
        cols = [c.column for c in mx[hdr] if _s(c.value).startswith(("核心", "辅助"))] if hdr else []
        for r in range(hdr + 1 if hdr else 1, mx.max_row + 1):
            for c in cols:
                for n in re.split(r"[\n/、]", _s(mx.cell(r, c).value)):
                    n = n.strip()
                    if n and n not in ("无", "—") and n not in names:
                        problems.append(f"对照矩阵「{_s(mx.cell(r, 1).value)}」引用了维度库中没有的「{n}」")
    if not route or not fixed or not example["items"]:
        problems.append("①表 B/C/E 部分没读到内容，Excel 结构可能被改动")
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()[:12]
    return {"source": os.path.basename(path), "version": digest, "dims": dims, "levels": levels, "route": route,
            "fixed": fixed, "rules": rules, "example": example, "tasks": list(fixed), "problems": problems}


class RulesCache:
    """文件修改时间变了就重新读，保证 Excel 改动即时生效"""

    def __init__(self, path):
        self.path, self._mtime, self._rules, self._lock = path, None, None, threading.Lock()

    def get(self):
        with self._lock:
            mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else None
            if self._rules is None or mtime != self._mtime:
                self._rules, self._mtime = load_rules(self.path), mtime
            return self._rules


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    r = load_rules(sys.argv[1])
    print(json.dumps({k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in r.items()}, ensure_ascii=False))
    print("fixed:", json.dumps(r["fixed"], ensure_ascii=False)[:600])
    print("example items:", len(r["example"]["items"]), r["example"]["items"][0], r["example"]["notes"])
    print("problems:", r["problems"])
