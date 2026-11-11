/* 古籍整理工作台 · M4 前端
   导入 → 识别 → 校字（候选/查字/简体/撤销/行字删除/行序/类型）→ 框调整 → 局部重识 → 导出 */
const $ = id => document.getElementById(id);
const escH = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const IMG = bid => `/img/${bid}`;
const TYPE_ORDER = ["正文", "标题", "小注", "其他"];
const S = {
  books: [], book: null, page: null, pageNo: 1, rows: [],
  sel: { r: 0, c: 0 }, charbox: false, clean: false, simp: true,
  zoom: 1, expanded: false,
  edit: "off", mode: "jiao", rowScale: (parseFloat(localStorage.getItem("guwen_rowscale") || "1") || 1),
  dirty: false, saveTimer: null, busy: false, statusMap: {},
  batchRunning: false, batchTimer: null,
  undo: [], redo: [], simpMap: {}, candCtx: null, lookupCtx: null,
  bdrag: null, rowDrag: null, dragMoved: false,
};

/* ---------------- 基础 ---------------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("show"), 1800);
}
function setBusy(on, text) {
  S.busy = on;
  if (text) $("busyText").textContent = text;
  $("busy").classList.toggle("show", on);
}
function setSaved(saved) {
  $("saveState").textContent = saved ? "已保存" : "未保存";
  $("saveState").style.color = saved ? "var(--sub)" : "var(--accent)";
}

/* ---------------- 首页 / 打开书 ---------------- */
async function refreshBooks() {
  S.books = await api("/api/books");
  const box = $("bookList"); box.innerHTML = "";
  $("emptyTip").hidden = S.books.length > 0;
  S.books.forEach(b => {
    const d = document.createElement("div");
    d.className = "bookcard";
    const pct = b.pages ? Math.round(b.done / b.pages * 100) : 0;
    const punct = b.has_punct ? '<span class="punchip">已标点</span>' : "";
    d.innerHTML = `<button class="bookdel" title="从书库删除">×</button>
      <h3>${escH(b.title)}</h3>
      <div class="meta">${b.pages} 叶 · 已识别 ${b.done} · 已校对 ${b.corrected}${punct}<br>${escH(b.source)}</div>
      <div class="bar"><i style="width:${pct}%"></i></div>`;
    d.querySelector(".bookdel").onclick = ev => { ev.stopPropagation(); deleteBook(b); };
    d.onclick = () => openBook(b.id);
    box.appendChild(d);
  });
}

async function deleteBook(b) {
  const msg = `从书库删除《${b.title}》？\n\n会删除这本书的识别稿、校对稿、标点稿和缩略图；原图片文件夹不会被删除。`;
  if (!window.confirm(msg)) return;
  try {
    await api(`/api/books/${b.id}`, { method: "DELETE" });
    try {
      localStorage.removeItem("guwen_undo_" + b.id);
      localStorage.removeItem("guwen_punct_" + b.id);
    } catch (e) {}
    toast(`已删除《${b.title}》`);
    if (S.book && S.book.id === b.id) S.book = null;
    await refreshBooks();
  } catch (e) { toast("删除失败：" + e.message); }
}

async function openBook(bid) {
  S.book = await api(`/api/books/${bid}`);
  const pm = $("punctMode");
  if (pm && pm.dataset.book && pm.dataset.book !== bid) {
    // 句读已按旧书挂载；脚本无法安全热切书，回到同一工作台的校字页重新载入
    window.location.href = "/?book=" + encodeURIComponent(bid);
    return;
  }
  setWorkMode("jiao");
  S.statusMap = {};
  S.book.pages.forEach(p => { S.statusMap[p.no] = p.status; });
  $("home").hidden = true; $("work").hidden = false;
  $("workTitle").textContent = S.book.title;
  $("pageTotal").textContent = S.book.pages.length;
  S.zoom = 1;
  loadHistory();
  renderThumbs();
  await loadPage(S.book.pages[0].no);
  resumeBatch();
  if (window.Punct) Punct.onOpenBook();
}

function renderThumbs() {
  const box = $("thumbs"); box.innerHTML = "";
  S.book.pages.forEach(p => {
    const d = document.createElement("div");
    d.className = "th" + (p.no === S.pageNo ? " cur" : "");
    d.dataset.no = p.no;
    const st = S.statusMap[p.no] || "raw";
    d.innerHTML = `<img loading="lazy" src="/thumb/${S.book.id}/${p.no}?w=220" alt="">
      <span class="no">${p.leaf}</span><span class="dot ${st}"></span>`;
    d.onclick = () => switchPage(p.no);
    box.appendChild(d);
  });
}
function updateThumbStatus(no, status) {
  S.statusMap[no] = status;
  const el = document.querySelector(`.th[data-no="${no}"] .dot`);
  if (el) el.className = "dot " + status;
}

async function switchPage(no) {
  if (no === S.pageNo || S.busy) return;
  await save(); setEdit("off"); closeCand(); closeLookup();
  await loadPage(no);
}

async function loadPage(no) {
  S.pageNo = no; setBusy(true, "加载中…");
  try {
    let d = await api(`/api/books/${S.book.id}/pages/${no}`);
    if (d.status === "raw" && !S.batchRunning && S.mode !== "punct") {
      setBusy(true, "识别中… 首次约 5–8 秒");
      d = await api(`/api/books/${S.book.id}/pages/${no}/recognize`, { method: "POST" });
      updateThumbStatus(no, "ocr");
    } else if (d.status === "raw" && S.mode === "punct") {
      toast("本叶未识别：回校字台识别后再标点");
    } else if (d.status === "raw") {
      toast("整本识别进行中，本叶识别完会自动出字");
    }
    S.page = d; S.rows = d.rows || [];
    S.sel = { r: 0, c: 0 }; S.edit = "off"; syncEditUI();
    S.dirty = false; setSaved(true);
    S.zoom = 1;
    $("pageNo").textContent = no;
    document.querySelectorAll(".th.cur").forEach(e => e.classList.remove("cur"));
    const cur = document.querySelector(`.th[data-no="${no}"]`);
    if (cur) { cur.classList.add("cur"); cur.scrollIntoView({ block: "nearest" }); }
    renderAll(); resetScroll();
    if (S.mode === "punct" && window.Punct) Punct.onPageLoaded(no);
    if (S.simp) ensureSimpMap().then(ch => { if (ch) renderRows(); });
  } catch (e) { toast("加载失败：" + e.message); }
  finally { setBusy(false); }
}

/* ---------------- 视图：滚动容器 / 缩放 ---------------- */
function baseFit() {
  const st = $("stage"), p = S.page;
  const availW = Math.max(120, st.clientWidth - 48);
  const availH = Math.max(120, st.clientHeight - 48);
  const h = Math.min(availH, availW * p.h / p.w);
  return { w: h * p.w / p.h, h };
}
function applyView() {
  if (!S.page) return;
  const b = baseFit(), pw = $("pagewrap");
  pw.style.width = Math.round(b.w * S.zoom) + "px";
  pw.style.height = Math.round(b.h * S.zoom) + "px";
  $("zoomPct").textContent = Math.round(S.zoom * 100) + "%";
  const img = $("pageimg");
  const src = `${IMG(S.book.id)}/${S.pageNo}`;
  if (img.getAttribute("src") !== src) img.src = src;
  renderBoxes();
}
$("pageimg").onload = () => renderBoxes();
function resetScroll() {
  const st = $("stage");
  st.scrollLeft = Math.max(0, (st.scrollWidth - st.clientWidth) / 2);
  st.scrollTop = Math.max(0, (st.scrollHeight - st.clientHeight) / 2);
}
function resetView() { S.zoom = 1; applyView(); resetScroll(); }
function zoomAt(z, clientX, clientY) {
  if (!S.page) return;
  z = Math.max(0.4, Math.min(4, z));
  const st = $("stage"), pw = $("pagewrap");
  const r = st.getBoundingClientRect();
  const ox = clientX === undefined ? r.width / 2 : clientX - r.left;
  const oy = clientY === undefined ? r.height / 2 : clientY - r.top;
  const fx = (st.scrollLeft + ox) / Math.max(1, pw.offsetWidth);
  const fy = (st.scrollTop + oy) / Math.max(1, pw.offsetHeight);
  S.zoom = z; applyView();
  st.scrollLeft = Math.max(0, fx * pw.offsetWidth - ox);
  st.scrollTop = Math.max(0, fy * pw.offsetHeight - oy);
}
function fitWidth() {
  if (!S.page) return;
  const st = $("stage"), b = baseFit();
  S.zoom = Math.max(0.4, Math.min(4, (st.clientWidth - 48) / b.w));
  applyView(); st.scrollLeft = 0; st.scrollTop = 0;
}
$("zoomIn").onclick = () => zoomAt(S.zoom + 0.15);
$("zoomOut").onclick = () => zoomAt(S.zoom - 0.15);
$("zoomFit").onclick = fitWidth;
$("zoomReset").onclick = resetView;

