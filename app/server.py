# -*- coding: utf-8 -*-
"""古籍整理工作台 · 本地服务（M1 最小闭环）

启动：  python app/server.py    →  http://127.0.0.1:8770
"""
import os
import re
import sys
import json
import time
import shutil
import hashlib
import tempfile
import uuid
import zipfile
import io
import threading
import subprocess
from typing import Optional
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Query, Request, Body
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)
import store
import pipeline
import charlib
import punctgen
import llm_punct
import llm_client
import structure_new

STATIC = os.path.join(APP, "static")
app = FastAPI(title="古籍整理工作台")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class LLMProbeReq(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    reasoning_effort: str = ""
    temperature: str = ""
    timeout: int = 600
    extra_body: str = ""
    clear_api_key: bool = False

@app.middleware("http")
async def _no_cache_static(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp

@app.exception_handler(RuntimeError)
def _runtime_error_handler(request, exc):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/api/llm/status")
def api_llm_status():
    """No network probe here: status reflects saved configuration only."""
    return llm_client.public_config()


@app.get("/api/llm/config")
def api_llm_config():
    return llm_client.public_config()


@app.put("/api/llm/config")
def api_llm_config_save(req: dict = Body(...)):
    try:
        return llm_client.save_config(req or {})
    except llm_client.LLMConfigError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/llm/models")
def api_llm_models(req: LLMProbeReq):
    try:
        models = llm_client.list_models(req.model_dump())
        return {"ok": True, "models": models}
    except llm_client.LLMConfigError as exc:
        raise HTTPException(400, str(exc))
    except llm_client.LLMError as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/llm/test")
def api_llm_test(req: LLMProbeReq):
    try:
        return llm_client.test_connection(req.model_dump())
    except llm_client.LLMConfigError as exc:
        raise HTTPException(400, str(exc))
    except llm_client.LLMError as exc:
        raise HTTPException(502, str(exc))


_RECO_LOCK = threading.Lock()

# 整本识别任务（单机单任务；内存状态，服务重启即丢）
_BATCH_LOCK = threading.Lock()
_BATCH = {
    "running": False, "bid": None, "total": 0, "done": 0,
    "current": 0, "current_leaf": 0, "chars": 0,
    "pages": [], "errors": [], "started": 0, "elapsed": 0,
    "cancel": False, "cancelled": False,
}

# 整本初版句读任务（每个书一个；单机内存状态）


class ImportReq(BaseModel):
    path: str


class SaveReq(BaseModel):
    rows: list
    w: float = 0
    h: float = 0


class RowReq(BaseModel):
    box: list
    chars: Optional[list] = None


class ManualReq(BaseModel):
    rows: list


class ConvertReq(BaseModel):
    text: str = ""
    dir: str = "t2s"


class ConvertCharsReq(BaseModel):
    chars: list
    dir: str = "t2s"


class ExportTextReq(BaseModel):
    title: str = ""
    text: str = ""
    simp: int = 1
    heading: int = 1
    font: str = "宋体"
    size: float = 12


class PunctReq(BaseModel):
    text: str = ""
    breaks: Optional[list] = None
    marks: Optional[list] = None
    structures: Optional[list] = None
    meta: Optional[dict] = None
    rev: Optional[int] = None


class PunctRunReq(BaseModel):
    scope: str = "book"
    page: Optional[int] = None
    act: Optional[str] = None


class SettingsReq(BaseModel):
    data: dict = {}


class PunctRestoreReq(BaseModel):
    name: str = ""


def _ensure_no_tasks(bid):
    """破坏性/恢复入口：本项目一次只处理一本书，存在任一写稿任务时拒绝。"""
    with _BATCH_LOCK:
        if _BATCH.get("running"):
            raise HTTPException(409, "整本 OCR 任务正在运行，请先停止")
    with _SP_LOCK:
        if any((j or {}).get("running") for j in _SP_JOBS.values()):
            raise HTTPException(409, "句读整本初版正在运行，请先停止")


_MAINT = set()


@contextmanager
def _book_maintenance(bid):
    """破坏性操作期间阻止新任务启动；进入前复查所有任务。"""
    with _TASK_LOCK:
        _ensure_no_tasks(bid)
        _MAINT.add(bid)
    try:
        yield
    finally:
        with _TASK_LOCK:
            _MAINT.discard(bid)


def _book_or_404(bid):
    if not re.fullmatch(r"[0-9a-f]{12}", str(bid or "")):
        raise HTTPException(404, "book not found")
    b = store.load_book(bid)
    if not b:
        raise HTTPException(404, "book not found")
    return b


@app.on_event("startup")
def _warm_models():
    """后台预热 OCR 模型：避免第一整本识别/第一页等 15 秒。"""
    def warm():
        try:
            with _RECO_LOCK:
                pipeline.models()
        except Exception:
            pass
    threading.Thread(target=warm, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/health")
def health():
    return {"ok": True, "service": "guji-workbench-m1"}


# ---------------- 离线字库：候选 / 查字 / 繁简 ----------------
@app.get("/api/char/cands")
def api_char_cands(ch: str = Query(..., min_length=1, max_length=4), n: int = Query(24, ge=1, le=60)):
    try:
        return {"ch": ch, "cands": charlib.candidates(ch, n)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/char/info")
def api_char_info(ch: str = Query(..., min_length=1, max_length=4)):
    try:
        return charlib.char_info(ch)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/char/lookup")
def api_char_lookup(mode: str = "text", q: str = "", strokes: Optional[str] = None, n: int = Query(60, ge=1, le=200)):
    try:
        return charlib.lookup(mode, q, strokes, n)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/char/radicals")
def api_char_radicals():
    return {"radicals": charlib.radicals()}


@app.post("/api/convert")
def api_convert(req: ConvertReq):
    try:
        return {"text": charlib.convert_text(req.text, req.dir)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/convert/chars")
def api_convert_chars(req: ConvertCharsReq):
    if len(req.chars) > 2000:
        raise HTTPException(400, "一次最多 2000 字")
    try:
        return {"items": [charlib.convert_text(str(ch), req.dir) for ch in req.chars]}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/books")
def api_books():
    return store.list_books()


@app.delete("/api/books/{bid}")
def api_delete_book(bid: str):
    """从书库删除一本书（只删 app/data 里的副本，不动原始图片文件夹）。"""
    book = _book_or_404(bid)
    _ensure_no_tasks(bid)
    with _BATCH_LOCK:
        if _BATCH["running"] and _BATCH["bid"] == bid:
            raise HTTPException(409, "这本书正在整本识别，请先停止再删除")
    with _book_maintenance(bid):
        target = os.path.join(store.BOOKS, bid)
        try:
            shutil.rmtree(target)
        except Exception as e:
            raise HTTPException(500, "删除失败：%s" % e)
    return {"ok": True, "id": bid, "title": book["title"]}


@app.post("/api/import")
def api_import(req: ImportReq):
    path = req.path.strip().strip('"')
    if not path or not os.path.isdir(path):
        raise HTTPException(400, "文件夹不存在：" + path)
    try:
        book = store.import_book(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "book": {"id": book["id"], "title": book["title"], "pages": len(book["pages"])}}


@app.get("/api/books/{bid}")
def api_book(bid: str):
    b = _book_or_404(bid)
    return {"id": b["id"], "title": b["title"], "source": b["source"], "w": b["w"], "h": b["h"],
            "volumes": b["volumes"],
            "pages": [{"no": p["no"], "vol": p["vol"], "leaf": p["leaf"], "status": p["status"]}
                      for p in b["pages"]]}


@app.get("/api/books/{bid}/pages/{no}")
def api_page(bid: str, no: int):
    _book_or_404(bid)
    pg = store.get_page_info(bid, no)
    if not pg:
        raise HTTPException(404, "page not found")
    data = store.read_page(bid, no)
    if data is None:
        return {"page": no, "vol": pg["vol"], "leaf": pg["leaf"], "status": "raw",
                "w": pg.get("w") or store.load_book(bid)["w"],
                "h": pg.get("h") or store.load_book(bid)["h"], "rows": []}
    if pg.get("w") and pg.get("h"):
        data["w"], data["h"] = pg["w"], pg["h"]   # 每页真实尺寸，避免框错位
    data["vol"] = pg["vol"]
    data["leaf"] = pg["leaf"]
    return data


@app.post("/api/books/{bid}/pages/{no}/recognize")
def api_recognize(bid: str, no: int):
    b = _book_or_404(bid)
    pg = store.get_page_info(bid, no)
    if not pg:
        raise HTTPException(404, "page not found")
    tmp = os.path.join(store.TMP, bid)
    os.makedirs(tmp, exist_ok=True)
    with _RECO_LOCK:
        try:
            result = pipeline.recognize_page(pg["file"], tmp)
        except Exception as e:  # OCR 失败要能报出来
            raise HTTPException(500, "识别失败: %s" % e)
    store.archive_text(bid, no)   # 旧校对稿改名 .bak，避免遮住新结果
    data = {"page": no, "w": result["w"], "h": result["h"], "rows": result["rows"],
            "stats": result["stats"]}
    store.save_ocr(bid, no, data)
    data["status"] = "ocr"
    data["vol"] = pg["vol"]
    data["leaf"] = pg["leaf"]
    return data


# ---------------- 整本批量识别（进度 / 停止） ----------------
def _batch_snapshot():
    with _BATCH_LOCK:
        st = dict(_BATCH)
        st["pages"] = list(_BATCH["pages"])
        st["errors"] = list(_BATCH["errors"])
        return st


def _batch_set(**kw):
    with _BATCH_LOCK:
        _BATCH.update(kw)


def _batch_worker(bid, pages):
    tmp = os.path.join(store.TMP, bid, "batch")
    os.makedirs(tmp, exist_ok=True)
    t0 = time.time()
    try:
        for i, pg in enumerate(pages, 1):
            if _BATCH["cancel"]:
                _batch_set(cancelled=True)
                break
            _batch_set(current=pg["no"], current_leaf=pg["leaf"])
            try:
                with _RECO_LOCK:
                    res = pipeline.recognize_page(pg["file"], tmp)
                data = {"page": pg["no"], "w": res["w"], "h": res["h"],
                        "rows": res["rows"], "stats": res["stats"]}
                store.save_ocr(bid, pg["no"], data)
                with _BATCH_LOCK:
                    _BATCH["pages"].append(pg["no"])
                    _BATCH["chars"] += int(res["stats"].get("chars") or 0)
            except Exception as e:
                with _BATCH_LOCK:
                    _BATCH["errors"].append({"page": pg["no"], "error": str(e)[:200]})
            _batch_set(done=i)
    finally:
        with _BATCH_LOCK:
            _BATCH["running"] = False
            _BATCH["elapsed"] = round(time.time() - t0, 1)
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/books/{bid}/batch-recognize")
def api_batch_recognize(bid: str):
    book = _book_or_404(bid)
    with _TASK_LOCK:
        if bid in _MAINT:
            raise HTTPException(409, "这本书正在维护，请稍后重试")
        with _SP_LOCK:
            if any((j or {}).get("running") for j in _SP_JOBS.values()):
                raise HTTPException(409, "句读整本初版正在运行，不能同时整本 OCR")
        with _BATCH_LOCK:
            if _BATCH["running"]:
                raise HTTPException(409, "已有识别任务在跑，先停止或等它结束")
            pages = [pg for pg in book["pages"] if pg["status"] == "raw"]
            if not pages:
                raise HTTPException(400, "没有未识别的叶，都识别过了")
            _BATCH.update(running=True, bid=bid, total=len(pages), done=0, current=0,
                          current_leaf=0, chars=0, pages=[], errors=[],
                          started=time.time(), elapsed=0, cancel=False, cancelled=False)
    threading.Thread(target=_batch_worker, args=(bid, pages), daemon=True).start()
    return {"ok": True, "total": len(pages)}


@app.get("/api/books/{bid}/batch-status")
def api_batch_status(bid: str):
    st = _batch_snapshot()
    if st["bid"] != bid:
        return {"running": False, "bid": None, "total": 0, "done": 0,
                "current": 0, "current_leaf": 0, "chars": 0, "pages": [], "errors": []}
    return st


@app.post("/api/books/{bid}/batch-cancel")
def api_batch_cancel(bid: str):
    with _BATCH_LOCK:
        if not _BATCH["running"] or _BATCH["bid"] != bid:
            return {"ok": True, "running": False}
        _BATCH["cancel"] = True
    return {"ok": True, "running": True}


# ---------------- 系统文件夹选择（本机，新版 IFileOpenDialog） ----------------
PICK_SCRIPT = os.path.join(APP, "pick_folder.ps1")


@app.post("/api/pick-folder")
def api_pick_folder(dry: int = 0):
    """弹出 Windows 新版文件夹选择框（IFileOpenDialog，自动置顶）；dry=1 只编译自测。"""
    if os.name != "nt":
        raise HTTPException(501, "仅支持 Windows 本机")
    if not os.path.exists(PICK_SCRIPT):
        raise HTTPException(500, "缺少 pick_folder.ps1")
    tmpdir = os.path.join(store.TMP, "_pick")
    os.makedirs(tmpdir, exist_ok=True)
    out = os.path.join(tmpdir, "picked.txt")
    if os.path.exists(out):
        try:
            os.remove(out)
        except OSError:
            pass
    cmd = ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", PICK_SCRIPT,
           "-OutFile", out, "-Title", "选择扫描图片文件夹"]
    if dry:
        cmd.append("-Dry")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60 if dry else 900)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "选择超时")
    except Exception as e:
        raise HTTPException(500, "打不开选择窗口：%s" % e)
    if dry:
        ok = "compiled-ok" in (r.stdout or "")
        return {"path": "", "dry": True, "compiled": ok, "stderr": (r.stderr or "")[-300:]}
    path = ""
    if os.path.exists(out):
        try:
            with open(out, encoding="utf-8") as f:
                path = f.read().strip()
        except OSError:
            path = ""
        try:
            os.remove(out)
        except OSError:
            pass
    return {"path": path}


@app.post("/api/books/{bid}/pages/{no}/save")
def api_save(bid: str, no: int, req: SaveReq):
    _book_or_404(bid)
    pg = store.get_page_info(bid, no)
    if not pg:
        raise HTTPException(404, "page not found")
    rows = req.rows
    chars = sum(len(r.get("chars") or []) for r in rows)
    red = sum(1 for r in rows for c in (r.get("chars") or []) if c.get("flag") == "red")
    book = store.load_book(bid)
    pw = pg.get("w") or (req.w if req.w and req.w > 0 else book["w"])
    ph = pg.get("h") or (req.h if req.h and req.h > 0 else book["h"])
    data = {"page": no, "w": pw, "h": ph,
            "rows": rows, "stats": {"rows": len(rows), "chars": chars, "red": red}}
    store.save_text(bid, no, data)
    return {"ok": True, "stats": data["stats"]}


def _page_ctx(bid, no):
    b = _book_or_404(bid)
    pg = store.get_page_info(bid, no)
    if not pg:
        raise HTTPException(404, "page not found")
    tmp = os.path.join(store.TMP, bid)
    os.makedirs(tmp, exist_ok=True)
    return pg, tmp


@app.post("/api/books/{bid}/pages/{no}/recognize-row")
def api_recognize_row(bid: str, no: int, req: RowReq):
    """按给定栏框（可带字框）重识一栏 —— 框调整后的「重识选中栏」。"""
    pg, tmp = _page_ctx(bid, no)
    if not req.box or len(req.box) != 4:
        raise HTTPException(400, "box 需要 [x0,y0,x1,y1]")
    with _RECO_LOCK:
        try:
            row = pipeline.recognize_row(pg["file"], [float(v) for v in req.box], req.chars, tmp)
        except Exception as e:
            raise HTTPException(500, "识别失败: %s" % e)
    return {"row": row}


@app.post("/api/books/{bid}/pages/{no}/recognize-manual")
def api_recognize_manual(bid: str, no: int, req: ManualReq):
    """按当前框结构重识整页。"""
    pg, tmp = _page_ctx(bid, no)
    if not req.rows:
        raise HTTPException(400, "rows 为空")
    with _RECO_LOCK:
        try:
            res = pipeline.recognize_rows(pg["file"], req.rows, tmp)
        except Exception as e:
            raise HTTPException(500, "识别失败: %s" % e)
    return res


FONTS = ("宋体", "楷体", "黑体", "仿宋")

# 与前端 stripPunct 保持一致：这些字符不算“原字”
_MARK_CHARS = set("。，、；：？！「」『』《》（）〈〉【】〔〕——……·“”‘’…—,.;:?!()[]<>\"' \t\r\n\u3000")


def _strip_marks(s):
    return "".join(ch for ch in (s or "") if ch not in _MARK_CHARS)


def _row_text(r):
    if r.get("chars"):
        return "".join((c.get("text") or "") for c in (r.get("chars") or []))
    return r.get("text") or ""


def _book_orig_stripped(bid):
    """当前校字稿的“纯原字”串（按页序、按栏序），用于校验标点稿。"""
    book = store.load_book(bid)
    parts = []
    rec = 0
    for pg in book["pages"]:
        d = store.read_page(bid, pg["no"])
        if not d:
            continue
        rec += 1
        for r in d.get("rows") or []:
            t = _row_text(r)
            if t:
                parts.append(t)
    return _strip_marks("".join(parts)), rec, len(book["pages"])


def _base_hash(orig):
    return hashlib.sha1((orig or "").encode("utf-8")).hexdigest()


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _docx_from_paras(title, para_text, simp, heading, font, size,
                     role_bold=False, indent_chars=0):
    """把「空行=分段、单换行=段内断行」的文本写成 DOCX，返回 FileResponse。"""
    if font not in FONTS:
        raise HTTPException(400, "字体只支持：" + "/".join(FONTS))
    size = max(9.0, min(20.0, float(size)))
    title = (title or "整理本").strip()
    paras = re.split(r"\n{2,}", (para_text or "").replace("\r", ""))
    if not any(p.strip() for p in paras):
        raise HTTPException(400, "没有可导出的文字")
    try:
        from docx import Document
        from docx.shared import Pt
        from docx.oxml.ns import qn
        from opencc import OpenCC
    except Exception as e:
        raise HTTPException(500, "缺少 python-docx / opencc：%s" % e)

    cc = OpenCC("t2s") if simp else None
    conv = (lambda s: cc.convert(s)) if cc else (lambda s: s)

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = font
    normal.font.size = Pt(size)
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rfonts.set(qn(attr), font)

    if heading:
        doc.add_heading(conv(title), level=1)

    n_para = 0
    for para in paras:
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        p = doc.add_paragraph()
        if indent_chars:
            p.paragraph_format.first_line_indent = Pt(size * float(indent_chars))
        for i, ln in enumerate(lines):
            # 角色/科介统一用（）包住；开启 role_bold 时整段加粗
            parts = re.split(r"(（[^）]*）)", conv(ln))
            for part in parts:
                if not part:
                    continue
                run = p.add_run(part)
                if role_bold and part.startswith("（") and part.endswith("）"):
                    run.bold = True
            if i < len(lines) - 1:
                p.add_run().add_break()
        n_para += 1
    if not n_para:
        raise HTTPException(400, "没有可导出的文字")

    out_dir = os.path.join(store.DATA, "exports")
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)
    fname = "%s-整理本-%s.docx" % (safe, "简体" if simp else "繁体")
    path = os.path.join(out_dir, fname[:-5] + "-" + uuid.uuid4().hex[:10] + ".docx")
    doc.save(path)
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        filename=fname)


