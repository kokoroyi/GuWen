# -*- coding: utf-8 -*-
"""古籍 OCR 流水线（M1 版）：检测栏框 → 识别 → 按识别字数切字。

- 检测：PP-OCRv6_small_det（整页缩到 1600 宽再检）
- 识别：PP-OCRv6_medium_rec（整列旋转 90°）
- CPU 加速：识别模型启用 oneDNN/MKLDNN，线程数默认 min(16, CPU 逻辑核)
  检测模型暂不启用 oneDNN（Paddle 3.3 PIR oneDNN 在该模型上报
  ConvertPirAttribute2RuntimeAttribute，保留普通 CPU 执行）
- 切字：先识别、用识别出的字数等距分格，边界吸附到墨量谷
- 单字/短标题欄：检测框小于 0.30 页高时单独接收，识别校验 1–2 字且可信才收
"""
import os
import sys
import numpy as np
from PIL import Image

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

DET_NAME = "PP-OCRv6_small_det"
REC_NAME = "PP-OCRv6_medium_rec"
DET_W = 1600
_MODELS = {}

# CPU inference tuning. Detection uses PP-OCRv6 + Paddle 3.3 PIR; enabling
# oneDNN on that model currently raises ConvertPirAttribute2RuntimeAttribute,
# so detection stays on the plain CPU engine. Recognition is compatible with
# oneDNN and benefits from it. Both can be switched via env for A/B testing.
def _env_flag(name, default):
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off")


_REC_MKLDNN = _env_flag("GUWEN_OCR_MKLDNN", "1")
_DET_MKLDNN = _env_flag("GUWEN_OCR_MKLDNN_DET", "0")
_default_threads = min(16, os.cpu_count() or 12)
_CPU_THREADS = max(1, min(int(os.environ.get("GUWEN_OCR_THREADS", str(_default_threads)) or str(_default_threads)), os.cpu_count() or 12))
_MKLDNN_CACHE = int(os.environ.get("GUWEN_OCR_MKLDNN_CACHE", "10") or "10")
if _REC_MKLDNN or _DET_MKLDNN:
    os.environ.setdefault("OMP_NUM_THREADS", str(_CPU_THREADS))


def _new_predictor(cls, model_name, enable_mkldnn):
    kwargs = {
        "model_name": model_name,
        "device": "cpu",
        "enable_mkldnn": bool(enable_mkldnn),
        "cpu_threads": _CPU_THREADS,
        "mkldnn_cache_capacity": _MKLDNN_CACHE,
    }
    try:
        return cls(**kwargs)
    except Exception as e:
        if not enable_mkldnn:
            raise
        # Some Paddle/CPU builds reject oneDNN for a model; degrade safely.
        print("OCR MKLDNN init failed, falling back to CPU: %s" % e, file=sys.stderr)
        kwargs["enable_mkldnn"] = False
        return cls(**kwargs)


def models():
    from paddleocr import TextDetection, TextRecognition
    if "det" not in _MODELS:
        _MODELS["det"] = _new_predictor(TextDetection, DET_NAME, _DET_MKLDNN)
    if "rec" not in _MODELS:
        _MODELS["rec"] = _new_predictor(TextRecognition, REC_NAME, _REC_MKLDNN)
    return _MODELS["det"], _MODELS["rec"]


def _json_res(r):
    try:
        return r.json["res"]
    except Exception:
        return r if isinstance(r, dict) else None


def detect_raw(det, im, tmp):
    sm = im.copy()
    sm.thumbnail((DET_W, 6000), Image.LANCZOS)
    p = os.path.join(tmp, "det.png")
    sm.save(p)
    s = im.width / sm.width
    boxes = []
    for r in det.predict(p):
        d = _json_res(r)
        if not isinstance(d, dict):
            continue
        for poly in (d.get("dt_polys") or d.get("dt_boxes") or []):
            xs = [q[0] for q in poly]
            ys = [q[1] for q in poly]
            boxes.append([min(xs) * s, min(ys) * s, max(xs) * s, max(ys) * s])
    return boxes


def is_column(b, im):
    return (b[3] - b[1]) > 0.30 * im.height and (b[2] - b[0]) > 40


def is_short_column(b, im):
    """短栏候选：1–6 个字的标题/题签栏，长宽比可以到很瘦。"""
    w, h = b[2] - b[0], b[3] - b[1]
    if not (0.03 * im.height <= h <= 0.40 * im.height):
        return False
    if not (50 <= w <= 0.25 * im.width):
        return False
    return 0.4 <= h / max(1.0, w) <= 6.0


