/* 标点模式（前端原型 v2）
   右栏 = 全书连续单块文本（不显示叶界标签，页与页直接相接）；
   当前叶用浅色底提示，页头显示第 N 叶。
   只允许加标点 / 空格断句 / 回车分段，不能改原字；草稿存 localStorage。 */
(function () {
  "use strict";
  const $id = id => document.getElementById(id);

  const PALETTE = ["。", "，", "、", "；", "：", "？", "！", "「", "」", "『", "』", "《", "》", "（", "）", "——", "……", "·"];
  const PUNCT = new Set([
    ..."。，、；：？！「」『』《》（）〈〉【】〔〕——……·“”‘’…—",
    ...",.;:?!()[]<>\"'", "\n", " ", "\u3000",
  ]);

  const P = {
    active: false, bookId: null, meta: [], pageMap: {}, origText: "",
    textEl: null, lastText: "", breaks: [], hist: [], redo: [], font: 19,
    activePunct: "。",
    backendExists: false, backendHash: null, saving: false, saveQueued: false, pendingMigrate: false,
    saveTimer: null, selTimer: null, pendingHL: null, pendingCaretOff: null,
    lastMsg: 0, currentNo: 0,
  };

  /* ---------- 换行语义：同一个 "\n"，靠「是不是分段」区分 ---------- */
  function addBreak(list, off) {
    if (list.indexOf(off) < 0) { list.push(off); list.sort((a, b) => a - b); }
  }
  function sanitizeBreaks(text, breaks) {
    const out = [];
    breaks.forEach(b => {
      if (b >= 0 && b < text.length && text[b] === "\n" && out.indexOf(b) < 0) out.push(b);
    });
    return out;
  }
  function remapBreaks(oldText, newText, oldBreaks) {
    if (oldText === newText) return oldBreaks.slice();
    let p = 0;
    const minLen = Math.min(oldText.length, newText.length);
    while (p < minLen && oldText[p] === newText[p]) p++;
    let s = 0;
    while (s < minLen - p && oldText[oldText.length - 1 - s] === newText[newText.length - 1 - s]) s++;
    const oldEnd = oldText.length - s;
    const delta = newText.length - oldText.length;
    const out = [];
    oldBreaks.forEach(b => {
      if (b < p) out.push(b);
      else if (b >= oldEnd) out.push(b + delta);
      // 落在替换区内的分段标记随文本一起删除
    });
    return out;
  }
  function migrateV2(text) {
    // 旧格式：空行=分段 -> 单个换行 + 分段标记
    let out = "", breaks = [];
    for (let i = 0; i < text.length; i++) {
      if (text[i] === "\n") {
        let j = i;
        while (j < text.length && text[j] === "\n") j++;
        const count = j - i;
        if (count >= 2) {
          addBreak(breaks, out.length);
          out += "\n";
        } else {
          out += "\n";
        }
        i = j - 1;
      } else out += text[i];
    }
    return { text: out, breaks };
  }
  function exportText() {
    if (!P.textEl) return "";
    const text = P.textEl.innerText.replace(/\r/g, "");
    const set = new Set(P.breaks);
    let out = "";
    for (let i = 0; i < text.length; i++) {
      out += (text[i] === "\n" && set.has(i)) ? "\n\n" : text[i];
    }
    return out;
  }

  function isPunctCh(ch) { return PUNCT.has(ch); }
  function stripPunct(s) { let out = ""; for (const ch of s) if (!isPunctCh(ch)) out += ch; return out; }
  function countOrig(s, upto) { let n = 0, i = 0; for (const ch of s) { if (i >= upto) break; if (!isPunctCh(ch)) n++; i++; } return n; }

  /* ---------- 文本构造 ---------- */
  function rowText(r) {
    if (r && r.chars && r.chars.length) return r.chars.map(c => c.text || "").join("");
    return (r && r.text) || "";
  }
  function originalText(d) {
    let out = "";
    (d.rows || []).forEach(r => {
      const t = rowText(r);
      if (!t) return;
      if ((r.type || "正文") === "标题") {
        if (out && !out.endsWith("\n")) out += "\n";
        out += t + "\n";
      } else {
        out += t;
      }
    });
    return out.replace(/\n{3,}/g, "\n\n");
  }
  function flattenChars(d) {
    const arr = [];
    (d.rows || []).forEach((r, ri) => {
      (r.chars || []).forEach((c, ci) => {
        if (c && c.text) arr.push({ ri, ci, text: c.text, box: c.box, type: r.type || "正文" });
      });
    });
    return arr;
  }

  /* ---------- 模式切换 ---------- */
  function chips() {
    $id("modeJiao").classList.toggle("on", S.mode !== "punct");
    $id("modePunct").classList.toggle("on", S.mode === "punct");
  }

  async function enter() {
    if (P.active || !S.book) return;
    P.active = true; S.mode = "punct";
    P.bookId = S.book.id;
    $id("work").classList.add("mode-punct");
    $id("punctToolbar").hidden = false;
    $id("punctPanel").hidden = false;
    const ptEx = $id("ptExpand");
    if (ptEx) { ptEx.classList.toggle("on", S.expanded); ptEx.textContent = S.expanded ? "收起" : "展开"; }
    try { ghost.blur(); } catch (e) {}
    chips();
    $id("saveState").textContent = "标点稿";
    $id("saveState").style.color = "var(--sub)";
    buildPalette();
    P.font = 19; applyFont();
    await initDoc();
    $id("ptHL").hidden = true;
  }

  function exit() {
    saveDraftNow();
    persistBackend(false, true);
    P.active = false; S.mode = "jiao";
    $id("work").classList.remove("mode-punct");
    $id("punctToolbar").hidden = true;
    $id("punctPanel").hidden = true;
    $id("ptHL").hidden = true;
    P.pendingHL = null; P.pendingCaretOff = null;
    if (window.CSS && CSS.highlights) { try { CSS.highlights.delete("curpage"); CSS.highlights.delete("parabreak"); } catch (e) {} }
    chips();
    renderAll();
    focusGhost();
  }

  /* ---------- 加载全书 ---------- */
  async function loadAll() {
    P.meta = []; P.pageMap = {}; P.origText = "";
    const all = S.book.pages;
    $id("ptPageTotal").textContent = all.length;
    const recognized = all.filter(p => p.status !== "raw");
    setBusy(true, `加载全书… 0/${recognized.length}`);
    let loaded = 0;
    const queue = recognized.slice();
    async function worker() {
      while (queue.length) {
        const pg = queue.shift();
        try {
          const d = await api(`/api/books/${S.book.id}/pages/${pg.no}`);
          P.pageMap[pg.no] = d;
        } catch (e) {}
        loaded++;
        if (loaded % 10 === 0 || loaded === recognized.length) setBusy(true, `加载全书… ${loaded}/${recognized.length}`);
      }
    }
    await Promise.all(Array.from({ length: Math.min(8, recognized.length || 1) }, worker));
    setBusy(false);
    let acc = 0;
    all.forEach(pg => {
      const d = P.pageMap[pg.no];
      const orig = d ? originalText(d) : "";
      const cnt = stripPunct(orig).length;
      P.meta.push({ no: pg.no, leaf: pg.leaf, raw: !d, orig, charStart: acc, charCount: cnt });
      acc += cnt;
    });
    P.origText = P.meta.map(m => m.orig).join("");
  }

  /* ---------- 服务端标点稿 ---------- */
  async function initDoc() {
    await loadAll();
    let serverDoc = null;
    try { serverDoc = await api(`/api/books/${S.book.id}/punct`); } catch (e) {}
    P.backendExists = !!(serverDoc && serverDoc.exists);
    let initial = null;
    if (serverDoc && serverDoc.exists && !serverDoc.stale &&
        typeof serverDoc.text === "string" &&
        stripPunct(serverDoc.text) === stripPunct(P.origText)) {
      initial = { text: serverDoc.text, breaks: sanitizeBreaks(serverDoc.text, serverDoc.breaks || []) };
      P.backendHash = serverDoc.base_hash;
    } else if (serverDoc && serverDoc.exists && serverDoc.stale) {
      toast("标点稿与当前校字稿不一致：先按当前原文显示，保存会重建标点稿");
    }
    if (!initial) {
      initial = loadDraftText();
      P.pendingMigrate = (initial.text !== P.origText || initial.breaks.length > 0);
    }
    buildText(initial);
    updateStats();
    onPageLoaded(S.pageNo);
    updateParaHighlights();
    $id("saveState").textContent = P.backendExists ? "标点稿（服务端）" : "标点稿（未保存）";
    if (P.pendingMigrate) { P.pendingMigrate = false; persistBackend(false, true); }
  }

  async function persistBackend(showToast, quiet) {
    if (!P.active || !P.textEl) return false;
    if (P.saving) { P.saveQueued = true; return false; }
    P.saving = true;
    const text = P.textEl.innerText.replace(/\r/g, "");
    const breaks = sanitizeBreaks(text, P.breaks);
    if (!quiet) $id("saveState").textContent = "保存中…";
    try {
      const d = await api(`/api/books/${S.book.id}/punct`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, breaks }),
      });
      P.backendExists = true; P.backendHash = d.base_hash;
      $id("saveState").textContent = "已保存";
      if (showToast) toast(`标点稿已保存：${d.chars} 字 · ${d.paragraphs} 段`);
      return true;
    } catch (e) {
      $id("saveState").textContent = "本地已存 · 服务端未保存";
      if (showToast) toast("服务端保存失败：" + e.message);
      return false;
    } finally {
      P.saving = false;
      if (P.saveQueued) { P.saveQueued = false; setTimeout(() => persistBackend(false, true), 120); }
    }
  }

  /* ---------- 草稿 ---------- */
  function draftKey() { return "guwen_punct_" + P.bookId; }
  function loadDraftText() {
    let raw = null;
    try { raw = JSON.parse(localStorage.getItem(draftKey()) || "null"); } catch (e) {}
    if (raw) {
      if (raw.v === 3 && typeof raw.text === "string" && stripPunct(raw.text) === stripPunct(P.origText)) {
        return { text: raw.text, breaks: sanitizeBreaks(raw.text, Array.isArray(raw.breaks) ? raw.breaks : []) };
      }
      if (raw.v === 2 && typeof raw.text === "string" && stripPunct(raw.text) === stripPunct(P.origText)) {
        return migrateV2(raw.text);
      }
      if (raw.v === 1 && raw.pages) {
        let out = "";
        P.meta.forEach(m => {
          if (m.raw) return;
          const t = raw.pages[m.no];
          out += (typeof t === "string" && stripPunct(t) === stripPunct(m.orig)) ? t : m.orig;
        });
        if (stripPunct(out) === stripPunct(P.origText)) return migrateV2(out);
      }
    }
    return { text: P.origText, breaks: [] };
  }
  function saveDraftNow() {
    try {
      if (P.textEl) {
        const text = P.textEl.innerText.replace(/\r/g, "");
        P.breaks = sanitizeBreaks(text, P.breaks);
        localStorage.setItem(draftKey(), JSON.stringify({ v: 3, text, breaks: P.breaks.slice() }));
      }
    } catch (e) {}
  }
  function saveDraftDebounced() {
    $id("saveState").textContent = "保存中…";
    clearTimeout(P.saveTimer);
    P.saveTimer = setTimeout(() => { saveDraftNow(); persistBackend(false, true); }, 500);
  }

  /* ---------- 渲染单块文本 ---------- */
  function buildText(initial) {
    const box = $id("punctText");
    box.innerHTML = "";
    const ed = document.createElement("div");
    ed.className = "ptext"; ed.contentEditable = "plaintext-only"; ed.spellcheck = false;
    const init = initial || loadDraftText();
    const { text, breaks } = init;
    ed.textContent = text;
    box.appendChild(ed);
    P.textEl = ed;
    P.lastText = text;
    P.breaks = breaks;
    ed.addEventListener("input", () => onInput());
    ed.addEventListener("click", onClickText);
    ed.addEventListener("focus", () => { P.currentNo = S.pageNo; });
    ed.addEventListener("beforeinput", e => {
      if (e.inputType === "insertText" && e.data) {
        for (const ch of e.data) if (!isPunctCh(ch)) { e.preventDefault(); hint(); return; }
      }
      if (e.inputType === "insertFromPaste") {
        const t = (e.clipboardData && e.clipboardData.getData("text")) || "";
        if ([...t].some(ch => !isPunctCh(ch))) { e.preventDefault(); hint(); }
      }
    });
  }

  function hint() {
    const now = Date.now();
    if (now - P.lastMsg < 1500) return;
    P.lastMsg = now;
    toast("标点台只加标点/断句；改字请回「校字」");
  }

  /* ---------- 校验 / 撤销 ---------- */
  function onInput(meta) {
    const ed = P.textEl;
    const text = ed.innerText.replace(/\r/g, "");
    if (stripPunct(text) !== stripPunct(P.origText)) {
      const idx = countOrig(ed.innerText.replace(/\r/g, ""), caretOffset(ed) || 0);
      ed.innerText = P.lastText;
      setCaret(ed, offsetOfCharIndex(idx));
      hint();
      return;
    }
    let breaks = remapBreaks(P.lastText, text, P.breaks);
    if (meta && meta.paraBreak && typeof meta.off === "number") addBreak(breaks, meta.off);
    breaks = sanitizeBreaks(text, breaks);
    if (text !== P.lastText || breaks.join(",") !== P.breaks.join(",")) {
      P.hist.push({ before: P.lastText, after: text, beforeBreaks: P.breaks.slice(), afterBreaks: breaks.slice() });
      if (P.hist.length > 40) P.hist.shift();
      P.redo.length = 0;
    }
    P.breaks = breaks;
    P.lastText = text;
    saveDraftDebounced();
    updateStats();
    updateLeafHighlight();
    updateParaHighlights();
  }

  function undo() {
    const h = P.hist.pop();
    if (!h) return toast("没有可撤销的");
    P.redo.push(h);
    P.textEl.innerText = h.before; P.lastText = h.before; P.breaks = (h.beforeBreaks || []).slice();
    saveDraftDebounced(); updateStats(); updateLeafHighlight(); updateParaHighlights();
  }
  function redo() {
    const h = P.redo.pop();
    if (!h) return toast("没有可重做的");
    P.hist.push(h);
    P.textEl.innerText = h.after; P.lastText = h.after; P.breaks = (h.afterBreaks || []).slice();
    saveDraftDebounced(); updateStats(); updateLeafHighlight(); updateParaHighlights();
  }

  /* ---------- 光标 ---------- */
  function caretOffset(ed) {
    const sel = window.getSelection();
    if (!sel.rangeCount) return null;
    if (!ed.contains(sel.anchorNode)) return null;
    const r = document.createRange();
    r.selectNodeContents(ed);
    r.setEnd(sel.anchorNode, sel.anchorOffset);
    return r.toString().length;
  }
  function setCaret(ed, offset) {
    ed.focus();
    const walker = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT, null);
    let acc = 0, node = null;
    while ((node = walker.nextNode())) {
      const len = node.textContent.length;
      if (offset <= acc + len) {
        const r = document.createRange();
        r.setStart(node, Math.max(0, offset - acc));
        r.collapse(true);
        const sel = window.getSelection();
        sel.removeAllRanges(); sel.addRange(r);
        return;
      }
      acc += len;
    }
    const r = document.createRange();
    r.selectNodeContents(ed); r.collapse(false);
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(r);
  }
  function setRangeOffsets(root, range, start, end) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    let acc = 0, node = null, started = false;
    while ((node = walker.nextNode())) {
      const len = node.textContent.length;
      if (!started && start <= acc + len) {
        range.setStart(node, Math.max(0, start - acc));
        started = true;
      }
      if (started && end <= acc + len) {
        range.setEnd(node, Math.max(0, end - acc));
        return true;
      }
      acc += len;
    }
    if (!started) { range.selectNodeContents(root); range.collapse(false); }
    else range.setEnd(root, root.childNodes.length);
    return started;
  }

  /* ---------- 原字 ↔ 文本位置 ---------- */
  function charIndexAtOffset(off) {
    return countOrig(P.textEl.innerText.replace(/\r/g, ""), off);
  }
  function offsetOfCharIndex(idx) {
    const text = P.textEl ? P.textEl.innerText.replace(/\r/g, "") : "";
    let seen = 0, i = 0;
    for (const ch of text) {
      if (seen >= idx) return i;
      if (!isPunctCh(ch)) seen++;
      i += ch.length;
    }
    return text.length;
  }
  function pageAtChar(idx) {
    let found = -1;
    P.meta.forEach(m => {
      if (!m.raw && idx >= m.charStart && idx < m.charStart + m.charCount) found = m;
    });
    if (found) return found;
    // 落在叶尾/空白：取后面最近的已识别叶
    for (const m of P.meta) if (!m.raw && idx < m.charStart) return m;
    const recs = P.meta.filter(m => !m.raw);
    return recs.length ? recs[recs.length - 1] : null;
  }
  function offsetOfPageStart(no) {
    const m = P.meta.find(x => x.no === no);
    return m ? offsetOfCharIndex(m.charStart) : 0;
  }
  function pageBoundaries(text) {
    const want = new Set();
    P.meta.forEach(m => { if (!m.raw) { want.add(m.charStart); want.add(m.charStart + m.charCount); } });
    const out = new Map();
    let seen = 0, off = 0;
    if (want.has(0)) out.set(0, 0);
    for (const ch of text) {
      if (!isPunctCh(ch)) {
        seen++;
        if (want.has(seen) && !out.has(seen)) out.set(seen, off + ch.length);
      }
      off += ch.length;
    }
    if (want.has(seen) && !out.has(seen)) out.set(seen, off);
    return out;
  }
  function pageDoneCount(text, bounds) {
    let done = 0;
    P.meta.forEach(m => {
      if (m.raw) return;
      const s = bounds.get(m.charStart), e = bounds.get(m.charStart + m.charCount);
      if (s != null && e != null && text.slice(s, e) !== m.orig) done++;
    });
    return done;
  }

  /* ---------- 高亮 ---------- */
  function highlightBox(no, box) {
    if (!box || !S.page || S.pageNo !== no) { $id("ptHL").hidden = true; return; }
    const p = S.page, el = $id("ptHL");
    el.hidden = false;
    el.style.left = (box[0] / p.w * 100) + "%";
    el.style.top = (box[1] / p.h * 100) + "%";
    el.style.width = ((box[2] - box[0]) / p.w * 100) + "%";
    el.style.height = ((box[3] - box[1]) / p.h * 100) + "%";
  }
  function updateLeafHighlight(no) {
    if (!P.textEl || !window.CSS || !CSS.highlights || !window.Highlight) return;
    const m = P.meta.find(x => x.no === (no || S.pageNo));
    if (!m || m.raw || !m.charCount) {
      try { CSS.highlights.delete("curpage"); } catch (e) {}
      return;
    }
    const s = offsetOfCharIndex(m.charStart);
    const e = offsetOfCharIndex(m.charStart + m.charCount);
    const range = document.createRange();
    if (!setRangeOffsets(P.textEl, range, s, e)) { try { CSS.highlights.delete("curpage"); } catch (e2) {} return; }
    try { CSS.highlights.set("curpage", new Highlight(range)); } catch (e) {}
  }

  /* ---------- 页同步 ---------- */
  function onPageLoaded(no) {
    if (!P.active) return;
    P.currentNo = no;
    const m = P.meta.find(x => x.no === no);
    if (m) {
      $id("ptLeafTag").textContent = "第 " + m.leaf + " 叶";
      $id("ptPageNo").textContent = no;
    }
    if (!P.textEl) return;
    if (m && !m.raw && m.charCount) {
      let off = P.pendingCaretOff != null ? P.pendingCaretOff : offsetOfPageStart(no);
      P.pendingCaretOff = null;
      setCaret(P.textEl, Math.max(0, Math.min(off, P.textEl.innerText.length)));
      scrollCaretIntoView();
    }
    if (P.pendingHL && P.pendingHL.no === no) highlightBox(no, P.pendingHL.box);
    else $id("ptHL").hidden = true;
    updateLeafHighlight(no);
  }
  function scrollCaretIntoView() {
    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    const r = sel.getRangeAt(0).getBoundingClientRect();
    if (!r || (!r.top && !r.bottom)) return;
    const box = $id("punctText"), br = box.getBoundingClientRect();
    if (r.top < br.top + 30 || r.bottom > br.bottom - 30) {
      box.scrollTop += (r.top - br.top - br.height / 3);
    }
  }

  /* ---------- 点文本 / 点原图 ---------- */
  function boxAtChar(m, ord) {
    const d = P.pageMap[m.no];
    if (!d) return null;
    const flat = flattenChars(d);
    return flat[ord] ? flat[ord].box : null;
  }
  function onClickText() {
    setTimeout(() => {
      if (!P.textEl) return;
      const off = caretOffset(P.textEl);
      if (off == null) return;
      const idx = charIndexAtOffset(off);
      const m = pageAtChar(idx);
      if (!m) return;
      const ord = idx - m.charStart;
      const box = boxAtChar(m, ord);
      P.pendingCaretOff = off;
      P.pendingHL = { no: m.no, box };
      if (S.pageNo !== m.no) switchPage(m.no);
      else { highlightBox(m.no, box); updateLeafHighlight(m.no); }
    }, 0);
  }

  function pickImage(x, y) {
    if (!P.active || !P.textEl) return;
    const d = P.pageMap[S.pageNo];
    if (!d) return;
    const flat = flattenChars(d);
    let hit = null, ord = -1;
    flat.forEach((f, i) => {
      const b = f.box;
      if (hit || !b) return;
      if (x >= b[0] - 6 && x <= b[2] + 6 && y >= b[1] - 6 && y <= b[3] + 6) { hit = f; ord = i; }
    });
    if (!hit) return;
    const m = P.meta.find(x2 => x2.no === S.pageNo);
    if (!m) return;
    const off = offsetOfCharIndex(m.charStart + ord);
    setCaret(P.textEl, off);
    P.pendingHL = { no: S.pageNo, box: hit.box };
    highlightBox(S.pageNo, hit.box);
    updateLeafHighlight(S.pageNo);
    scrollCaretIntoView();
  }

  /* ---------- 插标点 / 换行 ---------- */
  function insertAtCaret(str, paraBreak) {
    let ed = document.activeElement;
    if (!ed || !ed.classList || !ed.classList.contains("ptext")) ed = P.textEl;
    if (!ed) return;
    ed.focus();
    const sel = window.getSelection();
    let range;
    if (!sel.rangeCount || !ed.contains(sel.anchorNode)) {
      range = document.createRange();
      range.selectNodeContents(ed); range.collapse(false);
    } else range = sel.getRangeAt(0);
    const pre = document.createRange();
    pre.selectNodeContents(ed);
    pre.setEnd(range.startContainer, range.startOffset);
    const off = pre.toString().length;
    range.deleteContents();
    const tn = document.createTextNode(str);
    range.insertNode(tn);
    range.setStartAfter(tn); range.collapse(true);
    sel.removeAllRanges(); sel.addRange(range);
    onInput({ off, paraBreak: !!paraBreak });
  }

  /* ---------- 统计 / 待定标点 ---------- */
  const END_PUNCT = /[。，、；：？！…—」』）》]$/;
  function updateStats() {
    if (!P.textEl) return;
    const text = P.textEl.innerText.replace(/\r/g, "");
    const total = P.meta.filter(m => !m.raw).length;
    const bounds = pageBoundaries(text);
    const done = pageDoneCount(text, bounds);
    let pending = 0;
    text.split("\n").forEach(line => {
      const t = line.trim();
      if (!t || t.length < 2) return;
      if (!END_PUNCT.test(t)) pending++;
    });
    const chars = stripPunct(P.origText).length;
    $id("ptStatText").textContent = `已标 ${done}/${total} 叶 · 待定标点 ${pending} 处 · ${chars} 字`;
    $id("ptStats").textContent = `已标 ${done}/${total} 叶 · 待定 ${pending}`;
    $id("ptFoot").textContent = `共 ${P.meta.length} 叶（已识别 ${total}）· 全文连续 · 已标 ${done} 叶 · 待定 ${pending} 处`;
  }

  function nextPending() {
    if (!P.textEl) return;
    const text = P.textEl.innerText.replace(/\r/g, "");
    const caret = caretOffset(P.textEl) || 0;
    const lines = text.split("\n");
    let off = 0;
    for (const line of lines) {
      const end = off + line.length;
      const t = line.trim();
      if (end > caret && t.length >= 2 && !END_PUNCT.test(t)) {
        const idx = countOrig(text, end);
        const m = pageAtChar(idx);
        if (m && S.pageNo !== m.no) { P.pendingCaretOff = end; switchPage(m.no); }
        else { setCaret(P.textEl, end); scrollCaretIntoView(); }
        toast("下一处待定标点：第 " + (m ? m.leaf : "?") + " 叶");
        return;
      }
      off = end + 1;
    }
    toast("没有待定标点了");
  }

  function updateParaHighlights() {
    if (!P.textEl || !window.CSS || !CSS.highlights || !window.Highlight) return;
    if (!P.breaks.length) {
      try { CSS.highlights.delete("parabreak"); } catch (e) {}
      return;
    }
    const hl = new Highlight();
    P.breaks.forEach(b => {
      const r = document.createRange();
      if (setRangeOffsets(P.textEl, r, b, b + 1)) hl.add(r);
    });
    try { CSS.highlights.set("parabreak", hl); } catch (e) {}
  }

  function applyFont() { $id("punctText").style.fontSize = P.font + "px"; }

  /* ---------- 键盘 ---------- */
  function onDocKeydown(e) {
    if (!P.active || S.mode !== "punct") return;
    if (e.isComposing) return;
    const mod = e.ctrlKey || e.metaKey;
    const k = (e.key || "").toLowerCase();
    if (mod) {
      if (k === "s") { e.preventDefault(); saveDraftNow(); persistBackend(true); return; }
      if (k === "z" && !e.shiftKey) { e.preventDefault(); undo(); return; }
      if (k === "y" || (k === "z" && e.shiftKey)) { e.preventDefault(); redo(); return; }
    }
    if (e.altKey && /^[1-9]$/.test(e.key)) {
      const p = PALETTE[+e.key - 1];
      if (p) { e.preventDefault(); insertAtCaret(p); }
      return;
    }
    if (e.key === "Escape") {
      if ($id("modalExport").classList.contains("show")) {
        e.preventDefault();
        $id("modalExport").classList.remove("show");
      }
      return;
    }
    const ed = e.target && e.target.closest ? e.target.closest(".ptext") : null;
    if (!ed) return;
    if (e.key === " " || e.code === "Space") { e.preventDefault(); insertAtCaret(P.activePunct); return; }  // 空格=句读（插入当前标点）
    if (e.key === "Enter") { e.preventDefault(); insertAtCaret("\n", true); return; }                      // 回车=分段（单换行）
    if (e.key === "Tab") { e.preventDefault(); nextPending(); return; }
    if (e.key === "Backspace" || e.key === "Delete") {
      const off = caretOffset(ed);
      if (off == null) return;
      const text = ed.innerText.replace(/\r/g, "");
      const ch = e.key === "Backspace" ? text[off - 1] : text[off];
      if (ch && !isPunctCh(ch)) { e.preventDefault(); hint(); }
      return;
    }
  }
  document.addEventListener("keydown", onDocKeydown, true);
  document.addEventListener("selectionchange", () => {
    if (!P.active) return;
    clearTimeout(P.selTimer);
    P.selTimer = setTimeout(() => {
      if (!P.active || !P.textEl) return;
      const off = caretOffset(P.textEl);
      if (off == null) return;
      const idx = charIndexAtOffset(off);
      const m = pageAtChar(idx);
      if (m) {
        $id("ptLeafTag").textContent = "第 " + m.leaf + " 叶";
        if (P.pendingHL == null) updateLeafHighlight(m.no);
      }
    }, 200);
  });

  /* ---------- 导出（标点稿 → DOCX，无叶码） ---------- */
  function collectText() {
    const text = P.textEl ? exportText() : "";
    const skipped = P.meta.filter(m => m.raw).length;
    return { text: text.replace(/\n+$/, ""), skipped };
  }
  async function exportDocx() {
    if (!S.book) return;
    const { text, skipped } = collectText();
    if (!text.trim()) return toast("还没有可导出的文字");
    const simp = $id("exSimp").checked ? 1 : 0;
    const heading = $id("exHeading").checked ? 1 : 0;
    const font = $id("exFont").value;
    const size = $id("exSize").value;
    $id("modalExport").classList.remove("show");
    setBusy(true, "正在生成 DOCX…");
    saveDraftNow();
    const saved = await persistBackend(false, true);
    try {
      let r = null;
      if (saved) {
        r = await fetch(`/api/books/${S.book.id}/export-punct.docx?simp=${simp}&heading=${heading}` +
          `&font=${encodeURIComponent(font)}&size=${size}`);
      }
      if (!r || r.status === 404) {
        // 兜底：直接按当前编辑器文本生成
        r = await fetch("/api/export-text.docx", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: S.book.title, text, simp, heading, font, size }),
        });
      }
      if (!r.ok) {
        let msg = "导出失败";
        try { msg = (await r.json()).detail || msg; } catch (e) {}
        throw new Error(msg);
      }
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${S.book.title}-整理本${simp ? "-简体" : "-繁体"}.docx`;
      document.body.appendChild(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
      toast("已导出：" + a.download + (skipped ? `（跳过 ${skipped} 个未识别叶）` : ""));
    } catch (e) { toast("导出失败：" + e.message); }
    finally { setBusy(false); }
  }

  /* ---------- 绑定 ---------- */
  function buildPalette() {
    const box = $id("punctPalette");
    if (box.dataset.done) return;
    box.dataset.done = "1";
    PALETTE.forEach(p => {
      const b = document.createElement("span");
      b.className = "pb" + (p === P.activePunct ? " on" : "");
      b.textContent = p;
      b.title = "插入 " + p + "（Alt+" + (PALETTE.indexOf(p) + 1) + "；选中后空格可连续插这个）";
      b.dataset.p = p;
      b.addEventListener("mousedown", e => e.preventDefault());
      b.onclick = () => {
        if (!P.active) return;
        setActivePunct(p);
        insertAtCaret(p);
      };
      box.appendChild(b);
    });
  }
  function setActivePunct(p) {
    P.activePunct = p;
    document.querySelectorAll("#punctPalette .pb").forEach(el => {
      el.classList.toggle("on", el.dataset.p === p);
    });
    const tip = $id("ptCurPunct");
    if (tip) tip.textContent = p;
  }

  function bindUI() {
    // 旧“标点”模式已隐藏；顶栏“校字/句读”由 app.js 的工作台双模式接管
    $id("ptPrev").onclick = () => { if (S.pageNo > 1) switchPage(S.pageNo - 1); };
    $id("ptNext").onclick = () => { if (S.pageNo < S.book.pages.length) switchPage(S.pageNo + 1); };
    $id("ptBreak").onclick = () => insertAtCaret(P.activePunct);
    $id("ptPara").onclick = () => insertAtCaret("\n", true);
    $id("ptUndo").onclick = undo;
    $id("ptRedo").onclick = redo;
    $id("ptPending").onclick = nextPending;
    $id("ptFontMinus").onclick = () => { P.font = Math.max(15, P.font - 1); applyFont(); };
    $id("ptFontPlus").onclick = () => { P.font = Math.min(26, P.font + 1); applyFont(); };
    $id("ptExport").onclick = () => { if (P.active) $id("modalExport").classList.add("show"); };
    $id("exCancel").onclick = () => $id("modalExport").classList.remove("show");
    $id("exGo").onclick = exportDocx;
  }

  /* 暴露给 app.js 的钩子 */
  window.Punct = {
    enter, exit, onPageLoaded, pickImage,
    onOpenBook() {
      if (!P.active) return;
      P.bookId = S.book.id;
      P.meta = []; P.pageMap = {}; P.textEl = null; P.hist = []; P.redo = [];
      P.breaks = []; P.backendExists = false; P.backendHash = null;
      P.saving = false; P.saveQueued = false; P.pendingMigrate = false;
      $id("punctText").innerHTML = "";
      initDoc();
    },
    renderOverlay() {
      if (!P.active) return;
      if (P.pendingHL && P.pendingHL.no === S.pageNo) highlightBox(P.pendingHL.no, P.pendingHL.box);
      else $id("ptHL").hidden = true;
    },
    get active() { return P.active; },
    _P: P,
    _exportText: exportText,
  };

  bindUI();
})();
