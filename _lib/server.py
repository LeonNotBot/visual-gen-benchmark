"""提示词拆解助手 · 本地服务。只监听本机 127.0.0.1；接口需要启动时随机生成的令牌，其他网页调不了。"""
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import export  # noqa: E402
import llm  # noqa: E402
import optimize  # noqa: E402
import optimize_img  # noqa: E402
import store  # noqa: E402
from rules import NO_DIM, RulesCache, RulesError  # noqa: E402

TOKEN = secrets.token_urlsafe(18)
WEB, LOG = os.path.join(BASE, "web"), os.path.join(store.HOME, "logs", "server.log")
TYPES = {".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
_cache = {"path": None, "rc": None}


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{store.now()}] {msg}\n")


def rules(cfg):
    if _cache["path"] != cfg["excel_path"]:
        _cache.update(path=cfg["excel_path"], rc=RulesCache(cfg["excel_path"]))
    return _cache["rc"].get()


def rules_payload(R, cfg, customs):
    keys = ("no", "module", "name", "definition", "checks", "levels", "usage", "inputs")
    return {"source": R["source"], "path": cfg["excel_path"], "version": R["version"], "problems": R["problems"],
            "dims": [{k: d[k] for k in keys} for d in R["dims"]], "route": R["route"], "fixed": R["fixed"],
            "tasks": R["tasks"], "levels": R["levels"], "example": R["example"], "no_dim": NO_DIM,
            "allowed": {t: list(llm.allowed_dims(R, t, customs)) for t in R["tasks"]}}


def split_prompt(text):
    """支持直接粘贴 {"prompt": …, "negative_prompt": …} 这种 JSON"""
    t = str(text or "").strip()
    if t.startswith("{"):
        try:
            o = json.loads(t)
            if isinstance(o, dict) and isinstance(o.get("prompt"), str):
                neg = o.get("negative_prompt") or o.get("negative") or ""
                return o["prompt"], neg if isinstance(neg, str) else ""
        except json.JSONDecodeError:
            pass
    return t, ""


def state(cfg):
    customs, out = store.load_customs(), {"rules": None, "rules_error": ""}
    try:
        out["rules"] = rules_payload(rules(cfg), cfg, customs)
    except (RulesError, OSError) as e:
        out["rules_error"] = str(e)
    return out | {"customs": customs, "config": store.public_config(cfg), "h3": optimize.public_modes(),
                  "img": optimize_img.public_modes(),
                  "items": [store.summary(x) for x in store.all_items()]}


IMG_RE = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")


