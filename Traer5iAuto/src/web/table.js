// 見えている行だけを描く表(資料一覧・計測台帳)。
// 列: {key, title, width(基準の幅), align, cls(セルの class), minW, fixed(余りを配らない)}。
// 行: keys(表に出す順)と get(key) -> セルの値の並び。
// 列の幅: 表が基準の幅の合計より広ければ、余りを基準の幅の比で配って右に余白を残さない。狭ければ基準の幅のまま横にスクロールする。
// 見出しのクリック(onHead)・右クリック(onHeadMenu)・境目のドラッグで列の幅(onResize。そのときの幅が新しい基準)・
// 行のクリック(onRowClick)・ダブルクリック(onRowDblClick)/ Shift / Ctrl の選択・
// 上下キー・Enter(onEnter)・Delete(onDelete)・Esc(選択を外す)・行の右クリック(onRowMenu)・セルの編集(edit / onEdit)
"use strict";

class VTable {
  constructor(host, opt = {}) {
    this.opt = Object.assign({ rowH: 24, multi: false, minW: 24 }, opt);
    this.host = host;
    host.classList.add("vt");
    host.tabIndex = 0;
    this.head = document.createElement("div"); this.head.className = "vt-head";
    this.body = document.createElement("div"); this.body.className = "vt-body";
    this.space = document.createElement("div"); this.space.className = "vt-space";
    this.body.append(this.space);
    host.append(this.head, this.body);
    this.cols = []; this.keys = []; this.get = () => [];
    this.sel = new Set(); this.anchor = null; this.focusKey = null;
    this.rowClass = opt.rowClass || (() => ""); this.cellClass = opt.cellClass || (() => "");
    this.body.addEventListener("scroll", () => { this.head.scrollLeft = this.body.scrollLeft; this.render(); });
    new ResizeObserver(() => this.render()).observe(this.body);
    this.body.addEventListener("mousedown", e => this._down(e));
    this.body.addEventListener("contextmenu", e => {
      const k = this._keyAt(e); if (!k) return;
      e.preventDefault();
      if (!this.sel.has(k)) { this.sel = new Set([k]); this.anchor = k; this._changed(); }
      this.opt.onRowMenu && this.opt.onRowMenu(e, [...this.sel]);
    });
    host.addEventListener("keydown", e => this._key(e));
  }

  setColumns(cols) {
    this.cols = cols.map(c => Object.assign({ width: 80, align: "left" }, c));
    for (const c of this.cols) { c.base = c.width; c.minW = c.minW || this.opt.minW; }
    this._renderHead();
    this.render();
  }

  setRows(keys, get) {
    this.keys = keys; this.get = get;
    const has = new Set(keys);
    for (const k of [...this.sel]) if (!has.has(k)) this.sel.delete(k);
    this.render();
  }

  widths() { return Object.fromEntries(this.cols.map(c => [c.key, Math.round(c.base)])); }

  // 基準の幅から見える幅を決める(広ければ余りを fixed でない列へ基準の幅の比で配る。最後の列で端数をそろえる)
  _fit() {
    const avail = this.body.clientWidth, sum = this.cols.reduce((a, c) => a + c.base, 0);
    const grow = this.cols.filter(c => !c.fixed), gsum = grow.reduce((a, c) => a + c.base, 0);
    const extra = avail > sum && gsum > 0 ? avail - sum : 0;
    let used = 0;
    this.cols.forEach((c, i) => {
      const w = c.fixed || !extra ? c.base : Math.floor(c.base + extra * c.base / gsum);
      c.width = i === this.cols.length - 1 && extra ? Math.max(w, avail - used) : w;
      used += c.width;
    });
    const hs = this.head.querySelectorAll(".vt-h");
    this.cols.forEach((c, i) => { if (hs[i] && hs[i].style.width !== c.width + "px") hs[i].style.width = c.width + "px"; });
    this.stretched = extra > 0;
  }

