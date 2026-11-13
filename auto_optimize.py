# -*- coding: utf-8 -*-
"""批量优化提示词：读 提示词测试集.xlsx，调提示词小助手的优化函数，把结果填回 Excel。
只处理「优化后提示词」还空着的行；想重新优化某一行，把那一行的「优化后提示词」清空再运行。
优化记录也存进小助手的 data\\ 目录，网页「最近记录」里能看到。
参考图按「参考图」列的路径读取（相对路径以测试集所在文件夹为准），和网页一样把长边压到 1536 再发给模型。

用法：python auto_optimize.py [--file 提示词测试集.xlsx] [--ids IMG-01,VID-01]
"""
import argparse
import base64
import io
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import openpyxl
from PIL import Image

ROOT = Path(__file__).resolve().parent
HELPER = ROOT / "_lib"
DEFAULT_FILE = ROOT / "testset" / "提示词测试集.xlsx"

sys.path.insert(0, str(HELPER))
import optimize  # noqa: E402
import optimize_img  # noqa: E402
import server  # noqa: E402  只用它的输入校验，和网页走同一套规则
import store  # noqa: E402
from llm import LLMError  # noqa: E402

# 任务类型 → (用哪个优化器, 小助手里的模式)
TASKS = {"文生图": ("img", "T2I"), "图片编辑": ("img", "Edit"),
         "文生视频": ("h3", "T2VA"), "首帧生视频": ("h3", "I2VA"), "首尾帧生视频": ("h3", "FL2VA"),
         "尾帧生视频": ("h3", "L2VA"), "全参考生视频": ("h3", "Ref2VA")}
NEEDED = ("用例ID", "任务类型", "初始提示词", "参考图", "参考图说明", "参考视频/音频说明", "视频时长（秒）",
          "优化后提示词", "反向提示词", "中文摘要", "格式检查", "优化说明", "优化记录", "改写模型", "生成状态", "失败原因")
MAX_SIDE = 1536                       # 和网页 web/optimize.js 一致
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
SPLIT = re.compile(r"[;；\n]+")


def header(ws):
    """找列名所在的行，返回 (行号, {列名: 列号})"""
    for r in range(1, 6):
        cols = {str(c.value).strip(): c.column for c in ws[r] if c.value}
        if "用例ID" in cols:
            missing = [n for n in NEEDED if n not in cols]
            if missing:
                sys.exit(f"表里缺这些列：{'、'.join(missing)}。先运行 build_prompt_testset.py --upgrade 换成新模板")
            return r, cols
    sys.exit("前 5 行里找不到「用例ID」列，确认用的是 提示词测试集 模板")


def flags_text(flags):
    if not flags:
        return "通过"
    return "\n".join(("✖ " if lv == "error" else "⚠ ") + msg for lv, msg in flags)


def load_images(cell, base):
    """「参考图」列 → [data URL]。相对路径以测试集所在文件夹为准"""
    out = []
    for raw in SPLIT.split(str(cell or "")):
        raw = raw.strip().strip('"')
        if not raw:
            continue
        p = Path(raw)
        p = p if p.is_absolute() else base / p
        if not p.is_file():
            raise ValueError(f"找不到参考图：{raw}")
        if p.suffix.lower() not in IMG_EXT:
            raise ValueError(f"参考图只支持 PNG、JPG、WebP：{p.name}")
        try:
            im = Image.open(p)
            im.load()
        except OSError:
            raise ValueError(f"参考图打不开：{p.name}")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        if im.mode != "RGB":                      # 透明底和网页一样铺白
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.convert("RGBA").split()[-1])
            im = bg
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        out.append("data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii"))
    return out