/* ---------------- 渲染 ---------------- */
function updateBatchEntry() {
  const btn = $("tBatch");
  if (!btn) return;
  const hasRaw = !!(S.book && (S.book.pages || []).some(p => p.status === "raw"));
  btn.style.display = hasRaw ? "inline-flex" : "none";
}
function renderAll() { applyView(); renderRows(); renderFoot(); renderRailFoot(); updateBatchEntry(); }

function rectOf(b, p) {
  return `left:${b[0] / p.w * 100}%;top:${b[1] / p.h * 100}%;width:${(b[2] - b[0]) / p.w * 100}%;height:${(b[3] - b[1]) / p.h * 100}%`;
}
function renderBoxes() {
  const layer = $("boxLayer");
  layer.innerHTML = "";
  const p = S.page;
  if (!p) return;
  if (S.mode === "punct") { if (window.Punct) Punct.renderOverlay(); return; }
  layer.classList.toggle("editing", S.edit !== "off");
  if (S.clean && S.edit === "off") return;
  S.rows.forEach((row, ri) => {
    const d = document.createElement("div");
    d.className = "box col" + (ri === S.sel.r ? " cur" : "");
    if (S.edit === "col" && ri === S.sel.r) d.className += " sel";
    d.style.cssText = rectOf(row.box, p);
    d.dataset.kind = "col"; d.dataset.r = ri;
    layer.appendChild(d);
    const showChars = S.charbox || S.edit === "char";
    if (showChars) {
      row.chars.forEach((ch, ci) => {
        const c = document.createElement("div");
        c.className = "box chr" + (S.edit === "char" && ri === S.sel.r && ci === S.sel.c ? " sel" : "");
        c.style.cssText = rectOf(ch.box, p);
        c.dataset.kind = "char"; c.dataset.r = ri; c.dataset.ci = ci;
        layer.appendChild(c);
      });
    }
  });
  if (S.edit !== "off") drawHandles();
}
function drawHandles() {
  const el = $("boxLayer").querySelector(".box.sel");
  if (!el) return;
  ["n", "s", "w", "e", "nw", "ne", "sw", "se"].forEach(dir => {
    const h = document.createElement("div");
    h.className = "h " + dir; h.dataset.dir = dir;
    el.appendChild(h);
  });
}

function renderRows() {
  const box = $("rows"); box.innerHTML = "";
  const p = S.page;
  if (!p) return;
  S.rows.forEach((row, ri) => {
    const el = document.createElement("div");
    el.className = "row" + (ri === S.sel.r ? " active" : "");
    el.dataset.r = ri;
    const hdl = document.createElement("div"); hdl.className = "hdl";
    hdl.innerHTML = `<i class="dots" title="按住拖动调整栏序"></i>
      <span class="tag" title="点击切换类型">${escH(row.type || "正文")}</span>
      <svg class="del" viewBox="0 0 16 16" title="删除这一栏"><path d="M3 5h10M6 5V3h4v2M5 5l.6 8h4.8L11 5" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>`;
    hdl.querySelector(".dots").addEventListener("pointerdown", e => startRowDrag(e, ri));
    hdl.querySelector(".tag").onclick = () => cycleType(ri);
    hdl.querySelector(".del").onclick = () => deleteRow(ri);
    const body = document.createElement("div"); body.className = "body";
    if (!row.chars.length) {
      body.innerHTML = '<span class="empty">空栏 · 可「重识选中栏」或双击加字框</span>';
    } else {
      const SC = Math.max(0.7, Math.min(1.6, S.rowScale || 1));
      const pairs = document.createElement("div"); pairs.className = "pairs";
      row.chars.forEach((ch, ci) => {
        const b = ch.box;
        const TH = Math.round(56 * SC), kk = TH / Math.max(1, b[3] - b[1]);
        const tw = Math.max(10, Math.round((b[2] - b[0]) * kk));
        const pair = document.createElement("div");
        pair.className = "pair"; pair.style.width = (tw + 9) + "px";
        const im = document.createElement("div"); im.className = "im";
        im.style.cssText = `width:${tw}px;height:${TH}px;background-image:url(${IMG(S.book.id)}/${S.pageNo});` +
          `background-size:${p.w * kk}px ${p.h * kk}px;background-position:${-b[0] * kk}px ${-b[1] * kk}px`;
        const chd = document.createElement("span"); chd.className = "ch";
        chd.style.height = Math.round(42 * SC) + "px";
        const cur = ri === S.sel.r && ci === S.sel.c;
        const cls = ch.flag === "red" ? "red" : (ch.flag === "doubt" ? "doubt" : "ok");
        const simp = S.simp ? (S.simpMap[ch.text] || "") : "";
        chd.innerHTML = `<b class="${cls}${cur ? " cur" : ""}">${escH(ch.text || "□")}</b><em>${simp && simp !== ch.text ? escH(simp) : ""}</em>`;
        const bEl = chd.querySelector("b");
        if (bEl) bEl.style.fontSize = Math.round(30 * SC) + "px";
        const emEl = chd.querySelector("em");
        if (emEl) emEl.style.fontSize = Math.max(9, Math.round(11 * SC)) + "px";
        const x = document.createElement("span");
        x.className = "x"; x.textContent = "×"; x.title = "删去这个字";
        x.onclick = ev => { ev.stopPropagation(); deleteChar(ri, ci); };
        pair.appendChild(im); pair.appendChild(chd); pair.appendChild(x);
        pair.onclick = ev => { ev.stopPropagation(); openCand(ri, ci); };
        pairs.appendChild(pair);
      });
      body.appendChild(pairs);
    }
    el.appendChild(hdl); el.appendChild(body);
    box.appendChild(el);
  });
}

function renderFoot() {
  const total = S.rows.reduce((a, r) => a + r.chars.length, 0);
  let red = 0, doubt = 0;
  S.rows.forEach(r => r.chars.forEach(c => { if (c.flag === "red") red++; if (c.flag === "doubt") doubt++; }));
  $("pillRed").textContent = "待核 " + red + (doubt ? " · 存疑 " + doubt : "");
  $("pillRed").className = "pill" + (red ? " hot" : "");
  $("footChars").textContent = `本叶 ${total} 字 · 已确认 ${total - red - doubt}`;
  $("footRed").textContent = red + " 待核";
  $("footBar").style.width = (total ? Math.round((total - red - doubt) / total * 100) : 0) + "%";
  $("footCursor").textContent = `当前：第 ${S.sel.r + 1} 栏第 ${S.sel.c + 1} 字`;
  if (S.page) {
    $("footTitle").textContent = `${S.book.title} · ${S.page.vol ? "卷" + S.page.vol + " · " : ""}第 ${S.page.leaf} 叶`;
    $("workSub").textContent = `${S.page.vol ? "卷" + S.page.vol : ""} · 第 ${S.page.leaf} 叶`;
    $("footSrc").textContent = "本地数据 · app/data";
    $("pillSimp").textContent = "简 提示：" + (S.simp ? "开" : "关");
    $("pillSimp").className = "pill" + (S.simp ? "" : " off");
  }
}
function renderRailFoot() {
  const pages = S.book.pages.length;
  const done = S.book.pages.filter(p => (S.statusMap[p.no] || p.status) !== "raw").length;
  const corr = S.book.pages.filter(p => (S.statusMap[p.no] || p.status) === "corrected").length;
  $("railMeta").textContent = `${S.book.volumes.join("/")} · ${pages} 叶`;
  $("railTitle").textContent = S.book.title;
  $("railProg").textContent = `已识别 ${done}/${pages} · 已校对 ${corr}`;
  $("railBar").style.width = Math.round(done / pages * 100) + "%";
}

