# -*- coding: utf-8 -*-
"""LLM 句读引擎：分块调用 OpenAI 兼容接口，保字重建，带置信/校验。"""
import difflib
import json
import os
import re
import sys

APP = os.path.dirname(os.path.abspath(__file__))
if APP not in sys.path:
    sys.path.insert(0, APP)
import punctgen  # noqa: E402
import llm_client  # noqa: E402

PUNCT = set(punctgen.PUNCT_CHARS) if hasattr(punctgen, 'PUNCT_CHARS') else set('。，、；：？！')
SENT_PUNCT = set('，。、；：？！…—')
_ASCII_MAP = {',': '，', '.': '。', ';': '；', ':': '：', '?': '？', '!': '！'}


def sanitize_output(raw, out, with_info=False):
    """只允许新增中文句读标点；引号/括号/其他字符不得凭空出现，保字失败则回退原文。"""
    raw_set = set(raw)
    buf = []
    removed = 0
    for ch in (out or ''):
        if ch in raw_set:
            buf.append(ch)
        elif ch in _ASCII_MAP:
            buf.append(_ASCII_MAP[ch])
        elif ch in SENT_PUNCT:
            buf.append(ch)
        else:
            removed += 1
    cleaned = ''.join(buf)
    try:
        raw_body = ''.join(ch for ch in raw if ch not in punctgen.PUNCT_CHARS and ch not in '\n\t\u3000 ')
        out_body = ''.join(ch for ch in cleaned if ch not in punctgen.PUNCT_CHARS and ch not in '\n\t\u3000 ')
        ok = (raw_body == out_body)
    except Exception:
        ok = False
    if not ok:
        return (raw, removed, False) if with_info else raw
    return (cleaned, removed, True) if with_info else cleaned


SENT_END = set('。？！…')


def normalize_struct_boundaries(text, structs):
    """结构边界对齐到正文：不以标点开头；后置标点归前一段。"""
    out = []
    prev_end = 0
    try:
        for st in structs:
            s = int(st.get('start', 0)); e = int(st.get('end', 0)); typ = st.get('type', 'other')
            if typ not in ('title', 'subtitle'):
                while s < e and s < len(text) and text[s] in punctgen.PUNCT_CHARS and text[s] not in '\n\t ':
                    s += 1
                while e < len(text) and text[e] in punctgen.PUNCT_CHARS and text[e] not in '\n\t ':
                    e += 1
            if s < prev_end:
                s = prev_end
            if e > s:
                item = dict(st); item['start'] = s; item['end'] = e
                out.append(item)
                prev_end = e
    except Exception:
        return structs
    return out


def segment_structs(raw, structs, target=80):
    """把过长的 other/bai/wen 按句末切成段落级结构，避免整页一大块。"""
    out = []
    try:
        for st in structs:
            s = int(st.get('start', 0)); e = int(st.get('end', 0)); typ = st.get('type', 'other')
            # 边界修正：不要以标点开头；把后置标点并入当前段
            while s < e and s < len(raw) and raw[s] in punctgen.PUNCT_CHARS and raw[s] not in '\n\t ':
                s += 1
            while e < len(raw) and raw[e] in punctgen.PUNCT_CHARS and raw[e] not in '\n\t ':
                e += 1
            if e <= s:
                continue
            conf = float(st.get('conf', 0.5) or 0.5)
            if typ in ('title', 'subtitle') or e - s <= target:
                out.append(st)
                continue
            if typ not in ('other', 'bai', 'wen', 'qupai'):
                out.append(st)
                continue
            seg = raw[s:e]
            cuts = [i + 1 for i, ch in enumerate(seg) if ch in SENT_END]
            if not cuts:
                out.append(st)
                continue
            start = 0
            newtype = 'bai' if typ == 'other' else typ
            newconf = round(min(conf, 0.6) if typ == 'other' else conf, 2)
            for cut in cuts:
                if cut - start >= target or cut == cuts[-1]:
                    if cut > start:
                        out.append({'start': s + start, 'end': s + cut, 'type': newtype,
                                    'conf': newconf, 'attrs': dict(st.get('attrs') or {})})
                    start = cut
            if start < len(seg):
                out.append({'start': s + start, 'end': e, 'type': newtype,
                            'conf': newconf, 'attrs': dict(st.get('attrs') or {})})
    except Exception:
        return structs
    return out


def augment_title_structs(raw, structs):
    """给开头的书名/副题切出 title/subtitle 结构，供前端标题排版。"""
    try:
        if not raw or not structs:
            return structs
        first = structs[0]
        if int(first.get('start', 0)) != 0 or first.get('type') not in ('other', None):
            return structs
        head = raw[:160]
        m = re.match(r'^([\u4e00-\u9fff]{4,24}?(?:記|傳|譜|志|序|敘))', head)
        if not m:
            return structs
        title_end = m.end()
        title = m.group(1)
        rest = raw[title_end:title_end + 40]
        sm = re.match(r'^([\u4e00-\u9fff]{4,24}?(?:敘|序|引|題辭))', rest)
        sub_end = title_end + (sm.end() if sm else 0)
        out = [{'start': 0, 'end': title_end, 'type': 'title', 'conf': 0.94,
                'attrs': {'text': title}}]
        if sub_end > title_end:
            out.append({'start': title_end, 'end': sub_end, 'type': 'subtitle', 'conf': 0.9,
                        'attrs': {'text': raw[title_end:sub_end]}})
        if int(first.get('end', 0)) > sub_end:
            out.append({'start': sub_end, 'end': int(first['end']),
                        'type': first.get('type', 'other'), 'conf': first.get('conf', 0.3),
                        'attrs': first.get('attrs') or {}})
        out.extend(structs[1:])
        return out
    except Exception:
        return structs