  // ---- 見出し
  _renderHead() {
    const row = document.createElement("div"); row.className = "vt-row vt-hrow";
    this.cols.forEach((c, i) => {
      const h = document.createElement("div");
      h.className = "vt-cell vt-h" + (c.hcls ? " " + c.hcls : "");
      h.style.textAlign = c.align === "right" ? "right" : "left";
      h.append(...(Array.isArray(c.title) ? c.title : [c.title]).map(t => typeof t === "string" ? document.createTextNode(t) : t));
      h.addEventListener("click", e => { if (!this._dragged && this.opt.onHead) this.opt.onHead(c.key, e); });
      h.addEventListener("contextmenu", e => { e.preventDefault(); this.opt.onHeadMenu && this.opt.onHeadMenu(c.key, e); });
      const grip = document.createElement("i"); grip.className = "vt-grip";
      grip.addEventListener("mousedown", e => this._resize(e, i));
      grip.addEventListener("click", e => e.stopPropagation());
      h.append(grip);
      row.append(h);
    });
    this.head.replaceChildren(row);
  }

  _resize(e, i) {
    e.preventDefault(); e.stopPropagation();
    // 右に余白が無い(余りを配っている)ときは右隣の列と幅をやり取りする(合計は変えない)。
    // それ以外(横にスクロールしている・最後の列)はその列だけ変える
    const x0 = e.clientX, zoom = parseFloat(document.documentElement.style.zoom || "1");
    const w0 = this.cols.map(c => c.width), pair = this.stretched && i < this.cols.length - 1;
    this._dragged = false;
    const move = m => {
      this._dragged = true;
      const d = (m.clientX - x0) / zoom, c = this.cols[i];
      this.cols.forEach((x, j) => { x.base = w0[j]; });
      if (pair) {
        const n = this.cols[i + 1], both = w0[i] + w0[i + 1];
        c.base = Math.min(Math.max(c.minW, w0[i] + d), both - n.minW);
        n.base = both - c.base;
      } else c.base = Math.max(c.minW, w0[i] + d);
      this.render();
    };
    const up = () => {
      removeEventListener("mousemove", move); removeEventListener("mouseup", up);
      if (this._dragged && this.opt.onResize) this.opt.onResize(this.widths());
      setTimeout(() => { this._dragged = false; }, 0);
    };
    addEventListener("mousemove", move); addEventListener("mouseup", up);
  }

  // ---- 行(見えている所だけ)
  render() {
    const { rowH } = this.opt;
    this.space.style.height = this.keys.length * rowH + "px";   // 先に高さ(縦のスクロールバーの分を幅から除いて配る)
    this._fit();
    const total = this.cols.reduce((a, c) => a + c.width, 0);
    this.space.style.width = total + "px";
    const top = this.body.scrollTop, h = this.body.clientHeight;
    const first = Math.max(0, Math.floor(top / rowH) - 2), last = Math.min(this.keys.length, Math.ceil((top + h) / rowH) + 2);
    const frag = document.createDocumentFragment();
    for (let n = first; n < last; n++) {
      const k = this.keys[n], vals = this.get(k);
      const r = document.createElement("div");
      r.className = "vt-row" + (this.sel.has(k) ? " sel" : "") + (this.rowClass(k) ? " " + this.rowClass(k) : "");
      r.style.top = n * rowH + "px"; r.style.height = rowH + "px"; r.style.width = Math.max(total, this.body.clientWidth) + "px";
      r.dataset.n = n;
      this.cols.forEach((c, i) => {
        const d = document.createElement("div");
        const cc = this.cellClass(k, i);
        d.className = "vt-cell" + (c.cls ? " " + c.cls : "") + (cc ? " " + cc : "");
        d.style.width = c.width + "px";
        d.style.textAlign = c.align;
        d.textContent = vals[i] ?? "";
        r.append(d);
      });
      frag.append(r);
    }
    this.body.querySelectorAll(".vt-row").forEach(e => e.remove());
    this.body.append(frag);
    if (this.editing) this._placeEditor();
  }

  _keyAt(e) {
    const r = e.target.closest(".vt-row");
    return r && this.body.contains(r) ? this.keys[+r.dataset.n] : null;
  }

  _colAt(e) {
    const cell = e.target.closest(".vt-cell");
    return cell ? [...cell.parentNode.children].indexOf(cell) : -1;
  }