/* ---------------- 撤销 / 重做（全书级，跨页 + 持久化） ---------------- */
function snap() { return JSON.stringify({ rows: S.rows, sel: S.sel }); }
function restore(str) { const d = JSON.parse(str); S.rows = d.rows; S.sel = d.sel || { r: 0, c: 0 }; }
function histKey() { return S.book ? "guwen_undo_" + S.book.id : null; }
let histTimer = null;
let undoBusy = false;

function persistHistory() {
  clearTimeout(histTimer);
  histTimer = setTimeout(() => {
    const k = histKey();
    if (!k) return;
    try {
      localStorage.setItem(k, JSON.stringify({
        undo: S.undo.slice(-30), redo: S.redo.slice(-30),
      }));
    } catch (e) {}
  }, 300);
}
function loadHistory() {
  S.undo = []; S.redo = [];
  const k = histKey();
  if (!k) { updateUndoUI(); return; }
  try {
    const d = JSON.parse(localStorage.getItem(k) || "null");
    const ok = e => e && typeof e.page === "number" && typeof e.state === "string";
    if (d && Array.isArray(d.undo)) S.undo = d.undo.filter(ok).slice(-30);
    if (d && Array.isArray(d.redo)) S.redo = d.redo.filter(ok).slice(-30);
  } catch (e) {}
  updateUndoUI();
}
function clearHistory() {
  S.undo = []; S.redo = [];
  updateUndoUI();
  const k = histKey();
  if (k) { try { localStorage.removeItem(k); } catch (e) {} }
}

function pushUndo() {
  S.undo.push({ page: S.pageNo, state: snap() });
  if (S.undo.length > 30) S.undo.shift();
  S.redo.length = 0;
  updateUndoUI();
  persistHistory();
}
function updateUndoUI() {
  $("tUndo").classList.toggle("disabled", !S.undo.length);
  $("tRedo").classList.toggle("disabled", !S.redo.length);
}

async function undo() {
  if (undoBusy) return;
  if (!S.undo.length) return toast("没有可撤销的操作");
  const entry = S.undo.pop();
  undoBusy = true;
  try {
    const crossPage = entry.page !== S.pageNo;
    if (crossPage) {
      await save();
      await loadPage(entry.page);
    } else if (S.dirty) {
      await save();
    }
    S.redo.push({ page: S.pageNo, state: snap() });
    restore(entry.state);
    S.edit = "off"; syncEditUI();
    markDirty(); renderAll(); updateUndoUI(); persistHistory();
    toast(crossPage ? `已撤销（第 ${entry.page} 页的修改）` : "已撤销");
  } catch (e) {
    S.undo.push(entry);
    toast("撤销失败：" + e.message);
  } finally { undoBusy = false; }
}

async function redo() {
  if (undoBusy) return;
  if (!S.redo.length) return toast("没有可重做的操作");
  const entry = S.redo.pop();
  undoBusy = true;
  try {
    const crossPage = entry.page !== S.pageNo;
    if (crossPage) {
      await save();
      await loadPage(entry.page);
    } else if (S.dirty) {
      await save();
    }
    S.undo.push({ page: S.pageNo, state: snap() });
    if (S.undo.length > 30) S.undo.shift();
    restore(entry.state);
    S.edit = "off"; syncEditUI();
    markDirty(); renderAll(); updateUndoUI(); persistHistory();
    toast(crossPage ? `已重做（第 ${entry.page} 页的修改）` : "已重做");
  } catch (e) {
    S.redo.push(entry);
    toast("重做失败：" + e.message);
  } finally { undoBusy = false; }
}
$("tUndo").onclick = undo; $("tRedo").onclick = redo;

/* ---------------- 选字 / 编辑 ---------------- */
function selectChar(r, c) {
  if (!S.rows[r] || !S.rows[r].chars[c]) return;
  S.sel = { r, c };
  renderRows(); renderBoxes(); renderFoot(); syncEditUI();
  if (S.edit === "off") focusGhost();
}
function move(dr, dc) {
  if (!S.rows.length) return;
  let r = S.sel.r + dr, c = S.sel.c;
  if (dr !== 0) { r = Math.max(0, Math.min(S.rows.length - 1, r)); c = Math.min(c, S.rows[r].chars.length - 1); }
  if (dc !== 0) {
    c += dc;
    if (c < 0) { if (r > 0) { r--; c = S.rows[r].chars.length - 1; } else c = 0; }
    else if (c >= S.rows[r].chars.length) { if (r < S.rows.length - 1) { r++; c = 0; } else c = S.rows[r].chars.length - 1; }
  }
  selectChar(r, Math.max(0, c));
}
function rebuildText(row) { row.text = row.chars.map(c => c.text || "").join(""); }
function confirmChar() {
  const ch = S.rows[S.sel.r] && S.rows[S.sel.r].chars[S.sel.c];
  if (!ch) return;
  pushUndo(); ch.flag = "ok"; markDirty(); renderRows(); renderBoxes(); renderFoot(); move(0, 1);
}
function setChar(text) {
  const ch = S.rows[S.sel.r] && S.rows[S.sel.r].chars[S.sel.c];
  if (!ch) return;
  pushUndo(); ch.text = text; ch.flag = "ok";
  markDirty(); renderRows(); renderFoot();
}
function nextRed() {
  const list = [];
  S.rows.forEach((r, ri) => r.chars.forEach((c, ci) => { if (c.flag === "red") list.push([ri, ci]); }));
  if (!list.length) return toast("本叶没有待核字了");
  const cur = list.findIndex(([r, c]) => r === S.sel.r && c === S.sel.c);
  const [r, c] = list[(cur + 1) % list.length];
  selectChar(r, c);
}
function deleteChar(ri, ci) {
  const row = S.rows[ri];
  if (!row || !row.chars[ci]) return;
  pushUndo();
  row.chars.splice(ci, 1); rebuildText(row);
  S.sel = { r: ri, c: Math.max(0, Math.min(ci, row.chars.length - 1)) };
  markDirty(); renderRows(); renderBoxes(); renderFoot(); closeCand();
  toast("已删去这个字（Ctrl+Z 可撤销）");
}
function deleteRow(ri) {
  if (S.rows.length <= 1) return toast("至少要保留一栏");
  pushUndo();
  S.rows.splice(ri, 1);
  S.sel = { r: Math.min(ri, S.rows.length - 1), c: 0 };
  S.edit = "off"; syncEditUI();
  markDirty(); renderAll(); toast("已删除这一栏（Ctrl+Z 可撤销）");
}
function cycleType(ri) {
  const row = S.rows[ri]; if (!row) return;
  pushUndo();
  const i = TYPE_ORDER.indexOf(row.type || "正文");
  row.type = TYPE_ORDER[(i + 1) % TYPE_ORDER.length];
  markDirty(); renderRows(); renderFoot(); toast("类型：" + row.type);
}