def _merge_act_for_export(text, structures):
    import re as _re
    act_pat = _re.compile(r"^第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出]$")
    out = []
    arr = sorted(structures or [], key=lambda x: int(x.get("start", 0)))
    i = 0
    while i < len(arr):
        st = arr[i]
        body = text[int(st.get("start", 0)):int(st.get("end", 0))]
        act_name = _re.sub(r"[，。、；：？！…—\s]", "", body.strip())
        if st.get("type") == "other" and act_pat.match(act_name) and i + 1 < len(arr):
            nx = arr[i + 1]
            nb = text[int(nx.get("start", 0)):int(nx.get("end", 0))]
            if int(nx.get("start", 0)) == int(st.get("end", 0)) and len(_re.sub(r"[，。、；：？！…—\s]", "", nb.strip())) <= 4 \
                    and nx.get("type") in ("bai", "wen", "other"):
                out.append(dict(st, end=int(nx.get("end", 0)),
                                attrs=dict(st.get("attrs") or {}, kind="act", act_name=_re.sub(r"[，。、；：？！…—\s]", "", nb.strip()))))
                i += 2
                continue
        out.append(st)
        i += 1
    return out


def _docx_from_structures(title, text, structures, simp, heading, font, size,
                          role_bold=False, indent_chars=0):
    """点校本格式导出：曲牌【】、角色/科介（），唱词小四，宾白五号。"""
    if font not in FONTS:
        raise HTTPException(400, "字体只支持：" + "/".join(FONTS))
    try:
        from docx import Document
        from docx.shared import Pt
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from opencc import OpenCC
    except Exception as e:
        raise HTTPException(500, "缺少 python-docx / opencc：%s" % e)
    cc = OpenCC("t2s") if simp else None
    conv = (lambda s: cc.convert(s)) if cc else (lambda s: s)
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = font
    normal.font.size = Pt(12)
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rfonts.set(qn(attr), font)
    typo = lambda x: ''.join(ch for ch in (x or '') if ch not in '，。、；：？！…—「」『』《》（）【】〔〕')
    def run(p, txt, pt, bold=False):
        if not txt:
            return
        r = p.add_run(conv(txt))
        r.font.name = font
        r.font.size = Pt(pt)
        r._element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), font)
        if bold:
            r.bold = True
    base_size = max(9.0, min(20.0, float(size)))
    body_psize = max(9.0, base_size - 1.5)
    has_title = any((st.get("type") == "title") for st in (structures or []))
    if heading and not has_title:
        doc.add_heading(conv(title or "整理本"), level=1)
    current = None
    def ensure():
        nonlocal current
        if current is None:
            current = doc.add_paragraph()
            if indent_chars:
                current.paragraph_format.first_line_indent = Pt(12 * float(indent_chars))
        return current
    def flush():
        nonlocal current
        current = None
    cursor = 0
    for st in _merge_act_for_export(text, structures):
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        if a < cursor:
            a = cursor
        if b <= a:
            continue
        if a > cursor:
            p = ensure(); run(p, text[cursor:a], body_psize, False)
        cursor = a
        body = text[a:b]
        cursor = max(cursor, b)
        typ = st.get('type', 'other')
        kind = (st.get('attrs') or {}).get('kind')
        if kind in ('volume', 'act'):
            body = body.replace('\n', '')
        if typ == 'title':
            flush(); p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run(p, typo(body), base_size + 6, True); flush(); continue
        if typ == 'subtitle':
            flush(); p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run(p, typo(body), base_size + 2, False); flush(); continue
        if kind in ('volume', 'act'):
            flush(); p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run(p, typo(body), base_size + 2, True); flush(); continue
        if typ == 'qupai':
            flush(); p = ensure(); run(p, '【' + typo(body) + '】', base_size, False); continue
        if typ == 'role':
            p = ensure(); run(p, '（' + typo(body) + '）', body_psize, bool(role_bold)); continue
        if typ == 'ke':
            p = ensure(); run(p, '（' + typo(body) + '）', body_psize, False); continue
        if typ == 'wen':
            p = ensure()
            chen = []
            for pair in ((st.get('attrs') or {}).get('chen') or []):
                try:
                    ca, cb = int(pair[0]), int(pair[1])
                    if a <= ca < cb <= b:
                        chen.append((ca, cb))
                except Exception:
                    pass
            if not chen:
                run(p, body, base_size, False)
            else:
                pos2 = a
                for ca, cb in sorted(chen):
                    if ca > pos2:
                        run(p, text[pos2:ca], base_size, False)
                    run(p, text[ca:cb], body_psize, False)
                    pos2 = cb
                if pos2 < b:
                    run(p, text[pos2:b], base_size, False)
            continue
        if typ == 'bai':
            p = ensure(); run(p, body, body_psize, False); continue
        p = ensure(); run(p, body, body_psize, False)
        cursor = max(cursor, b)
    if cursor < len(text):
        p = ensure(); run(p, text[cursor:], body_psize, False)
    flush()
    out_dir = os.path.join(store.DATA, "exports")
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\/:*?"<>|]', "_", title or "整理本")
    fname = "%s-点校本-%s.docx" % (safe, "简体" if simp else "繁体")
    path = os.path.join(out_dir, fname[:-5] + "-" + uuid.uuid4().hex[:10] + ".docx")
    doc.save(path)
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        filename=fname)


