# -*- coding: utf-8 -*-
"""auto_generate.py 自测：假 ComfyUI + 临时目录，不连真实平台、不动真实测试集。
运行：python scripts\\tests\\test_auto_generate.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import openpyxl
from PIL import Image

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "auto_generate.py"
BUILDER = HERE.parent / "build_prompt_testset.py"

S = {"submitted": [], "polls": {}, "ready_after": 2, "fail": set()}
PNG = io.BytesIO()
Image.new("RGB", (64, 36), "orange").save(PNG, "PNG")


class FakeComfy(BaseHTTPRequestHandler):
    def _send(self, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        pid = f"pid-{len(S['submitted']) + 1}"
        S["submitted"].append((pid, body["prompt"]))
        self._send({"prompt_id": pid, "number": 1, "node_errors": {}})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/history/"):
            pid = u.path.rsplit("/", 1)[1]
            n = S["polls"][pid] = S["polls"].get(pid, 0) + 1
            if n < S["ready_after"]:
                return self._send({})
            g = dict(S["submitted"])[pid]
            if g["459:474"]["inputs"]["prompt"] in S["fail"]:
                return self._send({pid: {"status": {"status_str": "error", "completed": False, "messages": [
                    ["execution_error", {"exception_message": "CUDA out of memory"}]]}, "outputs": {}}})
            prefix = g["461"]["inputs"]["filename_prefix"]
            return self._send({pid: {"status": {"status_str": "success", "completed": True}, "outputs": {
                "461": {"images": [{"filename": f"{prefix}_00001.png", "subfolder": "", "type": "output"}]}}}})
        if u.path == "/queue":
            return self._send({"queue_running": [[0, pid, {}, {}, []] for pid, _ in S["submitted"]
                                                 if S["polls"].get(pid, 0) < S["ready_after"]], "queue_pending": []})
        if u.path == "/view":
            return self._send(PNG.getvalue(), "image/png")
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


def run(xlsx, *extra):
    env = dict(os.environ, EVAL_COMFY_URL=f"http://127.0.0.1:{PORT}", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), "--file", str(xlsx), "--interval", "0.05", *extra],
                          env=env, capture_output=True, text=True, encoding="utf-8", input="")


def rows(xlsx):
    ws = openpyxl.load_workbook(xlsx)["提示词"]
    head = {c.value: c.column for c in ws[2]}
    return {ws.cell(r, 1).value: {k: ws.cell(r, v).value for k, v in head.items()}
            for r in range(3, ws.max_row + 1) if ws.cell(r, 1).value}


def edit(xlsx, cid, **vals):
    wb = openpyxl.load_workbook(xlsx)
    ws = wb["提示词"]
    head = {c.value: c.column for c in ws[2]}
    r = next(r for r in range(3, ws.max_row + 1) if ws.cell(r, 1).value == cid)
    for k, v in vals.items():
        ws.cell(r, head[k]).value = v
    wb.save(xlsx)


def main():
    global PORT
    srv = HTTPServer(("127.0.0.1", 0), FakeComfy)
    PORT = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="auto_gen_"))
    try:
        xlsx = tmp / "提示词测试集.xlsx"
        b = tmp / "builder.py"
        b.write_text(BUILDER.read_text(encoding="utf-8").replace(
            'OUT = Path(__file__).resolve().parent / "testset" / "提示词测试集.xlsx"', f'OUT = Path(r"{xlsx}")'),
            encoding="utf-8")
        subprocess.run([sys.executable, str(b)], check=True, capture_output=True)
        edit(xlsx, "IMG-01", 优化后提示词="写实摄影，一只橘猫。", 反向提示词="低画质", 生成状态="待生成", 画面比例="16:9")

        # 1. 正常：提交 → 排队 → 完成 → 下载
        r = run(xlsx)
        print(r.stdout)
        assert r.returncode == 0, r.stderr
        d = rows(xlsx)
        assert d["IMG-01"]["生成状态"] == "已完成", d["IMG-01"]
        assert d["IMG-01"]["结果文件"] == r"outputs\qwen_image_2_1\IMG-01.png", d["IMG-01"]["结果文件"]
        assert (tmp / "outputs" / "qwen_image_2_1" / "IMG-01.png").read_bytes() == PNG.getvalue()
        assert d["VID-01"]["生成状态"] is None, "视频行状态不是待生成，不该动"
        assert len(S["submitted"]) == 1
        g = S["submitted"][0][1]
        assert g["459:474"]["inputs"]["prompt"] == "写实摄影，一只橘猫。"
        assert g["459:474"]["inputs"]["negative_prompt"] == "低画质"
        assert g["13"]["inputs"]["aspect_ratio"] == "16:9 (Widescreen)"
        seed1 = g["459:458"]["inputs"]["seed"]
        jobs = json.loads((tmp / "outputs" / "_任务记录.json").read_text(encoding="utf-8"))
        assert jobs["IMG-01"]["状态"] == "已完成" and jobs["IMG-01"]["seed"] == seed1

        # 2. 已完成的不再提交
        r = run(xlsx)
        assert "没有要生成的行" in r.stdout and len(S["submitted"]) == 1, r.stdout

        # 3. 超时：任务留在平台上，再运行接着等，不重复提交
        S["ready_after"] = 10 ** 9
        edit(xlsx, "IMG-01", 生成状态="待生成", 结果文件=None)
        r = run(xlsx, "--ids", "IMG-01", "--timeout", "1")
        assert "没完成" in r.stdout, r.stdout
        assert rows(xlsx)["IMG-01"]["生成状态"] == "生成中"
        assert len(S["submitted"]) == 2
        assert S["submitted"][1][1]["459:458"]["inputs"]["seed"] == seed1, "同一个用例 seed 要固定"
        S["ready_after"] = 0
        r = run(xlsx, "--ids", "IMG-01")
        assert "接着等" in r.stdout and len(S["submitted"]) == 2, r.stdout
        assert rows(xlsx)["IMG-01"]["生成状态"] == "已完成"

        # 4. 平台报错 → 失败原因写清楚
        S["fail"].add("会失败的提示词")
        edit(xlsx, "IMG-01", 优化后提示词="会失败的提示词", 生成状态="待生成", 结果文件=None)
        r = run(xlsx, "--ids", "IMG-01")
        d = rows(xlsx)["IMG-01"]
        assert d["生成状态"] == "失败" and "CUDA out of memory" in (d["失败原因"] or ""), d

        # 5. 改回待生成重做：失败原因清掉
        S["fail"].clear()
        edit(xlsx, "IMG-01", 生成状态="待生成")
        r = run(xlsx, "--ids", "IMG-01")
        d = rows(xlsx)["IMG-01"]
        assert d["生成状态"] == "已完成" and d["失败原因"] is None, d

        # 6. 比例填错 → 不提交
        n = len(S["submitted"])
        edit(xlsx, "IMG-01", 生成状态="待生成", 画面比例="2:1")
        run(xlsx, "--ids", "IMG-01")
        d = rows(xlsx)["IMG-01"]
        assert d["生成状态"] == "失败" and "画面比例" in d["失败原因"] and len(S["submitted"]) == n, d
        print("全部通过")
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
