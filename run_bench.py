# -*- coding: utf-8 -*-
"""评测主入口：依次完成 优化 → 生成 → 拆解 → 预填评测表，每完成一条更新进度文件，最后输出生成情况报告。
断点后重跑同一命令自动续跑，不重复提交。

用法：
  python run_bench.py --file 提示词测试集.xlsx --out <输出目录>
                      [--ids IMG-01,VID-01]  [--tags 文生视频]
                      [--timeout 1800]

输出目录结构（由 --out 指定）：
  status.json          进度文件
  outputs/             生成结果（图片/视频）
  评测记录.xlsx         预填好的 v1.5 评测表（测试人员填 H/I 列后运行 report.py）
  生成情况.md          阶段1产物，每条用例的生成结果摘要
  _任务记录.json       内部：断点续跑用的任务号记录

退出码：
  0  全部成功（可能有个别用例生成失败，见 status.json 里的 failed 字段）
  1  未预料的异常
  2  启动检查失败（配置错误、文件打不开等）
  3  额度不足，中止
  4  超过 --timeout，还有未完成的任务（再次运行同一命令可续跑）
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = Path(__file__).resolve().parent
DEFAULT_FILE = ROOT / "testset" / "提示词测试集.xlsx"
MODELS_DIR = SCRIPTS / "models"
DEFAULT_MODELS = {"图片": "qwen_image_2_1", "视频": "minimax_h3"}
SPLIT = re.compile(r"[;；\n]+")


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, prefix=""):
    print(f"{prefix}{msg}", flush=True)


# ---------- 进度文件 ----------
def load_status(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_status(path, state, total, done, failed, cases, extra=None):
    """写进度文件"""
    obj = load_status(path)   # 保留之前写入的字段（模型、开始时间等）
    obj.update({
        "state": state,
        "progress": {"total": total, "done": done, "failed": failed},
        "message": f"已完成 {done}/{total} 条" + (f"，失败 {failed} 条" if failed else ""),
        "updated_at": ts(),
        "cases": cases,
    })
    obj.update(extra or {})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------- 调用子脚本 ----------
def run_script(script, args, timeout=None, extra_env=None):
    """调用同目录下的子脚本，继承 stdout/stderr；返回退出码"""
    cmd = [sys.executable, "-I", str(SCRIPTS / script)] + args
    env = dict(os.environ, PYTHONIOENCODING="utf-8", **(extra_env or {}))
    try:
        r = subprocess.run(cmd, env=env, timeout=timeout)
        return r.returncode
    except subprocess.TimeoutExpired:
        return 4


def read_testset(path, only_ids, tags):
    """读测试集，返回 [(row_dict)] 按筛选条件过滤后的用例列表"""
    import openpyxl
    wb = openpyxl.load_workbook(path)
    ws = wb["提示词"] if "提示词" in wb.sheetnames else wb.active
    head = {}
    for r in range(1, 6):
        cols = {str(c.value).strip(): c.column for c in ws[r] if c.value}
        if "用例ID" in cols:
            head = cols
            start_row = r + 1
            break
    if not head:
        raise SystemExit("找不到「用例ID」列，确认用的是 提示词测试集 模板")
    rows = []
    for r in range(start_row, ws.max_row + 1):
        row = {name: ws.cell(r, c).value for name, c in head.items()}
        row = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        if not row.get("用例ID") or not row.get("初始提示词"):
            continue
        if only_ids and row["用例ID"] not in only_ids:
            continue
        if tags:   # 任务类型本身也算一个标签，所以 --tags 文生视频 不用另外打标签
            raw = f"{row.get('标签') or ''};{row.get('任务类型') or ''}".replace("；", ";")
            if not tags & {t.strip() for t in raw.split(";") if t.strip()}:
                continue
        rows.append(row)
    return rows


def find_header(ws):
    """返回 (表头所在行, {列名: 列号})；测试集第 1 行是说明，表头一般在第 2 行"""
    for r in range(1, 6):
        cols = {str(c.value).strip(): c.column for c in ws[r] if c.value}
        if "用例ID" in cols:
            return r, cols
    raise SystemExit("找不到「用例ID」列，确认用的是 提示词测试集 模板")


def read_prompts(xlsx_path):
    """读测试集，返回 {用例ID: 中文摘要}，用于核对评测表的提示词是否匹配"""
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb["提示词"] if "提示词" in wb.sheetnames else wb.active
    hr, col = find_header(ws)
    prompts = {}
    for r in range(hr + 1, ws.max_row + 1):
        cid = ws.cell(r, col.get("用例ID", 1)).value
        # 优先用中文摘要（评测表 ② 的「提示词/指令」列就是中文摘要），如果没有就用优化后提示词
        summary = ws.cell(r, col.get("中文摘要", 1)).value
        opt = ws.cell(r, col.get("优化后提示词", 1)).value
        if cid and (summary or opt):
            prompts[str(cid)] = str(summary or opt).strip()
    return prompts


def link_eval_results(eval_xlsx, cases, prompts, out_dir):
    """评测记录.xlsx 的 ②表：核对样本ID和本次生成的用例ID是否一一对应，并加「结果文件」列（HTTP 超链接）。
    样本ID = 用例ID + "-" + 样本序号（IMG-01-01 → 用例 IMG-01 → outputs\\<模型ID>\\IMG-01.png）
    prompts = {用例ID: 中文摘要}，用于核对评测表的「提示词/指令」列（就是中文摘要）是否和测试集一致"""
    import openpyxl
    from openpyxl.styles import Font
    base_url = os.environ.get("EVAL_FILE_URL", "").rstrip("/")
    wb = openpyxl.load_workbook(eval_xlsx)
    if "②逐行评测记录" not in wb.sheetnames:
        log("⚠ 评测表没有「②逐行评测记录」表，跳过")
        return
    ws = wb["②逐行评测记录"]
    hr = next((r for r in range(1, 8) if ws.cell(r, 1).value == "样本ID"), None)
    if not hr:
        log("⚠ ②表找不到「样本ID」表头，跳过")
        return
    cols = {str(c.value).strip(): c.column for c in ws[hr] if c.value}
    # 评测人员已开始填写时不再改写：openpyxl 保存会丢掉 Excel 加的部分下拉框和公式缓存值（report.py 读缓存值）
    filled = [cols[k] for k in ("要求结论", "维度等级") if k in cols]
    if any(ws.cell(r, c).value not in (None, "") for r in range(hr + 1, ws.max_row + 1) for c in filled):
        log("评测表已开始填写，不再改动；需要链接时看 生成情况.md")
        return
    if "结果文件" in cols:
        rc = cols["结果文件"]
    else:
        rc = max(cols.values()) + 1
        h = ws.cell(hr, rc, "结果文件")
        left = ws.cell(hr, rc - 1)
        from copy import copy
        h.font, h.fill, h.border, h.alignment = (copy(left.font), copy(left.fill),
                                                 copy(left.border), copy(left.alignment))
        ws.column_dimensions[openpyxl.utils.get_column_letter(rc)].width = 16

    in_table, not_run, mismatch, no_file = set(), [], [], []
    added = 0
    for r in range(hr + 1, ws.max_row + 1):
        sid = ws.cell(r, 1).value
        if not isinstance(sid, str) or "-" not in sid:
            continue
        cid = sid.strip().rsplit("-", 1)[0]
        in_table.add(cid)
        if cid not in cases:
            if cid not in not_run:
                not_run.append(cid)
            continue
        # 核对提示词：评测表「提示词/指令」列 = 测试集「中文摘要」列
        eval_prompt = ws.cell(r, 3).value  # C列
        if eval_prompt and cid in prompts:
            # 只比前 100 字符，避免换行符、空格差异导致误报
            if str(eval_prompt).strip()[:100] != prompts[cid][:100] and cid not in mismatch:
                mismatch.append(cid)
        rel = cases[cid].get("result_file", "")
        if not rel or not (out_dir / rel).exists():
            if cid not in no_file:
                no_file.append(cid)
            continue
        cell = ws.cell(r, rc, Path(rel).name)
        if base_url:
            cell.hyperlink = f"{base_url}/{urllib.parse.quote(out_dir.name)}/{rel.replace(chr(92), '/')}"
            cell.font = Font(color="0563C1", underline="single")
        added += 1
    not_in_table = [c for c in cases if c not in in_table]

    wb.save(eval_xlsx)
    log(f"评测表 ②表已填「结果文件」列：{added} 行" + ("（可点击）" if base_url else
        "（未设置 EVAL_FILE_URL，只写了文件名，不可点击）"))
    if not_run:
        log(f"⚠ 评测表里有、本次没跑的用例：{', '.join(not_run)}（用了 --ids/--tags 筛选时属正常）")
    if not_in_table:
        log(f"⚠ 本次生成了、但评测表里没有的用例：{', '.join(not_in_table)}（需在源机重新运行 auto_decompose.py）")
    if no_file:
        log(f"⚠ 没有结果文件的用例：{', '.join(no_file)}（生成失败或未完成）")
    if mismatch:
        log(f"⚠ 评测表里的提示词和测试集不一致：{', '.join(mismatch)}（评测表可能是旧版测试集拆的，需重新拆解）")


def prepare_work_copy(src, dst):
    """把测试集复制成这次运行的工作副本：生成相关的列清空，参考图改成绝对路径。
    原测试集不动，所以换模型重跑不会被「已完成」挡住，也不会覆盖别的模型的结果。"""
    import openpyxl
    shutil.copy2(src, dst)
    wb = openpyxl.load_workbook(dst)
    ws = wb["提示词"] if "提示词" in wb.sheetnames else wb.active
    hr, col = find_header(ws)
    for name in ("生成状态", "结果文件", "失败原因"):
        if name not in col:
            raise SystemExit(f"测试集缺少「{name}」列")
    for r in range(hr + 1, ws.max_row + 1):
        if not ws.cell(r, col["用例ID"]).value:
            continue
        ws.cell(r, col["生成状态"]).value = "待生成"
        ws.cell(r, col["结果文件"]).value = None
        ws.cell(r, col["失败原因"]).value = None
        cell = ws.cell(r, col["参考图"]) if "参考图" in col else None
        if cell is not None and cell.value:
            parts = [x.strip().strip('"') for x in SPLIT.split(str(cell.value)) if x.strip()]
            cell.value = ";".join(x if Path(x).is_absolute() else str(src.parent / x) for x in parts)
    wb.save(dst)


def model_info(mid):
    """模型 ID → 报告里的一行说明：名称、平台和主要参数"""
    p = MODELS_DIR / f"{mid}.json"
    if not p.exists():
        return f"{mid}（找不到 models\\{mid}.json）"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    params = "，".join(f"{k}={v}" for k, v in cfg.items() if k not in ("_comment", "id", "name", "task_types"))
    return f"{cfg.get('name', mid)}（{mid}；{params}）"


def result_link(out_dir, rel):
    """结果文件 → 报告里的链接。设置了 EVAL_FILE_URL（文件服务地址，服务的是 runs 目录）就给可点击的网址"""
    base = os.environ.get("EVAL_FILE_URL", "").rstrip("/")
    if not rel:
        return ""
    if not base:
        return f"`{rel}`"
    url = f"{base}/{urllib.parse.quote(out_dir.name)}/{urllib.parse.quote(rel.replace(chr(92), '/'))}"
    return f"[{Path(rel).name}]({url})"


def build_report(cases, out_dir, testset_name, duration_s, models):
    """生成情况.md：每条用例的状态摘要"""
    done = [c for c in cases.values() if c["gen_status"] == "已完成"]
    fail = [c for c in cases.values() if c["gen_status"] == "失败"]
    lines = [
        f"# 生成情况报告",
        f"",
        f"| 项目 | 内容 |",
        f"|---|---|",
        f"| 测试集 | {testset_name} |",
        f"| 图片模型 | {model_info(models['图片'])} |",
        f"| 视频模型 | {model_info(models['视频'])} |",
        f"| 生成时间 | {ts()} |",
        f"| 总用时 | {duration_s // 60:.0f} 分 {duration_s % 60:.0f} 秒 |",
        f"| 用例总数 | {len(cases)} |",
        f"| 已完成 | {len(done)} |",
        f"| 失败 | {len(fail)} |",
        f"",
    ]
    if done:
        lines += ["## 已完成的用例", ""]
        lines += ["| 用例ID | 任务类型 | 使用模型 | 结果文件 | 生成耗时 |", "|---|---|---|---|---|"]
        for c in done:
            ms = c.get("e2e_ms")
            t = f"{ms//60000:.0f} 分 {ms%60000//1000:.0f} 秒" if ms else "—"
            mname = c.get("model_name", "—")
            lines.append(f"| {c['id']} | {c['task']} | {mname} | {result_link(out_dir, c.get('result_file', ''))} | {t} |")
        lines.append("")
    if fail:
        lines += ["## 失败的用例", ""]
        lines += ["| 用例ID | 任务类型 | 使用模型 | 失败原因 |", "|---|---|---|---|"]
        for c in fail:
            mname = c.get("model_name", "—")
            lines.append(f"| {c['id']} | {c['task']} | {mname} | {c.get('fail_reason', '')} |")
        lines.append("")
    lines += [
        "---",
        "",
        "> 本文件是阶段 1（生成）的产物，由 run_bench.py 自动生成。",
        "> 评测记录（含评分结果）在同目录的 `评测记录.xlsx`，报告在 `report.md`。",
    ]
    (out_dir / "生成情况.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="评测主入口：优化→生成→拆解→预填评测表")
    ap.add_argument("--file", default=str(DEFAULT_FILE), help="提示词测试集 Excel 路径")
    ap.add_argument("--out", required=True, help="运行输出目录")
    ap.add_argument("--ids", default="", help="只跑这些用例ID，逗号分隔")
    ap.add_argument("--tags", default="", help="只跑包含这些标签的用例，逗号分隔（标签在「备注」列，分号隔开）")
    ap.add_argument("--timeout", type=int, default=1800, help="生成阶段最多等多少秒（默认 1800）")
    ap.add_argument("--model-img", default="", help="图片生成模型 ID（默认 qwen_image_2_1）")
    ap.add_argument("--model-vid", default="", help="视频生成模型 ID（默认 minimax_h3）")
    ap.add_argument("--skip-optimize", action="store_true", help="跳过优化步骤（优化过的行本来就会自动跳过）")
    ap.add_argument("--skip-generate", action="store_true", help="跳过生成步骤（用于只重新拆解）")
    ap.add_argument("--skip-decompose", action="store_true", help="跳过拆解步骤（用于只生成）")
    a = ap.parse_args()

    path = Path(a.file).resolve()
    if not path.exists():
        log(f"找不到测试集：{path}", "✖ "); sys.exit(2)

    out_dir = Path(a.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "logs").mkdir(exist_ok=True)
    src = path   # 原测试集：只在优化阶段写入（补「优化后提示词」），生成阶段不碰
    work = out_dir / src.name

    # 这次用哪组模型；同一个输出目录只能对应一组，换模型要换 --out
    models = {"图片": a.model_img or DEFAULT_MODELS["图片"], "视频": a.model_vid or DEFAULT_MODELS["视频"]}
    for mid in models.values():
        if not (MODELS_DIR / f"{mid}.json").exists():
            log(f"找不到模型配置 models\\{mid}.json", "✖ "); sys.exit(2)
    status_path = out_dir / "status.json"
    status = load_status(status_path)
    if status.get("models") and status["models"] != models:
        log(f"这个输出目录之前用的模型是 {status['models']}，这次是 {models}。"
            "换模型请换一个新的 --out 目录", "✖ "); sys.exit(2)

    only_ids = {x.strip() for x in a.ids.split(",") if x.strip()}
    tags = {x.strip() for x in a.tags.split(",") if x.strip()}

    # 读测试集，确定这次要跑哪些用例
    try:
        rows = read_testset(src, only_ids, tags)
    except SystemExit as e:
        log(str(e), "✖ "); sys.exit(2)
    if not rows:
        log("没有符合条件的用例"); sys.exit(0)

    cases = status.get("cases") or {}
    total = len(rows)
    for row in rows:
        cid = row["用例ID"]
        if cid not in cases:
            cases[cid] = {"id": cid, "task": row.get("任务类型", ""), "gen_status": "待生成"}

    started_at = status.get("started_at") or ts()
    t_start = time.time()
    save_status(status_path, "running", total, 0, 0, cases,
                {"started_at": started_at, "testset": src.name, "testset_path": str(src),
                 "run_dir": str(out_dir), "models": models})
    log(f"开始评测：{total} 条用例，输出目录 {out_dir}")
    log(f"图片模型：{model_info(models['图片'])}")
    log(f"视频模型：{model_info(models['视频'])}")

    ids_arg = ["--ids", ",".join(r["用例ID"] for r in rows)] if only_ids or tags else []

    # ── 阶段 1：优化（写回原测试集，所有模型共用同一份优化后提示词）──
    if not a.skip_optimize:
        log("\n── 阶段 1/3：优化提示词 ──")
        run_script("auto_optimize.py", ["--file", str(src)] + ids_arg)

    # ── 准备工作副本：第一次运行时从原测试集复制，续跑时沿用 ──
    if not work.exists():
        try:
            prepare_work_copy(src, work)
        except SystemExit as e:
            log(str(e), "✖ "); sys.exit(2)
        log(f"已复制测试集到 {work}（生成状态已重置为「待生成」）")
    path = work
    file_arg = ["--file", str(path)]

    # ── 阶段 2：生成（每条完成时更新进度文件）──────────────────
    if not a.skip_generate:
        log("\n── 阶段 2/3：生成图片/视频 ──")
        env = {"EVAL_STATUS_FILE": str(status_path),
               "EVAL_CRED_DIR": str(src.parent / "_凭据"),
               "EVAL_RUN_ID": out_dir.name}  # 幂等键包含运行ID，避免跨运行复用旧任务
        gen_args = file_arg + ids_arg + ["--timeout", str(a.timeout),
                                         "--model-img", models["图片"], "--model-vid", models["视频"]]
        rc = run_script("auto_generate.py", gen_args, extra_env=env)
        if rc == 4:
            log("\n⚠ 生成阶段超时，已有任务留在平台上。再次运行同一命令可续跑。")

    # ── 更新每条用例的生成状态 ──
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path)
        ws = wb["提示词"] if "提示词" in wb.sheetnames else wb.active
        hr, head = find_header(ws)
        for r in range(hr + 1, ws.max_row + 1):
            cid = ws.cell(r, head.get("用例ID", 1)).value
            if cid in cases:
                cases[cid]["gen_status"] = str(ws.cell(r, head.get("生成状态", 1)).value or "")
                cases[cid]["result_file"] = str(ws.cell(r, head.get("结果文件", 1)).value or "")
                cases[cid]["fail_reason"] = str(ws.cell(r, head.get("失败原因", 1)).value or "")
        # 拿到端到端耗时和模型名称
        jobs_path = out_dir / "outputs" / "_任务记录.json"
        if jobs_path.exists():
            jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
            for cid, job in jobs.items():
                if cid in cases:
                    if job.get("端到端毫秒"):
                        cases[cid]["e2e_ms"] = job["端到端毫秒"]
                    elif job.get("提交时间") and job.get("完成时间"):   # ComfyUI 不返回耗时，用提交到完成的时间
                        fmt = "%Y-%m-%d %H:%M:%S"
                        sec = (datetime.strptime(job["完成时间"], fmt) - datetime.strptime(job["提交时间"], fmt)).total_seconds()
                        cases[cid]["e2e_ms"] = int(sec * 1000)
                    # 添加模型名称到 cases
                    if job.get("模型名"):
                        cases[cid]["model_name"] = job["模型名"]
    except Exception:
        pass

    done = sum(1 for c in cases.values() if c["gen_status"] == "已完成")
    fail = sum(1 for c in cases.values() if c["gen_status"] == "失败")
    save_status(status_path, "running", total, done, fail, cases)

    # 生成情况报告
    build_report(cases, out_dir, src.name, int(time.time() - t_start), models)
    log(f"\n生成情况报告已写入 {out_dir / '生成情况.md'}")

    # ── 阶段 3：复制共享评测表到运行目录，添加结果文件列 ──
    if not a.skip_decompose:
        log("\n── 阶段 3/3：准备评测表 ──")
        # 评测表属于测试集，所有模型共用一份，从源测试集旁边的 评测记录/ 目录复制
        eval_dir = src.parent / "评测记录"
        dest_eval = out_dir / "评测记录.xlsx"
        if not dest_eval.exists():
            if eval_dir.exists():
                latest = sorted(eval_dir.glob("评测记录_*.xlsx"))
                if latest:
                    shutil.copy2(latest[-1], dest_eval)
                    log(f"已复制共享评测表到 {dest_eval}")
                else:
                    log("⚠ 未找到评测表，请先在源测试集上运行 auto_decompose.py 生成")
            else:
                log("⚠ 未找到评测记录目录，请先在源测试集上运行 auto_decompose.py 生成评测表")

        # ② 表加「结果文件」列（HTTP 超链接），并核对评测表和本次生成的用例ID、提示词是否一致
        if dest_eval.exists():
            try:
                link_eval_results(dest_eval, cases, read_prompts(path), out_dir)
            except PermissionError:
                log("⚠ 评测记录.xlsx 正被打开，关闭 Excel 后重新运行同一命令即可补上结果文件链接")
            except Exception as e:
                log(f"⚠ 添加结果文件列失败：{e}")

        # 更新拆解状态
        try:
            for r in range(2, ws.max_row + 1):
                cid = ws.cell(r, head.get("用例ID", 1)).value
                if cid in cases:
                    cases[cid]["decompose_status"] = str(ws.cell(r, head.get("拆解状态", 1)).value or "待拆解")
        except Exception:
            pass

    # ── 最终状态 ──
    done = sum(1 for c in cases.values() if c["gen_status"] == "已完成")
    fail = sum(1 for c in cases.values() if c["gen_status"] == "失败")
    pending = done < total - fail
    final = "waiting_review" if done > 0 else ("failed" if fail == total else "partial")
    save_status(status_path, final, total, done, fail, cases,
                {"finished_at": ts(), "duration_s": int(time.time() - t_start)})

    log(f"\n{'─'*50}")
    log(f"完成：{done}/{total} 条成功，{fail} 条失败")
    log(f"进度文件：{status_path}")
    if (out_dir / "评测记录.xlsx").exists():
        log(f"评测记录：{out_dir / '评测记录.xlsx'}")
    log(f"生成情况：{out_dir / '生成情况.md'}")
    if fail > 0:
        log("\n失败的用例（把「生成状态」改回「待生成」重新运行即可续跑）：")
        for c in cases.values():
            if c["gen_status"] == "失败":
                log(f"  {c['id']}：{c.get('fail_reason', '')}")

    if pending:
        sys.exit(4)
    elif fail == total:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except KeyboardInterrupt:
        log("\n已中断。再次运行同一命令可续跑，不会重复提交。")
        sys.exit(4)