/* 行序拖动 */
function startRowDrag(e, ri) {
  if (S.edit !== "off") return;
  e.preventDefault();
  const rows = [...document.querySelectorAll("#rows .row")];
  S.rowDrag = { from: ri, rects: rows.map(el => el.getBoundingClientRect()) };
  rows[ri].classList.add("dragging");
  focusGhost();
}
window.addEventListener("pointermove", e => {
  if (!S.rowDrag) return;
  const { rects } = S.rowDrag;
  let target = S.rowDrag.from;
  for (let i = 0; i < rects.length; i++) {
    if (e.clientY >= rects[i].top && e.clientY <= rects[i].bottom) { target = i; break; }
  }
  document.querySelectorAll("#rows .row").forEach((el, i) => el.classList.toggle("dragging", i === target));
  S.rowDrag.target = target;
});
window.addEventListener("pointerup", () => {
  if (!S.rowDrag) return;
  const { from, target } = S.rowDrag;
  S.rowDrag = null;
  document.querySelectorAll("#rows .row").forEach(el => el.classList.remove("dragging"));
  if (target === undefined || target === from) return;
  pushUndo();
  const [row] = S.rows.splice(from, 1);
  S.rows.splice(target, 0, row);
  S.sel = { r: target, c: 0 };
  markDirty(); renderAll(); toast("已调整栏序（Ctrl+Z 可撤销）");
});

/* ---------------- 保存 ---------------- */
function markDirty() {
  S.dirty = true; setSaved(false);
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(() => save(), 1400);
}
async function save() {
  if (!S.dirty || !S.book || !S.page) return;
  clearTimeout(S.saveTimer);
  try {
    await api(`/api/books/${S.book.id}/pages/${S.pageNo}/save`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows: S.rows, w: S.page.w, h: S.page.h }) });
    S.dirty = false; setSaved(true);
    updateThumbStatus(S.pageNo, "corrected"); renderRailFoot();
  } catch (e) { toast("保存失败：" + e.message); }
}

/* ---------------- 候选字 ---------------- */
async function openCand(r, c) {
  if (S.edit !== "off") return;
  const row = S.rows[r]; if (!row || !row.chars[c]) return;
  selectChar(r, c);
  S.candCtx = { r, c, list: [] };
  const ch = row.chars[c].text || "□";
  $("candChar").textContent = ch;
  $("candList").innerHTML = '<span style="font-family:sans-serif;font-size:11px;color:var(--sub)">加载候选…</span>';
  const pair = document.querySelectorAll("#rows .row")[r].querySelectorAll(".pair")[c];
  const rc = pair.getBoundingClientRect();
  let x = rc.left - 440; if (x < 12) x = Math.min(window.innerWidth - 440, rc.right + 12);
  const y = Math.min(window.innerHeight - 230, Math.max(70, rc.top - 20));
  $("cand").style.left = x + "px"; $("cand").style.top = y + "px";
  $("cand").classList.add("show");
  try {
    const d = await api(`/api/char/cands?ch=${encodeURIComponent(ch)}&n=20`);
    if (!S.candCtx || S.candCtx.r !== r || S.candCtx.c !== c) return;
    S.candCtx.list = d.cands;
    $("candList").innerHTML = d.cands.length
      ? d.cands.map((x2, i) => `<span data-i="${i}" title="${escH(x2.why)}">${escH(x2.text)}<i>${i + 1}</i></span>`).join("")
      : '<span style="font-family:sans-serif;font-size:11px;color:var(--sub)">无候选（字库未收录），可直接手输</span>';
  } catch (e) {
    $("candList").innerHTML = '<span style="font-family:sans-serif;font-size:11px">候选取不到</span>';
  }
}
function closeCand() { $("cand").classList.remove("show"); S.candCtx = null; }
function applyCand(ch) {
  if (!S.candCtx) return;
  const { r, c } = S.candCtx;
  pushUndo();
  const row = S.rows[r]; row.chars[c].text = ch; row.chars[c].flag = "ok"; rebuildText(row);
  markDirty(); renderRows(); renderBoxes(); renderFoot(); closeCand(); toast(`已替换为「${ch}」`);
  if (S.simp) ensureSimpMap().then(changed => { if (changed) renderRows(); });
}
$("candList").onclick = e => {
  const t = e.target.closest("span[data-i]"); if (!t || !S.candCtx) return;
  const item = S.candCtx.list[+t.dataset.i]; if (item) applyCand(item.text);
};
$("candClose").onclick = closeCand;
$("candDoubt").onclick = () => {
  if (!S.candCtx) return;
  const ch = S.rows[S.candCtx.r].chars[S.candCtx.c];
  pushUndo(); ch.flag = ch.flag === "doubt" ? "ok" : "doubt";
  markDirty(); renderRows(); renderFoot(); closeCand(); toast(ch.flag === "doubt" ? "已标为存疑" : "已取消存疑");
};
function insertChar(before) {
  if (!S.candCtx) return;
  const { r, c } = S.candCtx;
  const row = S.rows[r]; const at = before ? c : c + 1;
  pushUndo();
  row.chars.splice(at, 0, { box: row.chars[c].box.slice(), text: "□", score: 0, flag: "red" });
  rebuildText(row); S.sel = { r, c: at };
  markDirty(); renderRows(); renderBoxes(); renderFoot(); closeCand();
  toast("已插入占位字「□」，直接打字替换");
}
$("candPrev").onclick = () => insertChar(true);
$("candNext").onclick = () => insertChar(false);
$("candDel").onclick = () => { if (S.candCtx) deleteChar(S.candCtx.r, S.candCtx.c); };
$("candDict").onclick = () => { if (S.candCtx) openLookup(S.rows[S.candCtx.r].chars[S.candCtx.c].text); };
document.addEventListener("click", e => {
  if (!e.target.closest("#cand") && !e.target.closest(".pair")) closeCand();
  if (!e.target.closest("#reco")) { $("reco").classList.remove("show"); S.recoCtx = null; }
});

/* ---------------- 查字 ---------------- */
function updateLkMode() {
  const mode = $("lkMode").value;
  $("lkStrokes").style.display = mode === "radical" ? "" : "none";
  $("lkQ").placeholder = mode === "pinyin" ? "拼音 li / lǐ / li3"
    : mode === "radical" ? "部首字，如 口 / 水"
    : mode === "strokes" ? "总笔画数，如 11"
    : "要查的文本，如 敘";
}
$("lkMode").onchange = updateLkMode;
function openLookup(ch) {
  S.lookupCtx = { r: S.sel.r, c: S.sel.c };
  $("modalLookup").classList.add("show");
  updateLkMode();
  if (ch) {
    $("lkMode").value = "text"; $("lkQ").value = ch; updateLkMode(); doLookup();
  } else {
    $("lkMode").value = "pinyin"; $("lkQ").value = ""; updateLkMode(); $("lkQ").focus();
  }
}
function closeLookup() { $("modalLookup").classList.remove("show"); }
$("lkClose").onclick = closeLookup;
async function doLookup() {
  const mode = $("lkMode").value, q = $("lkQ").value.trim();
  const strokes = $("lkStrokes").value.trim();
  $("lkRes").innerHTML = "";
  $("lkInfo").textContent = "查询中…";
  try {
    const url = `/api/char/lookup?mode=${encodeURIComponent(mode)}&q=${encodeURIComponent(q)}&n=120` +
      (mode === "radical" && strokes ? `&strokes=${encodeURIComponent(strokes)}` : "");
    const d = await api(url);
    if (mode === "text" && d.items.length === 1) {
      const it = d.items[0];
      $("lkInfo").textContent = it.pinyin && it.pinyin.length
        ? `${escH(it.text)} · 拼音 ${escH(it.pinyin.join(" / "))} · 部首 ${escH(it.radical || "?")} · 笔画 ${escH(it.strokes || "?")}`
        : `${escH(it.text)} · 未收录`;
    } else {
      $("lkInfo").textContent = `共 ${d.total} 个结果` + (d.total > d.items.length ? `，显示前 ${d.items.length} 个` : "");
    }
    $("lkRes").innerHTML = d.items.map(it => {
      const py = (it.pinyin && it.pinyin[0]) || "";
      return `<div class="lc" data-ch="${escH(it.text)}" title="${escH((it.pinyin || []).join(" / "))} · 部首 ${escH(it.radical || "?")} · ${escH(it.strokes || "?")} 画"><b>${escH(it.text)}</b><i>${escH(py)}</i></div>`;
    }).join("");
  } catch (e) {
    $("lkInfo").textContent = "查询失败：" + e.message;
  }
}
$("lkGo").onclick = doLookup;
$("lkQ").addEventListener("keydown", e => { if (e.key === "Enter") { e.stopPropagation(); doLookup(); } });
$("lkStrokes").addEventListener("keydown", e => { if (e.key === "Enter") { e.stopPropagation(); doLookup(); } });
$("lkRes").onclick = e => {
  const el = e.target.closest(".lc"); if (!el) return;
  const ch = el.dataset.ch;
  const ctx = S.lookupCtx;
  if (ctx && S.rows[ctx.r] && S.rows[ctx.r].chars[ctx.c]) {
    pushUndo();
    const row = S.rows[ctx.r]; row.chars[ctx.c].text = ch; row.chars[ctx.c].flag = "ok"; rebuildText(row);
    markDirty(); renderRows(); renderBoxes(); renderFoot();
    toast(`已替换为「${ch}」`);
    if (S.simp) ensureSimpMap().then(changed => { if (changed) renderRows(); });
  }
  closeLookup();
};