def parse_assets(cell):
    """「参考视频/音频说明」列：一行一份，以「视频：」或「音频：」开头"""
    assets = {"videos": [], "audios": []}
    for line in str(cell or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(视频|音频)\s*[:：]\s*(.+)$", line)
        if not m:
            raise ValueError(f"「参考视频/音频说明」每行要以「视频：」或「音频：」开头：{line}")
        assets["videos" if m.group(1) == "视频" else "audios"].append({"desc": m.group(2)})
    return assets


def run_image(cfg, mode, prompt, images):
    if mode == "T2I" and images:
        raise ValueError("文生图不用传参考图；要基于图片改，任务类型选「图片编辑」")
    if mode == "Edit" and len(images) != 1:
        raise ValueError(f"图片编辑要传 1 张输入图，现在是 {len(images)} 张")
    image = images[0] if images else None
    t0 = time.time()
    res = optimize_img.optimize(cfg, mode, prompt, image)
    rec = store.save_img_opt({"mode": mode, "prompt": prompt, "seconds": round(time.time() - t0, 1),
                              "image": {"role": "输入图"} if image else None, "source": "批量脚本"} | res)
    return {"优化后提示词": res["result"], "反向提示词": res["negative"], "中文摘要": None,
            "格式检查": flags_text(res["flags"]), "优化说明": res["notes"], "优化记录": rec["id"], "改写模型": res["model"]}


def run_video(cfg, mode, prompt, dur, images, roles, assets):
    if mode == "Ref2VA":
        imgs = [{"data": d, "role": roles[i] if i < len(roles) else ""} for i, d in enumerate(images)]
    else:
        slots = optimize.MODES[mode]["slots"]   # 首帧/尾帧模式用固定角色名
        imgs = [{"data": d, "role": slots[i] if i < len(slots) else ""} for i, d in enumerate(images)]
        if any(a for a in assets.values()):
            raise ValueError("只有全参考生视频能用参考视频/音频")
    body = {"mode": mode, "prompt": prompt, "duration": dur, "images": imgs, "assets": assets}
    mode, prompt, dur, imgs, assets = server.clean_opt_input(body)   # 张数、格式、时长按网页同一套规则检查
    t0 = time.time()
    res = optimize.optimize(cfg, mode, prompt, dur, imgs, assets)
    rec = store.save_opt({"mode": mode, "prompt": prompt, "duration": dur, "seconds": round(time.time() - t0, 1),
                          "images": [{"role": im["role"]} for im in imgs], "assets": assets, "source": "批量脚本"} | res)
    return {"优化后提示词": res["result"], "反向提示词": None, "中文摘要": res["zh_summary"],
            "格式检查": flags_text(res["flags"]), "优化说明": res["notes"], "优化记录": rec["id"], "改写模型": res["model"]}


def save(wb, path):
    while True:
        try:
            wb.save(path)
            return
        except PermissionError:
            input(f"\n保存失败：{path.name} 正在被 Excel 打开。关掉 Excel 后按回车重试……")


def optimize_row(cfg, row, base):
    task, prompt = row["任务类型"], row["初始提示词"]
    if task not in TASKS:
        raise ValueError(f"任务类型只能是 {' / '.join(TASKS)}，现在是「{task}」")
    kind, mode = TASKS[task]
    images = load_images(row["参考图"], base)
    if kind == "img":
        return run_image(cfg, mode, prompt, images)
    try:
        dur = float(row["视频时长（秒）"])
    except (TypeError, ValueError):
        raise ValueError(f"{task}要填「视频时长（秒）」")
    roles = [x.strip() for x in SPLIT.split(str(row["参考图说明"] or "")) if x.strip()]
    return run_video(cfg, mode, prompt, dur, images, roles, parse_assets(row["参考视频/音频说明"]))


def main():
    ap = argparse.ArgumentParser(description="批量优化提示词测试集")
    ap.add_argument("--file", default=str(DEFAULT_FILE), help="提示词测试集 Excel 路径")
    ap.add_argument("--ids", default="", help="只处理这些用例ID，逗号分隔，如 IMG-01,VID-01")
    a = ap.parse_args()
    path = Path(a.file).resolve()
    only = {x.strip() for x in a.ids.split(",") if x.strip()}
    if not path.exists():
        sys.exit(f"找不到文件：{path}")
    if (path.parent / f"~${path.name}").exists():
        sys.exit(f"{path.name} 正在被 Excel 打开，先关掉再运行")

    cfg = store.load_config()
    if not cfg.get("base_url") or not cfg.get("model"):
        sys.exit("提示词小助手还没配置大模型：先打开小助手，在「设置」里填接口地址和模型名")
    wb = openpyxl.load_workbook(path)
    ws = wb["提示词"] if "提示词" in wb.sheetnames else wb.active
    hr, col = header(ws)

    todo = []
    for r in range(hr + 1, ws.max_row + 1):
        row = {name: ws.cell(r, c).value for name, c in col.items()}
        row = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        if row["用例ID"] and row["初始提示词"] and not row["优化后提示词"] and (not only or row["用例ID"] in only):
            todo.append((r, row))
    if not todo:
        print("没有要优化的行（「优化后提示词」都已经有内容，或者没填初始提示词）")
        return

    backup = path.parent / "备份"
    backup.mkdir(exist_ok=True)
    bak = backup / f"{path.stem}_优化前_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}"
    shutil.copy2(path, bak)
    print(f"共 {len(todo)} 条要优化，模型：{cfg['model']}。改之前已备份到 {bak}\n")

    ok = fail = 0
    for i, (r, row) in enumerate(todo, 1):
        put = lambda name, v: setattr(ws.cell(r, col[name]), "value", v)   # ws.cell(r, c, None) 不会清空格子
        print(f"[{i}/{len(todo)}] {row['用例ID']}（{row['任务类型']}）优化中……", end="", flush=True)
        try:
            out = optimize_row(cfg, row, path.parent)
        except (LLMError, ValueError) as e:
            fail += 1
            put("失败原因", f"优化失败：{e}")
            save(wb, path)
            print(f" 失败：{e}")
            if "额度" in str(e):
                print("\n额度不足，后面的也会失败，先停下。充值或换 key 后重新运行即可，已完成的不会重做。")
                break
            continue
        for name, v in out.items():
            put(name, v)
        put("生成状态", "待生成")
        put("失败原因", None)
        save(wb, path)
        ok += 1
        print(f" 完成，格式检查：{out['格式检查'].splitlines()[0]}")

    print(f"\n结束：成功 {ok} 条，失败 {fail} 条。结果已写回 {path}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