@app.get("/api/books/{bid}/export.docx")
def api_export(bid: str, simp: int = 1, leaf: int = 1, heading: int = 1,
               font: str = "宋体", size: float = 12):
    """导出整理本 DOCX。

    simp=1 简体 / 0 原字（繁体）；leaf=1 段首叶码；heading=1 含书名标题；
    font 字体（宋体/楷体/黑体/仿宋）；size 字号 pt（9–20）。
    """
    book = _book_or_404(bid)
    if font not in FONTS:
        raise HTTPException(400, "字体只支持：" + "/".join(FONTS))
    size = max(9.0, min(20.0, float(size)))
    try:
        from docx import Document
        from docx.shared import Pt
        from docx.oxml.ns import qn
        from opencc import OpenCC
    except Exception as e:
        raise HTTPException(500, "缺少 python-docx / opencc：%s" % e)

    cc = OpenCC("t2s") if simp else None
    conv = (lambda s: cc.convert(s)) if cc else (lambda s: s)

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = font
    normal.font.size = Pt(size)
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rfonts.set(qn(attr), font)

    if heading:
        doc.add_heading(conv(book["title"]), level=1)

    n_pages = 0
    for pg in book["pages"]:
        data = store.read_page(bid, pg["no"])
        if not data:
            continue
        text = "".join((r.get("text") or "") for r in (data.get("rows") or [])).strip()
        if not text:
            continue
        p = doc.add_paragraph()
        if leaf:
            run = p.add_run("第 %d 叶　" % pg["leaf"])
            run.bold = True
        p.add_run(conv(text))
        n_pages += 1
    if not n_pages:
        raise HTTPException(400, "还没有已识别/已校对的叶，无法导出")

    out_dir = os.path.join(store.DATA, "exports")
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", book["title"])
    tag = "整理本" + ("-简体" if simp else "-繁体")
    fname = "%s-%s.docx" % (safe, tag)
    path = os.path.join(out_dir, fname[:-5] + "-" + uuid.uuid4().hex[:10] + ".docx")
    doc.save(path)
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        filename=fname)


@app.post("/api/export-text.docx")
def api_export_text(req: ExportTextReq):
    """临时文本导出（前端兜底）：空行=分段，单换行=段内断行；不含叶码。"""
    return _docx_from_paras(req.title or "整理本", req.text, req.simp, req.heading, req.font, req.size)


# ---------------- 标点稿 v2（正文 + 分段 + 结构片段 + 机器标点状态） ----------------
def _clean_marks(marks, text):
    out = []
    if not isinstance(marks, list):
        return out
    for m in marks:
        try:
            pos = int(m.get("pos"))
            ch = m.get("ch") or ""
            state = m.get("state") or "ok"
        except Exception:
            continue
        if state not in ("a", "b", "cand", "ok") or not (0 <= pos < len(text)) or text[pos] != ch:
            continue
        item = {"pos": pos, "ch": ch, "state": state,
                "source": m.get("source") or ("human" if state == "ok" else "machine")}
        if "conf" in m:
            try:
                item["conf"] = round(float(m["conf"]), 2)
            except Exception:
                pass
        for k in ("qid", "qupai", "structure_id"):
            if k in m:
                item[k] = m[k]
        out.append(item)
    out.sort(key=lambda x: x["pos"])
    return out


def _clean_structs(structs, text):
    out = []
    if not isinstance(structs, list):
        return out
    for st in structs:
        try:
            s = int(st.get("start"))
            e = int(st.get("end"))
            typ = st.get("type") or "other"
        except Exception:
            continue
        if typ not in ("qupai", "wen", "bai", "ke", "role", "other", "title", "subtitle"):
            continue
        if not (0 <= s < e <= len(text)):
            continue
        try:
            conf = round(float(st.get("conf", 0.5)), 2)
        except Exception:
            conf = 0.5
        attrs = st.get("attrs")
        if not isinstance(attrs, dict):
            attrs = {}
        chen = attrs.get("chen")
        if isinstance(chen, list):
            clean_chen = []
            for pair in chen:
                try:
                    ca, cb = int(pair[0]), int(pair[1])
                except Exception:
                    continue
                if s <= ca < cb <= e:
                    clean_chen.append([ca, cb])
            if clean_chen:
                attrs = dict(attrs, chen=clean_chen)
            else:
                attrs = {k: v for k, v in attrs.items() if k != "chen"}
        out.append({"start": s, "end": e, "type": typ, "conf": conf, "attrs": attrs})
    out.sort(key=lambda x: (x["start"], x["end"]))
    fixed = []
    prev_end = -1
    for st in out:
        if st["start"] < prev_end:
            continue
        fixed.append(st)
        prev_end = st["end"]
    return fixed


def _heal_structures(bid, doc, orig):
    """旧稿可能只有极少结构；按整本原文重算结构并落盘，标点/正文不动。"""
    try:
        old = doc.get("structures") or []
        if len(old) >= 20 or len(orig) < 2000:
            return doc
        structs, _ = llm_punct.score_doc(orig, doc.get("text") or "", 0, 0)
        if len(structs) <= len(old):
            return doc
        doc["structures"] = structs
    except Exception:
        pass
    return doc


