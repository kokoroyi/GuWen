# -*- coding: utf-8 -*-
"""句读文档与初版生成（规则版）。

文档 v2：
{
  v:2, book, text, breaks, marks[], structures[], base_hash, ...
}
- marks: 标点位置（text 中的偏移），state: a/b/cand/ok
- structures: 片段 {start,end,type,conf,attrs}
初版生成：结构片段（曲牌/曲文/宾白/科介/角色/其他）+ 曲牌句格断点 + 宾白候选。
曲牌句格只覆盖 data/qupai/kb.json 里的种子曲牌，其余按 C 级只标结构。
"""
import json
import os
import re
import time

import store

APP = os.path.dirname(os.path.abspath(__file__))
QUPAI_DIR = os.path.join(store.DATA, "qupai")
PUNCT_CHARS = set(
    "。，、；：？！「」『』《》（）〈〉【】〔〕——……·“”‘’…—"
    ",.;:?!()[]<>\"' \t\r\n\u3000"
)
TYPE_SET = {"qupai", "wen", "bai", "ke", "role", "other", "title", "subtitle"}
STATE_SET = {"a", "b", "cand", "ok"}

_ROLE_RE = re.compile(
    r"(?:老旦|小旦|貼旦|贴旦|小生|副末|正末|淨|净|末|生|旦|丑|外|雜|杂|眾|众|內|内|官|駕|驾)"
    r"(?:唱|白|云|雲|上|下|科|介|哭|笑|嘆|叹|怒|坐|行|拜|跪|打|殺|杀|問|问|說|说|道)"
)
# 曲牌后紧跟的单独角色名（如「生京兆風流」「小生青青草色」）；只用于切掉曲文开头的角色标签。
_ROLE_LABEL_RE = re.compile(
    r"^(?:老旦|小旦|貼旦|贴旦|小生|副末|正末|副淨|副净|淨|净|末|生|旦|丑|外|雜|杂|眾|众|內|内)"
)
_KE_RE = re.compile(
    r"(?:做[^\n]{0,14}?科|(?:上|下|哭|笑|嘆|叹|怒|坐|行|拜|跪|打|殺|杀|見|见|背|扶)(?:科|介))"
)
_ACT_RE = re.compile(r"第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出]")
_QP_RE = None
_QP_NAMES = None
_KB = None
_MODEL = None
_MODEL_TRIED = False

# 索引来自搜韵时多为简体，而本书 OCR 为繁体；补齐常用繁体曲牌名。
_TRAD_EXTRA = [
    "滿庭芳", "漁家傲", "尾聲", "點絳唇", "皂羅袍", "江兒水", "混江龍",
    "二郎神", "亭前柳", "山坡羊", "朝天子", "梁州序", "梁州令", "梅花酒",
    "收江南", "新水令", "折桂令", "太平令", "太平歌", "刮鼓令", "哭相思",
    "六麼令", "摧拍", "下山虎", "桂枝香", "浣溪沙", "菊花新", "榴花泣",
    "夜遊朝", "夜游朝",
]

# 宾白句末/停顿虚词（只做候选，不追求高准确）
_BAI_END = set("矣焉哉乎耳耶夫者了罷罢麼么呢呵呀也畢毕謝谢")
_BAI_END2 = ("則個", "则个", "便了", "甚麼", "什么", "怎麼", "怎么", "如何",
             "這等", "这等", "那裏", "那里", "這裏", "这里")
_BAI_START = ("只是", "只因", "如今", "倘若", "難道", "难道", "爹爹", "母親", "母亲",
              "哥哥", "兄弟", "夫人", "相公", "小姐", "孩兒", "孩儿", "小生", "老旦",
              "副末", "正末", "承應", "承应", "院子", "廟祝", "庙祝", "道姑", "船家",
              "驛丞", "驿丞", "稟", "禀", "唱", "白", "正是",
              "况且", "況且", "因此", "但只", "賴有", "赖有", "眞是", "真是", "你看",
              "原是", "不覺", "不觉", "又是", "舊規", "旧规", "會集", "会集", "只得",
              "在此", "諸君", "诸君", "此酒", "皆是", "如此", "亦復", "亦复", "下官",
              "代有", "且喜", "四境", "近天", "呈送", "請老大人", "请老大人")