/* ---------------- 简体提示 ---------------- */
async function ensureSimpMap() {
  const chars = [...new Set(S.rows.flatMap(r => r.chars.map(c => c.text)).filter(Boolean))];
  const missing = chars.filter(ch => S.simpMap[ch] === undefined);
  if (!missing.length) return false;
  try {
    const d = await api("/api/convert/chars", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chars: missing, dir: "t2s" }) });
    missing.forEach((ch, i) => { S.simpMap[ch] = d.items[i] || ch; });
    return true;
  } catch (e) { return false; }
}
$("pillSimp").onclick = () => {
  S.simp = !S.simp;
  renderFoot(); renderRows();
  if (S.simp) ensureSimpMap().then(ch => { if (ch) renderRows(); });
};

/* ---------------- 框调整 ---------------- */
function setEdit(mode) {
  S.edit = (S.edit === mode) ? "off" : mode;
  if (S.edit === "col") {
    if (S.rows.length) S.sel = { r: Math.min(S.sel.r, S.rows.length - 1), c: 0 };
  } else if (S.edit === "char") {
    const r = Math.min(S.sel.r, Math.max(0, S.rows.length - 1));
    S.sel = { r, c: Math.min(S.sel.c, Math.max(0, (S.rows[r] ? S.rows[r].chars.length - 1 : 0))) };
  }
  syncEditUI(); renderBoxes(); renderRows(); renderFoot();
  if (S.edit === "off") focusGhost(); else { try { ghost.blur(); } catch (e) {} closeCand(); }
}
function syncEditUI() {
  $("mCol").classList.toggle("on", S.edit === "col");
  $("mChar").classList.toggle("on", S.edit === "char");
  $("editBar").classList.toggle("show", S.edit !== "off");
  $("editHint").textContent = S.edit === "col"
    ? "调栏框：拖动移动 · 拖边缩放 · 双击空白加栏 · Del 删框"
    : S.edit === "char" ? "调字框：拖动移动 · 拖边缩放 · 双击栏内加字框 · Del 删框" : "";
}
function getSelBox() {
  if (!S.rows.length) return null;
  const row = S.rows[S.sel.r]; if (!row) return null;
  if (S.edit === "char" && row.chars[S.sel.c]) return row.chars[S.sel.c].box;
  return row.box;
}
function setSelBox(b) {
  const row = S.rows[S.sel.r]; if (!row) return;
  if (S.edit === "char" && row.chars[S.sel.c]) row.chars[S.sel.c].box = b;
  else row.box = b;
}
function deleteSel() {
  if (S.edit === "off" || !S.rows.length) return;
  if (S.edit === "col") { deleteRow(S.sel.r); setEdit("off"); return; }
  deleteChar(S.sel.r, S.sel.c);
}
$("mCol").onclick = () => setEdit("col");
$("mChar").onclick = () => setEdit("char");

$("boxLayer").addEventListener("pointerdown", e => {
  if (S.edit === "off") return;
  const el = e.target.closest(".box");
  if (!el) return;
  if (el.dataset.kind !== S.edit) return;
  const r = +el.dataset.r, ci = el.dataset.ci !== undefined ? +el.dataset.ci : 0;
  S.sel = { r, c: S.edit === "char" ? ci : 0 };
  const hEl = e.target.closest(".h");
  const p = S.page, pr = $("pagewrap").getBoundingClientRect();
  S.bdrag = { dir: hEl ? hEl.dataset.dir : "move", x: e.clientX, y: e.clientY,
              sx: p.w / pr.width, sy: p.h / pr.height, box: getSelBox().slice(), before: snap() };
  e.preventDefault(); e.stopPropagation();
  renderBoxes(); renderRows(); renderFoot(); syncEditUI();
});
window.addEventListener("pointermove", e => {
  if (!S.bdrag) return;
  const dx = (e.clientX - S.bdrag.x) * S.bdrag.sx, dy = (e.clientY - S.bdrag.y) * S.bdrag.sy;
  const b = S.bdrag.box.slice(), d = S.bdrag.dir, MIN = 26;
  if (d === "move") { b[0] += dx; b[1] += dy; b[2] += dx; b[3] += dy; }
  else {
    if (d.includes("w")) b[0] = Math.min(b[0] + dx, b[2] - MIN);
    if (d.includes("e")) b[2] = Math.max(b[2] + dx, b[0] + MIN);
    if (d.includes("n")) b[1] = Math.min(b[1] + dy, b[3] - MIN);
    if (d.includes("s")) b[3] = Math.max(b[3] + dy, b[1] + MIN);
  }
  setSelBox(b.map(Math.round));
  renderBoxes();
});
window.addEventListener("pointerup", () => {
  if (!S.bdrag) return;
  const before = S.bdrag.before;
  S.bdrag = null;
  if (JSON.stringify({ rows: S.rows, sel: S.sel }) !== before) {
    S.undo.push({ page: S.pageNo, state: before });
    if (S.undo.length > 30) S.undo.shift();
    S.redo.length = 0; updateUndoUI(); persistHistory();
  }
  markDirty(); renderBoxes(); renderRows(); renderFoot();
});
$("stage").addEventListener("dblclick", e => {
  if (e.target.closest(".editbar") || e.target.closest(".zoombar")) return;
  if (S.edit === "off") { resetView(); return; }
  const pr = $("pagewrap").getBoundingClientRect();
  if (e.clientX < pr.left || e.clientX > pr.right || e.clientY < pr.top || e.clientY > pr.bottom) return;
  const p = S.page;
  const x = (e.clientX - pr.left) / pr.width * p.w, y = (e.clientY - pr.top) / pr.height * p.h;
  pushUndo();
  if (S.edit === "col") {
    S.rows.push({ box: [Math.round(x - 65), Math.round(Math.max(40, y - 520)), Math.round(x + 65), Math.round(Math.min(p.h - 40, y + 520))],
                  text: "", score: 0, type: "正文", chars: [] });
    S.sel = { r: S.rows.length - 1, c: 0 };
    toast("已加一栏，点「重识选中栏」识别它");
  } else {
    let ri = S.rows.findIndex(row => x >= row.box[0] && x <= row.box[2] && y >= row.box[1] && y <= row.box[3]);
    if (ri < 0) ri = S.sel.r;
    const row = S.rows[ri];
    row.chars.push({ box: [Math.round(x - 45), Math.round(y - 120), Math.round(x + 45), Math.round(y + 120)],
                     text: "□", score: 0, flag: "red" });
    rebuildText(row); S.sel = { r: ri, c: row.chars.length - 1 };
    toast("已加字框（占位「□」）");
  }
  markDirty(); renderBoxes(); renderRows(); renderFoot();
});
$("mReset").onclick = async () => {
  if (S.busy) return;
  if (!window.confirm("恢复自动框会按自动检测重识本页，当前校对文本会被替换。继续？")) return;
  setEdit("off"); await doRecognizePage();
};