def _normalize_doc(bid, doc, orig, rec, total):
    if not doc:
        return None
    text = doc.get("text") or ""
    breaks = doc.get("breaks") or []
    breaks = sorted({int(b) for b in breaks if isinstance(b, int) and 0 <= b < len(text) and text[b] == "\n"})
    marks = doc.get("marks")
    if not isinstance(marks, list):
        marks = punctgen.infer_marks(text)
    marks = _clean_marks(marks, text)
    have = {m.get("pos") for m in marks}
    extra = []
    for i, ch in enumerate(text):
        if ch in punctgen.PUNCT_CHARS and ch not in '\n \t\u3000' and i not in have:
            extra.append({"pos": i, "ch": ch, "state": "ok", "source": "human", "conf": 1})
    if extra:
        marks = sorted(marks + extra, key=lambda x: x["pos"])
    structures = _clean_structs(doc.get("structures"), text)
    return {
        "v": 2, "book": bid, "text": text, "breaks": breaks,
        "marks": marks, "structures": structures,
        "page_ranges": doc.get("page_ranges") or [],
        "base_hash": doc.get("base_hash") or _base_hash(orig),
        "updated_at": doc.get("updated_at"),
        "rev": int(doc.get("rev") or 0),
        "saved_chars": len(_strip_marks(text)),
        "paragraphs": (len(breaks) + 1 if text else 0),
        "recognized_pages": rec, "total_pages": total,
        "meta": doc.get("meta") or {},
    }


@app.get("/api/books/{bid}/punct")
def api_get_punct(bid: str):
    book = _book_or_404(bid)
    orig, rec, total = _book_orig_stripped(bid)
    bh = _base_hash(orig)
    doc = store.read_punct(bid)
    base = {"book": bid, "exists": bool(doc), "base_hash": bh,
            "chars": len(orig), "recognized_pages": rec, "total_pages": total}
    if not doc:
        return dict(base, text="", breaks=[], marks=[], structures=[], stale=False, meta={})
    doc = _heal_structures(bid, doc, orig)
    norm = _normalize_doc(bid, doc, orig, rec, total)
    stale = doc.get("base_hash") != bh
    return dict(base, exists=True, stale=stale, **{k: v for k, v in norm.items()
                                                    if k not in ("book", "recognized_pages", "total_pages", "base_hash")})


_SP_JOBS = {}
_SP_LOCK = threading.Lock()
_TASK_LOCK = threading.Lock()
_SP_CANCEL = set()


def _sp_update(bid, **kw):
    with _SP_LOCK:
        job = _SP_JOBS.setdefault(bid, {})
        job.update(kw)
        job["updated"] = time.time()


