"""本地存储：配置、提示词拆解记录、自定义维度。都是 JSON 文件，放在工具目录下的 data\\ 和 config.json。"""
import json
import os
import re
import threading
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("PROMPT_SPLITTER_HOME") or BASE          # 测试时用环境变量指到临时目录
DATA, ITEMS = os.path.join(HOME, "data"), os.path.join(HOME, "data", "items")
CONFIG_PATH, CUSTOMS_PATH = os.path.join(HOME, "config.json"), os.path.join(HOME, "data", "customs.json")
DEFAULT_CONFIG = {"base_url": "", "model": "", "api_key": "", "temperature": 0, "timeout": 180, "max_tokens": 4096,
                  "json_mode": False, "use_system_proxy": True, "extra_body": {},
                  "excel_path": os.path.join(os.path.dirname(BASE), "..", "docs", "评测体系_v1.5.xlsx"),
                  "output_dir": os.path.join(os.path.dirname(BASE), "testset", "评测记录")}
_lock = threading.RLock()


class StoreError(Exception):
    pass


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------- 配置 ----------
def load_config():
    with _lock:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({k: v for k, v in _read(CONFIG_PATH, {}).items() if k in DEFAULT_CONFIG})
        return cfg


def save_config(upd):
    with _lock:
        cfg = load_config()
        for k in DEFAULT_CONFIG:
            if k == "api_key" or k not in upd:
                continue
            v = upd[k]
            if k in ("temperature", "timeout"):
                v = float(v)
            elif k == "max_tokens":
                v = int(v) if str(v).strip() else 0
            elif k in ("json_mode", "use_system_proxy"):
                v = bool(v)
            elif k == "extra_body":
                v = json.loads(v) if isinstance(v, str) and v.strip() else (v or {})
                if not isinstance(v, dict):
                    raise StoreError("额外参数必须是 JSON 对象，如 {\"enable_thinking\": false}")
            else:
                v = str(v).strip()
            cfg[k] = v
        if str(upd.get("api_key") or "").strip():
            cfg["api_key"] = str(upd["api_key"]).strip()
        if upd.get("clear_api_key"):
            cfg["api_key"] = ""
        _write(CONFIG_PATH, cfg)
        return cfg


def public_config(cfg):
    out = {k: v for k, v in cfg.items() if k != "api_key"}
    key = cfg.get("api_key") or ""
    out["has_key"], out["key_hint"] = bool(key), (f"…{key[-4:]}" if len(key) > 8 else ("已设置" if key else ""))
    return out


# ---------- 自定义维度 ----------
def load_customs():
    return _read(CUSTOMS_PATH, [])


def save_custom(c, lib_names, old_name=None):
    with _lock:
        name = str(c.get("name", "")).strip()
        if not name or re.search(r"[/／；;\n（()）]", name):
            raise StoreError("维度名不能为空，也不能含 / ； 括号 换行")
        if name in lib_names:
            raise StoreError(f"维度库里已经有「{name}」，直接用它就行")
        customs = [x for x in load_customs() if x["name"] != (old_name or name)]
        if any(x["name"] == name for x in customs):
            raise StoreError(f"已经有叫「{name}」的自定义维度")
        item = {"name": name, "definition": str(c.get("definition", "")).strip(), "checks": str(c.get("checks", "")).strip(),
                "levels": {k: str((c.get("levels") or {}).get(k, "")).strip() for k in ("L1", "L2", "L3", "L4")},
                "tasks": [str(t) for t in (c.get("tasks") or [])], "created": c.get("created") or now(), "updated": now()}
        if not item["definition"]:
            raise StoreError("请写一句定义：这个维度判什么")
        customs.append(item)
        _write(CUSTOMS_PATH, customs)
        if old_name and old_name != name:
            rename_dim_in_items(old_name, name)
        return item


def delete_custom(name):
    with _lock:
        _write(CUSTOMS_PATH, [x for x in load_customs() if x["name"] != name])


