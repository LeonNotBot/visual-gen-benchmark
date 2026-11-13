# -*- coding: utf-8 -*-
"""生成 提示词测试集.xlsx 模板：你填初始提示词，脚本自动填优化结果和生成结果。
python build_prompt_testset.py            新建（已存在就不覆盖）
python build_prompt_testset.py --upgrade  已有文件换成新模板：按列名把已填内容搬过去，改前备份
"""
import shutil
import sys
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation

OUT = Path(__file__).resolve().parent / "testset" / "提示词测试集.xlsx"

YOU, AUTO = "D9E2F3", "E7E6E6"          # 表头底色：你填 / 脚本填
YOU_CELL, AUTO_CELL = "FFFFFF", "F2F2F2"
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TASKS = ["文生图", "图片编辑", "文生视频", "首帧生视频", "首尾帧生视频", "尾帧生视频", "全参考生视频"]

# (列名, 宽度, 谁填)
COLS = [
    ("用例ID", 10, "you"),
    ("任务类型", 12, "you"),
    ("初始提示词", 50, "you"),
    ("参考图", 26, "you"),
    ("参考图说明", 20, "you"),
    ("参考视频/音频说明", 22, "you"),
    ("视频时长（秒）", 9, "you"),
    ("画面比例", 9, "you"),
    ("备注", 16, "you"),
    ("优化后提示词", 60, "auto"),
    ("反向提示词", 30, "auto"),
    ("中文摘要", 50, "auto"),
    ("格式检查", 22, "auto"),
    ("优化说明", 40, "auto"),
    ("优化记录", 24, "auto"),
    ("改写模型", 14, "auto"),
    ("生成状态", 10, "auto"),
    ("结果文件", 24, "auto"),
    ("失败原因", 24, "auto"),
    ("拆解状态", 14, "auto"),
    ("拆解说明", 40, "auto"),
]
N_YOU = sum(1 for c in COLS if c[2] == "you")
NAMES = [c[0] for c in COLS]

EXAMPLES = [
    {"用例ID": "IMG-01", "任务类型": "文生图", "初始提示词": "一只橘猫趴在窗边晒太阳，旁边放着一杯牛奶，阳光暖暖的，治愈清新",
     "画面比例": "16:9", "备注": "示例，可替换"},
    {"用例ID": "VID-01", "任务类型": "文生视频", "初始提示词": "写实风格，清晨薄雾笼罩的山谷，镜头缓慢向前推进。男声旁白：「清晨的山谷还没有完全醒来，"
     "薄雾慢慢散开，新的一天开始了。」背景音乐：舒缓钢琴。", "视频时长（秒）": 10, "画面比例": "16:9", "备注": "示例，可替换"},
    {"用例ID": "VID-02", "任务类型": "首帧生视频", "参考图": r"参考图\VID-02_首帧.jpg",
     "初始提示词": "老式放映机开始转动，暖黄色光束里的灰尘缓缓飘动，镜头慢慢向放映机推近。只有放映机的转动声和轻微的环境声，没有人物，没有旁白。",
     "视频时长（秒）": 6, "画面比例": "16:9", "备注": "示例；首帧来自 B1-01 放映室"},
]
N_ROWS = 50   # 预留的空行数（带下拉和底色）


def fill(color):
    return PatternFill("solid", fgColor=color)


def build_main(ws, rows):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=N_YOU)
    ws.merge_cells(start_row=1, start_column=N_YOU + 1, end_row=1, end_column=len(COLS))
    for col, text, color in ((1, "你来填", YOU), (N_YOU + 1, "脚本自动填（不用手动改）", AUTO)):
        c = ws.cell(1, col, text)
        c.font, c.fill = Font(bold=True, size=11), fill(color)
        c.alignment = Alignment(horizontal="center", vertical="center")
    for i, (name, width, who) in enumerate(COLS, 1):
        c = ws.cell(2, i, name)
        c.font, c.fill, c.border = Font(bold=True), fill(YOU if who == "you" else AUTO), BOX
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[c.column_letter].width = width
    ws.row_dimensions[2].height = 30
    for r in range(3, 3 + max(N_ROWS, len(rows))):
        data = rows[r - 3] if r - 3 < len(rows) else {}
        for i, (name, _, who) in enumerate(COLS, 1):
            c = ws.cell(r, i, data.get(name))
            c.fill, c.border = fill(YOU_CELL if who == "you" else AUTO_CELL), BOX
            c.alignment = Alignment(vertical="top", wrap_text=True)
        if data:
            ws.row_dimensions[r].height = 60
    ws.freeze_panes = "C3"

    last = 2 + max(N_ROWS, len(rows))
    letter = {name: ws.cell(2, i).column_letter for i, name in enumerate(NAMES, 1)}
    task = DataValidation(type="list", formula1='"' + ",".join(TASKS) + '"', allow_blank=True,
                          error="从下拉里选任务类型", errorTitle="任务类型")
    dur = DataValidation(type="whole", operator="between", formula1="5", formula2="15", allow_blank=True,
                         error="视频平台只支持 5–15 的整数秒", errorTitle="视频时长")
    ratio = DataValidation(type="list", formula1='"16:9,9:16,1:1,4:3,3:4"', allow_blank=True)
    for dv, name in ((task, "任务类型"), (dur, "视频时长（秒）"), (ratio, "画面比例")):
        ws.add_data_validation(dv)
        dv.add(f"{letter[name]}3:{letter[name]}{last}")