# 这些字后面的「也」多为句中停顿，不直接断句
_BAI_AFTER_YE = set("不是有在要可未沒没無无能曾該该敢肯會会為为與与和同的了著着過过")
# 这些字后面接称谓/角色时，多为「动词+宾语」或「角色+称呼」，不宜断
_BAI_NO_BREAK_BEFORE = set("與与和同跟對对替為为把被讓让叫是攜携帶带領领留我你他個个兩两這这那有小生末生旦老旦副末正末")
_BAI_ROLE = ("老旦", "小旦", "貼旦", "贴旦", "小生", "副末", "正末", "副淨", "副净",
             "淨", "净", "末", "生", "旦", "丑", "外", "雜", "杂", "眾", "众", "內", "内")


def _load_qupai():
    global _QP_RE, _QP_NAMES
    if _QP_RE is not None:
        return
    names = ["前腔"]
    # 句格库里已有的繁体曲牌名也参与识别（索引多为简体）
    try:
        names += list(_load_kb().keys())
    except Exception:
        pass
    names += _TRAD_EXTRA
    path = os.path.join(QUPAI_DIR, "index.json")
    if os.path.exists(path):
        try:
            d = json.load(open(path, encoding="utf-8"))
            names += [it["name"] for it in d.get("items") or [] if it.get("name")]
        except Exception:
            pass
    # 去重 + 长名优先，避免「太平令」先被「太平」吃掉
    seen = set()
    names = [n for n in names if len(n) >= 2 and not (n in seen or seen.add(n))]
    names = sorted(names + ["前腔"], key=lambda x: (-len(x), x))
    _QP_NAMES = names
    _QP_RE = re.compile("|".join(re.escape(n) for n in names))


def _load_kb():
    global _KB
    if _KB is not None:
        return _KB
    _KB = {}
    path = os.path.join(QUPAI_DIR, "kb.json")
    if os.path.exists(path):
        try:
            _KB = json.load(open(path, encoding="utf-8")).get("curves") or {}
        except Exception:
            _KB = {}
    return _KB


def _load_bai_model():
    """本地实验句读模型；不存在时返回 None，不影响规则生成。"""
    global _MODEL, _MODEL_TRIED
    if _MODEL_TRIED:
        return _MODEL
    _MODEL_TRIED = True
    path = os.path.join(QUPAI_DIR, "bai_model.json")
    if not os.path.exists(path):
        return None
    try:
        d = json.load(open(path, encoding="utf-8"))
        _MODEL = {
            "threshold": float(d.get("threshold") or 0.5),
            "intercept": float(d.get("intercept") or 0.0),
            "features": d.get("features") or {},
            "meta": d.get("meta") or {},
        }
    except Exception:
        _MODEL = None
    return _MODEL


def _bai_feats(t, i):
    return {
        "l1": t[i - 1], "r1": t[i],
        "l2": t[max(0, i - 2):i], "r2": t[i:i + 2],
        "l3": t[max(0, i - 3):i], "r3": t[i:i + 3],
        "c2": t[i - 2] if i >= 2 else "^",
        "c3": t[i - 3] if i >= 3 else "^",
        "n2": t[i + 1] if i + 1 < len(t) else "$",
        "n3": t[i + 2] if i + 2 < len(t) else "$",
    }


def _model_candidates(text):
    """整页跑本地字符模型，只产生 cand 候选；模型不存在则空。"""
    import math
    m = _load_bai_model()
    if not m:
        return []
    feats = m["features"]
    intercept = m["intercept"]
    th = m["threshold"]
    out = []
    for i in range(2, len(text) - 2):
        f = _bai_feats(text, i)
        score = intercept
        for k, v in f.items():
            score += feats.get(k + "=" + v, 0.0)
        prob = 1.0 / (1.0 + math.exp(-score))
        if prob >= th:
            left = text[i - 1]
            ch = "。" if (left in _BAI_END or text[i] in "我你他這那但只") else "，"
            out.append((i, ch, "cand", {"source": "machine-model", "conf": round(prob, 2)}))
    return out


