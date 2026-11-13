# -*- coding: utf-8 -*-
"""批量拆要求，生成评测模板：读 提示词测试集.xlsx，调提示词小助手自动拆解，
导出一份填好 ②③ 表（要求预填，评分列留空）的评测记录 Excel。

- 只处理「优化后提示词」有内容 且「拆解状态」为空或 待核对 的行。
- 拆完只是草稿，必须人工核对后再冻结用于评测。
- 每次重新运行会跳过「拆解状态」= 已确认 的行，不覆盖。
- 导出的评测记录文件名格式：评测记录_<时间戳>.xlsx，在测试集旁边的 评测记录\\ 目录里。
- 调用的是提示词小助手里的 decompose / export，所以规则和导出格式和网页完全一样。

用法：python auto_decompose.py [--file 提示词测试集.xlsx] [--ids IMG-01,VID-01]
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent
HELPER = ROOT / "_lib"
DEFAULT_FILE = ROOT / "testset" / "提示词测试集.xlsx"
# 任务类型映射：测试集里的名字 → 小助手里的名字（只有不一样的才需要写）
TASK_MAP = {"首帧生视频": "图生视频", "首尾帧生视频": "图生视频", "尾帧生视频": "图生视频",
            "全参考生视频": "参考生视频"}
# 有旁白 / 音频要求时，这些条件项要自动勾上
AUDIO_COND = {"音画同步", "音频质量"}
MOTION_COND = {"动态程度"}
NEEDED = ("用例ID", "任务类型", "初始提示词", "优化后提示词", "生成状态", "拆解状态", "拆解说明")

sys.path.insert(0, str(HELPER))
import export as exp  # noqa: E402
import llm  # noqa: E402
import store  # noqa: E402
from llm import LLMError  # noqa: E402
from rules import RulesCache, RulesError  # noqa: E402


def has_audio(prompt):
    """提示词里有旁白或音乐，就勾上音频条件项"""
    p = str(prompt or "")
    return bool(re.search(r"旁白|配乐|背景音|音乐|soundscape|non_diegetic_music|overall_soundscape|<d>", p, re.I))


def has_motion(prompt):
    """有运镜指令就勾上动态程度"""
    p = str(prompt or "")
    return bool(re.search(r"镜头|camera|push|pull|pan|tilt|dolly|zoom|运镜|推近|拉远|平移|跟拍|环绕", p, re.I))


def header(ws):
    for r in range(1, 6):
        cols = {str(c.value).strip(): c.column for c in ws[r] if c.value}
        if "用例ID" in cols:
            missing = [n for n in NEEDED if n not in cols]
            if missing:
                sys.exit(f"表里缺这些列：{'、'.join(missing)}。先运行 build_prompt_testset.py --upgrade")
            return r, cols
    sys.exit("前 5 行里找不到「用例ID」，确认用的是 提示词测试集 模板")


def save(wb, path):
    while True:
        try:
            wb.save(path)
            return
        except PermissionError:
            input(f"\n{path.name} 正在被 Excel 打开。关掉后按回车重试……")


def decompose_row(cfg, rules, row):
    """拆一条，返回 item 对象（存进小助手 data\\items\\，code 和 prefix 都用用例ID）"""
    task_raw = str(row["任务类型"] or "").strip()
    task = TASK_MAP.get(task_raw, task_raw)
    if task not in rules["tasks"]:
        raise ValueError(f"任务类型「{task_raw}」不在评测体系里，支持：{', '.join(rules['tasks'])}")
    # 视频用中文摘要（K 列），如果有的话；图片直接用优化后提示词
    prompt = str(row.get("中文摘要") or row.get("优化后提示词") or "").strip()
    if not prompt:
        raise ValueError("没有「优化后提示词」，先运行自动优化")
    cid = str(row["用例ID"]).strip()
    negative = str(row.get("反向提示词") or "").strip()
    # 用 new_item 获取自动编号后，立即通过 update_item 把 prefix 覆盖成用例ID
    it = store.new_item(rules, task, prompt, negative, n_samples=1)
    it = store.update_item(rules, it["id"], {"prefix": cid})   # prefix 决定导出时的样本ID前缀
    # code 字段小助手不允许外部改，直接写入 item 文件里（code 只影响展示，不影响导出）
    it["code"] = cid
    store._write(store._item_path(it["id"]), it)
    # 根据提示词内容自动勾上对应的条件项
    for d in it["conditional"]:
        d["on"] = (d["dim"] in AUDIO_COND and has_audio(prompt)) or \
                  (d["dim"] in MOTION_COND and has_motion(prompt)) or d["on"]
    store.update_item(rules, it["id"], {"conditional": it["conditional"]})
    t0 = time.time()
    result = llm.decompose(cfg, rules, task, prompt)
    result["seconds"] = round(time.time() - t0, 1)
    store.set_decomposition(it["id"], result, rules.get("version", ""))
    it = store.get_item(it["id"])
    return it, result


def main():
    ap = argparse.ArgumentParser(description="批量拆要求，生成评测记录模板")
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--ids", default="", help="只处理这些用例ID，逗号分隔")
    ap.add_argument("--export", action="store_true", default=True,
                    help="拆完后立即导出评测记录（默认是；--no-export 只拆不导出）")
    ap.add_argument("--no-export", dest="export", action="store_false")
    a = ap.parse_args()
    path = Path(a.file).resolve()
    only = {x.strip() for x in a.ids.split(",") if x.strip()}
    if not path.exists():
        sys.exit(f"找不到文件：{path}")
    lock = path.parent / f"~${path.name}"
    if lock.exists():
        try:
            lock.unlink()   # 如果文件没被真正打开（只是残留锁文件），能直接删
        except OSError:
            sys.exit(f"{path.name} 正在被 Excel 打开，先关掉再运行")

    cfg = store.load_config()
    if not cfg.get("base_url") or not cfg.get("model"):
        sys.exit("提示词小助手还没配置大模型：先打开小助手，在「设置」里填接口地址和模型名")
    try:
        rules = RulesCache(str(ROOT / "docs" / "评测体系_v1.5.xlsx")).get()
    except (RulesError, OSError) as e:
        sys.exit(f"读取评测体系失败：{e}")

    wb = openpyxl.load_workbook(path)
    ws = wb["提示词"] if "提示词" in wb.sheetnames else wb.active
    hr, col = header(ws)

    todo = []
    for r in range(hr + 1, ws.max_row + 1):
        row = {name: ws.cell(r, c).value for name, c in col.items()}
        row = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        st = str(row.get("拆解状态") or "").strip()
        # 拆解只看提示词，不依赖生成结果：评测表属于测试集，所有模型共用一份
        if row["用例ID"] and row.get("优化后提示词") and st != "已确认" and (not only or row["用例ID"] in only):
            todo.append((r, row))
    if not todo:
        print("没有要拆解的行（还没有「优化后提示词」，或者拆解状态已经是已确认）")
        return

    backup = path.parent / "备份"
    backup.mkdir(exist_ok=True)
    shutil.copy2(path, backup / f"{path.stem}_拆解前_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
    print(f"共 {len(todo)} 条要拆解，模型：{cfg['model']}\n")

    items, ok, fail = [], 0, 0
    for i, (r, row) in enumerate(todo, 1):
        cid = row["用例ID"]
        print(f"[{i}/{len(todo)}] {cid}（{row['任务类型']}）拆解中……", end="", flush=True)
        try:
            it, result = decompose_row(cfg, rules, row)
        except (LLMError, ValueError) as e:
            fail += 1
            ws.cell(r, col["拆解状态"]).value = "失败"
            ws.cell(r, col["拆解说明"]).value = f"拆解失败：{e}"
            save(wb, path)
            print(f" 失败：{e}")
            if "额度" in str(e):
                print("\n额度不足，停下来。充值或换 key 后重新运行，已完成的不会重做。")
                break
            continue
        ok += 1
        n_req = len(it.get("requirements") or [])
        n_err = sum(1 for req in (it.get("requirements") or []) for f in req.get("flags") or [] if f[0] == "error")
        notes = result.get("not_split") or []
        questions = result.get("questions") or []
        status = "待核对（有错误需要改）" if n_err else "待核对"
        detail_parts = [f"拆出 {n_req} 条要求"]
        if n_err:
            detail_parts.append(f"{n_err} 处标红需要改")
        if notes:
            detail_parts.append("未拆分：" + "；".join(notes[:5]))
        if questions:
            detail_parts.append("请确认：" + "；".join(questions[:5]))
        ws.cell(r, col["拆解状态"]).value = status
        ws.cell(r, col["拆解说明"]).value = "、".join(detail_parts) + f"。在小助手里核对后，把状态改为「已确认」"
        items.append(it)
        save(wb, path)
        print(f" 完成，{n_req} 条要求" + (f"，{n_err} 处需要改" if n_err else ""))
        if "额度" in str(result.get("model", "")):
            break

    print(f"\n拆解结束：成功 {ok} 条，失败 {fail} 条")

    if a.export and items:
        out_dir = path.parent / "评测记录"
        try:
            result = exp.export(items, rules, store.load_customs(), str(ROOT / "docs" / "评测体系_v1.5.xlsx"),
                                str(out_dir), keep_examples=False, write_customs=True)
            print(f"已导出评测记录：{result['file']}")
            print(f"⚠  这份评测表是草稿！请打开小助手逐条核对（红色的必须改），核对完把测试集里的「拆解状态」改成「已确认」，再交给测试人员。")
        except exp.ExportError as e:
            print(f"\n导出失败：{e}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
