# -*- coding: utf-8 -*-
"""书库存储：导入图片文件夹、读写单页识别/校对结果、缩略图。"""
import os
import re
import json
import time
import hashlib
import uuid
import threading
from contextlib import contextmanager

from PIL import Image

APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(APP, "data")
BOOKS = os.path.join(DATA, "books")
JOBS = os.path.join(DATA, "jobs")
TMP = os.path.join(DATA, "tmp")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
VOL_ORDER = {"上": 0, "下": 1}

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def book_lock(bid):
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(bid), threading.RLock())
    with lock:
        yield


def _atomic_json(path, data, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".%s.tmp" % uuid.uuid4().hex
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent, separators=(",", ":") if indent is None else None)
    os.replace(tmp, path)


def _ensure():
    for d in (DATA, BOOKS, JOBS, TMP):
        os.makedirs(d, exist_ok=True)


def book_id(path):
    return hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]


def _book_dir(bid):
    if not re.fullmatch(r"[0-9a-f]{12}", str(bid or "")):
        raise ValueError("invalid book id")
    return os.path.join(BOOKS, bid)


def _paths(bid, no):
    d = _book_dir(bid)
    return (os.path.join(d, "ocr", "%04d.json" % no),
            os.path.join(d, "text", "%04d.json" % no))


def _img_size(path):
    try:
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None


def scan_folder(path):
    """扫描图片文件夹：去重复副本、解析卷/叶、按卷叶排序。

    文件名兼容多种命名：
      古本戏曲丛刊...上_04.png   → 卷上、叶4
      89鲛绡记  全_08.png        → 无卷、叶8
    """
    _ensure()
    names = [f for f in os.listdir(path) if f.lower().endswith(IMG_EXT)]
    names = [f for f in names if not re.search(r"\(\d+\)$", os.path.splitext(f)[0])]

    items = []
    for f in names:
        stem = os.path.splitext(f)[0]
        mvol = re.search(r"[._]([上下])_\d+$", stem)
        vol = mvol.group(1) if mvol else ""
        mleaf = re.search(r"(\d+)$", stem)
        leaf = int(mleaf.group(1)) if mleaf else 0
        items.append({"file": os.path.join(path, f), "vol": vol, "leaf": leaf, "name": f})
    if not items:
        raise ValueError("文件夹里没有图片: " + path)

    items.sort(key=lambda x: (VOL_ORDER.get(x["vol"], 9), x["leaf"], x["name"]))
    pages = []
    for i, it in enumerate(items, 1):
        size = _img_size(it["file"]) or (0, 0)
        pages.append({"no": i, "vol": it["vol"], "leaf": it["leaf"], "file": it["file"],
                      "w": size[0], "h": size[1], "status": "raw"})

    # 书名：去掉末尾的 "卷/上下/全 + 叶号" 后缀，取最后一个点号段，去掉开头的编号
    sample = os.path.splitext(items[0]["name"])[0]
    stem = re.sub(r"[\s._]*(?:[上下]|全)_\d+$", "", sample).strip()
    title = stem.split(".")[-1].strip() if "." in stem else stem
    title = re.sub(r"^[\d\s]+", "", title).strip()
    if not title:
        title = os.path.basename(path)

    with Image.open(items[0]["file"]) as first:
        fw, fh = first.width, first.height
    return {
        "id": book_id(path),
        "title": title,
        "source": path,
        "w": fw,
        "h": fh,
        "volumes": sorted({p["vol"] for p in pages if p["vol"]}, key=lambda v: VOL_ORDER.get(v, 9)),
        "pages": pages,
    }


def import_book(path):
    path = os.path.abspath(path)
    bid = book_id(path)
    book = scan_folder(path)
    book["id"] = bid
    for sub in ("ocr", "text", "thumb"):
        os.makedirs(os.path.join(_book_dir(bid), sub), exist_ok=True)
    save_book(book)
    return load_book(bid)


def save_book(book):
    bid = book["id"]
    with book_lock(bid):
        _atomic_json(os.path.join(_book_dir(bid), "book.json"), book, indent=1)