def strip_marks(s):
    return "".join(ch for ch in (s or "") if ch not in PUNCT_CHARS)


def page_full_text(d):
    """和前端 punct.js originalText 完全一致。"""
    out = ""
    for r in (d or {}).get("rows") or []:
        if r.get("chars"):
            t = "".join((c.get("text") or "") for c in (r.get("chars") or []))
        else:
            t = r.get("text") or ""
        if not t:
            continue
        if (r.get("type") or "正文") == "标题":
            if out and not out.endswith("\n"):
                out += "\n"
            out += t + "\n"
        else:
            out += t
    return re.sub(r"\n{3,}", "\n\n", out)


def book_raw(bid):
    book = store.load_book(bid)
    if not book:
        raise ValueError("book not found")
    pages = []
    acc = 0
    recognized = 0
    current_act = ""
    for pg in book["pages"]:
        d = store.read_page(bid, pg["no"])
        t = page_full_text(d) if d else ""
        if d:
            recognized += 1
        act = current_act
        if t:
            ms = _ACT_RE.findall(t)
            if ms:
                act = ms[-1]
                current_act = act
        pages.append({"no": pg["no"], "leaf": pg["leaf"], "start": acc, "end": acc + len(t),
                      "text": t, "act": act, "recognized": bool(d)})
        acc += len(t)
    return book, "".join(p["text"] for p in pages), pages, recognized


def detect_act_pages(bid):
    """返回 {act: [页号...]}，没有识别到齣名时归入「未分齣」。"""
    _, _, pages, _ = book_raw(bid)
    acts = {}
    for p in pages:
        if not p["recognized"]:
            continue
        acts.setdefault(p["act"] or "未分齣", []).append(p["no"])
    return acts


def _safe_spans(tokens, text_len):
    """涂掉重叠 token（曲牌 > 科介 > 角色），返回按位置排序的非重叠 token。"""
    prio = {"qupai": 3, "ke": 2, "role": 1}
    chosen = []
    for t in sorted(tokens, key=lambda x: (-prio.get(x[0], 0), x[1], -(x[2] - x[1]))):
        if any(not (t[2] <= c[1] or t[1] >= c[2]) for c in chosen):
            continue
        chosen.append(t)
    return sorted(chosen, key=lambda x: x[1])


def detect_structures(text):
    """在一个页文本上识别结构片段，返回 [(start,end,type,conf,attrs)]（页内偏移）。"""
    _load_qupai()
    tokens = []
    for m in _QP_RE.finditer(text):
        tokens.append(("qupai", m.start(), m.end(), 0.9, {"name": m.group(0)}))
    for m in _ROLE_RE.finditer(text):
        tokens.append(("role", m.start(), m.end(), 0.7, {}))
    for m in _KE_RE.finditer(text):
        tokens.append(("ke", m.start(), m.end(), 0.65, {}))
    chosen = _safe_spans(tokens, len(text))
    spans = []
    pos = 0
    mode = "other"
    for typ, s, e, conf, attrs in chosen:
        if s < pos:
            continue
        if s > pos:
            spans.append((pos, s, mode, conf if mode != "other" else 0.3, {}))
        spans.append((s, e, typ, conf, dict(attrs)))
        pos = e
        mode = "qu" if typ == "qupai" else ("bai" if typ in ("role", "ke") else mode)
    if pos < len(text):
        spans.append((pos, len(text), mode, 0.3, {}))
    # 前腔/前腔换头：属性继承上一支曲牌，用于查句格
    last_qp = None
    for sp in spans:
        if sp[2] == "qupai":
            nm = (sp[4] or {}).get("name") or ""
            if nm.startswith("前腔") and last_qp:
                sp[4]["prev"] = True
                sp[4]["name"] = last_qp
            else:
                last_qp = nm
    out = []
    for s, e, typ, conf, attrs in spans:
        if e <= s:
            continue
        # 纯空白/换行片段归其他，避免生成大量空结构
        if not (text[s:e].strip()):
            typ = "other"
        # 曲文开头常带一个单独的角色名（生/末/小生…），切出来，避免算进句格
        if typ == "qu":
            m = _ROLE_LABEL_RE.match(text[s:e])
            if m and m.end() < e - s:
                out.append({"start": s, "end": s + m.end(), "type": "role",
                            "conf": 0.7, "attrs": {}})
                s += m.end()
                body = text[s:e]
                if not body.strip():
                    continue
        out.append({"start": s, "end": e, "type": {"qu": "wen", "bai": "bai"}.get(typ, typ),
                    "conf": round(conf, 2), "attrs": attrs})
    return out