CANON_SYS = ('你是古籍整理助手。任务：只给原文加句读标点。硬性要求：逐字保留原文；'
             '只输出加好标点的文本；禁止输出任何说明、注释、括号内容、空格分隔或标题。')
CANON_STOP = ['（', '(注', '\n注', '注：']
_FALLBACK = [('fewshot', 0), ('dense', 0), ('system', 0)]
_QP_TERMS = None
_KB = None


def call_chat(chunk, max_tokens, timeout=None, style='dense'):
    if style == 'fewshot':
        messages = [
            {'role': 'system', 'content': CANON_SYS},
            {'role': 'user', 'content': '原文：學而時習之不亦說乎有朋自遠方來不亦樂乎\n標點：學而時習之，不亦說乎？有朋自遠方來，不亦樂乎？\n\n原文：' + chunk + '\n標點：'},
        ]
    elif style in ('bai', 'wen'):
        if style == 'bai':
            sys = ('你是古籍戏曲整理助手。只给下面这段宾白/对白加句读标点；逐字保留原文，'
                   '不得增删改字，不得加引号/括号/书名号/英文标点，只输出加好标点的原文。')
        else:
            sys = ('你是古籍戏曲整理助手。只给下面这段曲文/唱词加句读标点；逐字保留原文，'
                   '不得增删改字，不得加引号/括号/书名号/英文标点，只输出加好标点的原文。')
        messages = [{'role': 'system', 'content': sys},
                    {'role': 'user', 'content': chunk}]
    elif style == 'system':
        messages = [
            {'role': 'system', 'content': '你是古籍整理助手，只输出加好句读标点的原文，不得改动原字，不得解释。'},
            {'role': 'user', 'content': '为下面这段古文加标点，只输出结果：' + chunk},
        ]
    else:
        messages = [
            {'role': 'system', 'content': CANON_SYS + '曲牌名、科介、对白、曲文都要按语义断开；不要漏标；曲牌名内部不得切开。'},
            {'role': 'user', 'content': '为下面这段古文加标点，只输出结果：' + chunk},
        ]
    return llm_client.chat(
        messages,
        max_tokens=max_tokens,
        timeout=timeout,
        stop=CANON_STOP,
    ), {}


def parse_punct(out):
    chars, marks = [], []
    for ch in out:
        if ch in PUNCT:
            marks.append((len(chars), ch))
        elif not ch.strip():
            continue
        else:
            chars.append(ch)
    return ''.join(chars), marks


