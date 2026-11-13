# -*- coding: utf-8 -*-
"""auto_generate.py 视频部分自测：假 pgx-h3（按接口文档的规则校验 Origin、Cookie、幂等键）+ 临时目录。
运行：python scripts\\tests\\test_auto_generate_video.py
"""
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
MP4 = b"\x00\x00\x00\x18ftypmp42fake-video-bytes"
S = {"sessions": 0, "assets": {}, "jobs": {}, "keys": {}, "polls": {}, "ready_after": 2, "fail_prompts": set(),
     "bad_origin": 0}


class FakePgx(BaseHTTPRequestHandler):
    def _send(self, code, obj, ctype="application/json", headers=None):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _cookie_ok(self):
        return "pgxh3_session=sess-1" in (self.headers.get("Cookie") or "")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("Origin") != BASE:
            S["bad_origin"] += 1
            return self._send(403, {"detail": "请从本应用页面发起请求。"})
        if self.path == "/api/session":
            assert body == b"{}", body
            if not self._cookie_ok():
                S["sessions"] += 1
            return self._send(200, {"ready": True}, headers={
                "Set-Cookie": "pgxh3_session=sess-1; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict"})
        if not self._cookie_ok():
            return self._send(401, {"detail": "会话需要重新连接，请稍后重试。"})
        if self.path == "/api/assets":
            assert self.headers["Content-Type"] in ("image/png", "image/jpeg", "image/webp")
            aid = f"asset-{len(S['assets']) + 1}"
            S["assets"][aid] = len(body)
            return self._send(201, {"id": aid, "previewUrl": f"/api/assets/{aid}"})
        if self.path == "/api/jobs":
            key, req = self.headers.get("Idempotency-Key"), json.loads(body)
            assert key, "缺 Idempotency-Key"
            allowed = {"prompt", "mode", "width", "height", "seconds", "firstFrameId", "lastFrameId"}
            if set(req) - allowed:
                return self._send(422, {"detail": f"多余字段 {set(req) - allowed}"})
            if key in S["keys"]:
                jid, old = S["keys"][key]
                if old != req:
                    return self._send(409, {"detail": "幂等键参数冲突"})
                return self._send(202, S["jobs"][jid])
            jid = f"job-{len(S['jobs']) + 1}"
            S["jobs"][jid] = {"id": jid, "state": "queued", "queueAhead": 1, "request": req, "mediaUrl": None}
            S["keys"][key] = (jid, req)
            return self._send(202, S["jobs"][jid])
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if not self._cookie_ok():
            return self._send(401, {"detail": "会话需要重新连接，请稍后重试。"})
        parts = u.path.strip("/").split("/")
        if parts[:2] == ["api", "jobs"] and len(parts) == 3:
            j = S["jobs"].get(parts[2])
            if not j:
                return self._send(404, {"detail": "not found"})
            n = S["polls"][j["id"]] = S["polls"].get(j["id"], 0) + 1
            if n >= S["ready_after"]:
                if j["request"]["prompt"] in S["fail_prompts"]:
                    j.update(state="failed", message="显存不足")
                else:
                    j.update(state="completed", mediaUrl=f"/api/jobs/{j['id']}/video", endToEndMs=1234)
            elif n >= 1:
                j.update(state="running", phase="h3_draft")
            return self._send(200, j)
        if parts[:2] == ["api", "jobs"] and len(parts) == 4 and parts[3] == "video":
            assert urllib.parse.parse_qs(u.query).get("download") == ["1"]
            return self._send(200, MP4, "video/mp4")
        self._send(404, {"detail": "not found"})

    def log_message(self, *a):
        pass


def run(xlsx, *extra):
    env = dict(os.environ, EVAL_PGX_URL=BASE, EVAL_COMFY_URL="http://127.0.0.1:9", PYTHONIOENCODING="utf-8")
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


def last_job():
    return S["jobs"][f"job-{len(S['jobs'])}"]