def _bai_candidates(text, s, e, conf=0.4):
    """宾白/散文的候选断句：句末虚词、常见句首词、角色提示、科介。

    只产生候选（state=cand），供精校接受/拒绝；不追求直接判定。
    """
    out = []
    if e - s < 6:
        return out
    for i in range(s + 2, e):
        left = text[i - 1]
        right = text[i]
        add = None
        if left in _BAI_END:
            if left == "也" and right in _BAI_AFTER_YE:
                continue
            add = "。"
        elif any(text[max(s, i - len(w)):i] == w for w in _BAI_END2):
            add = "。"
        elif any(text.startswith(w, i) for w in _BAI_START):
            prev = text[i - 1]
            # 「與哥哥」「和母親」等介词/动词后不是句首；「只是爹爹」也不在爹爹前断
            if prev in _BAI_NO_BREAK_BEFORE:
                continue
            add = "，" if right not in "我你他這那但只" else "。"
        elif left in "介科":
            # 「以介春觴」不是科介；其余 …介/…科 多为舞台提示结尾
            if text[max(s, i - 2):i] == "以介":
                continue
            add = "。"
        elif i >= 2 and text[i] in "外衆众生旦末淨净丑" and text[i - 1] in "下了介坐酒畢毕謝谢上":
            # 「受了下…外…」「遞酒介…外…」这类角色提示前断句
            add = "。"
        elif text.startswith("老旦", i) or text.startswith("小生", i) or text.startswith("副末", i):
            add = "。"
        if add:
            out.append((i, add, "cand", {"source": "machine-bai", "conf": conf}))
    # 同一位置只留一个；去掉离片段边界太近的
    seen = set()
    clean = []
    for pos, ch, state, attrs in out:
        if pos in seen or pos <= s + 1 or pos >= e:
            continue
        seen.add(pos)
        clean.append((pos, ch, state, attrs))
    return clean