def repair_chunk(chunk, out):
    orig, marks = parse_punct(out)
    sm = difflib.SequenceMatcher(a=orig, b=chunk, autojunk=False)
    m = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i1, i2 + 1):
                m[k] = j1 + (k - i1)
        elif tag in ('insert', 'replace'):
            span = max(1, i2 - i1)
            for k in range(i1, i2 + 1):
                m[k] = int(round(j1 + (k - i1) / span * (j2 - j1)))
        else:
            m[i1] = j1
            m[i2] = j2
    for k in range(len(orig) + 1):
        m.setdefault(k, len(chunk))
    edits = sum((i2 - i1) + (j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes()
                if tag in ('replace', 'delete'))
    by = {}
    for k, ch in marks:
        pos = max(0, min(len(chunk), m.get(k, len(chunk))))
        by.setdefault(pos, set()).add(ch)
    text = ''
    for i in range(len(chunk) + 1):
        for ch in sorted(by.get(i, [])):
            text += ch
        if i < len(chunk):
            text += chunk[i]
    return text, edits, (edits == 0)


def looks_bad(out, piece):
    marks = parse_punct(out)[1]
    need = 3 if len(piece) >= 100 else 1
    if len(marks) < need:
        return True
    if out.count(' ') > 5:
        return True
    for bad in ['（', '(注', '註', '注：', '</think>', '[注', '標點：', '标点：']:
        if bad in out:
            return True
    return False


def density_score(text):
    qp = ['集唐', '梁州令', '普天樂', '普天乐', '北朝天子', '太平歌', '神仙浪', '桂枝香', '繞地遊',
          '绕地游', '六么令', '一江風', '一江风', '二犯江兒水', '二犯江儿水', '普賢歌', '普贤歌',
          '鎖南枝', '锁南枝', '山坡羊', '寄生草', '潔美酒', '洁美酒', '醉太平', '清江引', '尾聲',
          '尾声', '前腔', '梁州序', '畫眉序', '画眉序', '滴溜子', '鮑老催', '鲍老催', '雙聲子',
          '双声子', '錦衣香', '锦衣香', '漿水令', '浆水令', '收江南', '園林好', '园林好', '沽美酒',
          '太平令', '出隊子', '出队子', '神仗兒', '神仗儿', '啄木兒', '啄木儿', '三段子', '歸朝歡',
          '归朝欢', '水仙子', '村裏迓鼓', '村里迓鼓', '上馬嬌', '上马娇', '勝葫蘆', '胜葫芦',
          '天下樂', '天下乐', '哪吒令', '鵲踏枝', '鹊踏枝', '么篇', '煞尾', '賺煞', '赚煞']
    jie = text.count('介')
    q = sum(text.count(t) for t in qp)
    return (jie * 2 + q * 3) / max(1, len(text)) * 100


def _qp_terms():
    global _QP_TERMS
    if _QP_TERMS is None:
        try:
            punctgen._load_qupai()
            _QP_TERMS = list(punctgen._QP_NAMES or [])
        except Exception:
            _QP_TERMS = []
    return _QP_TERMS


def adaptive_pieces(text, thr=6.0, normal=280, dense=120):
    terms = _qp_terms()
    pieces, i, n = [], 0, len(text)
    while i < n:
        win = text[i:i + normal]
        sc = density_score(win)
        size = dense if sc >= thr else normal
        cut = min(n, i + size)
        if cut < n:
            for _ in range(12):
                moved = False
                for term in terms:
                    L = len(term)
                    if L < 2 or L > 8:
                        continue
                    for k in range(1, L):
                        if (cut - k >= i and text[cut - k:cut] == term[:k]
                                and text[cut:cut + L - k] == term[k:]):
                            cut += L - k
                            moved = True
                            break
                    if moved:
                        break
                if not moved:
                    break
            cut = min(cut, n)
        pieces.append((text[i:cut], 'dense' if sc >= thr else 'fewshot'))
        i = cut
    if len(pieces) > 1 and len(pieces[-1][0]) < 120:
        prev, ps = pieces[-2]
        pieces[-2] = (prev + pieces[-1][0], ps)
        pieces.pop()
    return pieces


def protect(text):
    try:
        names = _qp_terms()
    except Exception:
        names = []
    inner = '[' + re.escape(''.join(PUNCT)) + ']+'
    for term in sorted({t for t in names if 2 <= len(t) <= 8}, key=len, reverse=True):
        pat = inner.join(re.escape(c) for c in term)
        text = re.sub(pat, term, text)
    text = re.sub(r'([\u4e00-\u9fff]{1,4})[' + re.escape(''.join(PUNCT)) + r']+介', r'\1介', text)
    return text


def load_kb():
    global _KB
    if _KB is None:
        for name in ('kb_expanded.json', 'kb.json'):
            p = os.path.join(APP, 'data', 'qupai', name)
            if not os.path.exists(p):
                continue
            try:
                d = json.load(open(p, encoding='utf-8'))
                entries = d.get('entries') or d.get('curves') or {}
                _KB = {str(k): v for k, v in entries.items()}
                break
            except Exception:
                continue
        _KB = _KB or {}
    return _KB


def _norm(name):
    try:
        from opencc import OpenCC
        return OpenCC('t2s').convert(name or '').strip()
    except Exception:
        return (name or '').strip()


def _raw_positions(raw, text):
    """raw 下标 -> 标点文本下标。"""
    pos = [0] * (len(raw) + 1)
    ri = 0
    for i, ch in enumerate(text):
        if ch in PUNCT:
            continue
        if ri < len(pos):
            pos[ri] = i
        ri += 1
    if len(pos):
        pos[-1] = len(text)
    return pos


def _struct_cues(text, seg):
    try:
        punctgen._load_qupai()
        names = list(punctgen._QP_NAMES or [])
    except Exception:
        names = []
    has_qp = any(n and n in seg for n in names) if names else False
    try:
        has_role = bool(punctgen._ROLE_RE.search(seg))
    except Exception:
        has_role = bool(re.search(r'(^|[，。？！])(生|旦|末|外|淨|净|丑|小生|老旦|貼旦|貼|副末|副淨|眾|众)', seg))
    has_ke = bool(re.search(r'(作|打|跪|拜|哭|笑|喚|叫|上|下|走|唱|白|看|問|问|介|科)', seg))
    return has_qp, has_role, has_ke


def classify_long_segments_llm_v2(text, structs, min_len=40, batch=12):
    """混合 v2：长段分类，但要求文本有强线索；避免把混合长段整段改错。"""
    candidates = []
    LAB = {'bai': '宾白', 'wen': '曲文', 'other': '其他'}
    for i, st in enumerate(structs):
        typ = st.get('type')
        if typ not in ('bai', 'wen', 'other'):
            continue
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        if b - a < min_len:
            continue
        seg = re.sub(r'\s+', '', text[a:b])
        if len(seg) < min_len:
            continue
        candidates.append((i, typ, a, b, seg[:140]))
    changed = []
    for k in range(0, len(candidates), batch):
        part = candidates[k:k + batch]
        lines = [f'{j + 1}. {c[4]}' for j, c in enumerate(part)]
        prompt = ('结构类型只能从以下选：曲文、宾白、科介、角色、其他。\n'
                  '请逐条判断下列片段的结构类型，只输出“序号 类型”，每行一条，不要解释：\n'
                  + '\n'.join(lines))
        try:
            out = _struct_chat(prompt, max_tokens=300)
        except Exception:
            continue
        pred = {}
        for line in out.splitlines():
            m = re.match(r'\s*(\d+)\s*[\.、:：]?\s*(曲文|宾白|科介|角色|其他)', line)
            if m:
                pred[int(m.group(1))] = m.group(2)
        for j, (idx, base, a, b, seg) in enumerate(part, 1):
            label = pred.get(j)
            if not label:
                continue
            base_cn = LAB.get(base, '其他')
            if label == base_cn or label == '其他':
                continue
            has_qp, has_role, has_ke = _struct_cues(text, seg)
            prev = structs[idx - 1].get('type') if idx > 0 else ''
            ok = False
            if base == 'bai' and label == '曲文':
                ok = bool(has_qp or prev == 'qupai') and not has_role
            elif base == 'bai' and label == '科介':
                ok = ('介' in seg or '科' in seg) and has_ke
            elif base == 'wen' and label == '宾白':
                ok = has_role and not has_qp
            elif base == 'wen' and label == '科介':
                ok = ('介' in seg or '科' in seg) and has_ke
            if not ok:
                continue
            newtyp = {'曲文': 'wen', '宾白': 'bai', '科介': 'ke', '角色': 'role'}.get(label)
            if not newtyp:
                continue
            structs[idx]['type'] = newtyp
            structs[idx]['conf'] = min(float(structs[idx].get('conf', 0.5) or 0.5), 0.62)
            structs[idx].setdefault('attrs', {})['llm_type'] = label
            changed.append((idx, base_cn, label, seg[:50]))
    return structs, changed


SPLIT_QUPAI = ['北收江南', '收江南', '西江月', '僥僥令', '園林好', '一江風', '六么令', '步步嬌',
               '山坡羊', '尾聲', '前腔', '梁州序', '畫眉序', '江兒水', '滴溜子', '出隊子', '啄木兒',
               '普賢歌', '鎖南枝', '集唐', '太平令', '普天樂', '梁州令', '醉太平', '清江引']
ROLE_LABEL = r'(?:老旦|小旦|貼旦|贴旦|小生|副末|正末|副淨|副净|淨|净|末|生|旦|丑|外|雜|杂|眾|众|內|内)'


def secondary_split_structures(text, structs):
    """二次切分：把含曲牌/角色强边界的混合长段拆开，边界仍按原文位置。"""
    out = []
    for st in structs:
        typ = st.get('type', 'other')
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        if typ not in ('bai', 'wen', 'other') or b - a < 45:
            out.append(st); continue
        seg = text[a:b]
        tokens = []
        for term in SPLIT_QUPAI:
            start = 0
            while True:
                k = seg.find(term, start)
                if k < 0: break
                tokens.append(('qupai', k, k + len(term)))
                start = k + len(term)
        for m in re.finditer(r'[，。？！；：]\s*(' + ROLE_LABEL + r')', seg):
            rs = m.start(1)
            tokens.append(('role', rs, m.end(1)))
        tokens.sort(key=lambda x: (x[1], -x[2]))
        chosen = []
        for t in tokens:
            if any(not (t[2] <= c[1] or t[1] >= c[2]) for c in chosen):
                continue
            chosen.append(t)
        if not chosen:
            out.append(st); continue
        pos = 0
        gap_type = typ
        for kind, s0, e0 in chosen:
            if s0 > pos:
                gap = seg[pos:s0]
                # 曲牌后的文字优先按曲文；角色标签单独切出
                gt = 'wen' if gap_type == 'wen' else typ
                m = re.match(r'\s*(' + ROLE_LABEL + r')', gap)
                if m and m.end(1) < len(gap.rstrip()):
                    rs = s0 - len(gap) + m.start(1); re_ = s0 - len(gap) + m.end(1)
                    if rs > a + pos:
                        out.append({'start': a + pos, 'end': a + rs, 'type': typ, 'conf': st.get('conf', 0.5), 'attrs': {}})
                    out.append({'start': a + rs, 'end': a + re_, 'type': 'role', 'conf': 0.65, 'attrs': {}})
                    if re_ < s0:
                        out.append({'start': a + re_, 'end': a + s0, 'type': 'wen', 'conf': 0.6, 'attrs': {}})
                else:
                    out.append({'start': a + pos, 'end': a + s0, 'type': gt, 'conf': st.get('conf', 0.5), 'attrs': {}})
            out.append({'start': a + s0, 'end': a + e0, 'type': kind, 'conf': 0.75, 'attrs': {'source': 'secondary'}})
            pos = e0
            gap_type = 'wen' if kind == 'qupai' else typ
        if pos < len(seg):
            out.append({'start': a + pos, 'end': b, 'type': gap_type, 'conf': st.get('conf', 0.5), 'attrs': {}})
    return [x for x in out if x['end'] > x['start']]


STRUCT_CHOICES = ['曲文', '宾白', '科介', '角色', '其他']


def _struct_chat(prompt, max_tokens=500):
    return llm_client.chat(
        [{'role': 'system', 'content': '你是古籍戏曲结构标注助手，只输出编号和类型。'},
         {'role': 'user', 'content': prompt}],
        max_tokens=max_tokens,
        timeout=300,
        stop=['\n\n'],
    )


MERGE_TYPES = ('bai', 'wen', 'ke', 'other')


def merge_adjacent_structures(text, structs):
    """相邻且中间只有标点/空白的同类型段自动合并（角色/曲牌/标题不合并）。"""
    out = []
    for st in sorted(structs, key=lambda x: int(x.get('start', 0))):
        typ = st.get('type', 'other')
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        if b <= a:
            continue
        if out:
            last = out[-1]
            if (last.get('type') == typ and typ in MERGE_TYPES
                    and a >= int(last.get('end', 0))
                    and a - int(last.get('end', 0)) <= 6):
                gap = text[int(last.get('end', 0)):a]
                if all((ch in punctgen.PUNCT_CHARS or ch.isspace()) for ch in gap):
                    last['end'] = b
                    continue
        out.append(dict(st))
    return out


def audit_structure_purity(text, structs):
    """结构纯度审计：宾白/曲文块内部若出现其他结构强线索，标记 mixed。"""
    try:
        punctgen._load_qupai()
        qp_names = list(punctgen._QP_NAMES or [])
    except Exception:
        qp_names = []
    role_in = re.compile(r'(?:^|[，。？！；：、])\s*(?:老旦|小旦|貼旦|贴旦|小生|副末|正末|副淨|副净|淨|净|末|生|旦|丑|外|雜|杂|眾|众|內|内)')
    pure = mixed = 0
    for st in structs:
        typ = st.get('type', 'other')
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        body = text[a:b]
        reasons = []
        if typ in ('bai', 'wen', 'ke', 'other'):
            if any(n and n in body for n in qp_names):
                reasons.append('qupai')
            if re.search(r'(作[^\n]{0,12}?介|科介|[上下哭笑怒坐行拜跪打殺见見背扶][科介])', body):
                reasons.append('ke')
            if role_in.search(body):
                reasons.append('role')
            if re.search(r'第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出]', body):
                reasons.append('act')
        if typ in ('title', 'subtitle', 'qupai', 'role', 'ke'):
            reasons = []
        if typ == 'other' and re.fullmatch(r'第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出]', body or ''):
            reasons = []
        st.setdefault('attrs', {})['purity'] = 'mixed' if reasons else 'pure'
        if reasons:
            st['attrs']['mixed_reasons'] = reasons
            mixed += 1
        else:
            pure += 1
    return structs, {'pure': pure, 'mixed': mixed}


def cleanup_mixed_fragments(text, structs):
    """收尾：把整块就是角色名/齣名的 mixed 片段转正或切开。"""
    out = []
    act_re = re.compile(r'^(第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出])\s*')
    for st in sorted(structs, key=lambda x: int(x.get('start', 0))):
        typ = st.get('type', 'other')
        purity = (st.get('attrs') or {}).get('purity')
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        body = text[a:b]
        if purity == 'mixed' and typ in ('bai', 'wen', 'other'):
            core = re.sub(r'[，。、；：？！…—\s]+', '', body)
            if core and re.fullmatch(ROLE_LABEL, core):
                out.append({'start': a, 'end': b, 'type': 'role', 'conf': 0.7,
                            'attrs': {}})
                continue
            if core and re.fullmatch(r'(?:作[^，。？！；：]{0,12}?介|(?:上|下|哭|笑|怒|坐|行|拜|跪|打|殺|见|見|背|扶)(?:科|介)|科介)', core):
                out.append({'start': a, 'end': b, 'type': 'ke', 'conf': 0.7,
                            'attrs': {}})
                continue
            m = re.match(r'^(' + ROLE_LABEL + r')(?=[^，。、；：？！\s])', body)
            if m and m.end() < len(body):
                out.append({'start': a, 'end': a + m.end(), 'type': 'role',
                            'conf': 0.7, 'attrs': {}})
                out.append({'start': a + m.end(), 'end': b, 'type': typ,
                            'conf': st.get('conf', 0.5), 'attrs': {}})
                continue
            m = act_re.match(body)
            if m and m.end() < len(body):
                out.append({'start': a, 'end': a + m.end(), 'type': 'other',
                            'conf': 0.8, 'attrs': {'kind': 'act'}})
                out.append({'start': a + m.end(), 'end': b, 'type': typ,
                            'conf': st.get('conf', 0.5), 'attrs': {}})
                continue
        out.append(dict(st))
    return out


def split_impure_structures(text, structs):
    """把审计为 mixed 的宾白/曲文块按内部角色/科介/曲牌强线索再切一次。"""
    out = []
    try:
        punctgen._load_qupai()
        qp_names = list(punctgen._QP_NAMES or [])
    except Exception:
        qp_names = []
    qp_names = sorted(set(list(qp_names) + list(SPLIT_QUPAI)), key=len, reverse=True)
    role_re = re.compile(r'(?:^|[，。？！；：、])\s*((?:老旦|小旦|貼旦|贴旦|小生|副末|正末|副淨|副净|淨|净|末|生|旦|丑|外|雜|杂|眾|众|內|内))')
    ke_re = re.compile(r'(?:作[^。！？]{0,18}?介|(?:上船|打更|送|謝|谢|扯|扶|抬|擡|見|见|沉吟|照見|限|哭訴|哭|唱|喝|跪|拜|驚|惊|怒|笑|上|下)[^。！？]{0,12}?介|科介)')
    act_re = re.compile(r'第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出]')
    for st in sorted(structs, key=lambda x: int(x.get('start', 0))):
        typ = st.get('type', 'other')
        purity = (st.get('attrs') or {}).get('purity')
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        if purity != 'mixed' or typ not in ('bai', 'wen', 'other') or b - a < 8:
            out.append(dict(st)); continue
        seg = text[a:b]
        toks = []
        for name in qp_names:
            start = 0
            while True:
                k = seg.find(name, start)
                if k < 0: break
                toks.append(('qupai', k, k + len(name))); start = k + len(name)
        for m in ke_re.finditer(seg):
            toks.append(('ke', m.start(), m.end()))
        for m in act_re.finditer(seg):
            toks.append(('other', m.start(), m.end()))
        for m in role_re.finditer(seg):
            toks.append(('role', m.start(1), m.end(1)))
        toks.sort(key=lambda x: (x[1], -x[2]))
        chosen = []
        for t in toks:
            if any(not (t[2] <= c[1] or t[1] >= c[2]) for c in chosen):
                continue
            chosen.append(t)
        if not chosen:
            out.append(dict(st)); continue
        pos = 0
        gap_type = typ
        for kind, s0, e0 in chosen:
            if s0 > pos:
                out.append({'start': a + pos, 'end': a + s0, 'type': gap_type,
                            'conf': st.get('conf', 0.5), 'attrs': {}})
            out.append({'start': a + s0, 'end': a + e0, 'type': kind,
                        'conf': 0.75, 'attrs': {'source': 'splitmixed'}})
            pos = e0
            gap_type = 'wen' if kind == 'qupai' else typ
        if pos < len(seg):
            out.append({'start': a + pos, 'end': b, 'type': gap_type,
                        'conf': st.get('conf', 0.5), 'attrs': {}})
    return [x for x in out if x['end'] > x['start']]


def punctuate_by_structures(raw, structs, types=('bai', 'wen'), pure_only=False):
    """按结构块分别标点：曲牌/角色/科介保持原样，宾白与曲文各自用类型 prompt。"""
    parts = []
    out_structs = []
    marks = []
    pos = 0
    stats = {'blocks': 0, 'calls': 0, 'fallback': 0}
    for st in sorted(structs, key=lambda x: int(x.get('start', 0))):
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        if a < pos:
            a = pos
        if b <= a or a >= len(raw):
            continue
        if a > pos:
            gap = raw[pos:a]
            parts.append(gap)
            pos = a
        seg = raw[a:b]
        typ = st.get('type', 'other')
        fixed = seg
        purity = (st.get('attrs') or {}).get('purity')
        if pure_only and purity == 'mixed':
            fixed = seg
        elif typ in types and len(seg) >= 4:
            stats['blocks'] += 1
            try:
                stats['calls'] += 1
                out, _ = call_chat(seg, len(seg) + 160, style=typ)
                cand, edits, keep = repair_chunk(seg, out)
                cand = sanitize_output(seg, cand)
                # 标点只能落在块内部：块首/块尾新加的标点一律去掉
                edge = '，。、；：？！…—'
                cand = cand.lstrip(edge).rstrip(edge)
                if ''.join(ch for ch in cand if ch not in punctgen.PUNCT_CHARS) == seg:
                    fixed = cand
                else:
                    stats['fallback'] += 1
            except Exception:
                stats['fallback'] += 1
        start = sum(len(x) for x in parts)
        parts.append(fixed)
        end = start + len(fixed)
        out_structs.append(dict(st, start=start, end=end))
        pos = b
    if pos < len(raw):
        parts.append(raw[pos:])
    text = ''.join(parts)
    for i, ch in enumerate(text):
        if ch in punctgen.PUNCT_CHARS and ch not in '\n\t ':
            marks.append({'pos': i, 'ch': ch, 'state': 'cand', 'source': 'machine', 'conf': 0.8})
    return text, out_structs, marks, stats


def trim_qupai_structures(text, structs):
    """曲牌块只保留牌名；牌名后粘连的正文拆成 wen，避免【集唐】吞曲文。"""
    try:
        punctgen._load_qupai()
        names = list(punctgen._QP_NAMES or []) + list(SPLIT_QUPAI)
    except Exception:
        names = list(SPLIT_QUPAI)
    names = sorted(set(x for x in names if x), key=len, reverse=True)
    out = []
    for st in sorted(structs, key=lambda x: int(x.get('start', 0))):
        if st.get('type') != 'qupai':
            out.append(dict(st)); continue
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        body = text[a:b]
        chars = list(body)
        stripped = [ch for ch in chars if ch not in punctgen.PUNCT_CHARS and not ch.isspace()]
        name = None
        for cand in names:
            if ''.join(stripped).startswith(cand):
                name = cand; break
        if not name:
            out.append(dict(st)); continue
        # 找到牌名最后一个字在原文中的位置
        need = len(name)
        idx = -1
        for i, ch in enumerate(chars):
            if ch in punctgen.PUNCT_CHARS or ch.isspace():
                continue
            need -= 1
            if need == 0:
                idx = i; break
        if idx < 0:
            out.append(dict(st)); continue
        end_name = a + idx + 1
        out.append(dict(st, end=end_name, attrs=dict(st.get('attrs') or {}, name=name)))
        if end_name < b:
            out.append({'start': end_name, 'end': b, 'type': 'wen',
                        'conf': min(float(st.get('conf', 0.5) or 0.5), 0.6), 'attrs': {}})
    return out


def mark_volume_headings(text, structs):
    """把书名 + 卷上/卷下 识别为独立「卷名」结构，供点校本标题使用。"""
    titles = []
    for st in structs:
        if st.get('type') == 'title':
            titles.append(text[int(st['start']):int(st['end'])].strip())
    spans = []
    for title in titles:
        if not title:
            continue
        pat = re.compile(re.escape(title) + r'卷[上中下](?:終)?')
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))
    if not spans:
        return structs
    spans = sorted(set(spans))
    out = [dict(x) for x in structs]
    for hs, he in spans:
        pieces = []
        for x in out:
            a, b = int(x['start']), int(x['end'])
            if b <= hs or a >= he:
                pieces.append(x); continue
            if a < hs:
                pieces.append(dict(x, end=hs))
            if b > he:
                pieces.append(dict(x, start=he))
        pieces.append({'start': hs, 'end': he, 'type': 'other', 'conf': 0.9,
                       'attrs': {'kind': 'volume'}})
        out = sorted(pieces, key=lambda x: int(x['start']))
    return out