def main():
    global BASE
    srv = HTTPServer(("127.0.0.1", 0), FakePgx)
    BASE = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="auto_gen_v_"))
    try:
        xlsx = tmp / "提示词测试集.xlsx"
        b = tmp / "builder.py"
        b.write_text(BUILDER.read_text(encoding="utf-8").replace(
            'OUT = Path(__file__).resolve().parent / "testset" / "提示词测试集.xlsx"', f'OUT = Path(r"{xlsx}")'),
            encoding="utf-8")
        subprocess.run([sys.executable, str(b)], check=True, capture_output=True)
        (tmp / "参考图").mkdir()
        Image.new("RGB", (320, 180), "gray").save(tmp / "参考图" / "VID-02_首帧.jpg", "JPEG")
        edit(xlsx, "VID-01", 优化后提示词="integrated_multimodal_description: valley.", 生成状态="待生成")
        edit(xlsx, "VID-02", 优化后提示词="For the target video ... projector.", 生成状态="待生成")

        # 1. 文生视频 + 首帧生视频：建会话、传首帧、提交、轮询、下载
        r = run(xlsx, "--ids", "VID-01,VID-02")
        print(r.stdout)
        assert r.returncode == 0, r.stderr
        d = rows(xlsx)
        for cid in ("VID-01", "VID-02"):
            assert d[cid]["生成状态"] == "已完成", (cid, d[cid])
            assert d[cid]["结果文件"] == rf"outputs\minimax_h3\{cid}.mp4"
            assert (tmp / "outputs" / "minimax_h3" / f"{cid}.mp4").read_bytes() == MP4
        reqs = {j["request"]["prompt"][:10]: j["request"] for j in S["jobs"].values()}
        t2va = reqs["integrated"]
        assert t2va == {"prompt": "integrated_multimodal_description: valley.", "mode": "t2va",
                        "width": 1280, "height": 720, "seconds": 10}, t2va
        fl2va = reqs["For the ta"]
        assert fl2va["mode"] == "fl2va" and fl2va["seconds"] == 6 and fl2va["firstFrameId"] in S["assets"]
        assert "lastFrameId" not in fl2va, "首帧生视频不该传 lastFrameId"
        assert S["sessions"] == 1 and S["bad_origin"] == 0
        assert (tmp / "_凭据" / "pgx-h3.cookies").exists(), "Cookie 要存下来"
        jobs = json.loads((tmp / "outputs" / "_任务记录.json").read_text(encoding="utf-8"))
        assert jobs["VID-01"]["平台"] == "pgx-h3" and jobs["VID-01"]["端到端毫秒"] == 1234

        # 2. 第二次运行复用 Cookie，不新建会话；超时后接着等，不重复提交
        S["ready_after"] = 10 ** 9
        edit(xlsx, "VID-01", 生成状态="待生成", 结果文件=None)
        n = len(S["jobs"])
        r = run(xlsx, "--ids", "VID-01", "--timeout", "1")
        assert "没完成" in r.stdout and rows(xlsx)["VID-01"]["生成状态"] == "生成中", r.stdout
        assert S["sessions"] == 1, "已有 Cookie 不该新建会话"
        assert len(S["jobs"]) == n + 1, "改回待生成要用新的幂等键重新提交"
        S["ready_after"] = 0
        r = run(xlsx, "--ids", "VID-01")
        assert "接着等" in r.stdout and len(S["jobs"]) == n + 1, r.stdout
        assert rows(xlsx)["VID-01"]["生成状态"] == "已完成"

        # 3. 平台执行失败 → 写平台给的原因
        S["fail_prompts"].add("会失败的视频")
        edit(xlsx, "VID-01", 优化后提示词="会失败的视频", 生成状态="待生成")
        run(xlsx, "--ids", "VID-01")
        d = rows(xlsx)["VID-01"]
        assert d["生成状态"] == "失败" and "显存不足" in (d["失败原因"] or ""), d

        # 4. 参数不合法 → 不提交
        cases = [
            ("VID-01", {"视频时长（秒）": 4}, "5–15"),
            ("VID-01", {"视频时长（秒）": 7.5}, "整数"),
            ("VID-01", {"画面比例": "1:1"}, "16:9"),
            ("VID-02", {"参考图": None}, "1 张参考图"),
            ("VID-02", {"任务类型": "尾帧生视频"}, "不支持只给尾帧"),
            ("VID-02", {"任务类型": "全参考生视频"}, "没有参考生视频"),
        ]
        orig = rows(xlsx)
        for cid, vals, want in cases:
            n = len(S["jobs"])
            edit(xlsx, cid, 生成状态="待生成", 失败原因=None, **vals)
            run(xlsx, "--ids", cid)
            got = rows(xlsx)[cid]
            assert got["生成状态"] == "失败" and want in (got["失败原因"] or ""), (vals, got["失败原因"])
            assert len(S["jobs"]) == n, f"{vals} 填错了还提交了"
            edit(xlsx, cid, **{k: orig[cid][k] for k in vals})
        assert S["bad_origin"] == 0
        print("全部通过")
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