def _insertions_for_page(text, structures, prev_qp=None, use_model=False):
    """返回 ([(raw_pos, mark_char, state, attrs)], 本页结束时的曲牌名)。"""
    _load_kb()
    kb = _load_kb()
    marks = []
    last_qp = prev_qp
    page_has_qp = any(st.get("type") == "qupai" for st in structures)
    for st in structures:
        typ = st["type"]
        s, e = st["start"], st["end"]
        body = text[s:e]
        if typ == "qupai":
            last_qp = st.get("attrs", {}).get("name") or body
            continue
        if typ == "wen" and last_qp:
            curve = kb.get(last_qp) or {}
            variants = curve.get("variants") or []
            if not variants and curve.get("counts"):
                variants = [{"counts": curve.get("counts") or [],
                             "rhythms": curve.get("rhythms") or ""}]
            chars = [(s + i, ch) for i, ch in enumerate(body) if ch not in "\n \t"]
            # 选句格：本书 attested 变体优先；其次总字数最接近且不超过曲文长度
            best = None
            for v in variants:
                counts = v.get("counts") or []
                total = sum(counts)
                if total <= 0 or len(chars) < total:
                    continue
                label = str(v.get("label") or "")
                is_book = 0 if ("本書" in label or "本书" in label) else 1
                key = (is_book, abs(len(chars) - total), total)
                if best is None or key < best[0]:
                    best = (key, v)
            if not best:
                # 没有句格时，至少把「集唐」之后当宾白/集句做候选；曲文本身不硬断
                cut = body.find("集唐")
                if cut > 0:
                    marks.extend(_bai_candidates(text, s + cut, e, 0.3))
                continue
            v = best[1]
            counts = list(v.get("counts") or [])
            rhythms = v.get("rhythms") or ""
            total = sum(counts)
            extra = len(chars) - total
            # 曲文后通常紧跟宾白/集唐；只有很小的衬字余量才摊入句格，否则按正好 total 字截断
            max_extra = max(2, int(total * 0.10))
            used = total + extra if extra <= max_extra else total
            used = min(used, len(chars))
            if used < total:
                continue
            # 衬字余量平均摊到各句；只用于给候选断点，不追求语义准确
            alloc = list(counts)
            distribute = used - total
            i = 0
            while distribute > 0:
                alloc[i % len(alloc)] += 1
                i += 1
                distribute -= 1
            grade = curve.get("grade", "C")
            state = "a" if grade == "A" else ("b" if grade == "B" else "cand")
            conf = float(curve.get("conf") or 0.5)
            qid = curve.get("id")
            acc = 0
            for li, cnt in enumerate(alloc):
                acc += cnt
                if li >= len(alloc) - 1:
                    break
                if acc < used:
                    raw_pos = chars[acc - 1][0] + 1
                    if li < len(rhythms) and rhythms[li] == "問":
                        ch = "？"
                    else:
                        ch = "。" if (li < len(rhythms) and rhythms[li] in ("韵", "韻")) else "，"
                    marks.append((raw_pos, ch, state, {"source": "machine", "qid": qid,
                                                       "qupai": last_qp, "conf": conf}))
            # 曲文末尾补句号（只到截断处，避免把后面的宾白一起断掉）
            end_pos = chars[used - 1][0] + 1
            if not any(p == end_pos for p, _, _, _ in marks):
                marks.append((end_pos, "。", state, {"source": "machine", "qid": qid,
                                                     "qupai": last_qp, "conf": conf}))
            # 曲文之后常接宾白/集唐；对剩余部分做低置信候选
            if used < len(chars):
                marks.extend(_bai_candidates(text, chars[used][0], e, 0.4))
        elif typ == "bai":
            marks.extend(_bai_candidates(text, s, e, 0.5))
            end_pos = e
            if end_pos > s and text[end_pos - 1] not in "\n":
                marks.append((end_pos, "。", "cand", {"source": "machine", "conf": 0.45}))
        elif typ == "other":
            # 序跋/目录页通常整页没有曲牌，不做候选；有曲牌或上一页已进入曲文的，才做候选
            if page_has_qp or last_qp:
                marks.extend(_bai_candidates(text, s, e, 0.32))
    # 实验模型候选：默认关闭，只有设置开启时整页补 cand
    if use_model:
        marks.extend(_model_candidates(text))
    # 去重：同一位置只留一个
    seen = set()
    out = []
    for pos, ch, state, attrs in sorted(marks, key=lambda x: x[0]):
        if pos in seen:
            continue
        seen.add(pos)
        out.append((pos, ch, state, attrs))
    return out, last_qp


def apply_insertions(raw, insertions, structures):
    """把标点插进 raw 文本，返回 (text, marks, structures)（都映射到新偏移）。"""
    by_pos = {}
    for pos, ch, state, attrs in insertions:
        by_pos.setdefault(pos, []).append((ch, state, attrs))
    out = []
    ins_before = [0] * (len(raw) + 1)
    inserted = 0
    for i in range(len(raw) + 1):
        ins_before[i] = inserted
        for ch, state, attrs in by_pos.get(i, []):
            out.append((ch, state, attrs, len(out)))
            inserted += 1
        if i < len(raw):
            out.append((raw[i], None, None, len(out)))
    text = "".join(x[0] for x in out)
    marks = []
    for ch, state, attrs, final_pos in out:
        if state:
            marks.append({"pos": final_pos, "ch": ch, "state": state, **attrs})
    # 偏移映射
    def start_off(i):
        # 起点也包含边界上插入的标点，保证相邻片段不重叠
        return i + ins_before[i] + len(by_pos.get(i, []))

    def end_off(i):
        return i + ins_before[i] + len(by_pos.get(i, []))
    final_structures = []
    for st in structures:
        s = start_off(st["start"])
        e = end_off(st["end"])
        if e <= s:
            continue
        final_structures.append({
            "start": s, "end": e, "type": st["type"], "conf": st.get("conf", 0.5),
            "attrs": st.get("attrs") or {},
        })
    # 段落：原换行保持为普通换行；不额外造 breaks
    breaks = []
    return text, marks, final_structures, breaks