def _sp_ckpt_path(bid):
    d = os.path.join(store.DATA, "jobs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "struct-punct-%s.json" % re.sub(r"[^A-Za-z0-9_.-]", "_", bid))


def _sp_ckpt_hash(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _sp_ckpt_load(bid, text):
    try:
        p = _sp_ckpt_path(bid)
        if not os.path.exists(p):
            return None
        d = json.load(open(p, encoding="utf-8"))
        if d.get("hash") != _sp_ckpt_hash(text):
            return None
        return d
    except Exception:
        return None


def _sp_ckpt_save(bid, text, results, total):
    try:
        p = _sp_ckpt_path(bid)
        with store.book_lock(bid):
            store._atomic_json(p, {"hash": _sp_ckpt_hash(text), "results": results,
                                   "done": len(results), "total": total,
                                   "saved_at": _now_iso()})
    except Exception:
        pass


def _sp_ckpt_remove(bid):
    try:
        p = _sp_ckpt_path(bid)
        with store.book_lock(bid):
            if os.path.exists(p):
                os.remove(p)
    except Exception:
        pass


def _sp_map_chen(attrs, seg, fixed, old_start, new_start):
    """把旧块内的衬字绝对区间映射到新块；按非标点字符序号近似。"""
    if not attrs:
        return attrs
    chen = attrs.get('chen')
    if not isinstance(chen, list) or not chen:
        return attrs
    pc = punctgen.PUNCT_CHARS
    old_rp = [old_start + i for i, ch in enumerate(seg) if ch not in pc and ch != ' ']
    new_rp = [new_start + i for i, ch in enumerate(fixed) if ch not in pc and ch != ' ']
    if not old_rp or not new_rp:
        return dict(attrs, chen=[])
    mapped = []
    for pair in chen:
        try:
            ca, cb = int(pair[0]), int(pair[1])
        except Exception:
            continue
        ra = sum(1 for x in old_rp if x < ca)
        rb = sum(1 for x in old_rp if x < cb)
        if ra >= rb:
            continue
        na = new_rp[ra] if ra < len(new_rp) else new_start + len(fixed)
        nb = (new_rp[rb - 1] + 1) if rb > 0 else (new_start + len(fixed))
        if nb > na:
            mapped.append([na, nb])
    return dict(attrs, chen=mapped)


def _sp_shift_chen(attrs, shifts):
    if not attrs or not isinstance(attrs.get('chen'), list):
        return attrs
    out = []
    for pair in attrs.get('chen') or []:
        try:
            ca, cb = int(pair[0]), int(pair[1])
        except Exception:
            continue
        # 跨成功块边界的衬字无法可靠映射，直接丢弃
        if any(a < ca < b or a < cb < b for a, b, _d in shifts):
            continue
        sh = sum(d for a, b, d in shifts if b <= ca)
        if cb > ca:
            out.append([ca + sh, cb + sh])
    return dict(attrs, chen=out)


def _is_book_level_struct(st, text):
    typ = st.get("type")
    attrs = st.get("attrs") or {}
    if typ in ("title", "subtitle"):
        return True
    if attrs.get("kind") in ("volume", "act"):
        return True
    body = (text or "")[int(st.get("start", 0)):int(st.get("end", 0))].strip()
    # 只有整段就是齣名时才按书级处理；不能用前缀匹配吞掉后面正文
    return bool(re.fullmatch(r"第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出]", body))


def _split_act_headings(text, structs):
    """从 other/正文块中切出“第X齣”书级结构。"""
    act_re = re.compile(r"第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出]")
    out = []
    for st in sorted(structs, key=lambda x: int(x.get("start", 0))):
        typ = st.get("type")
        a, b = int(st.get("start", 0)), int(st.get("end", 0))
        if b <= a:
            continue
        body = text[a:b]
        matches = list(act_re.finditer(body))
        if not matches:
            out.append(st)
            continue
        # 已经是独立齣名块：不重复切
        if (st.get("attrs") or {}).get("kind") == "act":
            out.append(st)
            continue
        pos = 0
        for m in matches:
            ms, me = a + m.start(), a + m.end()
            if ms > a + pos:
                out.append(dict(st, start=a + pos, end=ms))
            out.append({"start": ms, "end": me, "type": "other", "conf": 0.9,
                        "attrs": {"kind": "act"}})
            pos = m.end()
        if a + pos < b:
            out.append(dict(st, start=a + pos, end=b))
    out = [dict(x) for x in out if int(x.get("end", 0)) > int(x.get("start", 0))]
    out.sort(key=lambda x: int(x.get("start", 0)))
    return out


def _compact_with_map(text):
    """去空白后的文本，以及每个紧凑字符对应的原文位置。"""
    chars = []
    pos = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        chars.append(ch)
        pos.append(i)
    pos.append(len(text))
    return "".join(chars), pos


def _split_span(text, structs, hs, he, new_type="other", attrs=None):
    """在 hs..he 处切出一个书级块，保留前后正文。"""
    out = []
    for st in structs:
        a, b = int(st.get("start", 0)), int(st.get("end", 0))
        if b <= hs or a >= he:
            out.append(st)
            continue
        if a < hs:
            out.append(dict(st, end=hs))
        if b > he:
            out.append(dict(st, start=he))
    out.append({"start": hs, "end": he, "type": new_type,
                "conf": 0.9, "attrs": dict(attrs or {})})
    out.sort(key=lambda x: int(x.get("start", 0)))
    return out


def _book_level_ws(text, structs):
    """处理带换行的开头书名/副题、卷名。"""
    compact, pos = _compact_with_map(text)
    out = list(structs)
    if out:
        first = out[0]
        if int(first.get("start", 0)) == 0 and first.get("type") in ("other", None):
            head = compact[:160]
            m = re.match(r"^([\u4e00-\u9fff]{4,24}?(?:記|傳|譜|志|序|敘))", head)
            if m:
                te = pos[m.end()]
                title = text[:te]
                rest = compact[m.end():m.end() + 40]
                sm = re.match(r"^([\u4e00-\u9fff]{4,24}?(?:敘|序|引|題辭))", rest)
                se = pos[m.end() + sm.end()] if sm else te
                new = [{"start": 0, "end": te, "type": "title", "conf": 0.94,
                        "attrs": {"text": title}}]
                if se > te:
                    new.append({"start": te, "end": se, "type": "subtitle",
                                "conf": 0.9, "attrs": {"text": text[te:se]}})
                if int(first.get("end", 0)) > se:
                    new.append(dict(first, start=se))
                new.extend(out[1:])
                out = new
    titles = [text[int(st["start"]):int(st["end"])].replace("\n", "").replace(" ", "")
              for st in out if st.get("type") == "title"]
    for title in titles:
        if not title:
            continue
        tc = title.replace("\n", "").replace(" ", "")
        for m in re.finditer(re.escape(tc) + r"卷[上中下](?:終)?", compact):
            # 只把“卷上/卷中/卷下”本身标成卷名，不覆盖书名
            vs = pos[m.start() + len(tc)]
            ve = pos[m.end()]
            out = _split_span(text, out, vs, ve, "other", {"kind": "volume"})
    return out


def _add_book_level_structures(text, structs):
    """第0步：标题/副题/卷名/齣名，优先于普通正文结构。"""
    out = _book_level_ws(text, structs)
    out = _split_act_headings(text, out)
    # 去掉重叠：只保留最先开始的书级块；正文被书级覆盖的部分由书级块吸收
    book = [x for x in out if _is_book_level_struct(x, text)]
    rest = [x for x in out if not _is_book_level_struct(x, text)]
    merged = list(book)
    book_ranges = [(int(x.get("start", 0)), int(x.get("end", 0))) for x in book]
    for st in sorted(rest, key=lambda x: int(x.get("start", 0))):
        a, b = int(st.get("start", 0)), int(st.get("end", 0))
        if b <= a:
            continue
        pieces = [(a, b)]
        for xa, xb in book_ranges:
            nxt = []
            for pa, pb in pieces:
                if pb <= xa or pa >= xb:
                    nxt.append((pa, pb))
                else:
                    if pa < xa:
                        nxt.append((pa, xa))
                    if pb > xb:
                        nxt.append((xb, pb))
            pieces = nxt
        for pa, pb in pieces:
            if pb > pa:
                merged.append(dict(st, start=pa, end=pb))
    merged.sort(key=lambda x: int(x.get("start", 0)))
    return merged




def _clean_punct_output(s):
    """清理 AI 块内相邻非法标点（如“。，”），只删标点，不动原文。"""
    out = []
    for ch in s or "":
        if ch in punctgen.PUNCT_CHARS and ch not in "\n":
            prev = out[-1] if out else ""
            if prev in "。！？；" and ch in "，、；：":
                continue
            if ch in "，、；：" and prev == ch:
                continue
        out.append(ch)
    return "".join(out)


def _refine_structures_by_punctuation(text, structs, marks):
    """第3步：用标点句界回调结构，切长短宾白/曲文。"""
    out = []
    for st in sorted(structs, key=lambda x: int(x.get("start", 0))):
        typ = st.get("type")
        a, b = int(st.get("start", 0)), int(st.get("end", 0))
        if typ not in ("bai", "wen") or b <= a:
            out.append(st)
            continue
        body = text[a:b]
        cuts = [a]
        for i, ch in enumerate(body):
            if ch in "。！？；":
                cuts.append(a + i + 1)
        cuts.append(b)
        cuts = sorted(set(cuts))
        if len(cuts) <= 2:
            out.append(st)
            continue
        base_attrs = dict(st.get("attrs") or {})
        chen = base_attrs.get("chen") if isinstance(base_attrs.get("chen"), list) else []
        for j in range(len(cuts) - 1):
            ca, cb = cuts[j], cuts[j + 1]
            if cb <= ca:
                continue
            attrs = dict(base_attrs)
            if chen:
                cc = []
                for pair in chen:
                    try:
                        x, y = int(pair[0]), int(pair[1])
                    except Exception:
                        continue
                    x2, y2 = max(x, ca), min(y, cb)
                    if y2 > x2:
                        cc.append([x2, y2])
                attrs["chen"] = cc
            attrs["refined_by"] = "punctuation"
            out.append(dict(st, start=ca, end=cb, attrs=attrs,
                            conf=min(float(st.get("conf", 0.5) or 0.5), 0.72)))
    return out


def _audit_output_structures(text, structs, marks):
    """第4步：输出纯度审计（pure/mixed/unpunctuated/low_conf/book）。"""
    llm_punct.audit_structure_purity(text, structs)
    mark_pos = [int(m.get("pos", 0)) for m in marks or []]
    counts = {"pure": 0, "mixed": 0, "unpunctuated": 0, "low_conf": 0,
              "book": 0, "structural": 0, "other": 0}
    for st in structs:
        typ = st.get("type")
        attrs = st.setdefault("attrs", {})
        a, b = int(st.get("start", 0)), int(st.get("end", 0))
        if _is_book_level_struct(st, text):
            attrs["audit"] = "book"
        elif typ in ("qupai", "role", "ke"):
            attrs["audit"] = "structural"
        elif typ == "other":
            attrs["audit"] = "other"
        elif attrs.get("purity") == "mixed":
            attrs["audit"] = "mixed"
        else:
            inside = [p for p in mark_pos if a <= p < b]
            low = float(st.get("conf", 0.5) or 0.5) < 0.5
            if not low:
                low = any(float((m or {}).get("conf", 0.8)) < 0.5 for m in
                          (marks or []) if a <= int(m.get("pos", 0)) < b)
            if low:
                attrs["audit"] = "low_conf"
            elif not inside:
                attrs["audit"] = "unpunctuated"
            else:
                attrs["audit"] = "pure"
        counts[attrs.get("audit")] = counts.get(attrs.get("audit"), 0) + 1
    return counts


def _rule_structure_doc(bid):
    """用新结构算法生成结构草稿，丢弃规则标点，只保留结构与原文。"""
    _book, full_raw, pages, recognized = punctgen.book_raw(bid)
    if not full_raw:
        raise ValueError("还没有已识别/已校对的叶")
    built = structure_new.build_structure_doc(full_raw)
    out = []
    for ch in full_raw:
        if ch in punctgen.PUNCT_CHARS and ch != "\n":
            continue
        out.append(ch)
    text = "".join(out)
    # built['text'] 是去空白 raw；映射到保留换行的结构文本坐标
    posmap = structure_new.raw_to_text_positions(built.get("text") or "", text)
    structs = []
    for st in built.get("structures") or []:
        try:
            a = posmap[min(len(posmap) - 1, max(0, int(st.get("start", 0))))]
            b = posmap[min(len(posmap) - 1, max(0, int(st.get("end", 0))))]
        except Exception:
            continue
        if b > a:
            structs.append(dict(st, start=a, end=b))
    breaks = [i for i, ch in enumerate(text) if ch == "\n"]
    structs = _add_book_level_structures(text, structs)
    book_structs = [st for st in structs if _is_book_level_struct(st, text)]
    coarse = [st for st in structs if not _is_book_level_struct(st, text)]
    return {
        "v": 2, "book": bid, "text": text, "breaks": breaks, "marks": [],
        "structures": structs,
        "page_ranges": punctgen.recompute_page_ranges(bid, text),
        "base_hash": punctgen._base_hash(full_raw), "updated_at": _now_iso(),
        "saved_chars": len(punctgen.strip_marks(text)),
        "paragraphs": (len(breaks) + 1 if text else 0),
        "meta": {"engine": "structure-new-v1", "scope": "book",
                 "generated_at": _now_iso(),
                 "stats": {"structures": len(structs),
                           "book_structures": len(book_structs),
                           "coarse_structures": len(coarse),
                           "rule_marks_discarded": 0,
                           "recognized_pages": recognized}},
    }



def _sp_worker(bid, pages, initial=False):
    started = time.time()
    try:
        existing = store.read_punct(bid)
        start_rev = int((existing or {}).get("rev") or 0)
        start_text = (existing or {}).get("text") or ""
        if initial:
            if existing and (existing.get("marks") or existing.get("structures")):
                raise RuntimeError("这本书已有句读稿或已完成整本初版，请使用局部“按选定结构生成标点”")
            _sp_update(bid, running=True, stage="规则识别整本结构", done=0, total=0,
                       failed=0, error="", cancelled=False, resumable=False,
                       elapsed=0, eta_seconds=0, current="", current_text="", started=started)
            doc = _rule_structure_doc(bid)
        else:
            doc = existing
            if not doc:
                raise RuntimeError("还没有标点稿")
        text = doc.get("text") or ""
        ranges = doc.get("page_ranges") or []
        structs = doc.get("structures") or []
        if initial:
            structs, _purity = llm_punct.audit_structure_purity(text, structs)
            doc["structures"] = structs
            book_n = sum(1 for x in structs if _is_book_level_struct(x, text))
            _sp_update(bid, stage="书级/粗结构完成（书级 %d，粗结构 %d）" % (book_n, len(structs) - book_n),
                       total=0, done=0, current="", current_text="")
        if pages:
            lo, hi = sorted(pages)
            keep = []
            for st in structs:
                pno = None
                for p in ranges:
                    if p["start"] <= st["start"] < p["end"]:
                        pno = p.get("no"); break
                if pno is not None and lo <= pno <= hi:
                    keep.append(st)
            structs = keep
        targets = [st for st in structs if st.get("type") in ("bai", "wen")
                   and (st.get("attrs") or {}).get("purity") != "mixed"]
        total = len(targets)
        if total == 0:
            _sp_ckpt_remove(bid)
            if initial:
                with store.book_lock(bid):
                    current = store.read_punct(bid) or {}
                    if int(current.get("rev") or 0) != start_rev or (current.get("text") or "") != start_text:
                        raise RuntimeError("任务运行期间文档已被修改，已停止保存（未覆盖新稿）")
                    doc["rev"] = start_rev + 1
                    doc["updated_at"] = _now_iso()
                    doc["meta"] = dict(doc.get("meta") or {})
                    doc["meta"]["initial_done"] = True
                    doc["meta"]["pipeline"] = ["book", "coarse", "audit"]
                    doc["meta"]["audit"] = _audit_output_structures(doc["text"], doc["structures"], [])
                    doc["meta"]["struct_punct"] = {"targets": 0, "done": 0, "failed": 0,
                                                   "finished_at": _now_iso()}
                    if current:
                        try:
                            store.push_punct_history(bid, current)
                        except Exception:
                            pass
                    store.save_punct(bid, doc)
            _sp_update(bid, running=False, stage="完成", total=0, done=0, failed=0,
                       elapsed=round(time.time() - started, 1), eta_seconds=0,
                       error="", resumable=False, current="", current_text="")
            return
        try:
            llm_client.ensure_configured()
        except llm_client.LLMConfigError as exc:
            raise RuntimeError(str(exc))
        ckpt = _sp_ckpt_load(bid, text) or {"results": {}}
        results = dict(ckpt.get("results") or {})
        resume_done = sum(1 for st in targets if str(int(st["start"])) in results)
        done = resume_done
        failed = 0
        _sp_update(bid, running=True, total=total, done=resume_done, failed=failed,
                   stage=("规则结构完成，按结构 AI 标点" if initial else "准备按结构标点"),
                   error="", cancelled=False,
                   resumable=bool(resume_done), resume_done=resume_done, resume_total=total,
                   elapsed=0, eta_seconds=0, current="", current_text="",
                   started=started, rev=start_rev)
        allst = sorted(doc.get("structures") or [], key=lambda x: int(x["start"]))
        target_map = {int(st["start"]): st for st in targets}
        old_marks = list(doc.get("marks") or [])
        out_parts = []
        new_structs = []
        new_marks = []
        shifts = []
        pos = 0
        out_len = 0
        cancelled = False
        for st in allst:
            a, b = int(st["start"]), int(st["end"])
            if a < pos:
                a = pos
            if b <= a:
                continue
            if a > pos:
                out_parts.append(text[pos:a])
                out_len += a - pos
                pos = a
            seg = text[a:b]
            typ = st.get("type")
            new_a = out_len
            success = False
            fixed = seg
            if a in target_map and typ in ("bai", "wen"):
                key = str(a)
                raw = ''.join(ch for ch in seg if ch not in punctgen.PUNCT_CHARS and ch != ' ')
                was_cached = key in results
                if key in results:
                    cached = results[key].get("text") or ""
                    c_ok = cached and ''.join(ch for ch in cached if ch not in punctgen.PUNCT_CHARS) == raw \
                        and any(ch in punctgen.PUNCT_CHARS for ch in cached)
                    if c_ok:
                        fixed = cached
                        success = True
                    else:
                        results.pop(key, None)
                if not success and raw and len(raw) > 1:
                    if bid in _SP_CANCEL:
                        cancelled = True
                        break
                    cand, stats = llm_punct.punctuate_range(raw, typ)
                    cand = _clean_punct_output(cand)
                    body_ok = bool(cand) and ''.join(ch for ch in cand if ch not in punctgen.PUNCT_CHARS) == raw
                    punct_added = bool(cand) and any(ch in punctgen.PUNCT_CHARS for ch in cand)
                    if body_ok and punct_added:
                        fixed = cand
                        results[key] = {"text": fixed}
                        success = True
                if success:
                    for i, ch in enumerate(fixed):
                        if ch in punctgen.PUNCT_CHARS and ch != '\n':
                            new_marks.append({"pos": new_a + i, "ch": ch,
                                              "state": "cand", "source": "machine", "conf": 0.8})
                    shifts.append((a, b, len(fixed) - (b - a)))
                else:
                    fixed = seg
                    failed += 1
                if not was_cached:
                    done += 1
                elapsed = max(0.1, time.time() - started)
                avg = elapsed / max(done, 1)
                eta = max(0, int(avg * (total - done)))
                _sp_ckpt_save(bid, text, results, total)
                _sp_update(bid, done=done, failed=failed,
                           elapsed=round(elapsed, 1), eta_seconds=eta,
                           stage="按结构标点 %d/%d" % (done, total),
                           current=str(a), current_text=seg[:40],
                           resumable=True, resume_done=done, resume_total=total)
                if bid in _SP_CANCEL:
                    cancelled = True
                    break
            attrs = dict(st.get("attrs") or {})
            if success:
                attrs = _sp_map_chen(attrs, seg, fixed, a, new_a)
            else:
                attrs = _sp_shift_chen(attrs, shifts)
            out_parts.append(fixed)
            out_len += len(fixed)
            new_structs.append(dict(st, start=new_a, end=new_a + len(fixed), attrs=attrs))
            pos = b
        if cancelled:
            if initial:
                _sp_ckpt_remove(bid)
            _sp_update(bid, running=False,
                       stage=("已停止（整本初版不续跑，请重试）" if initial else "已停止，可续跑"),
                       cancelled=True, resumable=True and not initial,
                       resume_done=done, resume_total=total,
                       elapsed=round(time.time() - started, 1), error="")
            return
        if pos < len(text):
            out_parts.append(text[pos:])
            out_len += len(text) - pos
        new_text = ''.join(out_parts)
        # 保留未被处理块的人工标点状态；只对成功处理块使用新候选标点
        for m in old_marks:
            p_old = int(m.get("pos", 0))
            inside_success = any(a <= p_old < b for a, b, _d in shifts)
            if inside_success:
                continue
            shift = sum(d for a, b, d in shifts if b <= p_old)
            nm = dict(m, pos=p_old + shift)
            new_marks.append(nm)
        if initial:
            _sp_update(bid, stage="标点完成，回调结构 / 纯度审计", done=done, total=total)
            new_structs = _refine_structures_by_punctuation(new_text, new_structs, new_marks)
            audit_counts = _audit_output_structures(new_text, new_structs, new_marks)
        else:
            audit_counts = {}
        with store.book_lock(bid):
            current = store.read_punct(bid) or {}
            if int(current.get("rev") or 0) != start_rev or (current.get("text") or "") != start_text:
                raise RuntimeError("任务运行期间文档已被修改，已停止保存（未覆盖新稿）")
            doc["text"] = new_text
            doc["structures"] = new_structs
            doc["marks"] = sorted(new_marks, key=lambda m: int(m.get("pos", 0)))
            doc["breaks"] = [i for i, ch in enumerate(new_text) if ch == "\n"]
            doc["page_ranges"] = punctgen.recompute_page_ranges(bid, new_text)
            doc["rev"] = start_rev + 1
            doc["updated_at"] = _now_iso()
            doc["meta"] = dict(doc.get("meta") or {})
            if initial:
                doc["meta"]["initial_done"] = True
                doc["meta"]["pipeline"] = ["book", "coarse", "punctuate", "refine", "audit"]
                doc["meta"]["audit"] = audit_counts
                stats = dict(doc["meta"].get("stats") or {})
                stats["structures"] = len(new_structs)
                stats["book_structures"] = sum(1 for x in new_structs if _is_book_level_struct(x, new_text))
                stats["coarse_structures"] = stats["structures"] - stats["book_structures"]
                stats["marks"] = len(new_marks)
                doc["meta"]["stats"] = stats
            doc["meta"]["struct_punct"] = {"targets": total, "done": done,
                                           "failed": failed, "finished_at": _now_iso()}
            try:
                store.push_punct_history(bid, current)
            except Exception:
                pass
            store.save_punct(bid, doc)
        _sp_ckpt_remove(bid)
        _sp_update(bid, running=False, stage="完成", done=done, total=total,
                   failed=failed, elapsed=round(time.time() - started, 1),
                   eta_seconds=0, error="", resumable=False)
    except Exception as e:
        if initial:
            _sp_ckpt_remove(bid)
        _sp_update(bid, running=False, stage="失败", error=str(e),
                   elapsed=round(time.time() - started, 1), resumable=False)


@app.post("/api/books/{bid}/struct-punct")
def api_struct_punct_start(bid: str, req: dict = Body(...)):
    _book_or_404(bid)
    try:
        llm_client.ensure_configured()
    except llm_client.LLMConfigError as exc:
        raise HTTPException(503, str(exc))
    with _TASK_LOCK:
        if bid in _MAINT:
            raise HTTPException(409, "这本书正在维护，请稍后重试")
        _ensure_no_tasks(bid)
        with _SP_LOCK:
            if _SP_JOBS.get(bid, {}).get("running"):
                raise HTTPException(409, "识别整本任务已在运行")
            _SP_CANCEL.discard(bid)
            initial = True
            existing = store.read_punct(bid)
            if existing and (existing.get("marks") or existing.get("structures")):
                raise HTTPException(409, "句读整本初版已完成，请使用局部“按选定结构生成标点”")
            pages = None
            doc = store.read_punct(bid) or {}
            ck = _sp_ckpt_load(bid, doc.get("text") or "")
            _SP_JOBS[bid] = {"running": True, "stage": "启动", "total": 0, "done": 0, "failed": 0,
                             "error": "", "started": time.time(), "elapsed": 0, "eta_seconds": 0,
                             "resumable": bool(ck) and not initial,
                             "resume_done": (ck or {}).get("done", 0) if not initial else 0,
                             "resume_total": (ck or {}).get("total", 0) if not initial else 0,
                             "initial": initial}
            threading.Thread(target=_sp_worker, args=(bid, pages, initial), daemon=True).start()
    return {"ok": True, "job": dict(_SP_JOBS.get(bid) or {})}


@app.get("/api/books/{bid}/struct-punct/status")
def api_struct_punct_status(bid: str):
    with _SP_LOCK:
        job = dict(_SP_JOBS.get(bid) or {"running": False, "stage": "未运行"})
    if not job.get("running"):
        try:
            doc = store.read_punct(bid) or {}
            ck = _sp_ckpt_load(bid, doc.get("text") or "")
            if ck:
                job["resumable"] = True
                job["resume_done"] = ck.get("done", 0)
                job["resume_total"] = ck.get("total", 0)
        except Exception:
            pass
    job["elapsed"] = round(max(0, time.time() - job.get("started", time.time())), 1) if job.get("running") else job.get("elapsed", 0)
    if job.get("total"):
        job["percent"] = round((job.get("done") or 0) * 100.0 / job["total"], 1)
    return job


@app.post("/api/books/{bid}/struct-punct/cancel")
def api_struct_punct_cancel(bid: str):
    _SP_CANCEL.add(bid)
    _sp_update(bid, stage="停止中…")
    return {"ok": True}


@app.post("/api/books/{bid}/punct/rewrite-range")
def api_punct_rewrite_range(bid: str, req: dict = Body(...)):
    """人工选中一个宾白/曲文结构块，只对该块重新 AI 标点。"""
    import punctgen
    _book_or_404(bid)
    doc = store.read_punct(bid)
    text = (doc or {}).get("text") or ""
    if not text:
        raise HTTPException(400, "还没有标点稿")
    try:
        start = int(req.get("start")); end = int(req.get("end"))
    except Exception:
        raise HTTPException(400, "范围无效")
    start = max(0, min(start, len(text))); end = max(start, min(end, len(text)))
    typ = None
    for st in (doc.get("structures") or []):
        if int(st.get("start", 0)) <= start and end <= int(st.get("end", 0)):
            typ = st.get("type"); break
    if typ not in ("bai", "wen"):
        raise HTTPException(400, "只有宾白/曲文块支持 AI 标点")
    raw = ''.join(ch for ch in text[start:end] if ch not in punctgen.PUNCT_CHARS or ch == ' ')
    if not raw:
        raise HTTPException(400, "选中范围没有正文")
    try:
        llm_client.ensure_configured()
    except llm_client.LLMConfigError as exc:
        raise HTTPException(503, str(exc))
    fixed, stats = llm_punct.punctuate_range(raw, typ)
    if (not fixed) or (fixed == raw) or (not any(ch in punctgen.PUNCT_CHARS for ch in fixed)):
        # 短块/模型首次空转：再试一次
        fixed, stats = llm_punct.punctuate_range(raw, typ)
    if (not fixed) or (fixed == raw) or (not any(ch in punctgen.PUNCT_CHARS for ch in fixed)):
        if len(raw) <= 6:
            return {"start": start, "end": end, "type": typ, "text": raw, "stats": {"short": 1}}
        raise HTTPException(502, "AI 没有返回有效标点（可能超时或模型未就绪），请重试")
    return {"start": start, "end": end, "type": typ, "text": fixed, "stats": stats}


@app.post("/api/books/{bid}/punct")
def api_save_punct(bid: str, req: PunctReq):
    _book_or_404(bid)
    orig, rec, total = _book_orig_stripped(bid)
    if not orig:
        raise HTTPException(400, "还没有已识别/已校对的叶，无法保存标点稿")
    text = (req.text or "").replace("\r", "")
    try:
        clean = punctgen.validate_doc(bid, text, req.marks, req.structures, req.breaks)
    except ValueError as e:
        raise HTTPException(409, str(e))
    old = store.read_punct(bid)
    old_rev = int((old or {}).get("rev") or 0)
    if req.rev is not None and int(req.rev) != old_rev:
        raise HTTPException(409, f"保存冲突：当前版本 rev={old_rev}，请刷新后再改")
    old_same = bool(old and (old.get("text") or "") == text)
    marks = req.marks
    structs = req.structures
    breaks = req.breaks
    if marks is None:
        marks = (_clean_marks(old.get("marks"), text) if old_same and old else punctgen.infer_marks(text))
    else:
        marks = clean["marks"]
    if structs is None:
        structs = (_clean_structs(old.get("structures"), text) if old_same and old else [])
    else:
        structs = clean["structures"]
    if breaks is None:
        breaks = (old.get("breaks") or []) if old_same and old else []
        breaks = [b for b in breaks if isinstance(b, int) and 0 <= b < len(text) and text[b] == "\n"]
    else:
        breaks = clean["breaks"]
    if old_same and old and (old.get("page_ranges") or []):
        page_ranges = old.get("page_ranges") or []
    else:
        page_ranges = punctgen.recompute_page_ranges(bid, text)
    unchanged = bool(old and old.get("text") == text and old.get("marks") == marks
                     and old.get("structures") == structs and old.get("breaks") == breaks)
    if unchanged:
        return {"ok": True, "saved_at": old.get("updated_at"), "unchanged": True,
                "chars": len(orig), "paragraphs":(old.get("paragraphs") or 0),
                "breaks": len(breaks), "marks": len(marks), "structures": len(structs),
                "base_hash": old.get("base_hash") or _base_hash(orig),
                "page_ranges": page_ranges, "rev": old_rev}
    if old:
        try:
            store.push_punct_history(bid, old)
        except Exception:
            pass
    data = {
        "v": 2, "book": bid, "text": text, "breaks": breaks,
        "marks": marks, "structures": structs, "page_ranges": page_ranges,
        "base_hash": _base_hash(orig), "updated_at": _now_iso(),
        "rev": old_rev + 1,
        "saved_chars": len(orig), "paragraphs": (len(breaks) + 1 if text else 0),
        "recognized_pages": rec, "total_pages": total,
        "meta": req.meta or (old.get("meta") if old_same and old else {}) or {},
    }
    if not store.save_punct_if_rev(bid, data, old_rev):
        raise HTTPException(409, "保存冲突：当前版本已被其他窗口/任务更新，请载入最新稿")
    return {"ok": True, "saved_at": data["updated_at"],
            "chars": data["saved_chars"], "paragraphs": data["paragraphs"],
            "breaks": len(breaks), "marks": len(marks), "structures": len(structs),
            "base_hash": data["base_hash"], "page_ranges": page_ranges,
            "rev": data["rev"]}


@app.delete("/api/books/{bid}/punct")
def api_delete_punct(bid: str):
    _book_or_404(bid)
    with _book_maintenance(bid):
        store.delete_punct(bid)
    return {"ok": True}


@app.get("/api/books/{bid}/punct/history")
def api_punct_history(bid: str):
    _book_or_404(bid)
    return {"items": store.list_punct_history(bid)}


@app.post("/api/books/{bid}/punct/restore")
def api_punct_restore(bid: str, req: PunctRestoreReq):
    _book_or_404(bid)
    _ensure_no_tasks(bid)
    snap = store.read_punct_history(bid, req.name)
    if not snap:
        raise HTTPException(404, "历史版本不存在")
    orig, _, _ = _book_orig_stripped(bid)
    if snap.get("base_hash") != _base_hash(orig):
        raise HTTPException(409, "该历史版本与当前校字稿不一致，不能恢复")
    with _book_maintenance(bid):
        old = store.read_punct(bid)
        old_rev = int((old or {}).get("rev") or 0)
        if old:
            try:
                store.push_punct_history(bid, old)
            except Exception:
                pass
        snap = dict(snap)
        snap["rev"] = old_rev + 1
        snap["updated_at"] = _now_iso()
        snap["book"] = bid
        if not store.save_punct_if_rev(bid, snap, old_rev):
            raise HTTPException(409, "恢复失败：标点稿已被其他窗口/任务更新，请重试")
    return {"ok": True, "rev": snap["rev"], "saved_at": snap["updated_at"], "doc": snap}


@app.get("/api/books/{bid}/backup")
def api_book_backup(bid: str):
    """把书库 JSON（book/ocr/text/punct/历史）打成 zip；不含原图和缩略图。"""
    book = _book_or_404(bid)
    root = store._book_dir(bid)
    if not os.path.isdir(root):
        raise HTTPException(404, "书库目录不存在")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({
            "v": 1, "book_id": bid, "title": book.get("title") or "",
            "created_at": _now_iso(),
            "note": "古籍整理工作台书库备份；不含原图/缩略图，恢复时仍引用原图片目录。",
        }, ensure_ascii=False, indent=1))
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in ("thumb", "__pycache__")]
            for name in files:
                path = os.path.join(base, name)
                rel = os.path.relpath(path, root).replace("\\", "/")
                if rel.startswith(".") or rel.endswith(".tmp") or rel.endswith(".zip"):
                    continue
                try:
                    if os.path.getsize(path) > 40 * 1024 * 1024:
                        continue
                    z.write(path, rel)
                except OSError:
                    continue
    data = buf.getvalue()
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="book-{bid}.zip"'})