def punctuate_range(raw, typ):
    """人工选区 AI 标点：只处理一段纯宾白/曲文，块首尾不加标点。"""
    if typ not in ('bai', 'wen') or not raw:
        return raw, {'fallback': 0}
    try:
        out, _ = call_chat(raw, len(raw) + 200, style=typ)
        fixed, edits, keep = repair_chunk(raw, out)
        fixed = sanitize_output(raw, fixed)
        # 只去掉块首多余前导标点；块尾必须保留句末标点
        fixed = fixed.lstrip('，。、；：？！…—')
        if ''.join(ch for ch in fixed if ch not in punctgen.PUNCT_CHARS) != raw:
            return raw, {'fallback': 1}
        return fixed, {'fallback': 0 if keep else 1, 'edits': edits}
    except llm_client.LLMError:
        # Endpoint/auth/network errors must reach the job status instead of
        # being reported as a mere "fallback" block.
        raise
    except Exception:
        return raw, {'fallback': 1}


def classify_long_segments_llm(text, structs, min_len=40, batch=12):
    """混合方案：边界保留规则切分，只让 LLM 对长宾白/曲文做类型分类，保守覆盖。"""
    candidates = []
    LAB = {'bai': '宾白', 'wen': '曲文', 'other': '其他'}
    for i, st in enumerate(structs):
        typ = st.get('type')
        if typ not in ('bai', 'wen', 'other'):
            continue
        if typ in ('title', 'subtitle'):
            continue
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        if b - a < min_len:
            continue
        seg = re.sub(r'\s+', '', text[a:b])
        if len(seg) < min_len:
            continue
        candidates.append((i, typ, a, b, seg[:140]))
    changed = []
    for k in range(0, len(candidates), batch):
        part = candidates[k:k + batch]
        lines = [f'{j + 1}. {c[4]}' for j, c in enumerate(part)]
        prompt = ('结构类型只能从以下选：曲文、宾白、科介、角色、其他。\n'
                  '请逐条判断下列片段的结构类型，只输出“序号 类型”，每行一条，不要解释：\n'
                  + '\n'.join(lines))
        try:
            out = _struct_chat(prompt, max_tokens=300)
        except Exception:
            continue
        pred = {}
        for line in out.splitlines():
            m = re.match(r'\s*(\d+)\s*[\.、:：]?\s*(曲文|宾白|科介|角色|其他)', line)
            if m:
                pred[int(m.group(1))] = m.group(2)
        for j, (idx, base, a, b, seg) in enumerate(part, 1):
            label = pred.get(j)
            if not label:
                continue
            base_cn = LAB.get(base, '其他')
            if label == base_cn:
                continue
            # 保守覆盖：不把长段改成“其他”；仅允许宾白->曲文/科介、曲文->科介/宾白
            if label == '其他':
                continue
            if base == 'bai' and label in ('曲文', '科介'):
                pass
            elif base == 'wen' and label in ('科介', '宾白'):
                pass
            elif base == 'other' and label in ('曲文', '宾白'):
                pass
            else:
                continue
            newtyp = {'曲文': 'wen', '宾白': 'bai', '科介': 'ke', '角色': 'role'}.get(label)
            if not newtyp:
                continue
            structs[idx]['type'] = newtyp
            structs[idx]['conf'] = min(float(structs[idx].get('conf', 0.5) or 0.5), 0.62)
            structs[idx].setdefault('attrs', {})['llm_type'] = label
            changed.append((idx, base_cn, label, seg[:50]))
    return structs, changed