def generate(bid, scope="book", page=None, act=None, progress=None, cancelled=None, use_model=False):
    """生成初版文档（规则 v1）。scope=book/page/act；use_model 为实验候选层。"""
    book, raw, pages, recognized = book_raw(bid)
    if not raw:
        raise ValueError("还没有已识别/已校对的叶")
    if scope == "page":
        if not page:
            raise ValueError("page 范围缺少 page")
        selected = [p for p in pages if p["no"] == int(page) and p["recognized"]]
    elif scope == "act":
        if not act:
            raise ValueError("act 范围缺少 act")
        selected = [p for p in pages if p["recognized"] and (p["act"] or "未分齣") == act]
    else:
        selected = [p for p in pages if p["recognized"]]
    if not selected:
        raise ValueError("指定范围内没有已识别叶")
    all_structs = []
    all_ins = []
    total = len(selected)
    global_last_qp = None
    for idx, p in enumerate(selected):
        if cancelled and cancelled():
            raise RuntimeError("cancelled")
        sts = detect_structures(p["text"])
        # 跨页「前腔」继承上一页曲牌，再生成句格/候选
        for st in sts:
            if st["type"] == "qupai":
                nm = (st.get("attrs") or {}).get("name") or ""
                if nm.startswith("前腔") and global_last_qp:
                    st["attrs"]["prev"] = True
                    st["attrs"]["name"] = global_last_qp
        ins, page_last_qp = _insertions_for_page(p["text"], sts, global_last_qp, use_model=use_model)
        for pos, ch, state, attrs in ins:
            all_ins.append((pos + p["start"], ch, state, attrs))
        for st in sts:
            st["start"] += p["start"]
            st["end"] += p["start"]
            if st["type"] == "qupai":
                nm = (st.get("attrs") or {}).get("name") or ""
                if not nm.startswith("前腔"):
                    global_last_qp = nm
        all_structs.extend(sts)
        if progress:
            progress(idx + 1, total, p["no"])
    text, marks, structures, breaks = apply_insertions(raw, all_ins, all_structs)
    # 页范围映射到最终文本（插入标点后偏移会变），供前端页图联动
    page_ranges = []
    for p in pages:
        start = p["start"] + sum(1 for pos, _, _, _ in all_ins if pos < p["start"])
        end = p["end"] + sum(1 for pos, _, _, _ in all_ins if pos <= p["end"])
        page_ranges.append({"no": p["no"], "leaf": p["leaf"], "start": start, "end": end,
                            "act": p["act"], "recognized": p["recognized"]})
    orig = strip_marks(raw)
    if strip_marks(text) != orig:
        raise ValueError("生成文本与原字不一致")
    grades = {"A": 0, "B": 0, "C": 0}
    for st in structures:
        if st["type"] == "qupai":
            name = (st.get("attrs") or {}).get("name") or ""
            curve = _load_kb().get(name) or {}
            grades[curve.get("grade", "C")] = grades.get(curve.get("grade", "C"), 0) + 1
    stats = {
        "marks": len(marks), "pending": sum(1 for m in marks if m["state"] in ("a", "b", "cand")),
        "structures": len(structures), "grades": grades,
        "pages": len(selected), "recognized_pages": recognized, "total_pages": len(pages),
    }
    doc = {
        "v": 2, "book": bid, "text": text, "breaks": breaks, "marks": marks,
        "structures": structures, "page_ranges": page_ranges, "base_hash": _base_hash(orig),
        "updated_at": _now_iso(), "saved_chars": len(orig),
        "paragraphs": 1, "recognized_pages": recognized, "total_pages": len(pages),
        "meta": {
            "engine": "rules-v1", "scope": scope, "page": page, "act": act,
            "model": bool(use_model), "generated_at": _now_iso(), "stats": stats,
        },
    }
    return doc