def load_book(bid):
    p = os.path.join(_book_dir(bid), "book.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        book = json.load(f)
    dirty = False
    # 用文件存在性刷新每页状态；补齐每页真实像素尺寸（老数据没有 w/h）
    for pg in book["pages"]:
        ocr, text = _paths(bid, pg["no"])
        pg["status"] = "corrected" if os.path.exists(text) else ("ocr" if os.path.exists(ocr) else "raw")
        if not pg.get("w") or not pg.get("h"):
            s = _img_size(pg["file"])
            if s:
                pg["w"], pg["h"] = s
                dirty = True
    if dirty:
        save_book(book)
    return book


def list_books():
    if not os.path.isdir(BOOKS):
        return []
    out = []
    for bid in sorted(os.listdir(BOOKS)):
        try:
            b = load_book(bid)
        except Exception:
            continue
        if not b:
            continue
        out.append({
            "id": b["id"], "title": b["title"], "source": b["source"],
            "w": b["w"], "h": b["h"], "volumes": b["volumes"],
            "pages": len(b["pages"]),
            "done": sum(1 for p in b["pages"] if p["status"] != "raw"),
            "corrected": sum(1 for p in b["pages"] if p["status"] == "corrected"),
            "has_punct": os.path.exists(punct_path(bid)),
        })
    return out


def get_page_info(bid, no):
    book = load_book(bid)
    if not book:
        return None
    for pg in book["pages"]:
        if pg["no"] == no:
            return pg
    return None


def image_path(bid, no):
    pg = get_page_info(bid, no)
    return pg["file"] if pg else None


def read_page(bid, no):
    """优先读校对稿，其次识别稿，都没有返回 None。"""
    ocr, text = _paths(bid, no)
    for p, kind in ((text, "corrected"), (ocr, "ocr")):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            d["status"] = kind
            d["page"] = no
            return d
    return None


def save_ocr(bid, no, data):
    with book_lock(bid):
        _atomic_json(_paths(bid, no)[0], data)
        _touch_status(bid, no)


def save_text(bid, no, data):
    with book_lock(bid):
        _atomic_json(_paths(bid, no)[1], data)
        _touch_status(bid, no, corrected=True)


def archive_text(bid, no):
    """重新识别前把旧校对稿改名为 .bak 移走，否则旧稿会遮住新识别结果。"""
    p = _paths(bid, no)[1]
    if os.path.exists(p):
        bak = p + ".bak"
        try:
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(p, bak)
        except OSError:
            pass


def punct_path(bid):
    return os.path.join(_book_dir(bid), "punct.json")


def read_punct(bid):
    p = punct_path(bid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError("punct.json 损坏，已拒绝读取以免覆盖：" + str(e))


def save_punct(bid, data):
    with book_lock(bid):
        _atomic_json(punct_path(bid), data)


def save_punct_if_rev(bid, data, expected_rev):
    """仅在当前 rev 等于 expected_rev 时写入，避免双窗口最后写入覆盖。"""
    with book_lock(bid):
        cur = read_punct(bid)
        if cur is None and int(expected_rev) > 0:
            return False
        if cur is not None and int(cur.get("rev") or 0) != int(expected_rev):
            return False
        _atomic_json(punct_path(bid), data)
        return True


def delete_punct(bid):
    with book_lock(bid):
        p = punct_path(bid)
        if os.path.exists(p):
            os.remove(p)


def _punct_history_dir(bid):
    return os.path.join(_book_dir(bid), "punct_history")


def push_punct_history(bid, data, limit=30):
    """保存前把旧稿存一份快照；最多保留 limit 份。"""
    d = _punct_history_dir(bid)
    os.makedirs(d, exist_ok=True)
    rev = int((data or {}).get("rev") or 0)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}_r{rev}.json"
    path = os.path.join(d, name)
    with book_lock(bid):
        _atomic_json(path, data or {})
    files = sorted((x for x in os.listdir(d) if x.endswith(".json")), reverse=True)
    for old in files[limit:]:
        try:
            os.remove(os.path.join(d, old))
        except Exception:
            pass
    return name


def list_punct_history(bid):
    d = _punct_history_dir(bid)
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted((x for x in os.listdir(d) if x.endswith(".json")), reverse=True):
        path = os.path.join(d, name)
        try:
            with open(path, encoding="utf-8") as f:
                obj = json.load(f)
            out.append({
                "name": name,
                "rev": int(obj.get("rev") or 0),
                "saved_at": obj.get("updated_at") or "",
                "chars": len(obj.get("text") or ""),
                "marks": len(obj.get("marks") or []),
                "structures": len(obj.get("structures") or []),
            })
        except Exception:
            continue
    return out


def read_punct_history(bid, name):
    if not name or not re.fullmatch(r"[0-9]{8}-[0-9]{6}_r[0-9]+\.json", str(name)):
        return None
    path = os.path.join(_punct_history_dir(bid), name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_job(bid, job):
    os.makedirs(JOBS, exist_ok=True)
    with book_lock(bid):
        _atomic_json(os.path.join(JOBS, bid + ".json"), job or {})


def read_job(bid):
    path = os.path.join(JOBS, bid + ".json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


SETTINGS_PATH = os.path.join(DATA, "settings.json")
DEFAULT_SETTINGS = {
    "default_scope": "book",
    "auto_struct": True,
    "baike_cand": True,
    "bai_model": False,
    "conf": "80",
    "a_auto": True,
    "b_cand": True,
    "c_skip": True,
    "default_review": True,
    "auto_next": True,
    "show_struct": True,
    "autosave": True,
    "undo": 30,
    # OpenAI-compatible endpoint configuration.  The API key is stored locally
    # in app/data/settings.json (which is gitignored) and is never returned by
    # the HTTP API in clear text.
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
    "llm_reasoning_effort": "",
    "llm_temperature": "",
    "llm_timeout": 600,
    "llm_extra_body": "",
}


def read_settings():
    data = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                data.update(json.load(f) or {})
        except Exception:
            pass
    return data


def public_settings():
    """Settings safe to expose to the browser (never include the API key)."""
    data = read_settings()
    key = str(data.pop("llm_api_key", "") or "")
    data["llm_api_key_set"] = bool(key)
    return data


def save_settings(data):
    _ensure()
    merged = dict(DEFAULT_SETTINGS)
    merged.update(read_settings())
    merged.update(data or {})
    _atomic_json(SETTINGS_PATH, merged, indent=1)
    return merged


def _touch_status(bid, no, corrected=False):
    book = load_book(bid)
    if not book:
        return
    for pg in book["pages"]:
        if pg["no"] == no:
            pg["status"] = "corrected" if corrected else ("ocr" if pg["status"] == "raw" else pg["status"])
            break
    save_book(book)


def thumb(bid, no, width=220):
    """生成/复用页缩略图，返回文件路径。"""
    d = os.path.join(_book_dir(bid), "thumb")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "%04d_%d.jpg" % (no, width))
    if os.path.exists(out):
        return out
    src = image_path(bid, no)
    if not src:
        return None
    with Image.open(src) as _im:
        im = _im.convert("RGB")
        h = max(24, round(width * im.height / im.width))
        im.resize((width, h), Image.LANCZOS).save(out, "JPEG", quality=85)
    return out