# ---------- 提示词拆解记录 ----------
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _item_path(item_id):
    if not _ID.match(item_id or ""):
        raise StoreError("编号格式不对")
    return os.path.join(ITEMS, f"{item_id}.json")


def all_items():
    if not os.path.isdir(ITEMS):
        return []
    out = []
    for fn in os.listdir(ITEMS):
        if fn.endswith(".json"):
            try:
                out.append(_read(os.path.join(ITEMS, fn), None))
            except (OSError, json.JSONDecodeError):
                continue
    return sorted([x for x in out if x], key=lambda x: x["code"])


def summary(it):
    return {k: it.get(k) for k in ("id", "code", "task", "status", "n_samples", "prefix", "updated")} | {
        "prompt": (it.get("prompt") or "")[:80], "n_req": len(it.get("requirements") or []), "has_llm": bool(it.get("llm"))}


def get_item(item_id):
    it = _read(_item_path(item_id), None)
    if it is None:
        raise StoreError("找不到这条提示词，可能已被删除")
    return it


def fixed_lists(rules, task, old=None):
    """按任务类型生成固定加评/条件加评清单；换任务类型时保留同名维度的勾选状态"""
    keep = {x["dim"]: x["on"] for x in (old or [])}
    fx = rules["fixed"].get(task) or {"fixed": [], "conditional": [], "condition": ""}
    return ([{"dim": d, "on": keep.get(d, True)} for d in fx["fixed"]],
            [{"dim": d, "on": keep.get(d, False)} for d in fx["conditional"]], fx["condition"])


def new_item(rules, task, prompt, negative="", n_samples=1):
    with _lock:
        codes = [int(m.group(1)) for x in all_items() if (m := re.match(r"^P(\d+)$", x.get("code", "")))]
        code = f"P{(max(codes) + 1 if codes else 1):03d}"
        it = {"id": f"{code}-{uuid.uuid4().hex[:6]}", "code": code, "task": task, "prompt": prompt.strip(),
              "negative": negative.strip(), "n_samples": max(1, int(n_samples or 1)), "prefix": code, "status": "未拆解",
              "requirements": [], "llm": None, "history": [], "created": now(), "updated": now()}
        it["fixed"], it["conditional"], it["condition"] = fixed_lists(rules, task)
        _write(_item_path(it["id"]), it)
        return it


EDITABLE = ("task", "prompt", "negative", "n_samples", "prefix", "status", "requirements", "fixed", "conditional")


def update_item(rules, item_id, upd):
    with _lock:
        it = get_item(item_id)
        if "task" in upd and upd["task"] != it["task"]:
            it["fixed"], it["conditional"], it["condition"] = fixed_lists(rules, upd["task"], it["fixed"] + it["conditional"])
        for k in EDITABLE:
            if k in upd and not (k in ("fixed", "conditional") and "task" in upd and upd["task"] != it["task"]):
                it[k] = upd[k]
        it["n_samples"] = max(1, min(200, int(it.get("n_samples") or 1)))
        it["prefix"] = re.sub(r"[\/:*?\"<>|\s]", "", str(it.get("prefix") or "")) or it["code"]
        it["updated"] = now()
        _write(_item_path(item_id), it)
        return it


def set_decomposition(item_id, result, rules_version):
    """写入大模型拆解结果；原清单留在 history（最多 5 份）"""
    with _lock:
        it = get_item(item_id)
        if it.get("requirements"):
            it["history"] = ([{"time": it["updated"], "requirements": it["requirements"]}] + it.get("history", []))[:5]
        it["requirements"] = [{**{k: r[k] for k in ("text", "dimension", "level", "note", "source", "confidence")},
                               "origin": "模型", "model": {k: r[k] for k in ("text", "dimension", "level")}}
                              for r in result["requirements"]]
        it["llm"] = {"model": result.get("model"), "time": now(), "usage": result.get("usage"), "rules_version": rules_version,
                     "not_split": result.get("not_split", []), "questions": result.get("questions", []), "raw": result.get("raw", "")}
        it["status"], it["updated"] = "草稿", now()
        _write(_item_path(item_id), it)
        return it