@app.post("/api/books/{bid}/backup/restore")
async def api_book_backup_restore(bid: str, request: Request):
    """用 backup zip 覆盖当前书的识别/校对/标点数据；恢复前自动留 .before-restore 目录。"""
    _book_or_404(bid)
    _ensure_no_tasks(bid)
    data = await request.body()
    _ensure_no_tasks(bid)
    if not data or len(data) > 200 * 1024 * 1024:
        raise HTTPException(400, "备份文件为空或过大")
    tmp = tempfile.mkdtemp(prefix="guwen-restore-")
    try:
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise HTTPException(400, "不是有效的 zip 备份")
        names = z.namelist()
        if any(n.startswith("/") or "\\" in n or ".." in n.split("/") for n in names):
            raise HTTPException(400, "备份包含非法路径")
        if sum(i.file_size for i in z.infolist()) > 200 * 1024 * 1024:
            raise HTTPException(400, "解压后备份过大")
        z.extractall(tmp)
        book_path = os.path.join(tmp, "book.json")
        if not os.path.isfile(book_path):
            raise HTTPException(400, "备份缺少 book.json")
        with open(book_path, encoding="utf-8") as f:
            b = json.load(f)
        if b.get("id") != bid:
            raise HTTPException(400, "备份书 id 与当前书不一致")
        cur = store._book_dir(bid)
        backup_root = os.path.join(store.DATA, "backup")
        os.makedirs(backup_root, exist_ok=True)
        bak = os.path.join(backup_root, bid + ".before-restore-" + time.strftime("%Y%m%d-%H%M%S"))
        if os.path.isdir(bak):
            shutil.rmtree(bak, ignore_errors=True)
        if os.path.isdir(cur):
            os.replace(cur, bak)
        try:
            shutil.move(tmp, cur)
            tmp = ""
        except Exception:
            if os.path.isdir(bak):
                os.replace(bak, cur)
            raise
        return {"ok": True, "book": b, "backup": bak}
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