def clean_opt_input(b):
    """校验优化请求；图片只收 png/jpeg/webp 的 data URL"""
    mode = b.get("mode")
    if mode not in optimize.MODES:
        raise ValueError(f"未知模式：{mode}")
    prompt = str(b.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("先写下你想要的视频内容")
    if len(prompt) > 8000:
        raise ValueError("提示词太长了（超过 8000 字）")
    try:
        dur = round(float(b.get("duration")), 2)
    except (TypeError, ValueError):
        raise ValueError("时长要填数字（秒）")
    if not 4 <= dur <= 15:
        raise ValueError("H3 支持 4–15 秒，请改一下时长")
    images = []
    for im in b.get("images") or []:
        data = str((im or {}).get("data") or "")
        if not IMG_RE.match(data):
            raise ValueError("图片只支持 PNG、JPG、WebP")
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("单张图片太大（压缩后仍超过 6MB），请换一张小一点的")
        images.append({"data": data, "role": str(im.get("role") or "").strip()[:200]})
    assets = {k: [{"desc": str((a or {}).get("desc") or "").strip()[:1000]} for a in ((b.get("assets") or {}).get(k) or [])]
              for k in ("videos", "audios")}
    slots, lim = optimize.MODES[mode]["slots"], optimize.REF_LIMITS
    if slots is not None:
        if len(images) != len(slots):
            raise ValueError(f"{optimize.MODES[mode]['name']}要传：{'、'.join(slots)}" if slots else "文生视频不用传图")
        assets = {}
    else:
        if any(not a["desc"] for k in assets for a in assets[k]):
            raise ValueError("参考视频/音频要写一句描述：内容是什么、在目标视频里怎么用")
        n = {"images": len(images), "videos": len(assets["videos"]), "audios": len(assets["audios"])}
        if not sum(n.values()):
            raise ValueError("全参考模式至少要有一份参考素材（图片、视频或音频）")
        for k, cn in (("images", "图片"), ("videos", "视频"), ("audios", "音频")):
            if n[k] > lim[k]:
                raise ValueError(f"{cn}最多 {lim[k]} 份")
        if sum(n.values()) > lim["total"]:
            raise ValueError(f"素材总数最多 {lim['total']} 份")
    return mode, prompt, dur, images, assets


def img_opt_api(method, parts, b, cfg):
    if len(parts) == 1 and method == "GET":
        return {"img_runs": store.list_img_opt()}
    if len(parts) == 1 and method == "POST":
        mode = str(b.get("mode") or "").strip()
        prompt = str(b.get("prompt") or "").strip()
        if mode not in optimize_img.MODES:
            raise ValueError(f"未知模式：{mode}")
        if not prompt:
            raise ValueError("先写下你的图片需求")
        if len(prompt) > 5000:
            raise ValueError("需求太长了（超过 5000 字）")
        image = str((b.get("image") or {}).get("data") or "") if mode == "Edit" else None
        if mode == "Edit" and not image:
            raise ValueError("图片编辑模式要上传输入图")
        if mode == "Edit" and not IMG_RE.match(image):
            raise ValueError("图片只支持 PNG、JPG、WebP")
        if mode == "Edit" and len(image) > 8 * 1024 * 1024:
            raise ValueError("图片太大（压缩后仍超过 6MB）")
        t0 = time.time()
        res = optimize_img.optimize(cfg, mode, prompt, image)
        sec = round(time.time() - t0, 1)
        rec = store.save_img_opt({"mode": mode, "prompt": prompt, "seconds": sec,
                                  "image": {"role": "输入图"} if image else None} | res)
        log(f"图片优化 {rec['id']}（{mode}）{res['rounds']} 轮，{sec}s，usage={res.get('usage')}")
        return {"run": rec, "img_runs": store.list_img_opt()}
    if len(parts) == 2 and method == "GET":
        return store.get_img_opt(parts[1])
    if len(parts) == 2 and method == "PUT":
        rec = store.get_img_opt(parts[1])
        pos = str(b.get("result") or "").strip()
        neg = str(b.get("negative") or "").strip()
        flags = optimize_img.check(pos, neg)
        rec.update(result=pos, negative=neg, flags=flags, edited=True)
        return {"run": store.save_img_opt(rec), "img_runs": store.list_img_opt()}
    if len(parts) == 2 and method == "DELETE":
        store.delete_img_opt(parts[1])
        return {"img_runs": store.list_img_opt()}
    raise ValueError("没有这个接口")


def opt_api(method, parts, b, cfg):
    if len(parts) == 1 and method == "GET":
        return {"runs": store.list_opt()}
    if len(parts) == 1 and method == "POST":
        mode, prompt, dur, images, assets = clean_opt_input(b)
        t0 = time.time()
        res = optimize.optimize(cfg, mode, prompt, dur, images, assets)
        sec = round(time.time() - t0, 1)
        n_err = sum(1 for f in res["flags"] if f[0] == "error")
        rec = store.save_opt({"mode": mode, "prompt": prompt, "duration": dur, "seconds": sec,
                              "images": [{"role": im["role"]} for im in images], "assets": assets} | res)
        log(f"优化 {rec['id']}（{mode}，{dur}s，{len(images)} 张图）{res['rounds']} 轮，{n_err} 处格式错误，{sec}s，usage={res.get('usage')}")
        return {"run": rec, "runs": store.list_opt()}
    if len(parts) == 2 and method == "GET":
        return store.get_opt(parts[1])
    if len(parts) == 2 and method == "PUT":   # 人工改过改写结果后重新检查
        rec = store.get_opt(parts[1])
        text = str(b.get("result") or "").strip()
        counts = {"images": len(rec.get("images") or []), "videos": len((rec.get("assets") or {}).get("videos") or []),
                  "audios": len((rec.get("assets") or {}).get("audios") or [])}
        text, fixes = optimize.fix_instruction(rec["mode"], text, rec["duration"])
        rec.update(result=text, fixes=fixes, flags=optimize.check(rec["mode"], text, rec["duration"], counts), edited=True)
        return {"run": store.save_opt(rec), "runs": store.list_opt()}
    if len(parts) == 2 and method == "DELETE":
        store.delete_opt(parts[1])
        return {"runs": store.list_opt()}
    raise ValueError("没有这个接口")


def precheck(R, items):
    lib = {d["name"] for d in R["dims"]} | {c["name"] for c in store.load_customs()}
    errs, warns = [], []
    for it in items:
        e, w = export.check_item(it, lib)
        errs, warns = errs + e, warns + w
    rows = sum(len(export._rows(it)) for it in items if it.get("requirements"))
    return {"errors": errs, "warnings": warns, "rows": rows, "samples": sum(int(it["n_samples"]) for it in items)}


def api(method, parts, b):
    cfg, head = store.load_config(), parts[0]
    if head == "state" and method == "GET":
        return state(cfg)
    if head == "reload" and method == "POST":
        _cache["path"] = None
        return state(cfg)
    if head == "config" and method == "POST":
        store.save_config(b)
        _cache["path"] = None
        return state(store.load_config())
    if head == "test" and method == "POST":
        t0 = time.time()
        reply, _ = llm.chat(cfg, [{"role": "user", "content": "请只回复两个字母：OK"}])
        return {"reply": reply.strip()[:200], "seconds": round(time.time() - t0, 1), "model": cfg["model"]}
    if head == "optimize":   # 不依赖评测体系 Excel，读不到 Excel 也能用
        return opt_api(method, parts, b, cfg)
    if head == "img-optimize":
        return img_opt_api(method, parts, b, cfg)
    R = rules(cfg)
    if head == "items":
        if method == "POST" and len(parts) == 1:
            task = b.get("task") or R["tasks"][0]
            if task not in R["fixed"]:
                raise ValueError(f"未知任务类型：{task}")
            texts = b.get("prompts") if isinstance(b.get("prompts"), list) else [b.get("prompt", "")]
            made = [store.new_item(R, task, *split_prompt(t), n_samples=b.get("n_samples", 1))
                    for t in texts if str(t).strip() or len(texts) == 1]
            return {"created": [x["id"] for x in made], "items": [store.summary(x) for x in store.all_items()]}
        if len(parts) == 2 and method == "GET":
            return store.get_item(parts[1])
        if len(parts) == 2 and method == "PUT":
            if b.get("task") and b["task"] not in R["fixed"]:
                raise ValueError(f"未知任务类型：{b['task']}")
            if "prompt" in b:
                p, neg = split_prompt(b["prompt"])
                b["prompt"], b["negative"] = p, (neg or b.get("negative", ""))
            it = store.update_item(R, parts[1], b)
            return {"item": it, "summary": store.summary(it)}
        if len(parts) == 2 and method == "DELETE":
            store.delete_item(parts[1])
            return {"ok": True}
        if len(parts) == 3 and parts[2] == "decompose" and method == "POST":
            it = store.get_item(parts[1])
            if not it["prompt"].strip():
                raise ValueError("提示词是空的，先填提示词再拆解")
            text = it["prompt"] + (f"\n反向提示词（不希望出现的内容）：{it['negative']}" if it.get("negative") else "")
            t0 = time.time()
            res = llm.decompose(cfg, R, it["task"], text, store.load_customs())
            sec = round(time.time() - t0, 1)
            log(f"拆解 {it['code']}（{it['task']}）{len(res['requirements'])} 条，{sec}s，usage={res.get('usage')}")
            it = store.set_decomposition(parts[1], res, R["version"])
            return {"item": it, "summary": store.summary(it), "seconds": sec}
    if head == "customs" and method == "POST":
        lib = {d["name"] for d in R["dims"]}
        if len(parts) == 2 and parts[1] == "delete":
            used = store.items_using(b.get("name", ""))
            if used and not b.get("force"):
                return {"used": used}
            store.delete_custom(b.get("name", ""))
        else:
            store.save_custom(b, lib, b.get("old_name"))
        return state(cfg)
    if head == "precheck" and method == "POST":
        return precheck(R, [store.get_item(i) for i in b.get("ids") or []])
    if head == "export" and method == "POST":
        items = [store.get_item(i) for i in b.get("ids") or []]
        try:
            res = export.export(items, R, store.load_customs(), cfg["excel_path"], cfg["output_dir"],
                                keep_examples=bool(b.get("keep_examples")), write_customs=b.get("write_customs", True),
                                file_name=b.get("file_name", ""), append_to=b.get("append_to", ""))
        except PermissionError:
            raise export.ExportError("文件被占用（可能正在 Excel 里打开），关掉后再导出")
        log(f"{'追加到' if res['appended'] else '导出'} {res['file']}：{len(items)} 条提示词，{res['samples']} 个样本，{res['rows']} 行")
        return res | {"download": f"/download?f={urllib.parse.quote(res['file'])}&t={TOKEN}"}
    if head == "exports" and method == "GET":
        return {"files": export.list_exports(cfg["output_dir"])}
    if head == "open" and method == "POST":
        os.makedirs(cfg["output_dir"], exist_ok=True)
        os.startfile(cfg["output_dir"])  # 本机 Windows：打开导出文件夹
        return {"ok": True}
    raise ValueError("没有这个接口")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        for k, v in {"Content-Type": ctype, "Content-Length": str(len(data)), "Cache-Control": "no-store",
                     "X-Content-Type-Options": "nosniff", **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _file(self, path, ctype, extra=None):
        with open(path, "rb") as f:
            return self._send(200, f.read(), ctype, extra)

    def _route(self, method):
        if (self.headers.get("Host") or "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._send(403, {"error": "只允许本机访问"})
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if method == "GET" and u.path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
                    html = f.read().replace("{{TOKEN}}", TOKEN)
                csp = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:"
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": csp})
            if method == "GET" and u.path.startswith("/static/"):
                name = os.path.basename(urllib.parse.unquote(u.path[8:]))
                ext, path = os.path.splitext(name)[1], os.path.join(WEB, name)
                if ext not in TYPES or not os.path.isfile(path):
                    return self._send(404, {"error": "没有这个文件"})
                return self._file(path, TYPES[ext] + "; charset=utf-8")
            if method == "GET" and u.path == "/download":
                if q.get("t", [""])[0] != TOKEN:
                    return self._send(403, {"error": "令牌不对，请刷新页面"})
                name = os.path.basename(q.get("f", [""])[0])
                path = os.path.join(store.load_config()["output_dir"], name)
                if not name.endswith(".xlsx") or not os.path.isfile(path):
                    return self._send(404, {"error": "文件不存在"})
                disp = "attachment; filename*=UTF-8''" + urllib.parse.quote(name)
                return self._file(path, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  {"Content-Disposition": disp})
            if not u.path.startswith("/api/"):
                return self._send(404, {"error": "没有这个地址"})
            if self.headers.get("X-Token") != TOKEN:
                return self._send(403, {"error": "令牌不对，请刷新页面（服务重启过）"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 40 * 1024 * 1024:   # 全参考模式最多 9 张图（页面上已压缩）
                return self._send(413, {"error": "内容太大"})
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            parts = [urllib.parse.unquote(p) for p in u.path[5:].strip("/").split("/")]
            return self._send(200, api(method, parts, body))
        except (store.StoreError, export.ExportError, llm.LLMError, RulesError, ValueError) as e:
            log(f"{method} {u.path} -> {type(e).__name__}: {e}")
            return self._send(400, {"error": str(e), "raw": getattr(e, "raw", "")})
        except Exception as e:  # noqa: BLE001  未预料的错误也给前端一个能看懂的提示
            log(f"{method} {u.path} -> {traceback.format_exc()}")
            return self._send(500, {"error": f"程序出错：{e}（详情见 logs\\server.log）"})


class Server(ThreadingHTTPServer):
    """独占端口。Windows 下默认允许多个进程绑同一端口，旧窗口没关时请求会被旧版本接走"""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    port = int(os.environ.get("PROMPT_SPLITTER_PORT") or 0)
    for p in ([port] if port else range(8765, 8786)):
        try:
            srv = Server(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    else:
        print("端口 8765–8785 都被占用了，请关掉其他程序再试")
        return
    url = f"http://127.0.0.1:{srv.server_port}/"
    print(f"提示词拆解助手已启动：{url}\n浏览器没自动打开的话，把上面的地址复制到浏览器里。\n用完直接关掉这个窗口即可。")
    log(f"启动 {url}")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