def score_doc(raw, text, fallback_chunks=0, edits_total=0):
    """给标点稿打置信：基础值 + 曲牌句格校验；结构在 raw 上检测后映射到标点文本。"""
    kb = load_kb()
    try:
        structs_raw = punctgen.detect_structures(raw)
        structs_raw = augment_title_structs(raw, structs_raw)
    except Exception:
        structs_raw = []
    r2t = _raw_positions(raw, text)
    out = []
    for st in structs_raw:
        s = r2t[min(len(raw), max(0, int(st.get('start', 0))))]
        e = r2t[min(len(raw), max(0, int(st.get('end', 0))))]
        if e <= s:
            continue
        conf = float(st.get('conf', 0.5) or 0.5)
        attrs = dict(st.get('attrs') or {})
        out.append({'start': s, 'end': e, 'type': st.get('type', 'other'),
                    'conf': round(min(0.95, max(0.2, conf)), 2), 'attrs': attrs,
                    'note': ''})
    marks = []
    for i, ch in enumerate(text):
        if ch in PUNCT:
            cfg = 0.85
            if fallback_chunks:
                cfg -= 0.08 * min(3, fallback_chunks)
            if edits_total:
                cfg -= 0.05
            marks.append({'pos': i, 'ch': ch, 'state': 'cand',
                          'conf': round(max(0.3, cfg), 2),
                          'conf_sources': ['model', 'fallback'] if fallback_chunks else ['model']})
    # 曲牌句格校验
    for st in out:
        if st['type'] != 'qupai':
            continue
        name = _norm((st.get('attrs') or {}).get('name') or '')
        ent = kb.get(name)
        if not ent or not ent.get('counts'):
            continue
        expected = len(ent['counts'])
        actual = sum(1 for m in marks if st['start'] <= m['pos'] <= st['end'])
        st['attrs']['meter_expected'] = expected
        st['attrs']['meter_actual'] = actual
        if expected - actual >= 2:
            st['conf'] = 0.35
            st['note'] = f'预期{expected}句/当前{actual}'
            for m in marks:
                if st['start'] <= m['pos'] <= st['end']:
                    m['conf'] = min(m['conf'], 0.4)
                    if 'meter' not in m['conf_sources']:
                        m['conf_sources'].append('meter')
    out = segment_structs(text, out)
    out = normalize_struct_boundaries(text, out)
    out = secondary_split_structures(text, out)
    out = normalize_struct_boundaries(text, out)
    out = trim_qupai_structures(text, out)
    out = mark_volume_headings(text, out)
    out = merge_adjacent_structures(text, out)
    return out, marks