@app.get("/api/books/{bid}/punct/raw")
def api_punct_raw(bid: str):
    """初版未运行时的空白/校字连续文本（与前端 originalText 同口径）。"""
    book = _book_or_404(bid)
    _book, raw, pages, rec = punctgen.book_raw(bid)
    if not raw:
        raise HTTPException(400, "还没有已识别/已校对的叶")
    return {
        "book": bid, "title": book["title"], "text": raw,
        "chars": len(punctgen.strip_marks(raw)),
        "recognized_pages": rec, "total_pages": len(pages),
        "pages": [{"no": p["no"], "leaf": p["leaf"], "start": p["start"],
                   "end": p["end"], "act": p["act"], "recognized": p["recognized"]}
                  for p in pages],
    }


@app.get("/api/books/{bid}/structure-new")
def api_structure_new(bid: str, scope: str = "page", page: int = 1, act: str = "", mapped: int = 0):
    """新结构算法预览：单页/单齣/整本，不写 punct.json。mapped=1 返回完整文本坐标。"""
    _book_or_404(bid)
    book, full_raw, pages, rec = punctgen.book_raw(bid)
    if not full_raw:
        raise HTTPException(400, "还没有已识别/已校对的叶")
    label = ""
    scope_offsets = []
    if scope == "page":
        target = next((p for p in pages if int(p["no"]) == int(page)), None)
        if not target:
            raise HTTPException(404, "页不存在")
        if not target.get("recognized"):
            raise HTTPException(400, "该页尚未识别")
        raw = target["text"]
        scope_offsets = list(range(target["start"], target["end"]))
        label = "page:%s" % target["no"]
    elif scope == "act":
        wanted = (act or "").strip()
        if not wanted:
            raise HTTPException(400, "缺少 act")
        selected = [p for p in pages if p.get("recognized") and (p.get("act") or "") == wanted]
        if not selected:
            raise HTTPException(404, "齣不存在")
        raw = "".join(p["text"] for p in selected)
        for p in selected:
            scope_offsets.extend(range(p["start"], p["end"]))
        label = "act:%s" % wanted
    elif scope == "book":
        raw = full_raw
        scope_offsets = list(range(len(full_raw)))
        label = "book"
    else:
        raise HTTPException(400, "scope 只能是 page/act/book")
    result = structure_new.build_structure_doc(raw)
    structures = result.get("structures") or []
    out = {
        "ok": True, "book": bid, "scope": scope, "label": label,
        "chars": len(result.get("text") or ""),
        "structures": structures,
        "types": result.get("meta", {}).get("types") or {},
        "meta": result.get("meta") or {},
    }
    if mapped:
        mapped_structs = structure_new.map_structures_to_full(result, raw, scope_offsets)
        doc = store.read_punct(bid) or {}
        full_text = doc.get("text") or ""
        if not full_text:
            full_text = full_raw
        try:
            r2t = structure_new.raw_to_text_positions(full_raw, full_text)
        except Exception:
            r2t = None
        if r2t and len(r2t) >= len(full_raw) + 1:
            for st in mapped_structs:
                a = min(len(full_raw), max(0, int(st.get("raw_start", 0))))
                b = min(len(full_raw), max(0, int(st.get("raw_end", 0))))
                st["full_start"] = r2t[a]
                st["full_end"] = r2t[b]
        out["mapped_structures"] = mapped_structs
        out["full_text_len"] = len(full_text)
    return out