def rec_text(rec, im, box, tmp, idx):
    x0, y0, x1, y1 = [int(v) for v in box]
    crop = im.crop((max(0, x0), max(0, y0), min(im.width, x1), min(im.height, y1)))
    strip = crop.rotate(90, expand=True)
    p = os.path.join(tmp, "rec_%03d.png" % idx)
    strip.save(p)
    texts, scores = [], []
    for r in rec.predict(p):
        d = _json_res(r)
        if not isinstance(d, dict):
            continue
        t = d.get("rec_text") or d.get("rec_texts")
        sc = d.get("rec_score") or d.get("rec_scores")
        if isinstance(t, list):
            texts += t
        elif isinstance(t, str):
            texts.append(t)
        if isinstance(sc, list):
            scores += sc
        elif isinstance(sc, (int, float)):
            scores.append(float(sc))
    return "".join(texts), (float(scores[0]) if scores else 0.0)


def is_cjk(ch):
    o = ord(ch)
    return (0x3400 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF) or (0x20000 <= o <= 0x2FFFF)


def count_cjk(txt):
    return sum(1 for ch in txt or "" if is_cjk(ch))


def clean_text(txt):
    """只保留汉字，作为校对文本（标点/数字不进校对层）。"""
    return "".join(ch for ch in txt or "" if is_cjk(ch))


def _est_chars(ink, height):
    if height < 120:
        return 1
    p = ink - ink.mean()
    if np.allclose(p, 0):
        return max(1, int(round(height / 230)))
    ac = np.correlate(p, p, mode="full")[len(p) - 1:]
    ac[:50] = 0
    hi = min(420, len(ac) - 2)
    best, bv = 0, 0
    for lag in range(60, hi):
        if ac[lag] > ac[lag - 1] and ac[lag] >= ac[lag + 1] and ac[lag] > bv:
            best, bv = lag, ac[lag]
    n = max(1, int(round(height / best))) if best > 0 else max(1, int(round(height / 230)))
    if height < 480:
        n = 1
    if best and height < best * 1.45:
        n = 1
    return max(1, min(n, 40))


