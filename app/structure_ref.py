# -*- coding: utf-8 -*-
"""结构参照转移：把同书的旧 canonical 结构按原文对齐到新文本。

适用：同一本书已有可信结构参照，但 OCR 文本发生了重识别/版本差异。
不适用：没有参照结构的新书。此时只能作为评测/教师信号。
"""
import bisect
import difflib


PUNCT = set("。，、；：？！「」『』《》（）〈〉【】〔〕——……·“”‘’…—,.;:?!()[]<>\"'")
SPACE = set(" \t\r\n\u3000")


def _raw_info(text):
    chars = []
    t2r = [0] * (len(text) + 1)
    r2t = []
    for i, ch in enumerate(text):
        t2r[i] = len(chars)
        if ch in PUNCT or ch in SPACE or ch.isspace():
            continue
        chars.append(ch)
        r2t.append(i)
    t2r[len(text)] = len(chars)
    return "".join(chars), t2r, r2t


def _old_to_new(old_raw, new_raw):
    sm = difflib.SequenceMatcher(None, old_raw, new_raw, autojunk=False)
    o2n = [-1] * (len(old_raw) + 1)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            o2n[a + k] = b + k
    mapped = [i for i, v in enumerate(o2n) if v >= 0]
    for i, v in enumerate(o2n):
        if v >= 0:
            continue
        j = bisect.bisect_left(mapped, i)
        cands = []
        if j:
            cands.append((abs(mapped[j - 1] - i), o2n[mapped[j - 1]]))
        if j < len(mapped):
            cands.append((abs(mapped[j] - i), o2n[mapped[j]]))
        o2n[i] = min(cands)[1] if cands else 0
    o2n[-1] = len(new_raw)
    return o2n, sm.ratio()


def transfer_structures(old_doc, new_text):
    """返回 (structures, info)。structures 使用 new_text 的文本坐标。"""
    old_text = old_doc.get("text") or ""
    old_raw, old_t2r, _ = _raw_info(old_text)
    new_raw, new_t2r, new_r2t = _raw_info(new_text)
    o2n, ratio = _old_to_new(old_raw, new_raw)
    out = []
    for st in old_doc.get("structures") or []:
        try:
            xa = old_t2r[max(0, min(len(old_text), int(st["start"])))]
            xb = old_t2r[max(0, min(len(old_text), int(st["end"])))]
        except Exception:
            continue
        na, nb = o2n[xa], o2n[xb]
        if nb <= na:
            continue
        ta = new_r2t[na] if na < len(new_r2t) else len(new_text)
        tb = new_r2t[nb] if nb < len(new_r2t) else len(new_text)
        if tb <= ta:
            continue
        item = dict(st)
        item["start"] = ta
        item["end"] = tb
        item["attrs"] = dict(st.get("attrs") or {})
        item["attrs"]["transferred_from"] = "canonical"
        out.append(item)
    # 去重叠，保持顺序
    out.sort(key=lambda x: (int(x["start"]), int(x["end"])))
    clean = []
    pos = 0
    for st in out:
        a, b = int(st["start"]), int(st["end"])
        if a < pos:
            a = pos
        if b > a:
            item = dict(st, start=a, end=b)
            clean.append(item)
            pos = b
    return clean, {"match_ratio": round(ratio, 5),
                   "old_raw": len(old_raw), "new_raw": len(new_raw)}