/* ---------------- 识别 ---------------- */
async function doRecognizePage() {
  if (S.busy) return;
  if (S.batchRunning) return toast("整本识别进行中，请先停止");
  await save(); pushUndo();
  setBusy(true, "重新识别中… 约 3–5 秒");
  try {
    const d = await api(`/api/books/${S.book.id}/pages/${S.pageNo}/recognize`, { method: "POST" });
    S.page = d; S.rows = d.rows; S.sel = { r: 0, c: 0 }; S.edit = "off"; syncEditUI();
    S.dirty = false; setSaved(true); updateThumbStatus(S.pageNo, "ocr");
    renderAll();
    if (S.simp) { await ensureSimpMap(); renderRows(); }
    toast(`识别完成：${d.stats.chars} 字 · 待核 ${d.stats.red}`);
  } catch (e) { toast("识别失败：" + e.message); }
  finally { setBusy(false); }
}
async function reocrRow(r) {
  const row = S.rows[r];
  if (!row || S.busy) return;
  if (S.batchRunning) return toast("整本识别进行中，请先停止");
  pushUndo(); setBusy(true, "重识这一栏…");
  try {
    const d = await api(`/api/books/${S.book.id}/pages/${S.pageNo}/recognize-row`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ box: row.box, chars: row.chars.map(c => c.box) }),
    });
    S.rows[r] = d.row; markDirty(); renderRows(); renderBoxes(); renderFoot();
    if (S.simp) { await ensureSimpMap(); renderRows(); }
    toast("本栏重识完成");
  } catch (e) { toast("重识失败：" + e.message); }
  finally { setBusy(false); }
}
let recoCtx = null;
async function reocrChar(r, c) {
  const row = S.rows[r];
  if (!row || !row.chars[c] || S.busy) return;
  if (S.batchRunning) return toast("整本识别进行中，请先停止");
  pushUndo(); setBusy(true, "单字重新识别（整栏上下文）…");
  try {
    const d = await api(`/api/books/${S.book.id}/pages/${S.pageNo}/recognize-row`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ box: row.box, chars: row.chars.map(ch => ch.box) }),
    });
    const newRow = d.row;
    const oldCh = row.chars[c] ? row.chars[c].text : "";
    const newCh = newRow.chars[c] ? newRow.chars[c].text : "";
    showReco(r, c, oldCh, newCh, newRow.score, newRow);
  } catch (e) { toast("重识失败：" + e.message); }
  finally { setBusy(false); }
}
function showReco(r, c, oldCh, newCh, score, newRow) {
  recoCtx = { r, c, newRow };
  $("recoPos").textContent = `第 ${r + 1} 栏 · 第 ${c + 1} 字`;
  $("recoOld").textContent = oldCh || "□";
  $("recoNew").textContent = newCh || "□";
  $("recoScore").textContent = `可信度 ${(score || 0).toFixed(2)}`;
  const rows = document.querySelectorAll(".row");
  const pair = rows[r] ? rows[r].querySelectorAll(".pair")[c] : null;
  let x = 420, y = 260;
  if (pair) {
    const rc = pair.getBoundingClientRect();
    x = rc.left - 330; if (x < 12) x = Math.min(window.innerWidth - 330, rc.right + 10);
    y = Math.min(window.innerHeight - 200, Math.max(80, rc.top - 20));
  }
  $("reco").style.left = x + "px"; $("reco").style.top = y + "px";
  $("reco").classList.add("show");
}
$("recoClose").onclick = $("recoCancel").onclick = () => { $("reco").classList.remove("show"); recoCtx = null; };
$("recoApply").onclick = () => {
  if (!recoCtx) return;
  const { r, c, newRow } = recoCtx;
  if (newRow) {
    pushUndo();
    S.rows[r] = newRow;
    if (S.rows[r].chars[c]) S.rows[r].chars[c].flag = "ok";
  }
  $("reco").classList.remove("show"); recoCtx = null;
  markDirty(); renderRows(); renderBoxes(); renderFoot(); toast("已采用识别结果");
};
$("ebReco").onclick = () => { if (S.edit === "char") reocrChar(S.sel.r, S.sel.c); else reocrRow(S.sel.r); };
$("ebManual").onclick = async () => {
  if (S.busy || !S.rows.length) return;
  if (S.batchRunning) return toast("整本识别进行中，请先停止");
  if (!window.confirm("按当前框重识整页：会用当前框结构重新识别并替换本页文字，继续？")) return;
  pushUndo(); setBusy(true, "按当前框重识整页…");
  try {
    const d = await api(`/api/books/${S.book.id}/pages/${S.pageNo}/recognize-manual`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rows: S.rows }) });
    S.rows = d.rows; S.dirty = true; setSaved(false); await save();
    renderRows(); renderBoxes(); renderFoot();
    if (S.simp) { await ensureSimpMap(); renderRows(); }
    toast(`重识完成：${d.stats.chars} 字 · 待核 ${d.stats.red}`);
  } catch (e) { toast("重识失败：" + e.message); }
  finally { setBusy(false); }
};
$("ebDel").onclick = deleteSel;
$("ebDone").onclick = () => setEdit("off");
$("tReco").onclick = doRecognizePage;

/* ---------------- 整本识别（进度 / 停止） ---------------- */
async function startBatch() {
  if (S.batchRunning) return toast("整本识别进行中");
  if (S.busy) return toast("请等当前操作完成");
  await save();
  try {
    const r = await api(`/api/books/${S.book.id}/batch-recognize`, { method: "POST" });
    S.batchRunning = true;
    $("batchBar").hidden = false;
    $("batchFill").style.width = "0%";
    $("batchText").textContent = "整本识别中…";
    $("batchCount").textContent = `0 / ${r.total}`;
    toast(`开始识别整本：${r.total} 叶（约 ${Math.max(1, Math.round(r.total * 3.2 / 60))} 分钟，可随时停）`);
    pollBatch();
  } catch (e) { toast("无法开始：" + e.message); }
}
function stopBatchPoll() {
  clearTimeout(S.batchTimer); S.batchTimer = null; S.batchRunning = false;
}
async function resumeBatch() {
  if (!S.book) return;
  try {
    const st = await api(`/api/books/${S.book.id}/batch-status`);
    if (st.running && st.bid === S.book.id) {
      S.batchRunning = true;
      $("batchBar").hidden = false;
      updateBatchUI(st);
      pollBatch();
    }
  } catch (e) {}
}
async function pollBatch() {
  clearTimeout(S.batchTimer);
  try {
    const st = await api(`/api/books/${S.book.id}/batch-status`);
    if (st.bid && st.bid !== S.book.id) {
      stopBatchPoll(); $("batchBar").hidden = true; return;
    }
    updateBatchUI(st);
    if (st.running) S.batchTimer = setTimeout(pollBatch, 800);
    else await finishBatch(st);
  } catch (e) {
    stopBatchPoll(); $("batchBar").hidden = true;
    toast("批量识别状态获取失败：" + e.message);
  }
}
function updateBatchUI(st) {
  const total = st.total || 0, done = st.done || 0;
  $("batchFill").style.width = (total ? Math.round(done / total * 100) : 0) + "%";
  $("batchText").textContent = st.running
    ? `整本识别中：正在第 ${st.current || "-"} 叶${st.current_leaf ? "（书叶 " + st.current_leaf + "）" : ""}`
    : "整本识别结束";
  $("batchCount").textContent = `${done} / ${total} · ${st.chars || 0} 字`;
  (st.pages || []).forEach(no => updateThumbStatus(no, "ocr"));
}
async function finishBatch(st) {
  stopBatchPoll();
  $("batchBar").hidden = true;
  const err = (st.errors || []).length;
  toast(`整本识别${st.cancelled ? "已停止" : "完成"}：${st.done}/${st.total} 叶，用时 ${Math.round(st.elapsed || 0)} 秒${err ? `，失败 ${err} 叶` : ""}`);
  try {
    S.book = await api(`/api/books/${S.book.id}`);
    S.statusMap = {};
    S.book.pages.forEach(p => { S.statusMap[p.no] = p.status; });
    renderThumbs(); renderRailFoot(); renderFoot(); updateBatchEntry();
    // 当前页如果是刚被批量识别出来的空页，自动刷新出文字
    if (!S.dirty && st.pages && st.pages.includes(S.pageNo) && !S.rows.length) await loadPage(S.pageNo);
    clearHistory();   // 批量识别改动多页，旧撤销快照作废
  } catch (e) {}
}
$("tBatch").onclick = startBatch;
$("batchCancelBtn").onclick = async () => {
  try {
    await api(`/api/books/${S.book.id}/batch-cancel`, { method: "POST" });
    toast("正在停止：当前叶识别完就停");
  } catch (e) { toast("停止失败：" + e.message); }
};

