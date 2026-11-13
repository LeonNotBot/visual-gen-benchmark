# -*- coding: utf-8 -*-
"""auto_optimize.py 自测：假大模型 + 临时目录，不动真实的 config.json、data\\ 和测试集。
运行：python scripts\\tests\\test_auto_optimize.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import openpyxl
from PIL import Image

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
SCRIPT = SCRIPTS / "auto_optimize.py"
TEMPLATE_BUILDER = SCRIPTS / "build_prompt_testset.py"

IMG_REPLY = ("<<<IMG_PROMPT>>>\n写实摄影，一只橘猫趴在窗台上。图像中未出现任何可识别文字。\n<<<END_IMG_PROMPT>>>\n"
             "<<<NEGATIVE>>>\n低画质，文字，水印\n<<<END_NEGATIVE>>>\n<<<NOTES>>>\n- 类型：通用图\n<<<END_NOTES>>>")
VID_REPLY = ("<<<H3_PROMPT>>>\nintegrated_multimodal_description: [Shot 1] Live-action, a wide shot of a valley.\n\n"
             "overall_soundscape: Wind.\n\nnon_diegetic_music: Piano.\n<<<END_H3_PROMPT>>>\n"
             "<<<NOTES>>>\n- 模式：T2VA\n<<<END_NOTES>>>\n<<<ZH_SUMMARY>>>\n镜头一：山谷远景。\n<<<END_ZH_SUMMARY>>>")
calls = []


class Fake(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        calls.append(body["messages"])
        text = json.dumps(body["messages"], ensure_ascii=False)
        reply = VID_REPLY if "<<<H3_PROMPT>>>" in text else IMG_REPLY
        out = json.dumps({"choices": [{"message": {"content": reply}}], "usage": {"total_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def run(home, xlsx, *extra):
    env = dict(os.environ, PROMPT_SPLITTER_HOME=str(home), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), "--file", str(xlsx), *extra], env=env,
                          capture_output=True, text=True, encoding="utf-8", input="")


def rows(xlsx):
    ws = openpyxl.load_workbook(xlsx)["提示词"]
    head = {c.value: c.column for c in ws[2]}
    return {ws.cell(r, 1).value: {k: ws.cell(r, v).value for k, v in head.items()}
            for r in range(3, ws.max_row + 1) if ws.cell(r, 1).value}, head


def edit(xlsx, cid, **vals):
    wb = openpyxl.load_workbook(xlsx)
    ws = wb["提示词"]
    head = {c.value: c.column for c in ws[2]}
    r = next(r for r in range(3, ws.max_row + 1) if ws.cell(r, 1).value == cid)
    for k, v in vals.items():
        ws.cell(r, head[k]).value = v
    wb.save(xlsx)


def image_parts(msgs):
    user = msgs[-1]["content"] if isinstance(msgs[-1]["content"], list) else []
    return [p for p in user if p.get("type") == "image_url"]


def main():
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="auto_opt_"))
    try:
        home = tmp / "helper"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({
            "base_url": f"http://127.0.0.1:{srv.server_port}/v1", "model": "fake-model", "api_key": "x",
            "use_system_proxy": False, "timeout": 10}), encoding="utf-8")
        xlsx = tmp / "提示词测试集.xlsx"
        builder = tmp / "builder.py"
        builder.write_text(TEMPLATE_BUILDER.read_text(encoding="utf-8").replace(
            'OUT = Path(__file__).resolve().parent / "testset" / "提示词测试集.xlsx"',
            f'OUT = Path(r"{xlsx}")'), encoding="utf-8")
        subprocess.run([sys.executable, str(builder)], check=True, capture_output=True)
        (tmp / "参考图").mkdir()
        Image.new("RGBA", (3000, 1600), (200, 100, 50, 128)).save(tmp / "参考图" / "VID-02_首帧.jpg".replace(".jpg", ".png"))
        edit(xlsx, "VID-02", 参考图=r"参考图\VID-02_首帧.png")

        r = run(home, xlsx)
        print(r.stdout)
        assert r.returncode == 0, r.stderr
        d, head = rows(xlsx)
        assert "橘猫" in d["IMG-01"]["优化后提示词"] and d["IMG-01"]["反向提示词"] == "低画质，文字，水印"
        assert d["VID-01"]["中文摘要"] == "镜头一：山谷远景。" and d["VID-01"]["反向提示词"] is None
        assert d["VID-02"]["优化后提示词"] and d["VID-02"]["生成状态"] == "待生成", d["VID-02"]
        assert len(calls) == 3, len(calls)
        assert not image_parts(calls[0]) and not image_parts(calls[1])
        parts = image_parts(calls[2])
        assert len(parts) == 1 and parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        rec = json.loads((home / "data" / "optimize" / f"{d['VID-02']['优化记录']}.json").read_text(encoding="utf-8"))
        assert rec["mode"] == "I2VA" and rec["images"] == [{"role": "首帧图"}] and rec["duration"] == 6.0, rec
        assert len(openpyxl.load_workbook(xlsx)["提示词"].data_validations.dataValidation) == 3

        # 再跑一次：都已优化，不调用模型
        n = len(calls)
        assert "没有要优化的行" in run(home, xlsx).stdout and len(calls) == n

        # 各种填错的情况：写失败原因，不调模型
        cases = [
            ("VID-02", {"参考图": r"参考图\不存在.png"}, "找不到参考图"),
            ("VID-02", {"参考图": None}, "首帧"),
            ("VID-01", {"视频时长（秒）": None}, "视频时长"),
            ("VID-01", {"参考视频/音频说明": "视频：参考运镜"}, "只有全参考"),
            ("IMG-01", {"参考图": r"参考图\VID-02_首帧.png"}, "图片编辑"),
            ("IMG-01", {"任务类型": "图片编辑", "参考图": None}, "1 张输入图"),
        ]
        for cid, vals, want in cases:
            n = len(calls)
            edit(xlsx, cid, 优化后提示词=None, 失败原因=None, **vals)
            run(home, xlsx, "--ids", cid)
            got = rows(xlsx)[0][cid]["失败原因"] or ""
            assert want in got, (cid, vals, got)
            assert len(calls) == n, f"{cid} 填错了还调了模型"
            edit(xlsx, cid, **{k: d[cid][k] for k in vals})   # 改回去

        # 改好再跑：成功后失败原因清掉
        edit(xlsx, "VID-01", 优化后提示词=None, 失败原因="上次的失败原因")
        run(home, xlsx, "--ids", "VID-01")
        assert rows(xlsx)[0]["VID-01"]["失败原因"] is None
        print("全部通过")
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
