"""Run the real benchmark with durable media and a small Bridge report bundle."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from run_bench import find_header, prepare_work_copy, read_testset
from bench_diagnostics import add_arguments, configure, emit, timestamp

ROOT = Path(__file__).resolve().parent


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def progress(path, status, total, stage=None):
    counts = status.get("progress") or {}
    if not isinstance(counts, dict):
        return
    done = counts.get("done", 0)
    failed = counts.get("failed", 0)
    if type(done) is not int or type(failed) is not int or min(done, failed) < 0:
        return
    completed = min(done + failed, total)
    write_json(path, {"stage": stage or "真实生成", "completed": completed, "total": total,
                      "message": f"成功 {done}/{total}，失败 {failed}；等待人工评分" if stage else
                                 f"已结束 {completed}/{total}，成功 {done}，失败 {failed}"})


def read_status(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def rating_workbook(source, destination, ids, *, clear_scores=False):
    """Keep the original sheets/columns; remove unrelated rows from the rating sheet."""
    import openpyxl

    workbook = openpyxl.load_workbook(source)
    try:
        sheet = next((s for s in workbook if s.title.startswith("②")), None)
        if sheet is None:
            raise ValueError("评测记录缺少②逐行评测记录 Sheet")
        heads = {str(sheet.cell(4, c).value or "").replace(" ", ""): c
                 for c in range(1, sheet.max_column + 1)}
        if "样本ID" not in heads:
            raise ValueError("评测记录第4行缺少样本ID列")
        selected = set(ids)
        seen = set()
        score_columns = [heads[name] for name in ("要求结论", "维度等级", "判定依据/工具输出",
                         "问题描述/证据位置", "关联验证点") if name in heads]
        for row in range(5, sheet.max_row + 1):
            sid = str(sheet.cell(row, heads["样本ID"]).value or "")
            cid = sid.rsplit("-", 1)[0] if "-" in sid else ""
            if cid in selected:
                seen.add(cid)
                if clear_scores:
                    for col in score_columns:
                        sheet.cell(row, col).value = None
            else:
                # Keep row positions used by existing workbook formulas.
                for cell in sheet[row]:
                    if cell.__class__.__name__ != "MergedCell":
                        cell.value = None
        if seen != selected:
            raise ValueError("现有评测表缺少所选用例，请先由业务侧拆解要求并生成评测表：" +
                             ",".join(sorted(selected - seen)))
        workbook.save(destination)
    finally:
        workbook.close()


def run(args, root=ROOT):
    configure(args)
    source = Path(args.file).resolve(strict=True)
    ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("必须通过 --ids 明确指定不重复的用例ID")
    rows = read_testset(source, set(ids), set())
    if len(rows) != len(ids) or {str(row["用例ID"]) for row in rows} != set(ids):
        raise ValueError("所选用例不存在、缺少提示词，或用例ID重复")
    if len(rows) > args.max_cases:
        raise ValueError(f"当前接入配置最多允许 {args.max_cases} 道用例")
    for mid in (args.model_img or "qwen_image_2_1", args.model_vid or "minimax_h3"):
        if Path(mid).name != mid or not (root / "models" / (mid + ".json")).is_file():
            raise ValueError("找不到对应模型配置：" + mid)
    candidates = sorted((source.parent / "评测记录").glob("评测记录_*.xlsx"))
    if not candidates:
        raise ValueError("缺少业务评测表，请先在测试集旁的评测记录目录生成评测记录_*.xlsx")
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Bridge 交付目录必须为空，不能覆盖已有结果")
    progress_file = Path(args.progress_file).resolve()
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    if progress_file == source or progress_file == output / "report.md" or progress_file.exists():
        raise ValueError("进度文件不能覆盖输入或已有文件")
    run_dir = root / "runs" / ("edison_" + uuid.uuid4().hex)
    run_dir.mkdir(parents=True)
    print(f"真实运行目录：{run_dir}", flush=True)
    inputs = run_dir / "inputs"
    inputs.mkdir()
    snapshot = inputs / source.name
    prepare_work_copy(source, snapshot)
    # Filter the isolated snapshot before optimization; never edit the source workbook.
    import openpyxl
    workbook = openpyxl.load_workbook(snapshot)
    try:
        sheet = workbook["提示词"] if "提示词" in workbook.sheetnames else workbook.active
        header_row, heads = find_header(sheet)
        for row in range(sheet.max_row, header_row, -1):
            if str(sheet.cell(row, heads["用例ID"]).value or "").strip() not in ids:
                sheet.delete_rows(row)
        workbook.save(snapshot)
    finally:
        workbook.close()
    eval_inputs = inputs / "评测记录"
    eval_inputs.mkdir()
    rating_workbook(candidates[-1], eval_inputs / candidates[-1].name, ids, clear_scores=True)
    command = [sys.executable, "-u", str(root / "run_bench.py"), "--file", str(snapshot),
               "--out", str(run_dir), "--ids", ",".join(ids), "--timeout", str(args.timeout)]
    for option, value in (("--model-img", args.model_img), ("--model-vid", args.model_vid)):
        if value:
            command.extend([option, value])
    if args.skip_optimize:
        command.append("--skip-optimize")
    for option, value in (("--log-level", getattr(args, "log_level", "INFO")),
                          ("--heartbeat-interval", getattr(args, "heartbeat_interval", 15)),
                          ("--poll-interval", getattr(args, "poll_interval", 5)),
                          ("--http-timeout", getattr(args, "http_timeout", None))):
        if value is not None:
            command.extend([option, str(value)])
    command_text = subprocess.list2cmdline(command)
    write_json(run_dir / "execution.json", {"created_at": timestamp(), "command": command,
        "command_line": command_text, "working_directory": str(root), "python_version": sys.version,
        "source_file": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "snapshot_file": str(snapshot), "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "selected_ids": ids, "generation_timeout_seconds": args.timeout,
        "log_level": getattr(args, "log_level", "INFO")})
    (run_dir / "command.txt").write_text(f"cwd: {root}\n{command_text}\n", encoding="utf-8")
    emit("REAL_COMMAND", command=command_text, cwd=str(root))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", EVAL_CRED_DIR=str(source.parent / "_凭据"))
    progress(progress_file, {}, len(ids))
    # Persist logs locally rather than sending possibly sensitive raw service logs to Edison.
    with (run_dir / "bridge-run.log").open("wb") as log:
        log.write(f"{timestamp()} LAUNCH cwd={root}\n{command_text}\n".encode("utf-8"))
        log.flush()
        process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        last_heartbeat = 0.0
        try:
            while process.poll() is None:
                progress(progress_file, read_status(run_dir / "status.json"), len(ids))
                if time.monotonic() - last_heartbeat >= getattr(args, "heartbeat_interval", 15):
                    # Keep a visible heartbeat even while a single HTTP request is blocked.
                    log.write(f"{timestamp()} CHILD_ALIVE pid={process.pid} status_file={run_dir / 'status.json'}\n".encode("utf-8"))
                    log.flush()
                    last_heartbeat = time.monotonic()
                time.sleep(0.5)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    status = read_status(run_dir / "status.json")
    if process.returncode:
        raise ValueError(f"真实脚本退出码 {process.returncode}；结果与日志保留在 {run_dir}，不要盲目重跑")
    cases = status.get("cases") or {}
    if set(cases) != set(ids) or any(case.get("gen_status") not in {"已完成", "失败"}
                                   for case in cases.values()):
        raise ValueError(f"真实脚本没有提供完整终态，检查 {run_dir}")
    succeeded = sum(case.get("gen_status") == "已完成" for case in cases.values())
    failed = len(ids) - succeeded
    if not succeeded:
        raise ValueError(f"全部用例生成失败，检查 {run_dir}")
    if not (run_dir / "评测记录.xlsx").is_file():
        raise ValueError(f"生成结束但缺少评测记录，检查 {run_dir}")
    rating_workbook(run_dir / "评测记录.xlsx", output / "评测记录.xlsx", ids)
    public = {"kind": "real_generation", "run_directory": str(run_dir), "selected_ids": ids,
              "succeeded": succeeded, "failed": failed, "requires_review": True,
              "media_delivery": "windows_local", "cases": {}}
    for cid, case in cases.items():
        # Media stays on the execution machine. No video upload or new file server.
        public["cases"][cid] = {"status": case["gen_status"], "result_file": case.get("result_file", "")}
    write_json(output / "run.json", public)
    lines = ["# Video Creator 真实生成结果", "", "> 生成结束，等待人工评分。这不是最终质量评测报告。", "",
             f"本次 {len(ids)} 道：成功 {succeeded} 道，失败 {failed} 道。", "",
             "[人工评测记录](评测记录.xlsx)", "", "| 用例 | 生成状态 |", "|---|---|"]
    lines += [f"| {cid.replace('|', '/')} | {case['gen_status']} |" for cid, case in cases.items()]
    lines += ["", "## 原始产物", "", "图片、视频及详细日志保留在 Windows 执行机器：", "",
              str(run_dir), "", "原始媒体未上传到 Edison。已有业务文件服务链接的可达性需另行验证。", "",
              "评分后提交评分记录；最终报告由业务原有 report.py 生成后上传。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (output / "review.required").write_text("real generation: human review required", encoding="utf-8")
    progress(progress_file, {"progress": {"done": succeeded, "failed": failed}}, len(ids), "生成结束，待人工评分")
    print(f"生成结束：成功 {succeeded}/{len(ids)}，失败 {failed}；产物保留在 {run_dir}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default=str(ROOT / "testset" / "提示词测试集.xlsx"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--progress-file", required=True)
    parser.add_argument("--ids", required=True)
    parser.add_argument("--max-cases", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--model-img", default="")
    parser.add_argument("--model-vid", default="")
    parser.add_argument("--skip-optimize", action="store_true")
    add_arguments(parser)
    args = parser.parse_args()
    if not 1 <= args.max_cases <= 3 or not 1 <= args.timeout <= 3600:
        parser.error("本次真实接入最多3道用例，单次生成等待最多3600秒")
    try:
        run(args)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"真实 Benchmark 执行失败：{exc}\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