/* ---------------- 导出已移到标点页（punct.js） ---------------- */

$("tPrev").onclick = () => { if (S.pageNo > 1) switchPage(S.pageNo - 1); };
$("tNext").onclick = () => { if (S.pageNo < S.book.pages.length) switchPage(S.pageNo + 1); };
$("tCharBox").onclick = () => { S.charbox = !S.charbox; $("tCharBox").classList.toggle("on", S.charbox); renderBoxes(); };
$("tClean").onclick = () => { S.clean = !S.clean; $("tClean").classList.toggle("on", S.clean); renderBoxes(); };
function setRowScale(v) {
  S.rowScale = Math.max(0.7, Math.min(1.6, Math.round(v * 10) / 10));
  try { localStorage.setItem("guwen_rowscale", String(S.rowScale)); } catch (e) {}
  renderRows();
}
$("tFontMinus").onclick = () => setRowScale(S.rowScale - 0.1);
$("tFontPlus").onclick = () => setRowScale(S.rowScale + 0.1);

/* ---------------- 右栏展开 / 拖宽 ---------------- */
function applyLayout() {
  const m = $("main");
  m.style.gridTemplateColumns = "";
  m.classList.toggle("expanded", S.expanded);
  ["tExpand", "ptExpand"].forEach(id => {
    const el = $(id);
    if (el) {
      el.classList.toggle("on", S.expanded);
      el.textContent = S.expanded ? "收起" : "展开";
    }
  });
  applyView();
}
$("tExpand").onclick = () => { S.expanded = !S.expanded; applyLayout(); };
$("ptExpand").onclick = () => { S.expanded = !S.expanded; applyLayout(); };
let divDrag = false;
$("divider").addEventListener("mousedown", e => { divDrag = true; e.preventDefault(); });
window.addEventListener("mousemove", e => {
  if (!divDrag) return;
  const rect = $("main").getBoundingClientRect();
  const w = Math.max(360, Math.min(1100, rect.right - e.clientX));
  $("main").style.gridTemplateColumns = `clamp(140px,16vw,204px) 1fr ${w}px`;
  applyView();
});
window.addEventListener("mouseup", () => { if (divDrag) { divDrag = false; applyView(); } });

/* ---------------- 画布交互：滚动 / 缩放 / 点页选字 ---------------- */
let panDrag = null;
$("stage").addEventListener("mousedown", e => {
  if (S.edit !== "off" || e.button !== 0) return;
  if (e.target.closest(".zoombar") || e.target.closest(".editbar") || e.target.closest(".busy")) return;
  const st = $("stage");
  panDrag = { x: e.clientX, y: e.clientY, sl: st.scrollLeft, st: st.scrollTop };
  S.dragMoved = false;
  e.preventDefault();
});
window.addEventListener("mousemove", e => {
  if (!panDrag) return;
  const st = $("stage");
  const dx = e.clientX - panDrag.x, dy = e.clientY - panDrag.y;
  if (Math.abs(dx) + Math.abs(dy) > 4) S.dragMoved = true;
  st.scrollLeft = panDrag.sl - dx;
  st.scrollTop = panDrag.st - dy;
});
window.addEventListener("mouseup", () => {
  if (!panDrag) return;
  panDrag = null;
  setTimeout(() => { S.dragMoved = false; }, 60);
});
$("stage").addEventListener("wheel", e => {
  if (S.edit !== "off") return;
  if (e.ctrlKey || e.metaKey) {
    e.preventDefault();
    zoomAt(S.zoom + (e.deltaY < 0 ? 0.15 : -0.15), e.clientX, e.clientY);
    return;
  }
  if (e.shiftKey) { e.preventDefault(); $("stage").scrollLeft += e.deltaY; }
  // 普通滚轮走浏览器原生滚动
}, { passive: false });
$("pageimg").addEventListener("click", e => {
  if (S.edit !== "off" || S.dragMoved) return;
  const p = S.page, img = $("pageimg"), r = img.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width * p.w, y = (e.clientY - r.top) / r.height * p.h;
  if (S.mode === "punct") { if (window.Punct) Punct.pickImage(x, y); return; }
  let found = -1;
  p && S.rows.forEach((row, ri) => { const b = row.box; if (x >= b[0] && x <= b[2] && y >= b[1] && y <= b[3]) found = ri; });
  if (found < 0) return;
  let c = 0;
  S.rows[found].chars.forEach((cb, ci) => {
    if (x >= cb.box[0] - 6 && x <= cb.box[2] + 6 && y >= cb.box[1] - 6 && y <= cb.box[3] + 6) c = ci;
  });
  selectChar(found, c);
});

/* ---------------- 输入法承接 ---------------- */
const ghost = document.createElement("input");
ghost.id = "ghost"; ghost.autocomplete = "off"; ghost.setAttribute("aria-hidden", "true");
ghost.style.cssText = "position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0";
document.body.appendChild(ghost);
function focusGhost() { try { ghost.focus({ preventScroll: true }); } catch (e) { ghost.focus(); } }
function applyTyped(text) {
  const chars = [...text].filter(ch => ch.trim().length);
  if (!chars.length) return;
  chars.forEach(ch => {
    setChar(ch);
    if (!(S.sel.r === S.rows.length - 1 && S.sel.c === S.rows[S.sel.r].chars.length - 1)) move(0, 1);
  });
  ghost.value = "";
  if (S.simp) ensureSimpMap().then(ch => { if (ch) renderRows(); });
}
ghost.addEventListener("input", () => { const v = ghost.value; ghost.value = ""; if (v) applyTyped(v); });
ghost.addEventListener("compositionend", () => { setTimeout(() => { const v = ghost.value; ghost.value = ""; if (v) applyTyped(v); }, 0); });

