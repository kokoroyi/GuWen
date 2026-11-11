# -*- coding: utf-8 -*-
"""新结构识别原型：恢复旧版强结构后处理，作为通用新书的结构候选生成器。

不依赖标点文本；输入为去标点 raw。
"""
import re
import collections

import punctgen
import llm_punct


_MULTI_ROLE_WORDS = ['老旦', '小旦', '貼旦', '贴旦', '小生', '副末', '正末', '副淨', '副净']
_MULTI_ROLE_RE = re.compile('|'.join(sorted(_MULTI_ROLE_WORDS, key=len, reverse=True)))


def _split_multi_roles(raw, structs):
    """把 bai/wen/other 中的多字角色标签单独切出。

    单字角色（生/末/外/内等）歧义大，不在这一层硬切；多字标签误切风险低。
    """
    out = []
    for st in structs:
        typ = st.get('type')
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        body = raw[a:b]
        if typ not in ('bai', 'wen', 'other') or len(body) < 6:
            out.append(dict(st))
            continue
        pos = 0
        for m in _MULTI_ROLE_RE.finditer(body):
            x, y = m.start(), m.end()
            if x > pos:
                out.append({'start': a + pos, 'end': a + x, 'type': typ,
                            'conf': st.get('conf', 0.5), 'attrs': {}})
            out.append({'start': a + x, 'end': a + y, 'type': 'role',
                        'conf': 0.75, 'attrs': {'source': 'rolelabel'}})
            pos = y
        if pos < len(body):
            out.append({'start': a + pos, 'end': b, 'type': typ,
                        'conf': st.get('conf', 0.5), 'attrs': {}})
    return [x for x in out if x['end'] > x['start']]


def raw_to_text_positions(raw, text):
    """完整 raw（可含换行/空白）位置 -> 完整 text 位置。

    raw/text 去掉空白（text 另去标点）后必须逐字一致。
    """
    raw_norm = []
    r2n = [0] * (len(raw) + 1)
    for i, ch in enumerate(raw):
        r2n[i] = len(raw_norm)
        if ch.isspace():
            continue
        raw_norm.append(ch)
    r2n[len(raw)] = len(raw_norm)
    tpos = []
    for i, ch in enumerate(text):
        if ch in punctgen.PUNCT_CHARS or ch.isspace():
            continue
        tpos.append(i)
    if len(raw_norm) != len(tpos) or ''.join(raw_norm) != ''.join(text[i] for i in tpos):
        raise ValueError('raw/text 正文不一致，无法映射')
    return [tpos[r2n[i]] if i < len(r2n) and r2n[i] < len(tpos) else len(text)
            for i in range(len(raw) + 1)]


def map_structures_to_full(scoped_result, scope_raw, scope_offsets):
    """把 scope 内结构坐标映射为完整 raw 坐标。

    scope_offsets: scope_raw 每个字符在 full_raw 中的下标。
    返回 [{'type','start','end','raw_start','raw_end',...}]，start/end 仍是 scope raw 坐标。
    """
    norm_offsets = []
    for i, ch in enumerate(scope_raw):
        if ch.isspace():
            continue
        norm_offsets.append(scope_offsets[i])
    out = []
    for st in scoped_result.get('structures') or []:
        a, b = int(st.get('start', 0)), int(st.get('end', 0))
        if b <= a or a >= len(norm_offsets):
            continue
        b = min(b, len(norm_offsets))
        item = dict(st)
        item['raw_start'] = norm_offsets[a]
        item['raw_end'] = norm_offsets[b] if b < len(norm_offsets) else (scope_offsets[-1] + 1 if scope_offsets else 0)
        out.append(item)
    return out


def _merge_act_names(raw, structs):
    act = re.compile(r'^第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出]$')
    out = []
    i = 0
    while i < len(structs):
        x = dict(structs[i])
        body = raw[x['start']:x['end']]
        if x.get('type') == 'other' and act.match(body.strip()) and i + 1 < len(structs):
            nx = structs[i + 1]
            nb = raw[nx['start']:nx['end']]
            if int(nx['start']) == int(x['end']) and len(nb) <= 4 and nx.get('type') in ('bai', 'wen', 'other'):
                x['end'] = nx['end']
                x['attrs'] = dict(x.get('attrs') or {}, act_name=nb.strip(), kind='act')
                out.append(x)
                i += 2
                continue
        out.append(x)
        i += 1
    return out


def build_structure_doc(raw, rounds=6, multi_roles=True):
    """旧版结构后处理恢复版。返回 {'text': raw, 'structures': [...], 'meta': ...}。"""
    if not raw:
        return {'text': '', 'structures': [], 'meta': {'engine': 'structure-new-v1'}}
    # 去掉空白；外部如果已经 raw 可跳过
    raw = ''.join(ch for ch in raw if not ch.isspace())
    structs = punctgen.detect_structures(raw)
    structs = llm_punct.augment_title_structs(raw, structs)
    structs = llm_punct.normalize_struct_boundaries(raw, structs)
    structs = llm_punct.secondary_split_structures(raw, structs)
    structs = llm_punct.trim_qupai_structures(raw, structs)
    structs = llm_punct.mark_volume_headings(raw, structs)
    audits = []
    for _ in range(rounds):
        structs, aud = llm_punct.audit_structure_purity(raw, structs)
        audits.append(aud)
        prev = len(structs)
        structs = llm_punct.cleanup_mixed_fragments(raw, structs)
        structs = llm_punct.split_impure_structures(raw, structs)
        structs = llm_punct.normalize_struct_boundaries(raw, structs)
        structs = llm_punct.trim_qupai_structures(raw, structs)
        structs = llm_punct.mark_volume_headings(raw, structs)
        structs = llm_punct.merge_adjacent_structures(raw, structs)
        if len(structs) == prev:
            break
    structs = _merge_act_names(raw, structs)
    if multi_roles:
        structs = _split_multi_roles(raw, structs)
        structs = llm_punct.normalize_struct_boundaries(raw, structs)
        structs = llm_punct.merge_adjacent_structures(raw, structs)
    structs = sorted(structs, key=lambda x: int(x['start']))
    structs, aud = llm_punct.audit_structure_purity(raw, structs)
    return {
        'text': raw,
        'structures': structs,
        'meta': {
            'engine': 'structure-new-v1',
            'rounds': len(audits),
            'audit': aud,
            'types': dict(collections.Counter(s.get('type') for s in structs)),
        },
    }


if __name__ == '__main__':
    import json
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else r'D:\GuWen-new\_analysis\versions\structure_v2_20261025-223035.json'
    d = json.load(open(p, encoding='utf-8'))
    r = build_structure_doc(d.get('text') or '')
    print(len(r['structures']), r['meta'])