@app.get("/api/qupai/stats")
def api_qupai_stats():
    """本地曲牌库状态：索引数、已缓存句格数、A/B/C 分级。"""
    try:
        idx = json.load(open(os.path.join(punctgen.QUPAI_DIR, "index.json"), encoding="utf-8"))
        idx_count = len({it.get("name") for it in (idx.get("items") or []) if it.get("name")})
    except Exception:
        idx_count = 0
    kb = punctgen._load_kb()
    grades = {"A": 0, "B": 0, "C": 0}
    variant_count = 0
    for v in kb.values():
        g = v.get("grade", "C")
        grades[g] = grades.get(g, 0) + 1
        variant_count += len(v.get("variants") or [])
    path = os.path.join(punctgen.QUPAI_DIR, "kb.json")
    updated = ""
    if os.path.exists(path):
        updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    model = punctgen._load_bai_model()
    meta = (model or {}).get("meta") or {}
    return {"index_count": idx_count, "curve_count": len(kb), "grades": grades,
            "variant_count": variant_count,
            "model_ready": bool(model),
            "model_features": meta.get("feature_count") or 0,
            "model_threshold": (model or {}).get("threshold"),
            "model_act4": (meta.get("act4") or {}).get("F1"),
            "source": "搜韵 + ccdb《钦定曲谱》转录 + 本书人工 gold 变体；本地个人研究用",
            "updated": updated}


@app.get("/api/books/{bid}/acts")
def api_book_acts(bid: str):
    _book_or_404(bid)
    acts = punctgen.detect_act_pages(bid)
    return {"acts": [{"name": k, "pages": v} for k, v in acts.items()]}


@app.post("/api/books/{bid}/punct/remap")
def api_punct_remap(bid: str):
    """校字稿改动后，用 diff 把旧标点/结构重映射到新原文，避免整稿 stale。"""
    _book_or_404(bid)
    doc = store.read_punct(bid)
    if not doc:
        raise HTTPException(404, "还没有标点稿")
    old_text = doc.get("text") or ""
    old_raw = _strip_marks(old_text)
    new_raw, rec, total = _book_orig_stripped(bid)
    if old_raw == new_raw:
        return {"ok": True, "unchanged": True, "rev": doc.get("rev"),
                "marks": len(doc.get("marks") or []),
                "structures": len(doc.get("structures") or [])}
    import difflib
    sm = difflib.SequenceMatcher(a=old_raw, b=new_raw, autojunk=False)
    amap = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or tag == "replace":
            span = max(1, i2 - i1)
            for k in range(i1, i2):
                amap[k] = min(len(new_raw), int(round(j1 + (k - i1) / span * (j2 - j1))))
            if tag == "equal":
                amap[i2] = j2
        elif tag == "delete":
            for k in range(i1, i2):
                amap[k] = j1
    amap[len(old_raw)] = len(new_raw)
    old_marks = _marks_raw(old_text, doc.get("marks") or [])
    new_marks = []
    for rp, m in old_marks:
        if rp in amap:
            m2 = dict(m); m2["pos"] = None
            new_marks.append((amap[rp], m2))
    # 保留旧换行骨架
    for rp in _old_newlines_raw(old_text):
        if rp in amap:
            new_marks.append((amap[rp], {"ch": "\n", "state": "ok",
                                         "source": "human", "conf": 1}))
    new_marks.sort(key=lambda x: x[0])
    text = _build_text(new_raw, new_marks)
    # 旧结构按 raw→new raw→text 位置映射；失败时才重新识别
    structures = None
    old_structs = doc.get("structures") or []
    if old_structs:
        try:
            rp_new = _raw_positions(text)
            mapped = []
            for st in old_structs:
                ra, rb = _structure_raw_span(old_text, st)
                if ra >= rb:
                    continue
                na = amap.get(ra, ra)
                nb = amap.get(rb, rb)
                if nb > na and nb < len(rp_new):
                    attrs = dict(st.get("attrs") or {})
                    old_chen = attrs.get("chen")
                    if isinstance(old_chen, list) and old_chen:
                        new_chen = []
                        for pair in old_chen:
                            try:
                                ca, cb = int(pair[0]), int(pair[1])
                            except Exception:
                                continue
                            cra, crb = _structure_raw_span(old_text, {"start": ca, "end": cb})
                            cna = amap.get(cra, cra)
                            cnb = amap.get(crb, crb)
                            if cnb > cna and cnb < len(rp_new):
                                new_chen.append([rp_new[cna], rp_new[cnb - 1] + 1])
                        attrs["chen"] = new_chen
                    mapped.append(dict(st, start=rp_new[na], end=rp_new[nb], attrs=attrs))
            if mapped:
                structures = mapped
        except Exception:
            structures = None
    if structures is None:
        structures, _ = llm_punct.score_doc(new_raw, text, 0, 0)
    by_raw = {}
    for rp, m in new_marks:
        by_raw.setdefault(int(rp), []).append(m)
    marks = []
    ri = 0
    for i, ch in enumerate(text):
        if ch in punctgen.PUNCT_CHARS and ch != "\n":
            srcm = by_raw.get(ri)
            mm = srcm.pop(0) if srcm else None
            m2 = dict(mm or {})
            m2["pos"] = i
            m2["ch"] = ch
            m2.setdefault("state", "cand")
            m2.setdefault("source", "machine")
            m2.setdefault("conf", 0.7)
            marks.append(m2)
        elif ch != "\n":
            ri += 1
    breaks = [i for i, ch in enumerate(text) if ch == "\n"]
    meta = dict(doc.get("meta") or {})
    meta["source"] = "remap"
    try:
        store.push_punct_history(bid, doc)
    except Exception:
        pass
    data = {"v": 2, "book": bid, "text": text, "breaks": breaks,
            "marks": marks, "structures": structures,
            "page_ranges": punctgen.recompute_page_ranges(bid, text),
            "base_hash": _base_hash(new_raw), "updated_at": _now_iso(),
            "rev": int(doc.get("rev") or 0) + 1, "saved_chars": len(new_raw),
            "paragraphs": (len(breaks) + 1 if text else 0), "meta": meta}
    if not store.save_punct_if_rev(bid, data, int(doc.get("rev") or 0)):
        raise HTTPException(409, "重映射失败：标点稿已被其他窗口/任务更新，请重试")
    return {"ok": True, "rev": data["rev"], "chars": len(new_raw),
            "marks": len(marks), "structures": len(structures),
            "added": max(0, len(new_raw) - len(old_raw))}


# ---------------- 全局设置 ----------------

@app.get("/api/settings")
def api_get_settings():
    return store.public_settings()


@app.put("/api/settings")
def api_save_settings(req: SettingsReq):
    # AI endpoint / secret changes go through /api/llm/config only.
    data = {k: v for k, v in (req.data or {}).items() if not k.startswith("llm_")}
    store.save_settings(data)
    return store.public_settings()


# ---------------- 标点稿导出 ----------------
@app.get("/api/books/{bid}/export-punct.docx")
def api_export_punct(bid: str, simp: int = 1, heading: int = 1,
                     font: str = "宋体", size: float = 12,
                     role_bold: int = 0, indent: float = 0):
    book = _book_or_404(bid)
    doc = store.read_punct(bid)
    if not doc:
        raise HTTPException(404, "还没有标点稿")
    orig, _, _ = _book_orig_stripped(bid)
    if doc.get("base_hash") != _base_hash(orig):
        raise HTTPException(409, "标点稿已过期（校字稿有改动），请先重新保存标点稿")
    text = doc.get("text") or ""
    structures = _clean_structs(doc.get("structures"), text)
    if structures:
        return _docx_from_structures(book["title"], text, structures, simp, heading, font, size,
                                     role_bold=bool(role_bold), indent_chars=indent)
    break_doc = sorted({int(b) for b in (doc.get("breaks") or [])
                        if isinstance(b, int) and 0 <= b < len(text)})
    if break_doc:
        parts, last = [], 0
        for b in break_doc:
            parts.append(text[last:b])
            last = b + 1
        parts.append(text[last:])
        para_text = "\n\n".join(parts)
    else:
        para_text = text
    return _docx_from_paras(book["title"], para_text, simp, heading, font, size,
                            role_bold=bool(role_bold), indent_chars=indent)


@app.get("/img/{bid}/{no}")
def api_img(bid: str, no: int):
    _book_or_404(bid)
    p = store.image_path(bid, no)
    if not p or not os.path.exists(p):
        raise HTTPException(404, "image not found")
    return FileResponse(p, headers={"Cache-Control": "max-age=86400"})


@app.get("/thumb/{bid}/{no}")
def api_thumb(bid: str, no: int, w: int = Query(220, ge=80, le=600)):
    _book_or_404(bid)
    try:
        p = store.thumb(bid, no, w)
    except Exception:
        p = None
    if not p or not os.path.exists(p):
        raise HTTPException(404, "thumb not found")
    return FileResponse(p, headers={"Cache-Control": "max-age=86400"})


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("GUWEN_HOST", "127.0.0.1")
    port = int(os.environ.get("GUWEN_PORT", "8770"))
    uvicorn.run(app, host=host, port=port, log_level="info")