def split_chars(im_gray, box, n_hint=None):
    """切字框。n_hint 给了就等距分格（边界吸附墨量谷）；否则退回墨量投影。"""
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(im_gray.width, x1), min(im_gray.height, y1)
    if x1 - x0 < 8 or y1 - y0 < 20:
        return []
    pad = 4
    col = im_gray.crop((x0 + pad, y0 + pad, x1 - pad, y1 - pad))
    g = np.array(col) < 185
    if not g.any():
        return []
    cr = g.mean(axis=0)
    g[:, cr > 0.92] = False
    ink = g.mean(axis=1).astype(float)
    rows = np.where(g.any(axis=1))[0]
    if not len(rows):
        return []
    top, bot = int(rows.min()), int(rows.max())
    span = bot - top + 1

    def _boxes_from_bounds(bounds):
        out = []
        for i in range(len(bounds) - 1):
            a, b = bounds[i], bounds[i + 1]
            cell = g[a:b, :]
            yy, xx = np.where(cell)
            if len(xx) < 16:
                continue
            out.append([x0 + pad + int(xx.min()), y0 + pad + a + int(yy.min()),
                        x0 + pad + int(xx.max()) + 1, y0 + pad + a + int(yy.max()) + 1])
        return out

    if n_hint and 1 <= n_hint <= 40:
        n = int(n_hint)
        sm = np.convolve(ink, np.ones(5) / 5.0, mode="same")
        step = span / n
        bounds = [top + int(round(i * step)) for i in range(n + 1)]
        bounds[0], bounds[-1] = top, bot + 1
        for i in range(1, n):
            pred = bounds[i]
            w = max(6, int(step * 0.18))
            lo, hi = max(top + 2, pred - w), min(bot - 1, pred + w)
            if hi > lo:
                bounds[i] = lo + int(np.argmin(sm[lo:hi + 1]))
        return _boxes_from_bounds(bounds)

    n_est = _est_chars(ink[top:bot + 1], span)
    char_h = max(24.0, min(span / max(1, n_est), span / 1.2))
    sm = np.convolve(ink, np.ones(5) / 5.0, mode="same")
    thr = max(0.006, sm.max() * 0.12)
    on = sm > thr
    bands, i = [], 0
    while i < len(on):
        if not on[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(on) and on[j + 1]:
            j += 1
        bands.append([i, j])
        i = j + 1
    merged = []
    for a, b in bands:
        if merged and (a - merged[-1][1] - 1) <= max(2, char_h * 0.18):
            merged[-1][1] = b
        else:
            merged.append([a, b])
    final = []
    colw = col.width
    for a, b in merged:
        cell = g[max(0, a):min(g.shape[0], b + 1), :]
        yy, xx = np.where(cell)
        if not len(xx):
            continue
        cw = int(xx.max()) - int(xx.min()) + 1
        chh = int(yy.max()) - int(yy.min()) + 1
        if chh < 0.30 * char_h and cw > 0.55 * colw:
            continue
        if chh < 0.30 * char_h and cw < 0.45 * colw:
            continue
        k = max(1, int(round((b - a + 1) / char_h)))
        if k > 1:
            step = (b - a + 1) / k
            for t in range(k):
                final.append([int(a + t * step), int(a + (t + 1) * step) - 1])
        else:
            final.append([a, b])
    bounds = []
    for a, b in final:
        bounds += [a, b + 1]
    return _boxes_from_bounds(bounds)


def recognize_page(img_path, tmp_dir):
    """识别一页，返回 {w, h, rows:[{box,text,score,type,chars:[{box,text,score,flag}]}]}"""
    os.makedirs(tmp_dir, exist_ok=True)
    det, rec = models()
    im = Image.open(img_path).convert("RGB")
    gray = im.convert("L")
    raw = detect_raw(det, im, tmp_dir)
    raw.sort(key=lambda b: -b[0])

    cols = []
    for b in raw:
        if is_column(b, im):
            cols.append({"box": b, "single": False})

    # 短栏（1–6 字标题/题签）：单独识别校验，可信才收
    idx = 0
    for b in raw:
        if is_column(b, im) or not is_short_column(b, im):
            continue
        txt, sc = rec_text(rec, im, b, tmp_dir, 100 + idx)
        idx += 1
        t = clean_text(txt)
        if 1 <= len(t) <= 6 and sc >= 0.55:
            cols.append({"box": b, "single": True, "text": t, "score": sc})
    cols.sort(key=lambda c: -c["box"][0])

    rows = []
    for ci, c in enumerate(cols):
        b = c["box"]
        if c.get("text") is not None:
            text, score = c["text"], c["score"]
        else:
            raw_txt, score = rec_text(rec, im, b, tmp_dir, ci)
            text = clean_text(raw_txt)
        n_hint = len(text) if c["single"] else (len(text) or None)
        cbs = split_chars(gray, b, n_hint=n_hint)
        if not cbs:
            continue
        # 字与框一一对应；框多/字少时补空字
        n = len(cbs)
        chars = []
        for i, cb in enumerate(cbs):
            ch = text[i] if i < len(text) else ""
            flag = "ok" if (ch and score >= 0.85) else "red"
            chars.append({"box": [int(v) for v in cb], "text": ch, "score": round(float(score), 3), "flag": flag})
        rows.append({
            "box": [round(float(v), 1) for v in b],
            "text": "".join(ch["text"] for ch in chars),
            "score": round(float(score), 3),
            "type": "标题" if (c["single"] or len(text) <= 4) else "正文",
            "chars": chars,
        })
    return {"w": im.width, "h": im.height, "rows": rows,
            "stats": {"rows": len(rows), "chars": sum(len(r["chars"]) for r in rows),
                      "red": sum(1 for r in rows for ch in r["chars"] if ch["flag"] == "red")}}


# ---------------- 按给定框识别（框调整后用） ----------------
def _row_from_box(im, gray, box, char_boxes, tmp_dir, idx):
    det, rec = models()
    raw_txt, score = rec_text(rec, im, box, tmp_dir, idx)
    text = clean_text(raw_txt)
    if char_boxes:
        cbs = [[float(v) for v in cb] for cb in char_boxes]
    else:
        cbs = split_chars(gray, box, n_hint=(len(text) or None))
    m, n = len(text), len(cbs)
    chars = []
    for i, cb in enumerate(cbs):
        if m == 0:
            ch = ""
        elif m == n:
            ch = text[i]
        elif m > n:
            ch = text[i] if i < n - 1 else text[i:]   # 多出来的字并到最后一格，不丢文本
        else:
            ch = text[i] if i < m else ""
        flag = "ok" if (ch and score >= 0.85) else "red"
        chars.append({"box": [int(round(float(v))) for v in cb], "text": ch,
                      "score": round(float(score), 3), "flag": flag})
    return {"box": [round(float(v), 1) for v in box],
            "text": "".join(c["text"] for c in chars),
            "score": round(float(score), 3),
            "type": "标题" if len(text) <= 4 else "正文",
            "chars": chars}


def recognize_row(img_path, box, char_boxes, tmp_dir):
    """用给定的栏框（可选字框）重识一栏。"""
    os.makedirs(tmp_dir, exist_ok=True)
    im = Image.open(img_path).convert("RGB")
    return _row_from_box(im, im.convert("L"), box, char_boxes, tmp_dir, 0)


def recognize_rows(img_path, rows, tmp_dir):
    """按当前框结构重识整页（框调整后使用）。"""
    os.makedirs(tmp_dir, exist_ok=True)
    im = Image.open(img_path).convert("RGB")
    gray = im.convert("L")
    out = []
    for i, row in enumerate(rows):
        box = row.get("box")
        if not box:
            continue
        cbs = [c.get("box") for c in (row.get("chars") or []) if c.get("box")]
        r = _row_from_box(im, gray, box, cbs or None, tmp_dir, i)
        if r:
            out.append(r)
    return {"w": im.width, "h": im.height, "rows": out,
            "stats": {"rows": len(out), "chars": sum(len(r["chars"]) for r in out),
                      "red": sum(1 for r in out for ch in r["chars"] if ch["flag"] == "red")}}