def generate(text, progress=None, cancel=None, resume=None, on_chunk=None):
    pieces = adaptive_pieces(text)
    rebuilt, fallback, edits_total, errors = [], 0, 0, []
    retries = 0
    saved = []
    chunk_info = []
    off = 0
    if resume and resume.get('total') == len(pieces) and isinstance(resume.get('pieces'), list):
        saved = resume['pieces']
        rebuilt = [x.get('text') or '' for x in saved]
        fallback = sum(int(x.get('fallback') or 0) for x in saved)
        edits_total = sum(int(x.get('edits') or 0) for x in saved)
        for i, x in enumerate(saved):
            plen = len(pieces[i][0]) if i < len(pieces) else 0
            chunk_info.append({'start': off, 'end': off + plen,
                               'bad': int(x.get('fallback') or 0), 'edits': int(x.get('edits') or 0)})
            off += plen
        if progress and saved:
            progress(len(saved), len(pieces), len(saved) - 1)
    for idx in range(len(saved), len(pieces)):
        piece, primary = pieces[idx]
        if cancel is not None and cancel():
            raise RuntimeError('cancelled')
        out = None
        used_fallback = False
        for style in (primary, 'dense', 'system'):
            try:
                out, _ = call_chat(piece, len(piece) + 160, style=style)
            except Exception as e:
                errors.append(str(e))
                continue
            if not looks_bad(out, piece):
                break
            used_fallback = True
        forced = out is None
        if out is None:
            out = piece
        fixed, edits, keep = repair_chunk(piece, out)
        clean, removed, body_ok = sanitize_output(piece, fixed, True)
        no_punct = not any(ch in punctgen.PUNCT_CHARS for ch in clean)
        bad = 0
        if forced or no_punct or not body_ok or removed:
            bad = 1
        if used_fallback and retries < 100000:
            retries += 1
        fallback += bad
        edits_total += edits
        rebuilt.append(clean)
        saved.append({'text': clean, 'fallback': bad, 'edits': edits, 'retried': 1 if used_fallback else 0,
                      'removed': removed, 'body_ok': body_ok, 'keep': keep})
        chunk_info.append({'start': off, 'end': off + len(piece), 'bad': bad, 'edits': edits})
        off += len(piece)
        if progress:
            progress(len(saved), len(pieces), idx)
        if on_chunk:
            try:
                on_chunk(idx, saved, len(pieces))
            except Exception:
                pass
    text_out = protect(''.join(rebuilt))
    text_out, _removed, body_ok = sanitize_output(text, text_out, True)
    raw_out, _ = parse_punct(text_out)
    if raw_out != text or not body_ok:
        # 保字/合法性硬校验失败：退回原文，避免污染
        text_out = text
        fallback += 1
    structs, marks = score_doc(text, text_out, 0, 0)
    # 按块置信度：正常块 0.85；有修复 0.68；fallback/非法字符 0.42
    try:
        r2t = _raw_positions(text, text_out)
        for m in marks:
            pos = m.get('pos')
            for ci in chunk_info:
                a = r2t[min(len(text), max(0, ci['start']))]
                b = r2t[min(len(text), max(0, ci['end']))]
                if a <= pos < b:
                    if ci['bad']:
                        m['conf'] = 0.42
                        m['conf_sources'] = ['chunk', 'fallback']
                    elif ci['edits']:
                        m['conf'] = 0.68
                        m['conf_sources'] = ['chunk', 'repair']
                    else:
                        m['conf'] = 0.85
                        m['conf_sources'] = ['chunk']
                    break
        for st in structs:
            for ci in chunk_info:
                if ci['bad'] and ci['start'] <= st.get('start', 0) < ci['end']:
                    st['conf'] = min(float(st.get('conf', 0.5) or 0.5), 0.5)
                    break
    except Exception:
        pass
    bad_chunks = [[int(ci.get('start', 0)), int(ci.get('end', 0))]
                  for ci in chunk_info if int(ci.get('bad') or 0)]
    return {'text': text_out, 'structures': structs, 'marks': marks,
            'stats': {'chunks': len(pieces), 'fallback': fallback, 'edits': edits_total,
                      'chars': len(text), 'marks': len(marks), 'errors': errors[:3],
                      'retries': retries, 'bad_chunks': bad_chunks}}