  _down(e) {
    if (e.button !== 0 || e.target.closest(".vt-edit")) return;
    const k = this._keyAt(e);
    this.host.focus({ preventScroll: true });
    if (!k) { this.clear(); return; }
    if (this.opt.multi && e.shiftKey && this.anchor != null) {
      const a = this.keys.indexOf(this.anchor), b = this.keys.indexOf(k);
      this.sel = new Set(this.keys.slice(Math.min(a, b), Math.max(a, b) + 1));
    } else if (this.opt.multi && e.ctrlKey) {
      this.sel.has(k) ? this.sel.delete(k) : this.sel.add(k); this.anchor = k;
    } else { this.sel = new Set([k]); this.anchor = k; }
    this._changed();
    const col = this._colAt(e);
    // 行は押すたびに描き直すので dblclick の知らせは当てにならない。押した回数(e.detail)で見る
    if (e.detail >= 2 && !e.shiftKey && !e.ctrlKey && this.opt.onRowDblClick) {
      const up = () => { removeEventListener("mouseup", up); this.opt.onRowDblClick(k, col, e); };
      addEventListener("mouseup", up);
      return;
    }
    if (!e.shiftKey && !e.ctrlKey) {
      const up = () => { removeEventListener("mouseup", up); this.opt.onRowClick && this.opt.onRowClick(k, col, e); };
      addEventListener("mouseup", up);
    }
  }

  _key(e) {
    if (e.target.closest(".vt-edit")) return;
    const n = this.keys.indexOf(this.anchor);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const m = Math.max(0, Math.min(this.keys.length - 1, (n < 0 ? 0 : n + (e.key === "ArrowDown" ? 1 : -1))));
      if (!this.keys.length) return;
      this.anchor = this.keys[m]; this.sel = new Set([this.anchor]); this.see(this.anchor); this._changed();
    } else if (e.key === "Enter" && this.sel.size) { e.preventDefault(); this.opt.onEnter && this.opt.onEnter([...this.sel]); }
    else if (e.key === "Delete" && this.sel.size) { e.preventDefault(); this.opt.onDelete && this.opt.onDelete([...this.sel]); }
    else if (e.key === "Escape") this.clear();
  }

  clear() { if (this.sel.size) { this.sel.clear(); this._changed(); } }

  see(k) {
    const n = this.keys.indexOf(k), { rowH } = this.opt;
    if (n < 0) return;
    if (n * rowH < this.body.scrollTop) this.body.scrollTop = n * rowH;
    else if ((n + 1) * rowH > this.body.scrollTop + this.body.clientHeight) this.body.scrollTop = (n + 1) * rowH - this.body.clientHeight;
  }

  _changed() {
    if (this.editing && !this.sel.has(this.editing.key)) this.endEdit(true);
    this.render();
    this.opt.onSelect && this.opt.onSelect([...this.sel]);
  }

  // ---- セルの編集(その場の入力欄。Enter・欄を離れると確定、Esc で取り消し)
  edit(k, col) {
    this.endEdit(true);
    const vals = this.get(k);
    const inp = document.createElement("input");
    inp.className = "vt-edit";
    inp.value = vals[col] ?? "";
    this.editing = { key: k, col, inp, done: false };
    this.body.append(inp);
    this._placeEditor();
    inp.focus(); inp.select();
    inp.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); this.endEdit(true); this.host.focus(); }
      if (e.key === "Escape") { e.preventDefault(); this.endEdit(false); this.host.focus(); }
    });
    inp.addEventListener("blur", () => this.endEdit(true));
  }

  _placeEditor() {
    const ed = this.editing, n = this.keys.indexOf(ed.key);
    if (n < 0) { this.endEdit(false); return; }
    const x = this.cols.slice(0, ed.col).reduce((a, c) => a + c.width, 0);
    Object.assign(ed.inp.style, { top: n * this.opt.rowH + "px", left: x + "px", width: this.cols[ed.col].width + "px",
                                  height: this.opt.rowH + "px" });
    if (ed.inp.parentNode !== this.body) this.body.append(ed.inp);
  }

  endEdit(commit) {
    const ed = this.editing;
    if (!ed || ed.done) return;
    ed.done = true; this.editing = null;
    const v = ed.inp.value;
    ed.inp.remove();
    if (commit && this.opt.onEdit && v !== (this.get(ed.key)[ed.col] ?? "")) this.opt.onEdit(ed.key, ed.col, v);
  }
}