/* ---------------- 键盘 ---------------- */
document.addEventListener("keydown", e => {
  const pm = document.getElementById("punctMode");
  if (pm && pm.style.display === "block") return;  // 句读模式由 punct_app.js 接管键盘
  const tag = (e.target && e.target.tagName) || "";
  // 输入框里的按键不要漏到正文（ghost 除外，它是用来接输入法的）
  if (e.target !== ghost && (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT")) {
    if (e.key === "Escape") {
      $("modalLookup").classList.remove("show");
      $("modalImport").classList.remove("show");
      $("modalExport").classList.remove("show");
    }
    return;
  }
  if (S.mode === "punct") return;   // 标点模式由 punct.js 接管键盘
  if (e.key === "Escape") {
    if ($("cand").classList.contains("show")) return closeCand();
    if ($("reco").classList.contains("show")) { $("reco").classList.remove("show"); recoCtx = null; return; }
    if ($("modalLookup").classList.contains("show")) return closeLookup();
    if ($("modalExport").classList.contains("show")) return $("modalExport").classList.remove("show");
    if (S.edit !== "off") return setEdit("off");
    $("modalImport").classList.remove("show");
    return;
  }
  if (!$("work").hidden) {
    if (e.ctrlKey || e.metaKey) {
      const k = e.key.toLowerCase();
      if (k === "s") { e.preventDefault(); save(); }
      else if (k === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
      else if (k === "y" || (k === "z" && e.shiftKey)) { e.preventDefault(); redo(); }
      else if (e.key === "ArrowRight") { e.preventDefault(); if (S.pageNo < S.book.pages.length) switchPage(S.pageNo + 1); }
      return;
    }
    if (e.key === "F2") { e.preventDefault(); openLookup(S.rows[S.sel.r] && S.rows[S.sel.r].chars[S.sel.c] ? S.rows[S.sel.r].chars[S.sel.c].text : ""); return; }
    if (S.edit !== "off") {
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSel(); return; }
      if (e.key === "ArrowUp" || e.key === "ArrowLeft") { e.preventDefault(); move(-1, 0); return; }
      if (e.key === "ArrowDown" || e.key === "ArrowRight") { e.preventDefault(); move(1, 0); return; }
      return;
    }
    if (e.key === "ArrowLeft") { e.preventDefault(); move(0, -1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); move(0, 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1, 0); }
    else if (e.key === "ArrowDown") { e.preventDefault(); move(1, 0); }
    else if (e.key === " ") { e.preventDefault(); confirmChar(); }
    else if (e.key === "Tab") { e.preventDefault(); nextRed(); }
    else if (e.key === "Enter") { e.preventDefault(); openCand(S.sel.r, S.sel.c); }
    else if (e.key === "Backspace" || e.key === "Delete") { e.preventDefault(); deleteChar(S.sel.r, S.sel.c); }
    else if (e.altKey && /^[1-9]$/.test(e.key) && S.candCtx && S.candCtx.list[+e.key - 1]) {
      e.preventDefault(); applyCand(S.candCtx.list[+e.key - 1].text);
    }
  }
});

/* ---------------- 校字 / 句读 双模式 ---------------- */
let punctHostMounted = false;

function bindPunctTop() {
  const ps = $("btnPunctSettings"), pe = $("btnPunctExport");
  if (!ps || !pe) return;
  const api = () => {
    const frame = document.querySelector(".punct-frame");
    return (frame && frame.contentWindow && frame.contentWindow.PunctAPI) || null;
  };
  const waitApi = async () => {
    for (let i = 0; i < 80; i++) {
      const a = api();
      if (a && (!a.isReady || a.isReady())) return a;
      await new Promise(r => setTimeout(r, 100));
    }
    return api();
  };
  ps.onclick = () => {
    if (window.GuWenSettings) window.GuWenSettings.open();
    else toast('AI 设置组件未加载，请刷新页面');
  };
  pe.onclick = async () => {
    let a = api();
    if (!a) { await setWorkMode('punct'); a = await waitApi(); }
    if (a) a.openExport(); else toast('导出：句读页未就绪');
  };
}

async function ensurePunctMode() {
  const host = $("punctMode");
  if (!host) return;
  if (!S.book) throw new Error("还没打开书");
  const bid = S.book.id;
  // 换书必须重建 iframe，否则旧书的 BID 仍会继续写入
  if (punctHostMounted && host.dataset.book && host.dataset.book !== bid) {
    host.innerHTML = "";
    punctHostMounted = false;
  }
  host.dataset.book = bid;
  if (punctHostMounted && host.querySelector("iframe")) return;
  host.innerHTML = '<iframe class="punct-frame" src="/static/punct_app.html?book=' +
    encodeURIComponent(bid) + '&embed=1" title="句读"></iframe>';
  const frame = host.querySelector('iframe');
  if (frame) frame.addEventListener('load', bindPunctTop);
  punctHostMounted = true;
  bindPunctTop();
}

function pageSubText() {
  const p = S.page;
  if (!p) return "";
  return (p.vol ? "卷" + p.vol + " · " : "") + "第 " + p.leaf + " 叶";
}

async function setWorkMode(mode) {
  const host = $("punctMode");
  if (!host) return;
  if (mode === "punct") {
    if (!S.book) return;
    host.style.display = "block";
    $("btnPunctNew").classList.add("on");
    $("modeJiao").classList.remove("on");
    bindPunctTop();
    $("workSub").textContent = pageSubText();
    if (S.dirty) save();
    try { await ensurePunctMode(); }
    catch (e) {
      host.style.display = "none";
      $("btnPunctNew").classList.remove("on");
      $("modeJiao").classList.add("on");
      $("workSub").textContent = pageSubText();
      toast("句读加载失败：" + e.message, 6000);
    }
  } else {
    host.style.display = "none";

    $("btnPunctNew").classList.remove("on");
    $("modeJiao").classList.add("on");
    if (S.book) $("workSub").textContent = pageSubText();
  }
}
$("modeJiao").onclick = () => setWorkMode("jiao");
$("btnPunctNew").onclick = () => setWorkMode("punct");
// 顶栏“导出/设置”即使还没进入句读模式也要先绑定，点击后自动切到句读
bindPunctTop();
const homeSettings = $("btnHomeSettings");
if (homeSettings) {
  homeSettings.onclick = () => {
    if (window.GuWenSettings) window.GuWenSettings.open();
    else toast("AI 设置组件未加载，请刷新页面");
  };
}
document.addEventListener("punct-save-state", e => {
  const text = (e.detail && e.detail.text) || "已保存";
  $("saveState").textContent = text;
});

/* ---------------- 返回 / 导入 ---------------- */
$("btnBack").onclick = async () => {
  setWorkMode("jiao");
  await save(); setEdit("off"); closeCand(); closeLookup();
  if (window.Punct && S.mode === "punct") Punct.exit();
  clearTimeout(S.batchTimer); S.batchTimer = null; S.batchRunning = false;
  $("batchBar").hidden = true;
  $("work").hidden = true; $("home").hidden = false; refreshBooks();
};
$("browseImport").onclick = async () => {
  toast("请在弹出的系统窗口里选择文件夹…");
  try {
    const d = await api("/api/pick-folder", { method: "POST" });
    if (d.path) { $("importPath").value = d.path; toast("已选择：" + d.path); }
    else toast("没有选择文件夹（也可以直接粘贴路径）");
  } catch (e) { toast("打开选择窗口失败：" + e.message); }
};
$("btnImport").onclick = () => {
  $("importPath").value = "F:\\BaiduNetdiskDownload\\春灯谜344";
  $("modalImport").classList.add("show"); $("importPath").focus();
};
$("cancelImport").onclick = () => $("modalImport").classList.remove("show");
$("okImport").onclick = async () => {
  const path = $("importPath").value.trim();
  if (!path) return;
  $("modalImport").classList.remove("show");
  setBusy(true, "导入中…");
  try {
    const d = await api("/api/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
    await refreshBooks();
    toast(`已导入：${d.book.title}（${d.book.pages} 叶）`);
    await openBook(d.book.id);
  } catch (e) { toast("导入失败：" + e.message); }
  finally { setBusy(false); }
};

window.addEventListener("beforeunload", e => {
  if (S.dirty) { e.preventDefault(); e.returnValue = "校字稿还有未保存的修改"; }
  const frame = document.querySelector(".punct-frame");
  const api = frame && frame.contentWindow && frame.contentWindow.PunctAPI;
  if (api && api.isDirty && api.isDirty()) {
    e.preventDefault();
    e.returnValue = "句读稿还有未保存的修改";
  }
});

/* ---------------- 启动 ---------------- */
window.addEventListener("resize", () => { if (S.page) applyView(); });
(async () => {
  await refreshBooks();
  const qp = new URLSearchParams(location.search);
  const bid = qp.get("book");
  if (bid) {
    try { await openBook(bid); }
    catch (e) { toast("打开书失败：" + e.message); }
    if (qp.get("punct")) { try { await setWorkMode("punct"); } catch (e) {} }
  }
})().catch(e => toast("加载书库失败：" + e.message));
