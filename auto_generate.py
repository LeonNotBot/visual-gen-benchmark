# -*- coding: utf-8 -*-
"""批量生成：读 提示词测试集.xlsx，调用指定模型配置里的平台出图/出视频，下载后回填测试集。
只处理「生成状态」是「待生成」或「生成中」的行；失败的行把状态改回「待生成」就会重做。
提交后把任务号记在 outputs\\_任务记录.json；中途关掉再运行，会接着等已提交的任务，不重复提交。

模型配置文件放在 eval-bench/models/*.json，每个文件对应一个模型版本。
不传 --model 时，图片默认用 qwen_image_2_1，视频默认用 minimax_h3。

用法：
  python auto_generate.py [--file 提示词测试集.xlsx]
                          [--model qwen_image_2_1]       # 只跑这个模型支持的任务类型
                          [--model-img A --model-vid B]  # 图片和视频分别指定
                          [--ids IMG-01] [--timeout 900]
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent
DEFAULT_FILE = ROOT / "testset" / "提示词测试集.xlsx"
MODELS_DIR_CANDIDATES = [Path(__file__).resolve().parent / "models"]   # 和脚本放在一起，scripts\ 和 eval-bench\ 都一样
T2I_TEMPLATE_DEFAULT = Path(__file__).resolve().parent / "comfy_templates" / "qwen_image_2_1_t2i_api.json"
NEEDED = ("用例ID", "任务类型", "参考图", "视频时长（秒）", "画面比例",
          "优化后提示词", "反向提示词", "生成状态", "结果文件", "失败原因")
RATIOS = {"16:9": "16:9 (Widescreen)", "9:16": "9:16 (Portrait Widescreen)", "1:1": "1:1 (Square)",
          "4:3": "4:3 (Standard)", "3:4": "3:4 (Portrait Standard)"}
VIDEO_SIZE_MAP = {"16:9": {"720p": (1280, 720), "480p": (832, 480)},
                  "9:16": {"720p": (720, 1280), "480p": (480, 832)}}
IMAGE_TASKS = {"文生图"}
VIDEO_TASKS = {"文生视频": ("t2va", 0), "首帧生视频": ("fl2va", 1), "首尾帧生视频": ("fl2va", 2)}
UNSUPPORTED = {"尾帧生视频": "平台只支持首帧或首尾帧，不支持只给尾帧",
               "全参考生视频": "平台公共接口没有参考生视频（只有文生视频和首帧/首尾帧）",
               "图片编辑": "图片编辑还没接（工作流已确认，需要时再加）"}
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
SPLIT = re.compile(r"[;；\n]+")


class GenError(Exception):
    pass


# ---------- 工具函数 ----------
def no_proxy():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_raw(method, url, body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"} if data else {}, method=method)
    try:
        with no_proxy().open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise GenError(f"平台返回 {e.code}：{e.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GenError(f"连不上生成平台 {url.split('/')[2]}：{getattr(e, 'reason', e)}")


def http_json(method, url, body=None, timeout=30):
    return json.loads(http_raw(method, url, body, timeout).decode("utf-8"))


def seed_for(cid):
    return int(hashlib.sha256(cid.encode("utf-8")).hexdigest()[:15], 16)


def prompt_hash(*parts):
    return hashlib.sha256("\x00".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()[:16]


def ref_images(cell, base):
    out = []
    for raw in SPLIT.split(str(cell or "")):
        raw = raw.strip().strip('"')
        if raw:
            p = Path(raw)
            p = p if p.is_absolute() else base / p
            if not p.is_file():
                raise GenError(f"找不到参考图：{raw}")
            out.append(p)
    return out


# ---------- 模型配置 ----------
def find_model_cfg(model_id):
    """读 models/<model_id>.json，找不到就报错"""
    for d in MODELS_DIR_CANDIDATES:
        p = d / f"{model_id}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    searched = ", ".join(str(d) for d in MODELS_DIR_CANDIDATES)
    raise GenError(f"找不到模型配置 {model_id}.json（搜索了：{searched}）")


def default_cfg(task_type):
    """没指定模型时的默认配置：优先读 models\\ 里的文件，没有才用内置值。
    自测时通过环境变量把平台地址指到假服务，这里也要生效。"""
    mid = "qwen_image_2_1" if task_type in IMAGE_TASKS else "minimax_h3"
    try:
        cfg = find_model_cfg(mid)
        if task_type in IMAGE_TASKS and os.environ.get("EVAL_COMFY_URL"):
            cfg["comfyui_url"] = os.environ["EVAL_COMFY_URL"]
        if task_type not in IMAGE_TASKS and os.environ.get("EVAL_PGX_URL"):
            cfg["pgx_h3_url"] = os.environ["EVAL_PGX_URL"]
        return cfg
    except GenError:
        pass
    # 兜底内置值（公开版本不含默认 IP，必须通过环境变量或 models/*.json 配置）
    if task_type in IMAGE_TASKS:
        url = os.environ.get("EVAL_COMFY_URL")
        if not url:
            raise GenError("未设置 ComfyUI 地址环境变量 EVAL_COMFY_URL，请先配置：setx EVAL_COMFY_URL http://你的ComfyUI地址:8188")
        return {"id": "qwen_image_2_1", "name": "Qwen-Image 2.1", "platform": "comfyui",
                "comfyui_url": url, "workflow_template": None, "megapixels": 0.92}
    url = os.environ.get("EVAL_PGX_URL")
    if not url:
        raise GenError("未设置视频平台地址环境变量 EVAL_PGX_URL，请先配置：setx EVAL_PGX_URL http://你的视频平台地址:8082")
    return {"id": "minimax_h3", "name": "MiniMax H3", "platform": "pgx_h3",
            "pgx_h3_url": url, "default_resolution": "720p"}


def resolve_cfg(model_id, task_type, img_cfg, vid_cfg):
    """返回这条用例应该用的模型配置"""
    is_img = task_type in IMAGE_TASKS
    # 如果调用方传了明确的 cfg，直接用
    explicit = img_cfg if is_img else vid_cfg
    if explicit:
        return explicit
    # 如果传了通用 --model，检查它支持这个任务类型
    if model_id:
        cfg = find_model_cfg(model_id)
        if task_type in (cfg.get("task_types") or []):
            return cfg
        # 通用 --model 不覆盖这个任务类型，回退默认
    return default_cfg(task_type)


# ---------- ComfyUI 平台 ----------
def _comfy_url(cfg):
    url = os.environ.get("EVAL_COMFY_URL") or cfg.get("comfyui_url")
    if not url:
        raise GenError("没有 ComfyUI 地址：请设置环境变量 EVAL_COMFY_URL（如 http://<ComfyUI地址>:8188）")
    return url.rstrip("/")


def _comfy_template(cfg):
    tpl = cfg.get("workflow_template")
    if tpl:
        t = Path(__file__).resolve().parent / tpl
        if not t.exists():
            raise GenError(f"找不到 ComfyUI 工作流文件：{t}")
        return t
    return T2I_TEMPLATE_DEFAULT


def comfy_submit_case(row, cid, cfg):
    ratio = str(row.get("画面比例") or "16:9").strip()
    if ratio not in RATIOS:
        raise GenError(f"画面比例只能是 {' / '.join(RATIOS)}，现在是「{ratio}」")
    seed = seed_for(cid)
    t = _comfy_template(cfg)
    g = json.loads(t.read_text(encoding="utf-8"))
    g["459:474"]["inputs"]["prompt"] = row["优化后提示词"]
    g["459:474"]["inputs"]["negative_prompt"] = str(row.get("反向提示词") or "")
    g["459:458"]["inputs"]["seed"] = seed
    g["13"]["inputs"]["aspect_ratio"] = RATIOS[ratio]
    if cfg.get("megapixels"):
        g["13"]["inputs"]["megapixels"] = str(cfg["megapixels"])
    g["461"]["inputs"]["filename_prefix"] = f"eval_{cid}"
    r = http_json("POST", f"{_comfy_url(cfg)}/prompt", {"prompt": g, "client_id": "eval-auto-generate"})
    if r.get("node_errors"):
        raise GenError(f"工作流校验失败：{json.dumps(r['node_errors'], ensure_ascii=False)[:300]}")
    return {"平台": "ComfyUI", "模型": cfg.get("id", ""), "模型名": cfg.get("name", ""),
            "地址": _comfy_url(cfg), "任务号": r["prompt_id"], "seed": seed}


def comfy_poll(job, cfg):
    url, pid = _comfy_url(cfg), job["任务号"]
    h = http_json("GET", f"{url}/history/{pid}").get(pid)
    if h:
        st = h.get("status") or {}
        if st.get("status_str") == "error":
            msg = next((m[1].get("exception_message") for m in st.get("messages") or []
                        if m[0] == "execution_error"), None) or "生成出错"
            return "error", msg.strip()[:300]
        if st.get("completed") or h.get("outputs"):
            imgs = [im for out in h.get("outputs", {}).values() for im in out.get("images", [])
                    if im.get("type") == "output"]
            return ("done", imgs[0]) if imgs else ("error", "生成完成但没有输出图片")
    q = http_json("GET", f"{url}/queue")
    if any(item[1] == pid for item in q.get("queue_running", [])):
        return "running", 0
    pending = [item[1] for item in sorted(q.get("queue_pending", []), key=lambda x: x[0])]
    if pid in pending:
        return "queued", pending.index(pid)
    return "lost", None


def comfy_download(img_info, dest, cfg):
    qs = urllib.parse.urlencode({"filename": img_info["filename"],
                                 "subfolder": img_info.get("subfolder", ""), "type": "output"})
    data = http_raw("GET", f"{_comfy_url(cfg)}/view?{qs}", timeout=120)
    dest = dest.with_suffix(Path(img_info["filename"]).suffix or ".png")
    dest.write_bytes(data)
    return dest


# ---------- pgx-h3 平台 ----------
class Pgx:
    ERRORS = {401: "会话缺失或过期", 403: "Host 或 Origin 不对",
              404: "任务或素材不存在（可能不属于当前会话）", 409: "幂等键参数冲突",
              413: "请求体太大", 422: "参数不合法", 429: "队列已满，稍后重试",
              503: "平台暂时不可用", 507: "存储空间不足"}

    def __init__(self, cookie_file, cfg):
        url = os.environ.get("EVAL_PGX_URL") or cfg.get("pgx_h3_url")
        if not url:
            raise GenError("没有视频平台地址：请设置环境变量 EVAL_PGX_URL（如 http://<视频平台地址>:8082）")
        self.base = url.rstrip("/")
        self.res = cfg.get("default_resolution", "720p")
        cookie_file.parent.mkdir(exist_ok=True)
        self.jar = MozillaCookieJar(str(cookie_file))
        if cookie_file.exists():
            self.jar.load(ignore_discard=True, ignore_expires=False)
        self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                              urllib.request.HTTPCookieProcessor(self.jar))
        self.ready = False

    def _req(self, method, path, data=None, ctype=None, headers=None, timeout=30, raw=False):
        h = dict(headers or {})
        if method in ("POST", "DELETE"):
            h["Origin"] = self.base
        if ctype:
            h["Content-Type"] = ctype
        req = urllib.request.Request(self.base + path, data, h, method=method)
        try:
            with self.op.open(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            try:
                detail = json.loads(detail).get("detail") or detail
            except (ValueError, AttributeError):
                pass
            raise GenError(f"视频平台返回 {e.code}（{self.ERRORS.get(e.code, '服务异常')}）：{detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GenError(f"连不上视频平台 {self.base}：{getattr(e, 'reason', e)}") from e
        return body if raw else json.loads(body.decode("utf-8"))

    def session(self):
        if not self.ready:
            self._req("POST", "/api/session", b"{}", "application/json")
            self.jar.save(ignore_discard=True, ignore_expires=False)
            self.ready = True

    def upload(self, img_path):
        mime = MIME.get(img_path.suffix.lower())
        if not mime:
            raise GenError(f"参考图只支持 PNG、JPG、WebP：{img_path.name}")
        data = img_path.read_bytes()
        if len(data) > 20 * 1024 * 1024:
            raise GenError(f"参考图超过 20MB：{img_path.name}")
        return self._req("POST", "/api/assets", data, mime, timeout=120)["id"]

    def submit(self, body, key):
        last = None
        for attempt in range(3):
            try:
                return self._req("POST", "/api/jobs", json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 "application/json", {"Idempotency-Key": key}, timeout=60)
            except GenError as e:
                last = e
                if not re.search(r"连不上|返回 (500|503)", str(e)):
                    raise
                time.sleep(3 * (attempt + 1))
        raise last

    def poll(self, job_id):
        j = self._req("GET", f"/api/jobs/{urllib.parse.quote(job_id)}")
        st = j.get("state")
        if st == "completed":
            return ("done", j) if j.get("mediaUrl") else ("error", "任务完成但没有视频文件")
        if st in ("failed", "cancelled", "interrupted"):
            what = {"failed": "执行失败", "cancelled": "已取消", "interrupted": "被中止"}[st]
            return "error", f"{what}：{j.get('message') or '平台没给原因'}"
        if st == "queued":
            return "queued", j.get("queueAhead", 0)
        return "running", j.get("phase") or st

    def download(self, job, dest):
        url = job["mediaUrl"]
        data = self._req("GET", url + ("&" if "?" in url else "?") + "download=1", timeout=600, raw=True)
        dest.with_suffix(".mp4").write_bytes(data)
        return dest.with_suffix(".mp4")


def pgx_submit_case(pgx, row, cid, base, h, attempt, cfg, run_id="local"):
    """提交 pgx-h3 视频生成任务，返回任务记录。幂等键包含 run_id，避免跨运行复用旧任务。"""
    task = row["任务类型"]
    mode, n_img = VIDEO_TASKS[task]
    ratio = str(row.get("画面比例") or "16:9").strip()
    if ratio not in VIDEO_SIZE_MAP:
        raise GenError(f"视频画面比例只能是 16:9（横屏）或 9:16（竖屏），现在是「{ratio}」")
    sizes = VIDEO_SIZE_MAP[ratio]
    if pgx.res not in sizes:
        raise GenError(f"模型配置里的分辨率「{pgx.res}」不支持，只能是 {' / '.join(sizes)}")
    w, hgt = sizes[pgx.res]
    try:
        sec = float(row["视频时长（秒）"])
    except (TypeError, ValueError):
        raise GenError("要填「视频时长（秒）」")
    if sec != int(sec) or not 5 <= sec <= 15:
        raise GenError(f"视频平台时长只支持 5–15 的整数秒，现在是 {row['视频时长（秒）']}")
    prompt = str(row["优化后提示词"]).strip()
    if len(prompt) > 4000:
        raise GenError(f"提示词 {len(prompt)} 字，超过平台上限 4000 字")
    imgs = ref_images(row.get("参考图"), base)
    if len(imgs) != n_img:
        raise GenError(f"{task}要 {n_img} 张参考图，现在是 {len(imgs)} 张" + ("（首帧;尾帧）" if n_img == 2 else ""))
    body = {"prompt": prompt, "mode": mode, "width": w, "height": hgt, "seconds": int(sec)}
    pgx.session()
    if n_img >= 1:
        body["firstFrameId"] = pgx.upload(imgs[0])
    if n_img == 2:
        body["lastFrameId"] = pgx.upload(imgs[1])
    safe = re.sub(r"[^A-Za-z0-9_-]", "", cid)[:40] or "case"
    run_safe = re.sub(r"[^A-Za-z0-9_-]", "", run_id)[:30]
    key = f"eval-{run_safe}-{safe}-{h}-{attempt}"
    job = pgx.submit(body, key)
    return {"平台": "pgx-h3", "模型": cfg.get("id", ""), "模型名": cfg.get("name", ""),
            "地址": pgx.base, "任务号": job["id"], "幂等键": key, "尝试": attempt,
            "模式": mode, "分辨率": f"{w}x{hgt}", "时长": int(sec),
            "提交时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------- 表格和任务记录 ----------
def header(ws):
    for r in range(1, 6):
        cols = {str(c.value).strip(): c.column for c in ws[r] if c.value}
        if "用例ID" in cols:
            missing = [n for n in NEEDED if n not in cols]
            if missing:
                sys.exit(f"表里缺这些列：{'、'.join(missing)}")
            return r, cols
    sys.exit("前 5 行里找不到「用例ID」列，确认用的是 提示词测试集 模板")


def save_xlsx(wb, path):
    while True:
        try:
            wb.save(path)
            return
        except PermissionError:
            input(f"\n保存失败：{path.name} 正在被 Excel 打开。关掉 Excel 后按回车重试……")


def load_jobs(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_jobs(path, jobs):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(description="批量生成图片/视频")
    ap.add_argument("--file", default=str(DEFAULT_FILE), help="提示词测试集 Excel 路径")
    ap.add_argument("--model", default="", help="模型配置 ID（models/<id>.json），同时用于图片和视频（按 task_types 匹配）")
    ap.add_argument("--model-img", default="", help="图片任务用的模型配置 ID（覆盖 --model）")
    ap.add_argument("--model-vid", default="", help="视频任务用的模型配置 ID（覆盖 --model）")
    ap.add_argument("--ids", default="", help="只处理这些用例ID，逗号分隔")
    ap.add_argument("--timeout", type=int, default=900, help="最多等多少秒")
    ap.add_argument("--interval", type=float, default=5, help="查询进度的间隔秒数")
    a = ap.parse_args()

    path = Path(a.file).resolve()
    only = {x.strip() for x in a.ids.split(",") if x.strip()}
    if not path.exists():
        sys.exit(f"找不到文件：{path}")
    if (path.parent / f"~${path.name}").exists():
        sys.exit(f"{path.name} 正在被 Excel 打开，先关掉再运行")

    # 模型配置
    try:
        img_cfg = find_model_cfg(a.model_img) if a.model_img else None
        vid_cfg = find_model_cfg(a.model_vid) if a.model_vid else None
    except GenError as e:
        sys.exit(str(e))

    out_dir = path.parent / "outputs"   # 结果按模型分子目录：outputs\<模型ID>\<用例ID>.png
    out_dir.mkdir(exist_ok=True)
    jobs_path = out_dir / "_任务记录.json"
    jobs = load_jobs(jobs_path)
    # Cookie 默认在测试集旁的 _凭据\；run_bench 用工作副本时通过环境变量指回原测试集那边
    cred_dir = Path(os.environ.get("EVAL_CRED_DIR") or path.parent / "_凭据")
    run_id = os.environ.get("EVAL_RUN_ID", "local")  # 幂等键包含运行ID，避免跨运行复用旧任务

    def model_out(cfg):
        d = out_dir / (cfg.get("id") or "unknown")
        d.mkdir(exist_ok=True)
        return d

    # 进度文件（run_bench.py 传进来的）
    status_file = os.environ.get("EVAL_STATUS_FILE")

    def tick_status(done_count, total_count, failed_count):
        if not status_file:
            return
        p = Path(status_file)
        try:
            obj = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (json.JSONDecodeError, OSError):
            obj = {}
        obj["progress"] = {"total": total_count, "done": done_count, "failed": failed_count}
        obj["message"] = f"已完成 {done_count}/{total_count} 条" + (f"，失败 {failed_count} 条" if failed_count else "")
        obj["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    wb = openpyxl.load_workbook(path)
    ws = wb["提示词"] if "提示词" in wb.sheetnames else wb.active
    hr, col = header(ws)
    rows = []
    for r in range(hr + 1, ws.max_row + 1):
        row = {name: ws.cell(r, c).value for name, c in col.items()}
        row = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        if row["用例ID"] and row["生成状态"] in ("待生成", "生成中") and (not only or row["用例ID"] in only):
            rows.append((r, row))
    if not rows:
        print("没有要生成的行（「生成状态」不是 待生成 / 生成中）")
        return

    backup = path.parent / "备份"
    backup.mkdir(exist_ok=True)
    shutil.copy2(path, backup / f"{path.stem}_生成前_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")

    pgx_clients = {}   # 每个 pgx_h3_url 共用一个客户端

    def get_pgx(cfg):
        url = cfg.get("pgx_h3_url", "")
        if url not in pgx_clients:
            pgx_clients[url] = Pgx(cred_dir / "pgx-h3.cookies", cfg)
        return pgx_clients[url]

    def put(r, name, v):
        ws.cell(r, col[name]).value = v

    def fail(r, cid, msg):
        put(r, "生成状态", "失败")
        put(r, "失败原因", msg)
        print(f"{cid}：{msg}")

    # 第一步：提交
    waiting = []
    for r, row in rows:
        cid, task = row["用例ID"], row["任务类型"]
        if task in UNSUPPORTED:
            fail(r, cid, f"不能自动生成：{UNSUPPORTED[task]}")
            continue
        if task not in IMAGE_TASKS and task not in VIDEO_TASKS:
            fail(r, cid, f"不认识的任务类型「{task}」")
            continue
        if not row["优化后提示词"]:
            fail(r, cid, "还没有「优化后提示词」，先运行自动优化")
            continue
        try:
            cfg = resolve_cfg(a.model, task, img_cfg, vid_cfg)
        except GenError as e:
            fail(r, cid, str(e))
            continue
        platform = cfg.get("platform", "comfyui" if task in IMAGE_TASKS else "pgx_h3")
        h = prompt_hash(task, row["优化后提示词"], row.get("反向提示词"), row.get("画面比例"),
                        row.get("视频时长（秒）"), row.get("参考图"), cfg.get("id", ""))
        job = jobs.get(cid)
        try:
            if job and job.get("提示词指纹") == h and job.get("状态") == "生成中":
                print(f"{cid}：上次已提交（任务号 {job['任务号']}），接着等")
            else:
                if platform == "comfyui":
                    job = comfy_submit_case(row, cid, cfg)
                    note = f"seed {job['seed']}，模型 {cfg.get('name', '')}"
                elif platform == "pgx_h3":
                    attempt = (job.get("尝试", 0) + 1) if job and job.get("平台") == "pgx-h3" else 1
                    job = pgx_submit_case(get_pgx(cfg), row, cid, path.parent, h, attempt, cfg, run_id)
                    note = f"{job['模式']} {job['分辨率']} {job['时长']} 秒，模型 {cfg.get('name', '')}"
                else:
                    raise GenError(f"不支持的平台「{platform}」，请在 auto_generate.py 里加对应的适配")
                job |= {"提示词指纹": h, "状态": "生成中",
                        "提交时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                jobs[cid] = job
                save_jobs(jobs_path, jobs)
                print(f"{cid}：已提交，任务号 {job['任务号']}，{note}")
        except GenError as e:
            fail(r, cid, f"提交失败：{e}")
            continue
        put(r, "生成状态", "生成中")
        put(r, "失败原因", None)
        waiting.append((r, cid, cfg))
    save_xlsx(wb, path)

    # 第二步：等结果、下载
    deadline = time.time() + a.timeout
    last_msg = {}
    while waiting and time.time() < deadline:
        for r, cid, cfg in list(waiting):
            job = jobs[cid]
            platform = job.get("平台", "ComfyUI")
            try:
                if platform == "ComfyUI":
                    st, info = comfy_poll(job, cfg)
                    if st == "done":
                        dest = comfy_download(info, model_out(cfg) / cid, cfg)
                else:
                    pgx = get_pgx(cfg)
                    st, info = pgx.poll(job["任务号"])
                    if st == "done":
                        dest = pgx.download(info, model_out(cfg) / cid)
            except GenError as e:
                if "401" in str(e) or "404" in str(e):
                    st, info = "lost", f"{e}。会话 Cookie 可能丢了，把状态改回「待生成」重新提交"
                else:
                    print(f"{cid}：查询进度失败（{e}），稍后重试")
                    continue
            if st == "done":
                job.update(状态="已完成", 完成时间=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 文件=dest.name)
                if platform != "ComfyUI":
                    job.update(端到端毫秒=info.get("endToEndMs"), 排队毫秒=info.get("queueElapsedMs"),
                               生成毫秒=info.get("generationElapsedMs"))
                put(r, "生成状态", "已完成")
                put(r, "结果文件", str(dest.relative_to(path.parent)))
                print(f"{cid}：完成，已保存 {dest.relative_to(path.parent)}")
            elif st in ("error", "lost"):
                reason = info or "平台上找不到这个任务，把状态改回「待生成」重新提交"
                job.update(状态="失败", 失败原因=reason)
                fail(r, cid, f"生成失败：{reason}")
            else:
                msg = f"排队中，前面还有 {info} 个" if st == "queued" else f"正在生成（{info}）"
                if last_msg.get(cid) != msg:
                    print(f"{cid}：{msg}")
                    last_msg[cid] = msg
                continue
            waiting.remove((r, cid, cfg))
            save_jobs(jobs_path, jobs)
            save_xlsx(wb, path)
            _done = sum(1 for j in jobs.values() if j.get("状态") == "已完成")
            _fail = sum(1 for j in jobs.values() if j.get("状态") == "失败")
            tick_status(_done, len(rows), _fail)
        if waiting:
            time.sleep(a.interval)

    if waiting:
        print(f"\n等了 {a.timeout} 秒还有 {len(waiting)} 条没完成：{'、'.join(c for _, c, _ in waiting)}。"
              "再运行一次就会接着下载，不会重复提交。")
    done = sum(1 for _, row in rows if jobs.get(row["用例ID"], {}).get("状态") == "已完成")
    print(f"\n结束。本次处理的行里已完成 {done} 条。结果在 {out_dir}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。已提交的任务还在平台上，下次运行会接着等，不会重复提交。")