def delete_item(item_id):
    with _lock:
        p = _item_path(item_id)
        if os.path.exists(p):
            os.remove(p)


def rename_dim_in_items(old, new):
    for it in all_items():
        hit = [r for r in it.get("requirements", []) if r.get("dimension") == old]
        for r in hit:
            r["dimension"] = new
        if hit:
            _write(_item_path(it["id"]), it)


def items_using(dim):
    return [it["code"] for it in all_items() if any(r.get("dimension") == dim for r in it.get("requirements", []))]


# ---------- 提示词优化（H3）记录：不存图片原图，只存角色说明 ----------
OPT = os.path.join(HOME, "data", "optimize")


def _opt_path(run_id):
    if not _ID.match(run_id or ""):
        raise StoreError("记录编号格式不对")
    return os.path.join(OPT, f"{run_id}.json")


def save_opt(rec):
    with _lock:
        if not rec.get("id"):
            t = time.time()   # 编号带毫秒，同一秒内的多条也能按先后排序
            rec["id"] = time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + f"{int(t * 1000) % 1000:03d}-" + uuid.uuid4().hex[:4]
            rec["created"] = now()
        rec["updated"] = now()
        _write(_opt_path(rec["id"]), rec)
        return rec


def get_opt(run_id):
    rec = _read(_opt_path(run_id), None)
    if rec is None:
        raise StoreError("找不到这条优化记录，可能已被删除")
    return rec


def list_opt(limit=50):
    if not os.path.isdir(OPT):
        return []
    out = []
    for fn in sorted(os.listdir(OPT), reverse=True)[:limit]:
        if fn.endswith(".json"):
            try:
                r = _read(os.path.join(OPT, fn), None)
            except (OSError, json.JSONDecodeError):
                continue
            if r:
                out.append({k: r.get(k) for k in ("id", "mode", "duration", "created", "model")} | {
                    "prompt": (r.get("prompt") or "")[:60], "n_err": sum(1 for f in r.get("flags") or [] if f[0] == "error")})
    return out


def delete_opt(run_id):
    with _lock:
        p = _opt_path(run_id)
        if os.path.exists(p):
            os.remove(p)


# ---------- 提示词优化（Qwen-Image）记录：不存图片原图，只存图片角色说明 ----------
IMG_OPT = os.path.join(HOME, "data", "img_optimize")


def _img_opt_path(run_id):
    if not _ID.match(run_id or ""):
        raise StoreError("记录编号格式不对")
    return os.path.join(IMG_OPT, f"{run_id}.json")


def save_img_opt(rec):
    with _lock:
        if not rec.get("id"):
            t = time.time()
            rec["id"] = time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + f"{int(t * 1000) % 1000:03d}-" + uuid.uuid4().hex[:4]
            rec["created"] = now()
        rec["updated"] = now()
        os.makedirs(IMG_OPT, exist_ok=True)
        _write(_img_opt_path(rec["id"]), rec)
        return rec


def get_img_opt(run_id):
    rec = _read(_img_opt_path(run_id), None)
    if rec is None:
        raise StoreError("找不到这条优化记录，可能已被删除")
    return rec


def list_img_opt(limit=50):
    if not os.path.isdir(IMG_OPT):
        return []
    out = []
    for fn in sorted(os.listdir(IMG_OPT), reverse=True)[:limit]:
        if fn.endswith(".json"):
            try:
                r = _read(os.path.join(IMG_OPT, fn), None)
            except (OSError, json.JSONDecodeError):
                continue
            if r:
                out.append({k: r.get(k) for k in ("id", "mode", "created", "model")} | {
                    "prompt": (r.get("prompt") or "")[:60], "n_err": sum(1 for f in r.get("flags") or [] if f[0] == "error")})
    return out


def delete_img_opt(run_id):
    with _lock:
        p = _img_opt_path(run_id)
        if os.path.exists(p):
            os.remove(p)