def validate_doc(bid, text, marks=None, structures=None, breaks=None, existing=None):
    """校验并清洗前端提交的文档；不一致抛 ValueError（调用方转 409）。"""
    book, raw, pages, recognized = book_raw(bid)
    orig = strip_marks(raw)
    if strip_marks(text) != orig:
        raise ValueError("标点稿与当前校字稿不一致")
    clean_marks = []
    if marks is not None:
        seen = set()
        for m in marks:
            try:
                pos = int(m.get("pos"))
                ch = m.get("ch") or (text[pos] if 0 <= pos < len(text) else "")
                state = m.get("state") or "ok"
            except Exception:
                continue
            if state not in STATE_SET or not (0 <= pos < len(text)) or text[pos] != ch:
                continue
            if pos in seen:
                continue
            seen.add(pos)
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
            clean_marks.append(item)
        clean_marks.sort(key=lambda x: x["pos"])
    clean_structs = []
    if structures is not None:
        for st in structures:
            try:
                s = int(st.get("start")); e = int(st.get("end"))
                typ = st.get("type") or "other"
            except Exception:
                continue
            if typ not in TYPE_SET or not (0 <= s < e <= len(text)):
                continue
            clean_structs.append({"start": s, "end": e, "type": typ,
                                  "conf": round(float(st.get("conf", 0.5)), 2),
                                  "attrs": st.get("attrs") or {}})
        clean_structs.sort(key=lambda x: (x["start"], x["end"]))
        # 不允许交叉；重叠的丢弃
        prev = -1
        clean_structs = [st for st in clean_structs if st["start"] >= prev and (prev := st["end"])]
    clean_breaks = []
    if breaks is not None:
        for b in breaks:
            try: b = int(b)
            except Exception: continue
            if 0 <= b < len(text) and text[b] == "\n":
                clean_breaks.append(b)
    clean_breaks = sorted(set(clean_breaks))
    return {"marks": clean_marks, "structures": clean_structs, "breaks": clean_breaks}


def _base_hash(orig):
    import hashlib
    return hashlib.sha1((orig or "").encode("utf-8")).hexdigest()


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def infer_marks(text):
    """给旧文档补标点来源：已有标点视为人工确认。"""
    out = []
    for i, ch in enumerate(text):
        if ch in PUNCT_CHARS and ch not in "\n \t\u3000":
            out.append({"pos": i, "ch": ch, "state": "ok", "source": "human"})
    return out


def recompute_page_ranges(bid, text):
    """按原字顺序把页范围映射到当前 text（标点/换行增删后仍可用）。

    页锚点落在该页第一个原字上；end 用下一页起点，最后一页到文末。
    """
    _book, raw, pages, rec = book_raw(bid)
    rank_prefix = [0] * (len(raw) + 1)
    for i, ch in enumerate(raw):
        rank_prefix[i + 1] = rank_prefix[i] + (0 if ch in PUNCT_CHARS else 1)
    orig_positions = [i for i, ch in enumerate(text) if ch not in PUNCT_CHARS]
    starts = []
    for p in pages:
        rank = rank_prefix[min(p["start"], len(raw))]
        starts.append(orig_positions[rank] if rank < len(orig_positions) else len(text))
    out = []
    for i, p in enumerate(pages):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        if end < starts[i]:
            end = starts[i]
        out.append({"no": p["no"], "leaf": p["leaf"], "start": starts[i], "end": end,
                    "act": p["act"], "recognized": p["recognized"]})
    return out

def format_structured_text(text, structures):
    """导出用：曲牌【】、科介（）/角色提示（）、片段分行。"""
    if not structures:
        return text
    sts = sorted(structures, key=lambda x: (x["start"], x["end"]))
    out = []
    pos = 0
    for st in sts:
        s, e = st["start"], st["end"]
        if s < pos or e <= s:
            continue
        if s > pos:
            out.append(text[pos:s])
        body = text[s:e]
        typ = st["type"]
        if typ == "qupai":
            if body.startswith("【") and body.endswith("】"):
                out.append(body)
            else:
                out.append("【" + body + "】")
        elif typ in ("ke", "role"):
            out.append("（" + body + "）")
        else:
            out.append(body)
        pos = e
    if pos < len(text):
        out.append(text[pos:])
    return "\n\n".join(x for x in out if x.strip())
