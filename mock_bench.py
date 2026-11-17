"""Offline Bridge fixtures using the business workbook exporter, never model APIs."""

import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_mock(args):
    from run_bench import read_testset

    source = Path(args.file).resolve(strict=True)
    rows = read_testset(source, {s.strip() for s in args.ids.split(",") if s.strip()},
                        {s.strip() for s in args.tags.split(",") if s.strip()})
    if not rows:
        raise ValueError("没有符合条件的 Mock 用例，请检查 --ids / --tags")
    ids = [str(row["用例ID"]) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Mock 测试集存在重复用例ID")
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Never overwrite real outputs, another run, or a partly reviewed workbook.
    if any(output.iterdir()):
        raise ValueError("Mock 需要空的 --out 目录，不能覆盖已有结果")
    progress = Path(args.progress_file).resolve() if args.progress_file else output / "progress.json"
    protected = {source, output / "status.json", output / "评测记录.xlsx", output / "report.md"}
    if progress in protected or progress.is_file():
        raise ValueError("进度文件不能覆盖输入或已有产物")

    print("MOCK TEST：仅生成联调数据，不调用模型、ComfyUI 或视频平台。", flush=True)
    publish_progress(progress, 0, len(rows), "准备模拟评测表")
    make_review_workbook(rows, output)
    # Use a genuine small PNG even for video cases; never disguise it as a video.
    from PIL import Image, ImageDraw

    cases = {}
    for index, row in enumerate(rows, 1):
        cid = str(row["用例ID"])
        name = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:16] + ".png"
        relative = Path("outputs") / "mock" / name
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (640, 360), "#eaf1ff")
        ImageDraw.Draw(image).text((30, 160), "MOCK ONLY - no image/video model was called", fill="#244f9b")
        image.save(destination)
        cases[cid] = {"id": cid, "task": row.get("任务类型", ""), "gen_status": "已完成",
                      "result_file": relative.as_posix(), "mock": True}
        publish_progress(progress, index, len(rows), "生成模拟占位图")
        write_json(output / "status.json", {"state": "running", "mock": True,
                   "progress": {"total": len(rows), "done": index, "failed": 0}, "cases": cases})
        time.sleep(0.25)  # Allow Bridge to observe incremental progress.

    attach_results(output, cases)
    lines = ["# Video Creator 联调报告（Mock）", "",
             "> 模拟数据，不代表模型质量；未调用模型或生成平台。视频用例也仅提供 PNG 占位图。", "",
             "生成阶段完成，请在 Edison 填写评测表，随后仅执行报告生成。", "",
             "[人工评测记录](评测记录.xlsx)", "", "## 模拟产物", ""]
    for cid, case in cases.items():
        lines.extend([f"### {markdown(cid)}", "", f"![Mock 占位图]({case['result_file']})", ""])
    report = "\n".join(lines)
    (output / "report.md").write_text(report, encoding="utf-8")
    (output / "生成情况.md").write_text(report, encoding="utf-8")
    (output / "review.required").write_text("Mock: human review required", encoding="utf-8")
    write_json(output / "status.json", {"state": "waiting_review", "mock": True,
               "progress": {"total": len(rows), "done": len(rows), "failed": 0}, "cases": cases})
    publish_progress(progress, len(rows), len(rows), "生成完成，等待人工评分")
    time.sleep(0.25)
    print(f"Mock 已完成，产物目录：{output}", flush=True)


def publish_progress(path, completed, total, stage):
    write_json(path, {"stage": stage, "message": f"Mock 已完成 {completed}/{total}",
                      "completed": completed, "total": total})


def make_review_workbook(rows, output):
    # Match the existing auto_decompose.py import convention. These two modules
    # only read the local rules and export Excel; no LLM/config/cookie is loaded.
    sys.path.insert(0, str(ROOT / "_lib"))
    try:
        from export import export, ExportError
        from rules import RulesCache
    finally:
        sys.path.pop(0)
    template = ROOT / "docs" / "评测体系_v1.5.xlsx"
    rules = RulesCache(str(template)).get()
    aliases = {"首帧生视频": "图生视频", "首尾帧生视频": "图生视频", "尾帧生视频": "图生视频",
               "全参考生视频": "参考生视频"}
    items = []
    for row in rows:
        task = aliases.get(row.get("任务类型"), row.get("任务类型"))
        if task not in rules["tasks"]:
            raise ValueError(f"不支持的 Mock 任务类型：{task}")
        items.append({"code": str(row["用例ID"]), "task": task, "status": "已确认",
                      "prompt": "【Mock 联调】" + str(row["初始提示词"]),
                      "requirements": [{"text": "模拟要求：人工检查占位产物，验证评分保存流程。",
                                        "level": "关键", "dimension": "指令遵循度"}],
                      "fixed": [{"dim": d, "on": True} for d in rules["fixed"][task]["fixed"]]})
    try:
        export(items, rules, [], str(template), str(output), file_name="评测记录.xlsx")
    except ExportError as exc:
        raise ValueError(str(exc)) from exc


def attach_results(output, cases):
    import openpyxl
    workbook = openpyxl.load_workbook(output / "评测记录.xlsx")
    sheet = next(s for s in workbook if s.title.startswith("②"))
    column = sheet.max_column + 1
    sheet.cell(4, column, "结果文件")
    sheet.column_dimensions[openpyxl.utils.get_column_letter(column)].width = 28
    for cells in sheet.iter_rows(min_row=5):
        sid = cells[0].value
        if sid:
            case = cases.get(str(sid).rsplit("-", 1)[0])
            if case:
                cell = sheet.cell(cells[0].row, column, case["result_file"])
                cell.hyperlink = case["result_file"]
                cell.style = "Hyperlink"
    workbook.save(output / "评测记录.xlsx")
    workbook.close()


def markdown(value):
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace("|", "\\|").replace("\n", "<br>").replace("\r", "")


def build_review_report(records, workbook_path):
    lines = ["# Video Creator 人工评测报告（Mock）", "",
             "> 仅汇总联调人工输入，不计算真实业务得分；未重新调用生成平台。", "",
             f"[已评分工作簿]({Path(workbook_path).name})", "",
             "| 样本 | 要求 / 维度 | 人工结果 | 问题描述 |", "| --- | --- | --- | --- |"]
    for row in records:
        fixed = row["级别"] == "固定加评"
        verdict = row["维度等级"] if fixed else row["要求结论"]
        allowed = {"L1", "L2", "L3", "L4"} if fixed else {"满足", "不满足"}
        if verdict not in allowed:
            raise ValueError(f"{row['样本ID']} 仍有未完成或待复核的人工评分")
        values = [row["样本ID"], row["评测维度"] if fixed else row["要求内容"],
                  verdict, row["问题描述 / 证据位置"]]
        lines.append("| " + " | ".join(markdown(v) for v in values) + " |")
    return "\n".join(lines) + "\n"
