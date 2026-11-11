/* 句读真实数据页：对接 /api/books/<id>/punct v2 + 句读整本初版 + settings */
(function () {
  'use strict';
  const ROOT = window.__PUNCT_ROOT__ || null;
  /* Python 端位置按 Unicode 码点，JS 按 UTF-16；含扩展区汉字时必须换算。 */
  function cpUnitMap(text) {
    const arr = []; let u = 0;
    for (const ch of Array.from(text || '')) { arr.push(u); u += ch.length; }
    arr.push(u);
    return arr;
  }
  function toUnits(pos, map) {
    const p = Math.max(0, Math.min(Number(pos) || 0, map.length - 1));
    return map[p];
  }
  function toCp(text, pos) {
    pos = Math.max(0, Number(pos) || 0);
    let cp = 0, u = 0;
    for (const ch of Array.from(text || '')) {
      if (u >= pos) return cp;
      u += ch.length; cp++;
    }
    return cp;
  }
  function docToUnits(doc) {
    const text = (doc && doc.text) || '';
    const map = cpUnitMap(text);
    const cv = (x) => {
      const o = Object.assign({}, x, { start: toUnits(x.start, map), end: toUnits(x.end, map) });
      if (o.attrs && Array.isArray(o.attrs.chen)) {
        o.attrs = Object.assign({}, o.attrs, { chen: o.attrs.chen.map(([a, b]) => [toUnits(a, map), toUnits(b, map)]) });
      }
      return o;
    };
    return {
      text,
      marks: ((doc && doc.marks) || []).map(m => Object.assign({}, m, { pos: toUnits(m.pos, map) })),
      structures: ((doc && doc.structures) || []).map(cv),
      pageRanges: ((doc && doc.page_ranges) || []).map(cv),
      breaks: ((doc && doc.breaks) || []).map(x => toUnits(x, map))
    };
  }
  function payloadToCp() {
    const map = []; let u = 0;
    for (const ch of Array.from(S.text || '')) { map.push(u); u += ch.length; }
    map.push(u);
    const inv = (pos) => {
      const p = Math.max(0, Math.min(Number(pos) || 0, map[map.length - 1] || 0));
      let lo = 0, hi = map.length - 1;
      while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (map[mid] <= p) lo = mid; else hi = mid - 1; }
      return lo;
    };
    return {
      text: S.text,
      marks: (S.marks || []).map(m => Object.assign({}, m, { pos: inv(m.pos) })),
      structures: (S.structures || []).map(st => {
        const o = Object.assign({}, st, { start: inv(st.start), end: inv(st.end) });
        if (o.attrs && Array.isArray(o.attrs.chen)) {
          o.attrs = Object.assign({}, o.attrs, { chen: o.attrs.chen.map(([a, b]) => [inv(a), inv(b)]) });
        }
        return o;
      }),
      breaks: (S.breaks || []).map(inv)
    };
  }

  const $ = id => ROOT ? ROOT.querySelector('#' + id) : document.getElementById(id);
  const q = sel => ROOT ? ROOT.querySelector(sel) : document.querySelector(sel);
  const qa = sel => ROOT ? ROOT.querySelectorAll(sel) : document.querySelectorAll(sel);
  const activeEl = () => ROOT ? ROOT.activeElement : document.activeElement;
  function isActive() {
    return !ROOT || (ROOT.host && getComputedStyle(ROOT.host).display !== 'none');
  }
  const BID = window.__PUNCT_BOOK__ || new URLSearchParams(location.search).get('book');
  const EMBED = new URLSearchParams(location.search).get('embed') === '1';
  if (EMBED) document.body.classList.add('embed');
  const PUNCTS = [...'、。，；：？！…「」『』《》（）——'];
  const TYPE_LABEL = { title: '标题', subtitle: '副题', qupai: '曲牌', wen: '唱词', bai: '念白', ke: '科介', role: '角色', other: '其他' };
  const TYPE_CLASS = { title: 'seg-title', subtitle: 'seg-subtitle', qupai: 'seg-qu', wen: 'seg-wen', bai: 'seg-bai', ke: 'seg-ke', role: 'seg-role', other: 'seg-other' };

  const S = {
    book: null, doc: null, raw: null, text: '', marks: [], structures: [],
    mode: 'review', pointView: false, selected: null, settings: {}, scope: { type: 'book', name: '' },
    currentPage: 1, dirty: false, saving: false, saveTimer: null, pollTimer: null,
    hist: [], redo: [], hasDoc: false, editBase: '', breaks: [], activePunct: '。', pageRanges: [],
    filterSource: 'all', onlyLow: false, rev: 0, aiScope: 'volume', pageZoom: 1, caret: null, activePunct: '。', aiRangeAbort: null, aiRangeCancelled: false, dirty: false, saveQueued: false,
    exportOpts: { simp: true, font: '宋体', size: 12, roleBold: false, indentChars: 0 },
    simpPreview: null,
  };

  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json; charset=utf-8' } }, opts));
    let d = null;
    try { d = await r.json(); } catch (e) {}
    if (!r.ok) {
      const err = new Error((d && d.detail) || ('HTTP ' + r.status));
      err.status = r.status;
      throw err;
    }
    return d;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
  function toast(msg, ms) {
    const t = $('toast'); t.textContent = msg; t.style.display = 'block';
    clearTimeout(t._timer); t._timer = setTimeout(() => t.style.display = 'none', ms || 2400);
  }
  function snapshot() {
    S.hist.push(JSON.stringify({ text: S.text, marks: S.marks, structures: S.structures }));
    if (S.hist.length > 30) S.hist.shift();
    S.redo.length = 0;
  }
  function restore(str) {
    const d = JSON.parse(str); S.text = d.text; S.marks = d.marks; S.structures = d.structures;
    S.editBase = S.text; S.selected = null;
    if (S.mode === 'edit') $('editArea').value = S.text;
    else renderText();
    updateStats(); saveSoon();
  }
  function undo() {
    if (!S.hist.length) return toast('没有可撤销的');
    S.redo.push(JSON.stringify({ text: S.text, marks: S.marks, structures: S.structures }));
    restore(S.hist.pop()); toast('已撤销');
  }
  function redo() {
    if (!S.redo.length) return toast('没有可重做的');
    S.hist.push(JSON.stringify({ text: S.text, marks: S.marks, structures: S.structures }));
    restore(S.redo.pop()); toast('已重做');
  }

  /* ---------- 加载 ---------- */
  async function loadAll() {
    if (!BID) throw new Error('缺少 book 参数（URL 加 ?book=<书id>）');
    S.book = await api('/api/books/' + BID);
    try { S.settings = await api('/api/settings'); } catch (e) { S.settings = {}; }
    try { S.raw = await api('/api/books/' + BID + '/punct/raw'); } catch (e) {}
    try {
      const doc = await api('/api/books/' + BID + '/punct');
      if (doc && doc.exists && doc.text) {
        const conv = docToUnits(doc);
        S.doc = doc; S.text = conv.text; S.marks = conv.marks; S.structures = conv.structures;
        S.breaks = conv.breaks; S.pageRanges = conv.pageRanges; S.hasDoc = true;
        S.rev = Number(doc.rev || 0);
        if (doc.stale) {
        toast('标点稿与当前校字稿不一致（原文有改动），可点“重映射”保留旧标点', 7000);
        const rb = $('btnRemap'); if (rb) rb.style.display = 'inline-flex';
        const sf = $('btnStaleFix'); if (sf) sf.style.display = 'inline-flex';
        const rl = $('btnReloadDoc'); if (rl) rl.style.display = 'none';
      }
      }
    } catch (e) {}
    if (!S.hasDoc) {
      if (!S.raw) throw new Error('读取校字原稿失败');
      const convRaw = docToUnits({ text: S.raw.text, marks: [], structures: [], page_ranges: S.raw.pages || [] });
      S.text = convRaw.text; S.marks = []; S.structures = []; S.pageRanges = convRaw.pageRanges;
      S.doc = null; S.hasDoc = false;
    }
    if (!S.pageRanges.length && S.raw) S.pageRanges = S.raw.pages || [];
    const pages = S.book.pages || [];
    const first = pages.find(p => p.status !== 'raw') || pages[0];
    const _pageQ = Number(new URLSearchParams(location.search).get('page')) || 0;
  S.currentPage = (_pageQ && pages.some(p => p.no === _pageQ)) ? _pageQ : (first ? first.no : 1);
    setShell();
    renderText();
    updateStats();
    setTimeout(() => scrollToPage(S.currentPage), 80);
    $('saveState').textContent = S.hasDoc ? '已保存' : '空白稿';
    const rb2 = $('btnReview'); if (rb2) rb2.style.display = S.hasDoc ? 'inline-flex' : 'none';
  }

  function updateInitialEntry() {
    const btn = $('btnStructPunct');
    if (!btn) return;
    // 全流程只允许一次：已有结构/标点稿或正在运行，就隐藏整本初版入口
    const done = !!(S.hasDoc && ((S.structures || []).length ||
      ((S.doc || {}).meta || {}).initial_done));
    btn.style.display = (done || S.spTimer) ? 'none' : 'inline-flex';
  }

  function setShell() {
    const book = S.book, pages = book.pages || [];
    const vols = (book.volumes || []).join('/');
    if (!ROOT) document.title = '句读 · ' + book.title;
    $('bookTitle').textContent = book.title;
    const curPg = pages.find(p => p.no === S.currentPage);
    const sub = $('workSub');
    if (sub) sub.textContent = (curPg && curPg.vol ? '卷' + curPg.vol + ' · ' : '') + '第 ' + S.currentPage + ' 叶';
    $('railMeta').textContent = (vols ? vols + ' · ' : '') + pages.length + ' 叶';
    $('railTitle').textContent = book.title || '';
    const box = $('railThumbs');
    if (box.dataset.built !== String(book.id)) {
      box.innerHTML = '';
      pages.forEach(p => {
        const d = document.createElement('div');
        d.className = 'th';
        d.dataset.no = p.no;
        d.innerHTML = `<img src="/thumb/${BID}/${p.no}?w=220" loading="lazy"><span class="no">${p.leaf}</span><span class="dot ${p.status === 'corrected' ? 'corrected' : p.status === 'ocr' ? 'ocr' : ''}"></span>`;
        d.onclick = () => { S.currentPage = p.no; setShell(); scrollToPage(p.no); };
        box.appendChild(d);
      });
      box.dataset.built = String(book.id);
    }
    [...box.children].forEach(el => el.classList.toggle('cur', Number(el.dataset.no) === S.currentPage));
    const curEl = box.querySelector('.th[data-no="' + S.currentPage + '"]');
    if (curEl) curEl.scrollIntoView({ block: 'nearest' });
    applyPageView();
    const pg = pages.find(p => p.no === S.currentPage);
    $('leafTitle').textContent = '第 ' + (pg ? pg.leaf : S.currentPage) + ' 葉';
    $('pheadSub').textContent = S.hasDoc
      ? '标点稿 v2 · 点定位 · 空格插入 · 回车分段'
      : '校字稿 · 未初版（首版会按结构重排）';
    const pno = $('tbPageNo'), ptot = $('tbPageTotal');
    if (pno) pno.textContent = S.currentPage;
    if (ptot) ptot.textContent = pages.length;
    renderFooters();
    updateInitialEntry();
  }

  function rawCharCount(t) {
    return (t == null ? S.text : t).replace(/[。，、；：？！…「」『』《》（）——\n]/g, '').length;
  }
  function pageCharCount() {
    const pr = (S.pageRanges || []).find(x => x.no === S.currentPage);
    return pr ? rawCharCount(S.text.slice(pr.start, pr.end)) : rawCharCount();
  }
  function renderFooters() {
    const pages = (S.book && S.book.pages) || [];
    const rec = pages.filter(p => p.status !== 'raw').length;
    const corr = pages.filter(p => p.status === 'corrected').length;
    const c = counts();
    const chars = pageCharCount();
    const rp = $('railProg');
    if (rp) rp.innerHTML = `已识别 <b>${rec}/${pages.length}</b> · 已标点 <b>${S.hasDoc ? pages.length : 0}/${pages.length}</b><div class="bar"><i style="width:${pages.length ? Math.round(rec / pages.length * 100) : 0}%"></i></div>`;
    const pf = $('pfoot');
    if (pf) {
  const pages = (S.book && S.book.pages) || [];
  const curNo = S.currentPage;
  const nextPg = pages.find(p => p.no > curNo && p.vol === (pages.find(q => q.no === curNo) || {}).vol);
  const nextBtn = nextPg ? `<button class="pfnext" id="pfNext" data-no="${nextPg.no}">下一葉 · 第 ${nextPg.no} 葉</button>` : '';
  pf.innerHTML = `<span>本叶 <b>${chars}</b> 字</span><span>已确认 <b>${c.ok || 0}</b></span><div class="bar"><i style="width:${c.pending ? Math.round((c.ok || 0) / Math.max(1, (c.ok || 0) + c.pending) * 100) : 100}%"></i></div><span><b>${c.pending}</b> 待确认</span><span>结构 <b>${S.structures.length}</b> 段</span>${nextBtn}`;
  const nb = $('pfNext');
  if (nb) nb.onclick = () => {
    S.currentPage = Number(nb.dataset.no) || curNo;
    setShell(); scrollToPage(S.currentPage);
  };
}
    const gf = $('gfooter');
    if (gf && S.book) {
      const vols = (S.book.volumes || []).join('/');
      gf.innerHTML = `<span>${esc(S.book.title || '')} · ${esc(vols)} · 第 <b>${S.currentPage}</b> 叶</span><div class="right">本地数据 · app/data</div>`;
    }
  }
  function basePageFit() {
    const st = q('.stage');
    const img = $('pageImg');
    const pg = (S.book.pages || []).find(p => p.no === S.currentPage) || {};
    const pw = Number(pg.w) || (img && img.naturalWidth) || 2336;
    const ph = Number(pg.h) || (img && img.naturalHeight) || 3586;
    const availW = Math.max(120, (st ? st.clientWidth : 800) - 48);
    const availH = Math.max(120, (st ? st.clientHeight : 700) - 48);
    const h = Math.min(availH, availW * ph / pw);
    return { w: h * pw / ph, h };
  }
  function applyPageSizing() {
    const img = $('pageImg');
    const st = q('.stage');
    const pw = q('.pagewrap');
    if (!img || !st || !pw) return;
    if (!isFinite(S.pageZoom) || S.pageZoom <= 0) S.pageZoom = 1;
    const iw = img.naturalWidth || 2336;
    const ih = img.naturalHeight || 3586;
    const availW = Math.max(120, st.clientWidth - 48);
    const availH = Math.max(120, st.clientHeight - 48);
    const h = Math.min(availH, availW * ih / iw);
    const baseW = h * iw / ih;
    pw.style.width = Math.round(baseW * S.pageZoom) + 'px';
    pw.style.height = Math.round(h * S.pageZoom) + 'px';
    const zp = $('zoomPct');
    if (zp) zp.textContent = Math.round(S.pageZoom * 100) + '%';
  }
  function applyPageView() {
    const img = $('pageImg');
    if (img) img.src = '/img/' + BID + '/' + S.currentPage;
    applyPageSizing();
    const im = $('pageImg');
    if (im && !im.complete) im.addEventListener('load', applyPageSizing, { once: true });
  }

  window.addEventListener('resize', applyPageSizing);
  function scrollToPage(no) {
    if (!no) return;
    const box = $('ptext');
    const m = q(`#ptext .pgmark[data-pg="${no}"]`);
    if (!m || !box) return;
    const top = m.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
    box.scrollTop = Math.max(0, top - 12);
  }

  /* ---------- 渲染 ---------- */
  function normalizeStructs(text, structs) {
    const out = [];
    let prevEnd = 0;
    for (const raw of (structs || [])) {
      let s = Number(raw.start) || 0;
      let e = Number(raw.end) || 0;
      const typ = raw.type || 'other';
      if (typ !== 'title' && typ !== 'subtitle') {
        while (s < e && s < text.length && (isPunctChar(text[s]) || text[s] === ' ' || text[s] === '\n')) s++;
        while (e < text.length && (isPunctChar(text[e]) || text[e] === ' ')) e++;
      }
      if (s < prevEnd) s = prevEnd;
      if (e > s) {
        out.push(Object.assign({}, raw, { start: s, end: e }));
        prevEnd = e;
      }
    }
    return out;
  }

  function mergeActHeadings(text, structs) {
    const out = [];
    const isAct = (x) => x.type === 'other' && /^第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出]$/.test((text.slice(x.start, x.end) || '').replace(/[，。、；：？！…—\s]/g, ''));
    for (let i = 0; i < structs.length; i++) {
      const st = structs[i];
      if (isAct(st) && i + 1 < structs.length) {
        const nx = structs[i + 1];
        if (Number(nx.start) === Number(st.end)) {
          const name = text.slice(nx.start, nx.end).replace(/[，。、；：？！…—\s]/g, '');
          if (name && name.length <= 4 && ['wen', 'bai', 'other'].includes(nx.type)) {
            out.push(Object.assign({}, st, { end: nx.end, type: 'other', attrs: Object.assign({}, st.attrs || {}, { act_name: name }) }));
            i++;
            continue;
          }
        }
      }
      out.push(st);
    }
    return out;
  }

  function renderPointText() {
    const text = S.text || '';
    const box = $('ptext');
    if (!box) return;
    const structs = mergeActHeadings(text, normalizeStructs(text, S.structures));
    const escLine = (x) => esc(x || '');
    const cleanLabel = (x) => (x || '').replace(/[，。、；：？！…—「」『』《》（）【】〔〕]/g, '');
    let html = '<div class="pt-viewhint">点校本：完整句读稿，不受普通视图「待确认 / 低置信」筛选影响</div>', pos = 0;
    const newLine = () => { if (html && !html.endsWith('<br>')) html += '<br>'; };
    for (const st of structs) {
      const a = Number(st.start), b = Number(st.end), typ = st.type || 'other';
      if (a > pos) html += `<span class="pt-gap">${escLine(text.slice(pos, a))}</span>`;
      const body = text.slice(a, b);
      if (typ === 'title') html += `<div class="pt-title">${escLine(body)}</div>`;
      else if (typ === 'subtitle') html += `<div class="pt-subtitle">${escLine(body)}</div>`;
      else if (typ === 'qupai') { newLine(); html += `<span class="pt-qp">【${escLine(cleanLabel(body))}】</span>`; }
      else if (typ === 'role') html += `<span class="pt-role">（${escLine(cleanLabel(body))}）</span>`;
      else if (typ === 'ke') html += `<span class="pt-ke">（${escLine(cleanLabel(body))}）</span>`;
      else if (typ === 'bai' || typ === 'wen') {
        // 页面边界把「集唐」曲牌拆成「集……唐」时，点校本还原成【集唐】标记
        const jt = body.match(/^([\s\S]*[。？！])(集)([\s\S]*)(唐)$/);
        if (jt && jt[2] === '集' && jt[4] === '唐') {
          html += `<span class="pt-body">${escLine(jt[1])}</span><span class="pt-qp">【集唐】</span><span class="pt-body">${escLine(jt[3])}</span>`;
        } else {
          html += `<span class="pt-body">${escLine(body)}</span>`;
        }
      }
      else {
        if ((st.attrs || {}).kind === 'volume') {
          html += `<div class="pt-volume">${escLine(cleanLabel(body))}</div>`;
        } else if (/^第[一二三四五六七八九十百零〇壹貳貮叁叄參肆伍陸柒捌玖拾\d]{1,4}[齣出]/.test(body)) {
          html += `<div class="pt-act">${escLine(cleanLabel(body))}</div>`;
        } else html += `<span class="pt-gap">${escLine(body)}</span>`;
      }
      pos = b;
    }
    if (pos < text.length) html += `<span class="pt-gap">${escLine(text.slice(pos))}</span>`;
    box.innerHTML = html;
    box.classList.add('pointed');
  }

  function renderText() {
    if (S.pointView) { renderPointText(); return; }
    $('ptext').classList.remove('pointed');
    const text = S.text || '';
    S.structures = normalizeStructs(text, S.structures);
    const starts = {}, ends = {}, markMap = {};
    S.structures.forEach((st, idx) => {
      (starts[st.start] = starts[st.start] || []).push(idx);
      (ends[st.end] = ends[st.end] || []).push(idx);
    });
    S.marks.forEach(m => { markMap[m.pos] = m; });
    const chenMap = {};
    S.structures.forEach(st => ((st.attrs || {}).chen || []).forEach(([a, b]) => {
      for (let i = Math.max(0, a); i < Math.min(text.length, b); i++) chenMap[i] = true;
    }));
    const pageStarts = {};
    (S.pageRanges || []).forEach(p => { if (!(p.start in pageStarts)) pageStarts[p.start] = p.no; });
    let html = '', open = [];
    let i = 0;
    while (i <= text.length) {
      if (ends[i]) {
        ends[i].slice().reverse().forEach(idx => {
          const pos = open.lastIndexOf(idx);
          if (pos >= 0) { open.splice(pos, 1); html += '</span></span>'; }
        });
      }
      if (pageStarts[i] != null) {
        html += `<span class="pgmark" data-pg="${pageStarts[i]}"></span>`;
      }
      if (i === S.caret) html += '<span class="caretmark"></span>';
      if (starts[i]) {
        starts[i].forEach(idx => {
          const st = S.structures[idx];
          // “其他”（序跋/目录等）不加标签和底色，避免整页黄框
          if (st.type === 'other') return;
          open.push(idx);
          const cls = TYPE_CLASS[st.type] || 'seg-other';
          const low = (st.conf != null && st.conf < 0.7) ? ' seg-low' : '';
          const label = (st.type === 'other' && (st.attrs || {}).kind === 'act') ? '齣名' : ((st.type === 'other' && (st.attrs || {}).kind === 'volume') ? '卷名' : (TYPE_LABEL[st.type] || '其他'));
          const conf = (st.conf != null) ? Math.round(st.conf * 100) + '%' : '';
          const sug = (st.attrs || {}).llm_suggest;
        const sugLabel = sug && sug !== st.type ? (TYPE_LABEL[sug] || sug) : '';
        const audit = (st.attrs || {}).audit;
        const auditCls = audit && !['pure', 'book', 'structural'].includes(audit) ? ' audit-' + audit : '';
        const auditTitle = audit ? ' title="审计：' + audit + '"' : '';
        html += `<span class="seg ${cls}${low}${auditCls}"${auditTitle} data-idx="${idx}"><span class="pgutter"><span class="plabel${sugLabel ? ' has-sug' : ''}" title="${sugLabel ? esc('LLM 建议：' + sugLabel) : ''}">${label}</span>${conf ? `<span class="pconf">${conf}</span>` : ''}</span><span class="sbody">`;
        });
      }
      if (i >= text.length) break;
      // 按 Unicode 码点渲染：避免把扩展区汉字拆成两个 UTF-16 代理半字符
      const cp = text.codePointAt(i);
      const ch = String.fromCodePoint(cp);
      const adv = ch.length;
      const m = markMap[i];
      if (ch === '\n') html += '<br>';
      else if (m) {
        const src = m.source || '';
        const conf = (m.conf != null) ? Number(m.conf) : 1;
        html += `<span class="mp ${m.state}${conf < 0.5 ? ' lowconf' : ''}${chenMap[i] ? ' chen' : ''}" data-pos="${i}" data-source="${esc(src)}" data-conf="${conf}">${esc(ch)}</span>`;
      }
      else html += chenMap[i] ? `<span class="chen">${esc(ch)}</span>` : esc(ch);
      i += adv;
    }
    while (open.length) { html += '</span>'; open.pop(); }
    $('ptext').innerHTML = html;
    applyFilters();
  }

  function counts() {
    const c = { a: 0, b: 0, cand: 0, ok: 0, pending: 0 };
    S.marks.forEach(m => { c[m.state] = (c[m.state] || 0) + 1; });
    c.pending = (c.a || 0) + (c.b || 0) + (c.cand || 0);
    return c;
  }
  function applyFilters() {
    const box = $('ptext');
    box.classList.toggle('src-rule', S.filterSource === 'rule');
    box.classList.toggle('src-model', S.filterSource === 'model');
    box.classList.toggle('only-low', !!S.onlyLow);
    const srcChip = $('filterSource');
    if (srcChip) {
      srcChip.textContent = S.filterSource === 'rule' ? '来源：规则'
        : S.filterSource === 'model' ? '来源：模型' : '来源：全部';
      srcChip.classList.toggle('on', S.filterSource !== 'all');
    }
    const lowChip = $('filterLow');
    if (lowChip) lowChip.classList.toggle('on', !!S.onlyLow);
  }

  function updateStats() {
    const c = counts();
    const modelCount = S.marks.filter(m => m.source === 'machine-model').length;
    $('badgePending').textContent = c.pending;
    const pageChars = pageCharCount();
    $('pstats').innerHTML = S.hasDoc
      ? `<span class="stat warn">待确认 ${c.pending}</span><span class="stat ok">已确认 ${c.ok || 0}</span><span class="stat">结构 ${S.structures.length} 段</span><span class="stat">本叶 ${pageChars} 字</span>`
      : `<span class="stat warn">尚无标点稿</span>`;
    $('legend').innerHTML = `<span><i class="lg-qu"></i>曲牌句格</span><span><i class="lg-wen"></i>唱词</span><span><i class="lg-bai"></i>念白</span><span><i class="lg-ke"></i>科介</span><span><i class="lg-role"></i>角色</span><span><i class="lg-other"></i>其他</span><span style="margin-left:auto">未确认 ${c.pending} · 已确认 ${c.ok || 0} · 结构 ${S.structures.length} 段</span>`;
    const hb = $('hintbar'); if (hb) hb.innerHTML = S.mode === 'review'
      ? `<span class="hl">精校模式</span><span><kbd>空格</kbd>接受当前未确认　<kbd>X</kbd>拒绝　<kbd>Tab</kbd>下一处待确认</span><span class="sep"></span><span><span class="hl">A</span> 曲牌句格　<span class="hl">B</span> 候选/变格　<span class="hl">白</span> 念白候选</span><span class="sep"></span><span>待确认 ${c.pending} · 点结构片段可改类型</span>`
      : `<span class="hl">编辑模式</span><span><kbd>空格</kbd>插入当前句读　<kbd>⏎</kbd>分段　点标点盘换当前标点；只能加标点/换行，不改原字</span>`;
    $('modeReview').classList.toggle('on', S.mode === 'review');
    $('modeEdit').classList.toggle('on', S.mode === 'edit');
    renderFooters();
  }

  /* ---------- 保存 ---------- */
  function setSaveState(text) {
    try { $('saveState').textContent = text; } catch (e) {}
    try { document.dispatchEvent(new CustomEvent('punct-save-state', { detail: { text } })); } catch (e) {}
  }
  function saveSoon() { S.dirty = true; clearTimeout(S.saveTimer); S.saveTimer = setTimeout(() => save(false), 500); }
  async function save(showToast) {
    if (!S.hasDoc && !S.text) return;
    if (S.saving) { S.saveQueued = true; return; }
    S.saving = true;
    S.dirty = false;
    setSaveState('保存中…');
    try {
      const r = await api('/api/books/' + BID + '/punct', {
        method: 'POST',
        body: JSON.stringify(Object.assign(payloadToCp(), { meta: (S.doc && S.doc.meta) || {}, rev: S.rev })),
      });
      S.hasDoc = true;
      if (r.rev != null) S.rev = Number(r.rev);
      if (Array.isArray(r.page_ranges) && r.page_ranges.length) S.pageRanges = docToUnits({ text: S.text, page_ranges: r.page_ranges }).pageRanges;
      if (S.dirty) { S.saveQueued = true; }
      else { setSaveState('已保存'); }
      const sf = $('btnStaleFix'); if (sf) sf.style.display = 'none';
      const rl = $('btnReloadDoc'); if (rl) rl.style.display = 'none';
      if (showToast) toast(`已保存：${r.marks} 标点 · ${r.structures} 段`);
    } catch (e) {
      if ((e.message || '').includes('损坏')) {
        S.hasDoc = false;
        $('ptext').innerHTML = `<div class="banner">标点稿损坏：${esc(e.message)}<br>请从备份恢复或重映射；不要直接保存覆盖。</div>`;
        toast('标点稿损坏，已停止自动保存；请恢复备份', 12000);
        return;
      }
      if (e.status === 409) {
        const isRev = /保存冲突|当前版本\s*rev=/.test(e.message || '');
        if (isRev) {
          S.dirty = true;
          setSaveState('需载入最新');
          const rb = $('btnReloadDoc'); if (rb) rb.style.display = 'inline-flex';
          const sf = $('btnStaleFix'); if (sf) sf.style.display = 'none';
          toast('文档已被其他任务/窗口更新：' + e.message + '。请点“载入最新”。', 9000);
        } else {
          S.dirty = true;
          setSaveState('需重映射');
          const rb = $('btnStaleFix'); if (rb) rb.style.display = 'inline-flex';
          const rl = $('btnReloadDoc'); if (rl) rl.style.display = 'none';
          toast('保存被拒：' + e.message + '。请点工具条“重映射”。', 9000);
        }
      } else {
        S.dirty = true;
        setSaveState('保存失败');
        toast('保存失败：' + e.message, 5000);
      }
    } finally {
      S.saving = false;
      if (S.saveQueued) { S.saveQueued = false; setTimeout(() => save(false), 0); }
    }
  }

  /* ---------- 编辑模式（纯文本，只加标点/换行） ---------- */
  const PUNCT_CHARS = new Set([...PUNCTS, '\n']);
  function isPunctChar(ch) { return PUNCT_CHARS.has(ch); }
  function recalcBreaks() {
    const out = [];
    for (let i = 0; i < S.text.length; i++) if (S.text[i] === '\n') out.push(i);
    S.breaks = out;
  }
  function enterEdit() {
    if (S.mode === 'edit') return;
    S.mode = 'edit'; S.editBase = S.text; S.selected = null; S.caret = null;
    $('ptext').style.display = 'none';
    const ta = $('editArea');
    ta.style.display = 'block'; ta.value = S.text;
    $('editPreview').style.display = 'block';
    renderEditPreview();
    ta.focus();
    try { ta.setSelectionRange(0, 0); ta.scrollTop = 0; } catch (e) {}
    updateStats();
    toast('编辑模式：上方为结构/标点预览，下方可编辑；空格插入当前句读，Enter 分段', 4600);
  }
  function exitEdit() {
    if (S.mode !== 'edit') return;
    S.mode = 'review';
    $('editArea').style.display = 'none';
    $('editPreview').style.display = 'none';
    $('ptext').style.display = '';
    renderText(); updateStats(); saveSoon();
  }
  function renderEditPreview() {
    renderText();
    $('editPreview').innerHTML = $('ptext').innerHTML;
  }
  function revertEdit() {
    const ta = $('editArea');
    const pos = ta.selectionStart || 0;
    ta.value = S.editBase;
    try { const p = Math.min(pos, S.editBase.length); ta.setSelectionRange(p, p); } catch (e) {}
    toast('只能插入标点或换行，不能改原字', 3500);
  }
  function applyEdit(newText) {
    newText = newText.replace(/\r/g, '');
    const old = S.editBase;
    if (newText === old) return true;
    let p = 0;
    while (p < old.length && p < newText.length && old[p] === newText[p]) p++;
    let s = 0;
    while (s < old.length - p && s < newText.length - p && old[old.length - 1 - s] === newText[newText.length - 1 - s]) s++;
    const ins = newText.slice(p, newText.length - s);
    const del = old.slice(p, old.length - s);
    const editable = (ch) => isPunctChar(ch) || ch === ' ' || ch === '\u3000';
    const badIns = [...ins].filter(ch => !editable(ch)).join('');
    const badDel = [...del].filter(ch => !editable(ch)).join('');
    if (badIns || badDel) {
      window.__rangeBad = { badIns, badDel, ins, del };
      return false;
    }
    snapshot();
    const shift = ins.length - del.length;
    // 结构偏移：删除段夹紧，插入/删除后整体平移
    S.structures = S.structures.map(st => {
      let a = st.start, b = st.end;
      if (a >= p + del.length) a += shift;
      else if (a > p) a = p;
      if (b >= p + del.length) b += shift;
      else if (b > p) b = p;
      const o = Object.assign({}, st, { start: a, end: b });
      if (o.attrs && Array.isArray(o.attrs.chen)) {
        const mapP = (x) => {
          if (x <= p) return x;
          if (x >= p + del.length) return x + shift;
          return p;
        };
        o.attrs = Object.assign({}, o.attrs, { chen: o.attrs.chen.map(([x, y]) => [mapP(x), mapP(y)]).filter(([x, y]) => y > x) });
      }
      return o;
    }).filter(st => st.end > st.start);
    // 先删掉被删掉的标点，再平移剩余标点
    let marks = S.marks.filter(m => !(m.pos >= p && m.pos < p + del.length));
    marks = marks.map(m => m.pos >= p + del.length ? Object.assign({}, m, { pos: m.pos + shift }) : m);
    // 新插入的标点视为人工确认
    [...ins].forEach((ch, i) => {
      if (ch !== '\n') marks.push({ pos: p + i, ch: ch, state: 'ok', source: 'human' });
    });
    S.marks = marks.sort((a, b) => a.pos - b.pos);
    S.text = newText; S.editBase = newText; S.hasDoc = true;
    recalcBreaks();
    if (S.mode === 'edit') renderEditPreview();
    updateStats();
    saveSoon();
    return true;
  }
  function markChenSelection() {
    const sel = _textSelection();
    if (!sel || sel.collapsed || sel.end <= sel.start) { toast('先在唱词里选中要标为衬字的文字'); return; }
    let st = null;
    for (const x of S.structures) {
      if (x.type === 'wen' && x.start <= sel.start && x.end >= sel.end) { st = x; break; }
    }
    if (!st) { toast('衬字只能标在唱词块内'); return; }
    snapshot();
    const a = sel.start, b = sel.end;
    const attrs = st.attrs || {};
    let ranges = (attrs.chen || []).map(([x, y]) => [Number(x), Number(y)]);
    const covered = ranges.findIndex(([x, y]) => x <= a && y >= b);
    if (covered >= 0) {
      // 再点一次同一段：取消衬字
      ranges.splice(covered, 1);
    } else {
      // 去掉与选区重叠的部分，再标为新衬字
      ranges = ranges.flatMap(([x, y]) => {
        if (y <= a || x >= b) return [[x, y]];
        const out = [];
        if (x < a) out.push([x, a]);
        if (y > b) out.push([b, y]);
        return out;
      });
      ranges.push([a, b]);
      ranges.sort((p, q) => p[0] - q[0]);
    }
    st.attrs = Object.assign({}, attrs, { chen: ranges });
    S.caret = null;
    renderText(); updateStats(); saveSoon();
    toast(covered >= 0 ? '已取消衬字标记' : '已标为衬字');
  }

  function mapChenBody(attrs, seg, fixed, oldStart, newStart) {
    if (!attrs || !Array.isArray(attrs.chen) || !attrs.chen.length) return attrs;
    const isP = (ch) => isPunctChar(ch) || ch === ' ' || ch === '\u3000';
    const oldRp = [], newRp = [];
    for (let i = 0; i < seg.length; i++) if (!isP(seg[i])) oldRp.push(oldStart + i);
    for (let i = 0; i < fixed.length; i++) if (!isP(fixed[i])) newRp.push(newStart + i);
    if (!oldRp.length || !newRp.length) return Object.assign({}, attrs, { chen: [] });
    const mapped = [];
    for (const [ca, cb] of attrs.chen) {
      const ra = oldRp.filter(x => x < ca).length;
      const rb = oldRp.filter(x => x < cb).length;
      if (ra >= rb) continue;
      const na = ra < newRp.length ? newRp[ra] : newStart + fixed.length;
      const nb = rb > 0 ? newRp[rb - 1] + 1 : newStart + fixed.length;
      if (nb > na) mapped.push([na, nb]);
    }
    return Object.assign({}, attrs, { chen: mapped });
  }

  function applyRangePunctuation(u0, u1, newText) {
    const old = S.text.slice(u0, u1);
    const strip = (x) => [...x].filter(ch => !isPunctChar(ch) && ch !== ' ' && ch !== '\u3000').join('');
    if (strip(old) !== strip(newText)) return false;
    snapshot();
    const delta = newText.length - (u1 - u0);
    S.structures = S.structures.map(st => {
      let a = st.start, b = st.end;
      if (b <= u0) return st;
      if (a >= u1) return Object.assign({}, st, { start: a + delta, end: b + delta });
      if (a <= u0 && b >= u1) {
        const attrs = mapChenBody(st.attrs || {}, old, newText, u0, u0);
        return Object.assign({}, st, { end: b + delta, attrs });
      }
      return Object.assign({}, st, { end: Math.max(u0, b + delta) });
    }).filter(st => st.end > st.start);
    let marks = S.marks.filter(m => !(m.pos >= u0 && m.pos < u1));
    marks = marks.map(m => m.pos >= u1 ? Object.assign({}, m, { pos: m.pos + delta }) : m);
    let off = 0;
    for (const ch of newText) {
      if (isPunctChar(ch) && ch !== '\n') marks.push({ pos: u0 + off, ch: ch, state: 'cand', source: 'machine', conf: 0.8 });
      off += ch.length;
    }
    S.marks = marks.sort((a, b) => a.pos - b.pos);
    S.text = S.text.slice(0, u0) + newText + S.text.slice(u1);
    S.editBase = S.text;
    recalcBreaks();
    updateStats();
    saveSoon();
    return true;
  }
  function insertAtCaret(txt) {
    const ta = $('editArea');
    if (S.mode !== 'edit') return;
    if (S.saving) return;
    const a = ta.selectionStart, b = ta.selectionEnd;
    ta.setRangeText(txt, a, b, 'end');
    if (!applyEdit(ta.value)) revertEdit();
  }

  /* ---------- 正文光标 / 手工补标点 ---------- */
  const PUNCT_KEYS = '，。、；：？！…—「」『』《》（）〈〉【】〔〕,.;:?!';
  const ASCII_PUNCT = { ',': '，', '.': '。', ';': '；', ':': '：', '?': '？', '!': '！' };
  function normalizePunct(ch) { return ASCII_PUNCT[ch] || ch; }
  let _lastPunctKey = '', _lastPunctAt = 0;
  function punctKeyChar(e) {
    if (!e) return '';
    if (e.key && e.key.length === 1 && PUNCT_KEYS.includes(e.key)) return e.key;
    const code = e.charCode || e.which || 0;
    if (code) {
      try {
        const ch = String.fromCharCode(code);
        if (PUNCT_KEYS.includes(ch)) return ch;
      } catch (err) {}
    }
    return '';
  }
  function insertPunctKey(ch) {
    const now = Date.now();
    if (ch === _lastPunctKey && now - _lastPunctAt < 150) return;
    _lastPunctKey = ch; _lastPunctAt = now;
    insertPunctAt(ch);
  }
  function _rangeLen(r) {
    const frag = r.cloneContents();
    let n = 0;
    (function walk(node) {
      for (const c of node.childNodes) {
        if (c.nodeType === 3) n += c.data.length;
        else if (c.nodeType === 1) {
          if (c.classList && c.classList.contains('pgutter')) continue;
          if (c.tagName === 'BR') n += 1;
          else walk(c);
        }
      }
    })(frag);
    return n;
  }
  function _textOffset(node, offset) {
    const box = $('ptext');
    if (!box || !node) return null;
    const r = document.createRange();
    try { r.selectNodeContents(box); r.setEnd(node, offset); } catch (e) { return null; }
    return _rangeLen(r);
  }
  function _textSelection() {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    const r = sel.getRangeAt(0);
    const box = $('ptext');
    if (!box.contains(r.commonAncestorContainer)) return null;
    return { start: _textOffset(r.startContainer, r.startOffset),
             end: _textOffset(r.endContainer, r.endOffset), collapsed: r.collapsed };
  }
  function _redrawAtCaret() {
    const box = $('ptext'); if (!box) return;
    const top = box.scrollTop;
    renderText();
    box.scrollTop = top;
    const cm = q('#ptext .caretmark');
    if (cm) cm.scrollIntoView({ block: 'nearest' });
  }
  function focusSink() {
    const sk = $('punctSink'); if (!sk) return;
    try { sk.focus({ preventScroll: true }); sk.value = ''; } catch (e) {}
  }
  function blurSink() {
    const sk = $('punctSink');
    if (sk && document.activeElement === sk) sk.blur();
  }
  function deletePunctAt(pos) {
    if (pos == null || pos < 0 || pos >= S.text.length) return false;
    if (!isPunctChar(S.text[pos])) return false;
    S.editBase = S.text;
    const ok = applyEdit(S.text.slice(0, pos) + S.text.slice(pos + 1));
    if (!ok) return false;
    S.caret = Math.min(S.text.length, pos);
    S.selected = null;
    renderText(); updateStats(); focusSink();
    return true;
  }
  function deleteSelectedPunct(backspace) {
    if (S.selected != null) { const p = S.selected; selectMark(null); return deletePunctAt(p); }
    const sel = _textSelection();
    if (sel && !sel.collapsed) {
      let n = 0;
      for (let p = Math.min(sel.end, S.text.length) - 1; p >= sel.start; p--) {
        if (deletePunctAt(p)) n++;
      }
      if (n) { S.caret = sel.start; renderText(); updateStats(); focusSink(); return true; }
      return false;
    }
    if (S.caret == null) { toast('先点正文定位，或选中一个标点'); return false; }
    let p = S.caret;
    if (backspace) {
      if (p > 0 && isPunctChar(S.text[p - 1])) p = p - 1;
      else if (isPunctChar(S.text[p])) { /* 删除光标处 */ }
      else { toast('光标前后没有标点'); return false; }
    } else {
      if (!isPunctChar(S.text[p])) { toast('光标后没有标点'); return false; }
    }
    return deletePunctAt(p);
  }
  function insertNewlineAt() {
    if (!S.hasDoc) { toast('还没有标点稿'); return false; }
    let pos = null;
    const sel = _textSelection();
    if (sel && !sel.collapsed) pos = sel.end;
    else if (S.caret != null) pos = S.caret;
    else if (sel && sel.collapsed) pos = sel.end;
    if (pos == null) return false;
    S.editBase = S.text;
    const ok = applyEdit(S.text.slice(0, pos) + '\n' + S.text.slice(pos));
    if (!ok) return false;
    S.caret = Math.min(S.text.length, pos + 1);
    S.selected = null;
    renderText(); updateStats(); focusSink();
    const cm = q('#ptext .caretmark');
    if (cm) cm.scrollIntoView({ block: 'nearest' });
    return true;
  }

  function setCaretFromPoint(x, y) {
    const box = $('ptext');
    const sel = window.getSelection();
    // 真实鼠标点击会先把原生光标放在点击位置：优先用它，比 caretRangeFromPoint 稳定
    if (sel && sel.rangeCount && box) {
      const sr = sel.getRangeAt(0);
      if (box.contains(sr.startContainer)) {
        if (!sr.collapsed) { S.caret = null; blurSink(); return; }
        const so = _textOffset(sr.startContainer, sr.startOffset);
        if (so != null) {
          S.caret = Math.max(0, Math.min(S.text.length, so));
          S.selected = null;
          qa('#ptext .mp.selected').forEach(e => e.classList.remove('selected'));
          _redrawAtCaret();
          focusSink();
          return;
        }
      }
    }
    let r = null;
    if (document.caretRangeFromPoint) r = document.caretRangeFromPoint(x, y);
    if (!r) return;
    const off = _textOffset(r.startContainer, r.startOffset);
    if (off == null) return;
    S.caret = Math.max(0, Math.min(S.text.length, off));
    S.selected = null;
    qa('#ptext .mp.selected').forEach(e => e.classList.remove('selected'));
    _redrawAtCaret();
    focusSink();
  }
  function insertPunctAt(ch) {
    if (!S.hasDoc) { toast('还没有标点稿'); return false; }
    ch = normalizePunct(ch);
    if (!ch) return false;
    const box = $('ptext');
    let pos = null;
    const sel = box ? _textSelection() : null;
    if (sel && !sel.collapsed) pos = sel.end;
    else if (S.caret != null) pos = S.caret;
    else if (sel && sel.collapsed) pos = sel.end;
    if (pos == null) { toast('先点正文定位，再插入标点'); return false; }
    S.editBase = S.text;
    const ok = applyEdit(S.text.slice(0, pos) + ch + S.text.slice(pos));
    if (!ok) { toast('插入失败'); return false; }
    if (!S.marks.some(m => m.pos === pos && m.ch === ch)) {
      S.marks.push({ pos: pos, ch: ch, state: 'ok', source: 'human', conf: 1 });
      S.marks.sort((a, b) => a.pos - b.pos);
      saveSoon();
    }
    S.caret = Math.min(S.text.length, pos + ch.length);
    S.selected = null;
    renderText();
    updateStats();
    focusSink();
    const cm = q('#ptext .caretmark');
    if (cm) cm.scrollIntoView({ block: 'nearest' });
    return true;
  }

  /* ---------- 标点操作 ---------- */
  function selectMark(pos) {
    qa('#ptext .mp.selected').forEach(e => e.classList.remove('selected'));
    S.selected = (pos == null) ? null : pos;
    if (pos != null) {
      const el = q(`#ptext .mp[data-pos="${pos}"]`);
      if (el) { el.classList.add('selected'); el.scrollIntoView({ block: 'nearest' }); }
    }
  }
  function nextPending() {
    const list = S.marks.filter(m => m.state === 'a' || m.state === 'b' || m.state === 'cand').map(m => m.pos);
    if (!list.length) { toast('没有待确认标点'); return null; }
    let i = S.selected == null ? -1 : list.indexOf(S.selected);
    const pos = list[(i + 1) % list.length];
    selectMark(pos); return pos;
  }
  function acceptMark(pos) {
    const m = S.marks.find(x => x.pos === pos);
    if (!m || m.state === 'ok') return;
    snapshot(); m.state = 'ok'; m.source = 'human'; selectMark(null);
    renderText(); updateStats(); saveSoon(); nextPending();
  }
  function rejectMark(pos) {
    const m = S.marks.find(x => x.pos === pos);
    if (!m || m.state === 'ok') return;
    snapshot();
    S.text = S.text.slice(0, pos) + S.text.slice(pos + 1);
    S.marks = S.marks.filter(x => x.pos !== pos).map(x => x.pos > pos ? Object.assign({}, x, { pos: x.pos - 1 }) : x);
    S.structures = S.structures.map(st => {
      let s = st.start, e = st.end;
      let attrs = st.attrs;
      if (attrs && Array.isArray(attrs.chen)) {
        const chen = attrs.chen.map(([a, b]) => [a > pos ? a - 1 : a, b > pos ? b - 1 : b])
          .filter(([a, b]) => b > a);
        attrs = Object.assign({}, attrs, { chen });
      }
      if (s > pos) s--;
      if (e > pos) e--;
      return Object.assign({}, st, { start: s, end: e, attrs });
    }).filter(st => st.end > st.start);
    recalcBreaks();
    selectMark(null); renderText(); updateStats(); saveSoon(); nextPending();
  }

  function filterLabel() {
    const a = [];
    if (S.filterSource === 'rule') a.push('来源=规则');
    if (S.filterSource === 'model') a.push('来源=模型');
    if (S.onlyLow) a.push('低置信');
    return a.length ? a.join(' · ') : '全部来源';
  }
  function filteredPending() {
    return S.marks.filter(m => {
      if (!(m.state === 'a' || m.state === 'b' || m.state === 'cand')) return false;
      if (S.filterSource === 'rule' && m.source === 'machine-model') return false;
      if (S.filterSource === 'model' && m.source !== 'machine-model') return false;
      if (S.onlyLow) {
        const c = (m.conf != null) ? Number(m.conf) : 1;
        if (c >= 0.5) return false;
      }
      return true;
    });
  }
  function batchAccept() {
    const list = filteredPending();
    if (!list.length) { toast('当前筛选没有待确认标点'); return; }
    openConfirm('批量接受', `<div class="sub">范围：${esc(filterLabel())}<br>将接受 ${list.length} 个未确认标点，可在撤销里回退。</div>`,
      `接受 ${list.length} 个`, () => {
        snapshot();
        list.forEach(m => { m.state = 'ok'; m.source = 'human'; });
        selectMark(null); renderText(); updateStats(); saveSoon();
        toast(`已接受 ${list.length} 个`);
      });
  }
  function batchReject() {
    const list = filteredPending();
    if (!list.length) { toast('当前筛选没有待确认标点'); return; }
    openConfirm('批量拒绝', `<div class="sub">范围：${esc(filterLabel())}<br>将删除 ${list.length} 个未确认标点，可在撤销里回退。</div>`,
      `拒绝 ${list.length} 个`, () => {
        snapshot();
        const set = new Set(list.map(m => m.pos));
        const removedBefore = new Array(S.text.length + 1).fill(0);
        for (let i = 0; i < S.text.length; i++) {
          removedBefore[i + 1] = removedBefore[i] + (set.has(i) ? 1 : 0);
        }
        let kept = ''; for (let i = 0; i < S.text.length; i++) { if (!set.has(i)) kept += S.text[i]; } S.text = kept;
        S.marks = S.marks.filter(m => !set.has(m.pos))
          .map(m => Object.assign({}, m, { pos: m.pos - removedBefore[m.pos] }));
        S.structures = S.structures.map(st => {
          const s = st.start - removedBefore[st.start];
          const e = st.end - removedBefore[st.end];
          let attrs = st.attrs;
          if (attrs && Array.isArray(attrs.chen)) {
            const chen = attrs.chen.map(([a, b]) => [a - removedBefore[a], b - removedBefore[b]])
              .filter(([a, b]) => b > a);
            attrs = Object.assign({}, attrs, { chen });
          }
          return Object.assign({}, st, { start: s, end: e, attrs });
        }).filter(st => st.end > st.start);
        recalcBreaks();
        selectMark(null); renderText(); updateStats(); saveSoon();
        toast(`已拒绝 ${list.length} 个`);
      });
  }

  /* ---------- 结构 ---------- */
  function openStructure(idx, ev) {
    const st = S.structures[idx]; if (!st) return;
    const p = $('popover');
    const sug = (st.attrs || {}).llm_suggest;
    const sugName = sug && sug !== st.type ? (TYPE_LABEL[sug] || sug) : '';
    p.innerHTML = `<button class="ppclose" id="ppClose" type="button" title="关闭">×</button><h4>结构片段 <span class="conf">置信度 ${Math.round((st.conf || .5) * 100)}%</span></h4>
      <div style="font-size:11px;color:#7A6E5E;margin-top:6px;line-height:1.6">当前：「${TYPE_LABEL[st.type] || '其他'}」<br>改类型后会按新类型重算唱词断句（联调版先改数组）。</div>
      <div class="pgrid">${Object.entries(TYPE_LABEL).map(([k, v]) => `<button data-t="${k}" class="${k === st.type ? 'on' : ''}">${v}<small>${k === 'qupai' ? '牌名 / 前腔 / 集曲' : k === 'wen' ? '唱词正文' : k === 'bai' ? '对白 / 独白 / 夹白' : k === 'ke' ? '动作 / 舞台提示' : k === 'role' ? '说话人' : '标题 / 序跋'}</small></button>`).join('')}</div>
      ${sugName ? `<div class="llmSug">LLM 建议：<b>${esc(sugName)}</b> <button id="ppAcceptSug">采用建议</button><button id="ppDismissSug">忽略</button></div>` : ''}
      <div class="row"><button id="ppAI" ${['bai','wen'].includes(st.type) ? '' : 'disabled title="只有念白/唱词块支持"'}>AI 标点此段</button><span class="aistatus" id="ppAIStatus"></span></div>
      <div class="row"><button id="ppMerge">与上段合并</button><button id="ppSplit">从中点拆开</button><button id="ppDelete" style="color:#A8472F">删除此段</button></div>
      <div class="note">删除只删结构标注，不动正文。</div>`;
    const r = (ev.target.closest('.pgutter') || ev.target.closest('.seg') || ev.target).getBoundingClientRect();
    const ax = (ev && ev.clientX) ? ev.clientX : r.left;
    const ay = (ev && ev.clientY) ? ev.clientY : r.top;
    p.style.left = Math.min(window.innerWidth - 390, Math.max(10, ax + 14)) + 'px';
    p.style.top = Math.min(window.innerHeight - 430, Math.max(10, ay - 30)) + 'px';
    p.style.display = 'block';
    p.querySelectorAll('[data-t]').forEach(b => b.onclick = () => {
      snapshot(); S.structures[idx].type = b.dataset.t; closePopover(); renderText(); updateStats(); saveSoon();
      toast('已改为：' + TYPE_LABEL[b.dataset.t]);
    });
    $('ppMerge').onclick = () => {
      const prev = S.structures[idx - 1];
      if (!prev || prev.end !== st.start) { toast('上面没有可合并的相邻片段'); return; }
      snapshot(); prev.end = st.end; S.structures.splice(idx, 1); closePopover(); renderText(); updateStats(); saveSoon();
    };
    $('ppSplit').onclick = () => {
      const mid = st.start + Math.floor((st.end - st.start) / 2);
      if (mid <= st.start || mid >= st.end) { toast('片段太短'); return; }
      snapshot();
      const at = Object.assign({}, st.attrs || {});
      const chen = (at.chen || []).map(([a, b]) => [a, b]);
      const c1 = chen.filter(([a, b]) => b <= mid).map(([a, b]) => [a, b]);
      const c2 = chen.filter(([a, b]) => a >= mid).map(([a, b]) => [a - mid, b - mid]);
      S.structures.splice(idx, 1,
        { start: st.start, end: mid, type: st.type, conf: st.conf, attrs: Object.assign({}, at, { chen: c1 }) },
        { start: mid, end: st.end, type: st.type, conf: st.conf, attrs: Object.assign({}, at, { chen: c2 }) });
      closePopover(); renderText(); updateStats(); saveSoon();
    };
    const accept = $('ppAcceptSug');
    if (accept) accept.onclick = () => {
      if (!Object.prototype.hasOwnProperty.call(TYPE_LABEL, sug)) { toast('非法建议类型'); return; }
      snapshot(); S.structures[idx].type = sug;
      const at = S.structures[idx].attrs || {};
      delete at.llm_suggest; delete at.llm_suggest_from;
      S.structures[idx].attrs = at;
      closePopover(); renderText(); updateStats(); saveSoon();
      toast('已采用 LLM 建议：' + TYPE_LABEL[sug]);
    };
    const dismiss = $('ppDismissSug');
    if (dismiss) dismiss.onclick = () => {
      snapshot();
      const at = S.structures[idx].attrs || {};
      delete at.llm_suggest; delete at.llm_suggest_from;
      S.structures[idx].attrs = at;
      closePopover(); renderText(); updateStats(); saveSoon();
      toast('已忽略建议');
    };
    const aiBtn = $('ppAI');
    if (aiBtn) aiBtn.onclick = async () => {
      if (!['bai', 'wen'].includes(st.type)) { toast('只有念白/唱词块支持 AI 标点'); return; }
      const u0 = Number(st.start), u1 = Number(st.end);
      const c0 = toCp(S.text, u0), c1 = toCp(S.text, u1);
      const statusEl = $('ppAIStatus');
      const t0 = Date.now();
      const est = Math.max(2, Math.round((u1 - u0) / 10));
      const tick = () => {
        if (!statusEl) return;
        const sec = ((Date.now() - t0) / 1000).toFixed(0);
        statusEl.textContent = 'AI 标点中… ' + sec + 's / 预计约 ' + est + 's';
      };
      tick();
      const timer = setInterval(tick, 500);
      aiBtn.disabled = true; aiBtn.textContent = 'AI 标点中…';
      const ac = new AbortController();
      S.aiRangeAbort = ac;
      S.aiRangeCancelled = false;
      try {
        const r = await api('/api/books/' + BID + '/punct/rewrite-range', {
          method: 'POST', body: JSON.stringify({ start: c0, end: c1 }), signal: ac.signal
        });
        if (ac.signal.aborted || S.aiRangeCancelled) return;
        if (ac.signal.aborted || S.aiRangeCancelled) return;
        if (r.stats && r.stats.short) {
          if (statusEl) statusEl.textContent = '该段过短，无需标点';
          toast('该段过短，无需 AI 标点', 4000);
          return;
        }
        if (ac.signal.aborted || S.aiRangeCancelled) return;
        const ok = applyRangePunctuation(u0, u1, r.text);
        if (!ok) {
          if (statusEl) statusEl.textContent = 'AI 结果无法合并（可能改动了原字）';
          toast('AI 结果无法合并（可能改动了原字）', 6000);
        } else {
          S.caret = Math.min(S.text.length, u0 + r.text.length);
          S.aiRangeAbort = null;
          closePopover(); renderText(); updateStats(); saveSoon();
          toast('已用 AI 重标此段（' + Math.round((Date.now() - t0) / 1000) + 's）', 5000);
        }
      } catch (e) {
        if (e && e.name === 'AbortError') {
          if (statusEl) statusEl.textContent = '已取消';
        } else {
          if (statusEl) statusEl.textContent = '失败：' + e.message;
          toast('AI 标点失败：' + e.message, 8000);
        }
      }
      finally {
        clearInterval(timer);
        if (S.aiRangeAbort === ac) S.aiRangeAbort = null;
        aiBtn.disabled = false; aiBtn.textContent = 'AI 标点此段';
      }
    };
    const ppClose = $('ppClose');
    if (ppClose) ppClose.onclick = (e) => { e.stopPropagation(); closePopover(); };
    $('ppDelete').onclick = () => {
      snapshot(); S.structures.splice(idx, 1);
      closePopover(); renderText(); updateStats(); saveSoon();
      toast('已删除结构标注（正文未变）');
    };
    ev.stopPropagation();
  }
  function addStructureAt(start, end, type) {
    if (!S.hasDoc) return;
    if (!(start < end)) { toast('先选中要标为结构的文字'); return; }
    snapshot();
    const kept = [];
    S.structures.forEach(st => {
      const a = Number(st.start), b = Number(st.end);
      if (b <= start || a >= end) { kept.push(st); return; }
      if (a < start) kept.push(Object.assign({}, st, { end: start }));
      if (b > end) kept.push(Object.assign({}, st, { start: end }));
    });
    kept.push({ start: start, end: end, type: type, conf: 1, attrs: { source: 'human' } });
    S.structures = kept.filter(x => x.end > x.start).sort((a, b) => a.start - b.start);
    closePopover(); S.selected = null; S.caret = null;
    renderText(); updateStats(); saveSoon();
    toast('已新增结构：' + (TYPE_LABEL[type] || type));
  }
  function openAddStructure() {
    const sel = _textSelection();
    if (!sel || sel.collapsed || sel.end <= sel.start) {
      toast('先在正文里选中要标为结构的文字'); return;
    }
    const p = $('popover');
    p.innerHTML = `<h4>新增结构</h4><div style="font-size:11px;color:#7A6E5E;margin:5px 0 8px">将选中文字标为：</div>
      <div class="pgrid">${Object.entries(TYPE_LABEL).map(([k, v]) => `<button data-add-t="${k}">${v}</button>`).join('')}</div>`;
    let rect = null;
    try {
      const r = window.getSelection().getRangeAt(0).getBoundingClientRect();
      if (r && r.width) rect = r;
    } catch (e) {}
    p.style.left = Math.min(window.innerWidth - 380, Math.max(10, (rect ? rect.left : 300))) + 'px';
    p.style.top = Math.max(10, Math.min(window.innerHeight - 320, (rect ? rect.bottom : 200) + 8)) + 'px';
    p.style.display = 'block';
    p.querySelectorAll('[data-add-t]').forEach(b => b.onclick = () => addStructureAt(sel.start, sel.end, b.dataset.addT));
    p.onclick = e => e.stopPropagation();
  }

  function closePopover() {
    if (S.aiRangeAbort) {
      try { S.aiRangeAbort.abort(); } catch (e) {}
      S.aiRangeAbort = null;
      S.aiRangeCancelled = true;
      toast('已取消本次 AI 标点请求', 3000);
    }
    $('popover').style.display = 'none';
  }

  /* ---------- 初版入口 ---------- */
  function setStructProgress(st) {
    const chip = $('aiRunChip');
    if (!chip) return;
    if (st && st.running) {
      const pct = st.total ? Math.round((st.done || 0) / st.total * 100) : 0;
      chip.style.display = 'inline-flex';
      chip.textContent = '句读整本初版 ' + pct + '% · ' + (st.done || 0) + '/' + (st.total || 0);
      chip.title = (st.stage || '') + (st.eta_seconds ? ' · 预计约 ' + Math.ceil(st.eta_seconds / 60) + ' 分钟' : '');
    } else if (st && st.stage === '失败') {
      chip.style.display = 'inline-flex';
      chip.textContent = '句读整本初版 失败';
      chip.title = st.error || '';
    } else if (st && st.resumable) {
      chip.style.display = 'inline-flex';
      chip.textContent = '句读整本初版 可续';
      chip.title = '上次中断于 ' + (st.resume_done || 0) + '/' + (st.resume_total || '?') + ' 段';
    } else {
      chip.style.display = 'none';
    }
    renderStructJobModal(st);
  }

  function fmtSec(n) {
    n = Math.max(0, Math.round(Number(n) || 0));
    if (n < 60) return n + ' 秒';
    return Math.floor(n / 60) + ' 分 ' + (n % 60) + ' 秒';
  }

  function renderStructJobModal(st) {
    if (!S.spModal || !st) return;
    const root = $('modalRoot');
    if (!root || !root.querySelector('.structjob')) return;
    const total = Number(st.total) || 0, done = Number(st.done) || 0;
    const pct = total ? Math.min(100, Math.round(done * 100 / total)) : 0;
    const bar = $('spBarFill'); if (bar) bar.style.width = pct + '%';
    const set = (id, text) => { const el = $(id); if (el) el.textContent = text; };
    set('spPct', pct + '%');
    set('spDone', done + ' / ' + (total || '?') + ' 段');
    set('spElapsed', '已用 ' + fmtSec(st.elapsed));
    set('spEta', st.running ? (st.eta_seconds ? '预计约 ' + fmtSec(st.eta_seconds) : '预计中…') : '');
    set('spStage', st.stage || '');
    set('spCurrent', st.current_text ? '当前：' + st.current_text : '');
    const fail = $('spFail'); if (fail) fail.textContent = st.failed ? ('失败 ' + st.failed + ' 段') : '';
    const resume = $('spResume');
    if (resume) {
      if (!st.running && st.resumable) {
        resume.style.display = 'block';
        resume.innerHTML = `<div class="spresume">上次已处理 ${st.resume_done || done}/${st.resume_total || total} 段，可从断点继续。</div><button id="spResumeBtn" class="primary">继续识别</button>`;
        const rb = $('spResumeBtn');
        if (rb) rb.onclick = () => { closeModal(); startStructJob(); };
      } else { resume.style.display = 'none'; resume.innerHTML = ''; }
    }
    const cancel = $('spCancel');
    if (cancel) {
      cancel.textContent = st.running ? '停止' : '关闭';
      cancel.disabled = false;
      cancel.onclick = async () => {
        if (st.running) {
          cancel.disabled = true; cancel.textContent = '停止中…';
          try { await api('/api/books/' + BID + '/struct-punct/cancel', { method: 'POST', body: '{}' }); } catch (e) { toast('停止失败：' + e.message); }
        } else closeModal();
      };
    }
    const close = $('spClose');
    if (close) close.onclick = () => closeModal();
  }

  function openStructJob(st) {
    S.spModal = true;
    $('overlay').style.display = 'block';
    $('modalRoot').innerHTML = `<div class="modal structjob">
      <h3>句读整本初版 · 规则结构 + AI 标点</h3>
      <div class="sub">先用规则识别整本结构，再对纯念白/唱词块逐块调用已配置的 AI 服务生成标点初版。全流程只执行一次。</div>
      <div class="spbar"><div id="spBarFill" style="width:0%"></div></div>
      <div class="sprow"><b id="spPct">0%</b><span id="spDone">0 / ? 段</span><span id="spElapsed">已用 0 秒</span><span id="spEta">预计中…</span></div>
      <div class="spstage" id="spStage">准备中…</div>
      <div class="spcurrent" id="spCurrent"></div>
      <div class="spfail" id="spFail"></div>
      <div class="spresume" id="spResume"></div>
      <div class="actions"><button id="spCancel">停止</button><button id="spClose">关闭</button></div>
    </div>`;
    renderStructJobModal(st);
  }

  async function startStructJob() {
    try {
      const r = await api('/api/books/' + BID + '/struct-punct', { method: 'POST', body: JSON.stringify({ mode: 'initial' }) });
      openStructJob((r && r.job) || { running: true, done: 0, total: 0, stage: '启动' });
      if (S.spTimer) clearTimeout(S.spTimer);
      pollStructPunct();
    } catch (e) {
      try {
        const st = await api('/api/books/' + BID + '/struct-punct/status');
        openStructJob(st);
      } catch (e2) {
        toast('启动失败：' + e.message, 6000);
      }
    }
  }

  async function pollStructPunct() {
    try {
      const st = await api('/api/books/' + BID + '/struct-punct/status');
      setStructProgress(st);
      if (st.running) { S.spTimer = setTimeout(pollStructPunct, 1500); return; }
      S.spTimer = null;
      if (st.stage === '完成') {
        if (!st.total) {
          toast('句读整本初版完成：没有可处理的纯念白/唱词块', 5000);
        } else {
        const ok = Math.max(0, (st.done || 0) - (st.failed || 0)); toast('句读整本初版完成：成功 ' + ok + '/' + (st.total || 0) + ' 段' + (st.failed ? '，失败 ' + st.failed + ' 段' : ''), 5000);
        }
        const root = $('modalRoot'); if (S.spModal && root && root.querySelector('.structjob')) {
          setTimeout(() => { if (S.spModal) closeModal(); }, 1800);
        }
        reloadDoc();
      } else if (st.stage === '失败') {
        toast('句读整本初版失败：' + (st.error || '未知错误') + (st.resumable ? '（可续跑）' : ''), 9000);
      } else if (st.resumable) {
        toast((st.error ? ('上次中断：' + st.error) : '已停止') + '，可从断点继续（已完成 ' + (st.resume_done || 0) + ' 段）', 7000);
      } else if (st.stage === '失败') {
        toast('句读整本初版失败：' + (st.error || '未知错误'), 7000);
      }
    } catch (e) { S.spTimer = null; toast('查询句读整本初版状态失败：' + e.message, 6000); }
  }

  async function reloadDoc() {
    const doc = await api('/api/books/' + BID + '/punct');
    if (!doc.exists) {
      S.hasDoc = false; S.doc = null; S.text = ''; S.marks = []; S.structures = [];
      S.rev = 0; $('saveState').textContent = '服务端无稿';
      renderText(); updateStats();
      throw new Error('服务端暂无标点稿，请先保存或重新生成');
    }
    const conv2 = docToUnits(doc);
    S.doc = doc; S.text = conv2.text; S.marks = conv2.marks; S.structures = conv2.structures; S.pageRanges = conv2.pageRanges; S.breaks = conv2.breaks; S.hasDoc = true;
    S.rev = Number(doc.rev || 0);
    S.mode = 'review'; $('editArea').style.display = 'none'; $('editPreview').style.display = 'none'; $('ptext').style.display = '';
    setShell(); renderText(); updateStats(); $('saveState').textContent = '已保存';
    const rb = $('btnReview'); if (rb) rb.style.display = 'inline-flex';
    return true;
  }
  function openReview() {
    const lowMarks = S.marks.filter(m => (m.conf != null ? Number(m.conf) : 1) < 0.5);
    const lowStructs = S.structures.filter(st => (st.conf != null ? Number(st.conf) : 1) < 0.5);
    const rows = lowMarks.slice(0, 80).map(m => {
      const p = m.pos; const ctx = S.text.slice(Math.max(0, p - 12), p + 12).replace(/\n/g, ' ');
      return `<div class="histitem"><div><b>标点 @${p}「${esc(m.ch)}」</b><span>${esc(ctx)}</span></div><button data-pos="${p}">定位</button></div>`;
    }).join('') + lowStructs.slice(0, 40).map(st => {
      const ctx = S.text.slice(st.start, Math.min(S.text.length, st.start + 30)).replace(/\n/g, ' ');
      return `<div class="histitem"><div><b>${TYPE_LABEL[st.type] || '结构'} 置信 ${Math.round((st.conf || 0) * 100)}%</b><span>${esc(ctx)}（${st.start}-${st.end}）</span></div><button data-seg="${st.start}">定位</button></div>`;
    }).join('');
    $('overlay').style.display = 'block';
    $('modalRoot').innerHTML = `<div class="modal history"><h3>低置信复核</h3>
      <div class="sub">标点 <b>${lowMarks.length}</b> 处 · 结构 <b>${lowStructs.length}</b> 段（阈值 50%）。点击定位到正文。</div>
      <div class="histlist">${rows || '<div class="sub" style="padding:16px 0">没有低置信项。</div>'}</div>
      <div class="actions"><button id="mCancel">关闭</button></div></div>`;
    $('mCancel').onclick = closeModal;
    $('modalRoot').querySelectorAll('[data-pos]').forEach(b => b.onclick = () => { closeModal(); selectMark(parseInt(b.dataset.pos)); });
    $('modalRoot').querySelectorAll('[data-seg]').forEach(b => b.onclick = () => {
      closeModal();
      const pos = parseInt(b.dataset.seg);
      const el = document.querySelector(`#ptext [data-pos="${pos}"]`) || document.querySelector(`#ptext .seg[data-idx]`);
      if (el) el.scrollIntoView({ block: 'center' });
    });
  }
  function reloadLatestDoc() {
  openConfirm('载入最新标点稿', '<div class="sub">将丢弃本窗口未保存的修改，从服务端重新载入当前标点稿。</div>', '载入最新', async () => {
    try {
      await reloadDoc();
      const rb = $('btnReloadDoc'); if (rb) rb.style.display = 'none';
      const sf = $('btnStaleFix'); if (sf) sf.style.display = 'none';
      setSaveState('已保存');
      toast('已载入最新标点稿');
    } catch (e) { toast('载入失败：' + e.message, 6000); }
  });
}
function remapStale() {
    openConfirm('重映射标点', '<div class="sub">校字稿有改动。将用 diff 把旧标点/结构映射到新原文，尽量保留已确认内容；无法映射的标点会丢失。</div>', '重映射', async () => {
      try {
        const r = await api('/api/books/' + BID + '/punct/remap', { method: 'POST' });
        toast(`已重映射：rev ${r.rev} · ${r.marks} 标点`); const rb = $('btnRemap'); if (rb) rb.style.display = 'none'; const sf = $('btnStaleFix'); if (sf) sf.style.display = 'none';
        await reloadDoc();
      } catch (e) { toast('重映射失败：' + e.message, 6000); }
    });
  }
  function openConfirm(title, body, okLabel, onOk) {
    $('overlay').style.display = 'block';
    $('modalRoot').innerHTML = `<div class="modal confirm"><div class="mtitle" style="text-align:left;width:100%">${title}</div>${body}<div class="actions" style="display:flex;justify-content:flex-end;gap:8px;width:100%"><button id="mCancel">取消</button><button class="primary" id="mOk">${okLabel}</button></div></div>`;
    $('mCancel').onclick = closeModal;
    $('mOk').onclick = () => { closeModal(); onOk(); };
  }
  function closeModal() { $('overlay').style.display = 'none'; $('modalRoot').innerHTML = ''; }

  /* ---------- 历史版本 ---------- */
  async function openHistory() {
    if (!S.hasDoc) return toast('还没有标点稿');
    await save(false);
    let d = { items: [] };
    try { d = await api('/api/books/' + BID + '/punct/history'); } catch (e) { return toast('读取历史失败：' + e.message); }
    const items = d.items || [];
    $('overlay').style.display = 'block';
    $('modalRoot').innerHTML = `<div class="modal history"><h3>标点稿历史</h3>
      <div class="sub">每次保存前自动留快照，最多 30 份；恢复也会先备份当前稿。</div>
      <div class="histlist">${items.length ? items.map(x => `<div class="histitem"><div><b>${esc(x.saved_at || x.name)}</b><span>rev ${x.rev} · ${x.marks} 标点 · ${x.structures} 段 · ${x.chars} 字</span></div><button data-hist="${esc(x.name)}">恢复</button></div>`).join('') : '<div class="sub" style="padding:16px 0">还没有历史快照；保存一次后就会生成。</div>'}</div>
      <div class="actions"><button id="mCancel">关闭</button></div></div>`;
    $('mCancel').onclick = closeModal;
    $('modalRoot').querySelectorAll('[data-hist]').forEach(b => b.onclick = () => restoreHistory(b.dataset.hist));
  }
  async function restoreHistory(name) {
    openConfirm('恢复历史版本', `<div class="sub">将把当前稿替换为历史版本：<br>${esc(name)}<br><br>当前稿会先存入历史，可在撤销里回退。</div>`, '恢复', async () => {
      try {
        const r = await api('/api/books/' + BID + '/punct/restore', { method: 'POST', body: JSON.stringify({ name }) });
        const doc = r.doc || {};
        const conv3 = docToUnits(doc);
        S.doc = doc; S.text = conv3.text; S.marks = conv3.marks; S.structures = conv3.structures;
        S.breaks = conv3.breaks; S.pageRanges = conv3.pageRanges; S.rev = Number(r.rev || doc.rev || 0);
        S.hasDoc = true; S.mode = 'review'; $('editArea').style.display = 'none'; $('editPreview').style.display = 'none'; $('ptext').style.display = '';
        setShell(); renderText(); updateStats(); $('saveState').textContent = '已恢复';
        toast('已恢复历史版本 rev ' + S.rev);
      } catch (e) { toast('恢复失败：' + e.message, 5000); }
    });
  }

  /* ---------- 备份 / 恢复 ---------- */
  async function backupBook() {
    try {
      const r = await fetch('/api/books/' + BID + '/backup');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (S.book.title || 'book') + '-备份.zip';
      document.body.appendChild(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
      toast('已导出备份：' + a.download);
    } catch (e) { toast('备份失败：' + e.message, 5000); }
  }
  function chooseRestore() {
    const f = $('backupFile'); f.value = ''; f.click();
  }
  async function doRestore(file) {
    openConfirm('恢复书库备份', `<div class="sub">用 <b>${esc(file.name)}</b> 覆盖当前书的识别/校对/标点数据？<br><br>原图和缩略图不包含在备份里；恢复前会自动把当前书库留成 <code>.before-restore-时间</code> 目录。</div>`, '覆盖恢复', async () => {
      try {
        const r = await fetch('/api/books/' + BID + '/backup/restore', {
          method: 'POST', headers: { 'Content-Type': 'application/zip' }, body: file,
        });
        let d = null; try { d = await r.json(); } catch (e) {}
        if (!r.ok) throw new Error((d && d.detail) || ('HTTP ' + r.status));
        toast('恢复完成，正在刷新…', 3000);
        setTimeout(() => location.reload(), 800);
      } catch (e) { toast('恢复失败：' + e.message, 6000); }
    });
  }

  /* ---------- 质量面板 ---------- */
  function openQuality() {
    if (!S.hasDoc) return toast('还没有标点稿');
    const c = counts();
    const chars = (S.text || '').replace(/\s/g, '').length;
    const modelCount = S.marks.filter(m => m.source === 'machine-model').length;
    const accepted = c.ok || 0;
    const total = accepted + c.pending;
    const byType = {};
    S.structures.forEach(st => { byType[st.type] = (byType[st.type] || 0) + 1; });
    const pages = S.book.pages || [];
    const recognized = pages.filter(p => p.status !== 'raw').length;
    const pendingPct = total ? (c.pending / total * 100).toFixed(1) : '0.0';
    const acceptPct = total ? (accepted / total * 100).toFixed(1) : '0.0';
    const density = chars ? (S.marks.length / chars * 1000).toFixed(1) : '0.0';
    $('overlay').style.display = 'block';
    $('modalRoot').innerHTML = `<div class="modal quality"><h3>本书质量</h3>
      <div class="sub">当前标点稿的接受率、待确认和结构分布；只统计当前稿，不是评估集指标。</div>
      <div class="qgrid">
        <div><b>${chars}</b><span>正文字数</span></div>
        <div><b>${S.marks.length}</b><span>标点总数</span></div>
        <div><b>${accepted}</b><span>已确认</span></div>
        <div><b>${c.pending}</b><span>待确认</span></div>
        <div><b>${acceptPct}%</b><span>接受率</span></div>
        <div><b>${density}</b><span>标点/千字</span></div>
        <div><b>${modelCount}</b><span>模型候选</span></div>
        <div><b>${recognized}/${pages.length}</b><span>已识别叶</span></div>
      </div>
      <div class="qsec"><b>结构分布</b><div class="qtypes">${Object.entries(TYPE_LABEL).map(([k, v]) => `<span>${v} <b>${byType[k] || 0}</b></span>`).join('')}</div></div>
      <div class="qsec"><b>待确认构成</b><div class="qtypes"><span>A 曲牌 <b>${c.a || 0}</b></span><span>B 候选 <b>${c.b || 0}</b></span><span>念白 <b>${c.cand || 0}</b></span><span>模型 <b>${modelCount}</b></span></div></div>
      <div class="actions"><button id="mCancel">关闭</button></div></div>`;
    $('mCancel').onclick = closeModal;
  }

  /* ---------- 设置 ---------- */
  function openSettings() {
    if (window.GuWenSettings) window.GuWenSettings.open();
    else toast('AI 设置组件未加载，请刷新页面');
  }

  /* ---------- 导出 ---------- */
  function buildExportPreview(maxChars = 3500, previewText = null) {
    const text = previewText || S.text || '';
    const opts = S.exportOpts;
    const structs = mergeActHeadings(text, normalizeStructs(text, S.structures));
    const clean = (x) => (x || '').replace(/[，。、；：？！…—「」『』《》（）【】〔〕]/g, '');
    const escp = (x) => esc(x || '');
    const chenRanges = (st) => ((st.attrs || {}).chen || []).map(([a, b]) => [Number(a), Number(b)]).sort((x, y) => x[0] - y[0]);
    const base = Math.max(9, Math.min(20, Number(opts.size) || 12));
    const bodySize = Math.max(9, base - 1.5);
    const fontStyle = 'font-family:' + opts.font;
    const roleWeight = opts.roleBold ? 700 : 600;
    const pStyle = opts.indentChars ? ' style="text-indent:' + opts.indentChars + 'em"' : '';
    const bodyHTML = (st, a, b) => {
      const body = text.slice(a, b);
      const ranges = chenRanges(st);
      if (!ranges.length) return escp(body);
      let out = '', p = a;
      for (const [ca, cb] of ranges) {
        if (cb <= p) continue;
        const x = Math.max(p, ca), y = Math.min(b, cb);
        if (x > p) out += escp(text.slice(p, x));
        if (y > x) out += `<span class="pv-chen" style="${fontStyle};font-size:${bodySize}px">${escp(text.slice(x, y))}</span>`;
        p = y;
      }
      if (p < b) out += escp(text.slice(p, b));
      return out;
    };
    let html = '', open = false;
    const closeP = () => { if (open) { html += '</div>'; open = false; } };
    const openP = () => { if (!open) { html += '<div class="pv-p"' + pStyle + '>'; open = true; } };
    const startP = () => { closeP(); openP(); };
    for (const st of structs) {
      const a = Number(st.start), b = Number(st.end);
      if (a >= maxChars) break;
      const bb = Math.min(b, maxChars);
      const body = text.slice(a, bb);
      const typ = st.type || 'other';
      const kind = (st.attrs || {}).kind;
      if (typ === 'title') { closeP(); html += `<div class="pv-title" style="${fontStyle};font-size:${base + 6}px">${escp(clean(body))}</div>`; continue; }
      if (typ === 'subtitle') { closeP(); html += `<div class="pv-sub" style="${fontStyle};font-size:${base + 2}px">${escp(clean(body))}</div>`; continue; }
      if (kind === 'volume' || kind === 'act') { closeP(); html += `<div class="pv-head" style="${fontStyle};font-size:${base + 2}px">${escp(clean(body))}</div>`; continue; }
      if (typ === 'qupai') { startP(); html += `<span class="pv-qp" style="${fontStyle};font-size:${base}px">【${escp(clean(body))}】</span>`; continue; }
      if (typ === 'role' || typ === 'ke') {
        // 与 DOCX 一致：角色/科介和后续念白/唱词在同一段内，不单独另起一段
        openP();
        const weight = typ === 'role' ? roleWeight : 400;
        html += `<span class="pv-role" style="${fontStyle};font-size:${bodySize}px;font-weight:${weight}">（${escp(clean(body))}）</span>`;
        continue;
      }
      if (typ === 'wen' || typ === 'bai') {
        openP();
        const fs = typ === 'wen' ? base : bodySize;
        html += `<span class="${typ === 'wen' ? 'pv-wen' : 'pv-bai'}" style="${fontStyle};font-size:${fs}px">${bodyHTML(st, a, bb)}</span>`;
        continue;
      }
      openP();
      html += `<span class="pv-bai" style="${fontStyle};font-size:${bodySize}px">${escp(body)}</span>`;
    }
    closeP();
    return html;
  }

  function convertedSameUnits(original, converted) {
    // OpenCC 可能把扩展区汉字（1 个码点/2 个 UTF-16 单元）转成 BMP 字（1 个单元），
    // 直接用会改变正文偏移；按码点对齐，长度不守恒的字符保留原字。
    const oa = Array.from(original || '');
    const ca = Array.from(converted || '');
    if (oa.length !== ca.length) return null;
    let out = '';
    for (let i = 0; i < oa.length; i++) {
      out += (oa[i].length === ca[i].length) ? ca[i] : oa[i];
    }
    return out.length === (original || '').length ? out : null;
  }

  async function previewTextForExport() {
    if (!S.exportOpts.simp) return S.text;
    if (S.simpPreview && S.simpPreview.src === S.text) return S.simpPreview.text;
    // 失败时不要缓存回退原文，否则后续点“简体”会一直拿到繁体缓存
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const r = await api('/api/convert', {
          method: 'POST',
          body: JSON.stringify({ text: S.text, dir: 't2s' }),
        });
        const converted = r && r.text;
        const safe = (typeof converted === 'string') ? convertedSameUnits(S.text, converted) : null;
        if (safe) {
          S.simpPreview = { src: S.text, text: safe };
          return safe;
        }
      } catch (e) {}
    }
    return S.text;
  }

  async function openExport() {
    if (!S.hasDoc) return toast('还没有标点稿，先跑初版或手动保存');
    await save(false);
    const pvText = await previewTextForExport();
    let pvTitle = S.book.title || '';
    if (S.exportOpts.simp) {
      try {
        const tr = await api('/api/convert', { method: 'POST', body: JSON.stringify({ text: pvTitle, dir: 't2s' }) });
        const safe = (tr && typeof tr.text === 'string') ? convertedSameUnits(pvTitle, tr.text) : null;
        if (safe) pvTitle = safe;
      } catch (e) {}
    }
    const pages = S.book.pages || [];
    const rawPages = pages.filter(p => p.status === 'raw').length;
    const blankPages = (S.pageRanges || []).filter(r => {
      if (!r.recognized) return false;
      const seg = (S.text || '').slice(r.start || 0, r.end || 0);
      return !seg.replace(/[。，、；：？！…「」『』《》（）〈〉【】〔〕—\s]/g, '').trim();
    }).length;
    const pending = counts().pending;
    const modelPending = S.marks.filter(m => m.state !== 'ok' && m.source === 'machine-model').length;
    const o = S.exportOpts;
    const fonts = ['宋体', '楷体', '黑体', '仿宋'];
    const sizes = [{ label: '五号', v: 10.5 }, { label: '小四', v: 12 }, { label: '四号', v: 14 }, { label: '小三', v: 15 }];
    const check = (ok, text) => `<div style="margin-top:5px;color:${ok ? '#4E6B4E' : '#A05A2C'}">${ok ? '✓' : '!'} ${text}</div>`;
    const sizeLabel = (sizes.find(x => x.v === o.size) || sizes[1]).label;
    $('overlay').style.display = 'block';
    $('modalRoot').innerHTML = `<div class="modal export"><div class="left"><h3 style="text-align:left">导出整理本</h3>
      <div class="sub">标点稿 v2 · ${S.structures.length} 段结构 · ${pending} 处待确认</div>
      <div class="checks">
        ${check(true, '标点稿已保存')}
        ${check(pending === 0, pending ? `未确认标点 ${pending} 处，仍会按原样导出` : '没有未确认标点')}
        ${check(rawPages === 0, rawPages ? `未识别叶 ${rawPages} 叶，导出会缺这些页` : '全部叶已识别')}
        ${check(blankPages === 0, blankPages ? `空白识别叶 ${blankPages} 叶（无正文）` : '识别叶都有正文')}
        ${check(true, `结构 ${S.structures.length} 段 · 模型候选 ${modelPending} 处`)}
      </div>
      <div class="opts">
        <span class="opt ${o.simp ? 'on' : ''}" data-exp="simp">简体</span>
        <span class="opt ${o.simp ? '' : 'on'}" data-exp="orig">原字</span>
        <span class="opt" data-exp="font">${o.font}</span>
        <span class="opt" data-exp="size">${sizeLabel}</span>
        <span class="opt ${o.roleBold ? 'on' : ''}" data-exp="role">角色加粗</span>
        <span class="opt ${o.indentChars ? 'on' : ''}" data-exp="indent">首行缩进</span>
      </div>
      <div class="sub" style="margin-top:10px">未确认标点、模型候选都按当前位置保留；导出为服务端标点稿。</div>
      <div class="actions"><button id="mCancel">取消</button><button class="primary" id="mExport">导出 DOCX</button></div></div>
      <div class="right"><div class="preview-title" style="font-family:${esc(o.font)};font-size:${Math.max(9, Number(o.size) || 12) + 7}px">${esc(pvTitle)}</div>
      <div class="preview-point" style="font-family:${esc(o.font)}">${buildExportPreview(3500, pvText)}</div>
      <div class="preview-note">预览：当前句读稿的点校本样式，随下方选项实时变化；标题、卷名、齣名、曲牌【】、角色/科介（）、唱词、念白。</div></div></div>`;
    const _pv = $('modalRoot').querySelector('.preview');
    if (_pv) { _pv.scrollTop = 0; setTimeout(() => { _pv.scrollTop = 0; }, 80); }
    const _rt = $('modalRoot').querySelector('.right');
    if (_rt) { _rt.scrollTop = 0; setTimeout(() => { _rt.scrollTop = 0; }, 80); }
    $('mCancel').onclick = closeModal;
    $('modalRoot').querySelectorAll('[data-exp]').forEach(el => el.onclick = () => {
      const k = el.dataset.exp;
      if (k === 'simp') S.exportOpts.simp = true;
      else if (k === 'orig') S.exportOpts.simp = false;
      else if (k === 'font') {
        const i = fonts.indexOf(S.exportOpts.font);
        S.exportOpts.font = fonts[(i + 1) % fonts.length];
      } else if (k === 'size') {
        const i = sizes.findIndex(x => x.v === S.exportOpts.size);
        S.exportOpts.size = sizes[(i + 1) % sizes.length].v;
      } else if (k === 'role') {
        S.exportOpts.roleBold = !S.exportOpts.roleBold;
      } else if (k === 'indent') {
        S.exportOpts.indentChars = S.exportOpts.indentChars ? 0 : 2;
      }
      openExport();
    });
    $('mExport').onclick = async () => {
      closeModal();
      try {
        const r = await fetch(`/api/books/${BID}/export-punct.docx?simp=${S.exportOpts.simp ? 1 : 0}&heading=1&font=${encodeURIComponent(S.exportOpts.font)}&size=${S.exportOpts.size}&role_bold=${S.exportOpts.roleBold ? 1 : 0}&indent=${S.exportOpts.indentChars}`);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const blob = await r.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = S.book.title + '-整理本-' + (S.exportOpts.simp ? '简体' : '原字') + '.docx';
        document.body.appendChild(a); a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
        toast('已导出：' + a.download);
      } catch (e) { toast('导出失败：' + e.message); }
    };
  }

  /* ---------- 绑定 ---------- */
  function bind() {
    const back = $('linkBackJiao');
    if (back) back.href = '/?book=' + encodeURIComponent(BID);
    $('palette').innerHTML = PUNCTS.map((p, i) => `<span class="pb${i === 1 ? ' on' : ''}">${p}</span>`).join('');
    (ROOT || document).addEventListener('click', e => {
      if (!isActive()) return;
      if (!e.target.closest('#popover') && !e.target.closest('.seg')) closePopover();
    });
    $('modeReview').onclick = () => { exitEdit(); };
    $('modeEdit').onclick = () => { enterEdit(); };
    $('editArea').addEventListener('input', () => {
      if (S.mode !== 'edit') return;
      if (!applyEdit($('editArea').value)) revertEdit();
    });
    $('palette').addEventListener('click', e => {
      const pb = e.target.closest('.pb'); if (!pb) return;
      qa('#palette .pb').forEach(x => x.classList.remove('on'));
      pb.classList.add('on');
      S.activePunct = pb.textContent;
    });
    $('btnBreak').onclick = () => { if (S.mode === 'edit') insertAtCaret(S.activePunct); else toast('空格键插入句读；先切到编辑模式'); };
    $('btnPara').onclick = () => { if (S.mode === 'edit') insertAtCaret('\n'); else toast('回车分段；先切到编辑模式'); };
    const sink = $('punctSink');
    if (sink) {
      let composing = false;
      sink.addEventListener('keydown', e => {
        if (e.key === 'Escape') { e.preventDefault(); blurSink(); return; }
        if (e.key === 'Backspace' || e.key === 'Delete') {
          e.preventDefault(); e.stopPropagation();
          deleteSelectedPunct(e.key === 'Backspace'); return;
        }
        if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); insertNewlineAt(); return; }
        if (e.key === ' ') { e.preventDefault(); e.stopPropagation(); insertPunctAt(S.activePunct || '。'); return; }
        const _ch = punctKeyChar(e);
        if (_ch) {
          e.preventDefault(); e.stopPropagation(); insertPunctKey(_ch); return;
        }
      });
      sink.addEventListener('compositionstart', () => { composing = true; });
      sink.addEventListener('compositionend', e => {
        composing = false;
        const data = e.data || '';
        sink.value = '';
        [...data].forEach(ch => { if (PUNCT_KEYS.includes(ch)) insertPunctAt(ch); });
      });
      sink.addEventListener('input', () => {
        if (composing) return;
        const v = sink.value; sink.value = '';
        [...v].forEach(ch => { if (PUNCT_KEYS.includes(ch)) insertPunctAt(ch); });
      });
    }
    $('btnNextPending').onclick = nextPending;
    $('btnUndo').onclick = undo; $('btnRedo').onclick = redo;
    $('btnSettings').onclick = openSettings; $('btnExport').onclick = openExport; $('btnHistory').onclick = openHistory; $('btnQuality').onclick = openQuality;
    $('btnRemap').onclick = remapStale;
    $('btnBackup').onclick = backupBook; $('btnRestore').onclick = chooseRestore;
    $('backupFile').onchange = () => { const f = $('backupFile').files && $('backupFile').files[0]; if (f) doRestore(f); };
    $('btnPrev').onclick = () => { const ps = S.book.pages || []; const i = ps.findIndex(p => p.no === S.currentPage); if (i > 0) { S.currentPage = ps[i - 1].no; setShell(); scrollToPage(S.currentPage); } };
    $('btnNext').onclick = () => { const ps = S.book.pages || []; const i = ps.findIndex(p => p.no === S.currentPage); if (i >= 0 && i < ps.length - 1) { S.currentPage = ps[i + 1].no; setShell(); scrollToPage(S.currentPage); } };
    $('zoomOut').onclick = () => { S.pageZoom = Math.max(0.4, S.pageZoom - 0.15); applyPageSizing(); };
    $('zoomIn').onclick = () => { S.pageZoom = Math.min(4, S.pageZoom + 0.15); applyPageSizing(); };
    $('zoomReset').onclick = () => { S.pageZoom = 1; applyPageSizing(); };
    $('zoomFit').onclick = () => {
      const st = q('.stage'); const pw = q('.pagewrap');
      const b = basePageFit();
      const baseW = (pw && S.pageZoom > 0 && isFinite(S.pageZoom)) ? (pw.offsetWidth / S.pageZoom) : b.w;
      const availW = Math.max(120, (st ? st.clientWidth : 800) - 48);
      S.pageZoom = Math.max(0.4, Math.min(4, availW / Math.max(1, baseW)));
      applyPageSizing();
      if (st) { st.scrollLeft = 0; st.scrollTop = 0; }
    };
    $('btnAminus').onclick = () => { const e = $('ptext'); e.style.fontSize = Math.max(14, parseFloat(getComputedStyle(e).fontSize) - 1) + 'px'; };
    $('btnAplus').onclick = () => { const e = $('ptext'); e.style.fontSize = Math.min(28, parseFloat(getComputedStyle(e).fontSize) + 1) + 'px'; };
    function setExpanded(on) {
      const main = q('.main');
      if (main) main.classList.toggle('expanded', !!on);
      const b = $('btnExpand');
      if (b) {
        b.classList.toggle('on', !!on);
        b.textContent = on ? '收起' : '展开';
      }
      try { localStorage.setItem('guwen_punct_expanded', on ? '1' : '0'); } catch (e) {}
    }
    $('btnExpand').onclick = () => {
      const on = !q('.main').classList.contains('expanded');
      setExpanded(on);
      toast(on ? '已展开右栏：加宽文字栏，保留左栏和页图' : '已收起右栏');
    };
    setExpanded(localStorage.getItem('guwen_punct_expanded') === '1');
    $('filterPending').onclick = () => { const on = $('ptext').classList.toggle('only-pending'); $('filterPending').classList.toggle('on', on); };
    $('btnReview').onclick = () => openReview();
    $('filterSource').onclick = () => {
      S.filterSource = S.filterSource === 'all' ? 'rule' : (S.filterSource === 'rule' ? 'model' : 'all');
      applyFilters(); selectMark(null);
    };
    $('filterLow').onclick = () => { S.onlyLow = !S.onlyLow; applyFilters(); selectMark(null); };
    $('batchAccept').onclick = batchAccept;
    $('batchReject').onclick = batchReject;
    $('btnAddStruct').onclick = (e) => { e.stopPropagation(); openAddStructure(); };
    const staleFix = $('btnStaleFix');
    if (staleFix) staleFix.onclick = () => remapStale();
    const reloadDocBtn = $('btnReloadDoc');
    if (reloadDocBtn) reloadDocBtn.onclick = () => reloadLatestDoc();
    $('btnStructPunct').onclick = async () => {
      try {
        const st = await api('/api/books/' + BID + '/struct-punct/status');
        if (st.running || st.resumable) { openStructJob(st); if (st.running) pollStructPunct(); return; }
      } catch (e) {}
      openConfirm('句读整本初版', '<div class="sub">规则识别整本结构，再按结构逐块生成 AI 标点初版。全流程只执行一次；角色、曲牌、科介不加标点。</div>', '开始', async () => {
        closeModal();
        await startStructJob();
      });
    };
    const _chenBtn = $('btnChen');
    if (_chenBtn) {
      // 点击工具栏前保住正文选区，否则浏览器会先折叠选区，衬字永远读不到范围
      _chenBtn.addEventListener('mousedown', e => e.preventDefault());
      _chenBtn.onclick = () => markChenSelection();
    }
    $('btnPointView').onclick = () => {
      S.pointView = !S.pointView;
      $('btnPointView').classList.toggle('on', S.pointView);
      renderText(); updateStats();
    };
    $('filterStruct').onclick = () => { const off = $('ptext').classList.toggle('struct-off'); $('filterStruct').textContent = off ? '结构标注：关' : '结构标注：开'; $('filterStruct').classList.toggle('on', !off); };
    $('filterType').onclick = () => toast('类型过滤下一步接入');
    $('ptext').addEventListener('click', e => {
      const mp = e.target.closest('.mp'); if (S.mode === 'review' && mp) { selectMark(parseInt(mp.dataset.pos)); S.caret = null; blurSink(); return; }
      const gutter = e.target.closest('.pgutter');
      const seg = e.target.closest('.seg');
      if (S.mode === 'review' && gutter && seg) { openStructure(parseInt(seg.dataset.idx), e); S.caret = null; blurSink(); return; }
      // 曲牌/角色/科介/标题等结构体不可直接落光标，点到正文等同选中结构
      if (S.mode === 'review' && seg) {
        const st = S.structures[parseInt(seg.dataset.idx)];
        if (st && !['bai', 'wen'].includes(st.type)) { openStructure(parseInt(seg.dataset.idx), e); S.caret = null; blurSink(); return; }
      }
      setCaretFromPoint(e.clientX, e.clientY);
    });
    $('editPreview').addEventListener('click', e => {
      if (S.mode !== 'edit') return;
      const mp = e.target.closest('.mp');
      if (!mp) return;
      const pos = parseInt(mp.dataset.pos);
      const ta = $('editArea');
      ta.focus();
      try { ta.setSelectionRange(pos, pos); } catch (err) {}
    });
    (ROOT || document).addEventListener('keypress', e => {
      if (!isActive() || e.ctrlKey || e.metaKey || e.altKey) return;
      if (S.mode === 'edit' && activeEl() === $('editArea')) return;
      const _ch = punctKeyChar(e);
      if (!_ch) return;
      if ($('overlay').style.display === 'block') return;
      e.preventDefault();
      insertPunctKey(_ch);
    });
    (ROOT || document).addEventListener('keydown', e => {
      if (!isActive()) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault(); save(false); return;
      }
      if (S.mode === 'edit' && activeEl() === $('editArea')) return;
      if (e.key === 'Backspace' || e.key === 'Delete') {
        if (S.selected != null || S.caret != null || (_textSelection() && !_textSelection().collapsed)) {
          e.preventDefault(); deleteSelectedPunct(e.key === 'Backspace'); return;
        }
      }
      if (e.key === 'Escape') { closePopover(); if ($('overlay').style.display === 'block') closeModal(); return; }
      if ($('overlay').style.display === 'block') return;
      if (S.mode === 'edit' && activeEl() === $('editArea')) {
        if (e.key === ' ') { e.preventDefault(); insertAtCaret(S.activePunct); return; }
        if (e.key === 'Enter') { e.preventDefault(); insertAtCaret('\n'); return; }
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); return; }
      if (S.mode !== 'review') return;
      if (e.key === 'Enter' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault(); insertNewlineAt(); return;
      }
      if (e.key === ' ' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        if (S.selected != null) { acceptMark(S.selected); return; }
        const sel = _textSelection();
        if (S.caret != null || (sel && !sel.collapsed)) { insertPunctAt(S.activePunct || '。'); return; }
        const pos = nextPending(); if (pos != null) acceptMark(pos);
        return;
      }
      if (!e.ctrlKey && !e.metaKey && !e.altKey) {
        const _ch = punctKeyChar(e);
        if (_ch) { e.preventDefault(); insertPunctKey(_ch); return; }
      }
      if (!e.ctrlKey && !e.metaKey && !e.altKey && (e.key === 'x' || e.key === 'X')) {
        e.preventDefault(); if (S.selected != null) rejectMark(S.selected); else toast('先选一处待确认');
      }
      if (!e.ctrlKey && !e.metaKey && !e.altKey && e.key === 'Tab') { e.preventDefault(); nextPending(); }
    });
  }

  window.addEventListener('beforeunload', e => {
    if (S.dirty || S.saving || S.saveQueued) {
      e.preventDefault();
      e.returnValue = '还有未保存的修改';
    }
  });
  bind();
  window.PunctAPI = { openSettings, openExport, openReview, openHistory, openQuality, backupBook, chooseRestore, isReady: () => !!S.hasDoc, isDirty: () => !!(S.dirty || S.saving || S.saveQueued) };
  loadAll().catch(e => {
    $('bookTitle').textContent = '加载失败';
    $('ptext').innerHTML = `<div class="banner">加载失败：${esc(e.message)}</div>`;
    toast('加载失败：' + e.message, 6000);
  });
})();