def build_help(ws):
    rows = [
        ("怎么用", ""),
        ("1", "在「提示词」表填白色的列：用例ID、任务类型、初始提示词；视频填时长；要参考图的任务填参考图。灰色的列不用管。"),
        ("2", "关掉 Excel，双击「自动优化提示词.bat」，脚本调提示词小助手优化提示词，把结果填进灰色列。"),
        ("3", "脚本只处理「优化后提示词」空着的行。想让某一条重新优化，就把它的「优化后提示词」清空再运行。"),
        ("", ""),
        ("任务类型", "要传的参考图"),
        ("文生图", "不用传"),
        ("图片编辑", "1 张输入图"),
        ("文生视频", "不用传"),
        ("首帧生视频", "1 张首帧图"),
        ("首尾帧生视频", "2 张：首帧图;尾帧图（顺序不能反）"),
        ("尾帧生视频", "1 张尾帧图（视频平台不支持，只能优化提示词，不能自动生成）"),
        ("全参考生视频", "1–9 张参考图；用到参考视频/音频时，在「参考视频/音频说明」里用文字描述（视频平台不支持，只能优化提示词）"),
        ("", ""),
        ("列说明", ""),
        ("用例ID", "唯一编号，图片用 IMG-01…，视频用 VID-01…；也是结果文件名"),
        ("参考图", "图片路径，多张用英文分号 ; 隔开，按顺序对应 Picture 1、Picture 2…。"
                 "建议把图放在测试集旁边的「参考图」文件夹，写成 参考图\\xxx.jpg。支持 PNG、JPG、WebP"),
        ("参考图说明", "只有全参考生视频要填：每张图的用途，用英文分号 ; 隔开，和「参考图」一一对应，如：角色林晚清;场景旧巷"),
        ("参考视频/音频说明", "只有全参考生视频用：一行一份素材，以「视频：」或「音频：」开头，写清内容和怎么用。"
                         "如：视频：参考它的跟拍运镜"),
        ("视频时长（秒）", "视频任务要填，5–15 的整数秒（视频平台的限制）；图片留空"),
        ("画面比例", "默认 16:9"),
        ("优化后提示词", "图片：中文正向提示词；视频：英文 H3 提示词。生成时提交这一列"),
        ("反向提示词", "只有图片有，生成时一起提交"),
        ("中文摘要", "只有视频有，是英文 H3 提示词的中文翻译，拆要求时用"),
        ("格式检查", "小助手的规则检查结果：通过，或列出问题"),
        ("优化说明", "小助手的改写说明，其中「请确认」几条要看一下"),
        ("优化记录", "小助手里的记录编号，网页的「最近记录」里能找到"),
        ("生成状态", "待生成 / 生成中 / 已完成 / 失败"),
        ("结果文件", "下载到本地的图片或视频路径"),
        ("拆解状态", "待核对 / 已确认 / 失败。自动拆解后是「待核对」，你在小助手里核对完，手动改成「已确认」；已确认的不会再被重拆"),
        ("拆解说明", "拆出几条要求、有几处标红、模型拿不准的地方"),
    ]
    for r, (a, b) in enumerate(rows, 1):
        ws.cell(r, 1, a).font = Font(bold=a in ("怎么用", "列说明", "任务类型") or a.isdigit())
        ws.cell(r, 2, b).alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 90


def read_old(path):
    """按列名读出已有文件的数据行（列名找不到的列丢弃前会提示）"""
    ws = openpyxl.load_workbook(path)["提示词"]
    head = {str(c.value).strip(): c.column for c in ws[2] if c.value}
    dropped = [h for h in head if h not in NAMES]
    if dropped:
        print("这些列新模板里没有，内容不搬：", "、".join(dropped))
    rows = []
    for r in range(3, ws.max_row + 1):
        row = {h: ws.cell(r, c).value for h, c in head.items() if h in NAMES}
        if any(v not in (None, "") for v in row.values()):
            rows.append(row)
    return rows


def write(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "提示词"
    build_main(ws, rows)
    build_help(wb.create_sheet("说明"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--upgrade" in sys.argv:
        if not OUT.exists():
            sys.exit(f"找不到要升级的文件：{OUT}")
        if (OUT.parent / f"~${OUT.name}").exists():
            sys.exit(f"{OUT.name} 正在被 Excel 打开，先关掉再运行")
        rows = read_old(OUT)
        have = {r.get("用例ID") for r in rows}
        rows += [ex for ex in EXAMPLES if ex["用例ID"] not in have]
        bak = OUT.parent / "备份" / f"{OUT.stem}_换模板前_{datetime.now():%Y%m%d_%H%M%S}{OUT.suffix}"
        bak.parent.mkdir(exist_ok=True)
        shutil.copy2(OUT, bak)
        write(rows)
        print(f"已升级：{OUT}（{len(rows)} 行；原文件备份在 {bak}）")
    elif OUT.exists():
        sys.exit(f"已存在，不覆盖：{OUT}（要换成新模板用 --upgrade）")
    else:
        write(list(EXAMPLES))
        print("已生成：", OUT)
