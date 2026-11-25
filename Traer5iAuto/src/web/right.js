// WebView2 版の右の列: 計測ファイル・資料一覧・計測台帳・段の境目(決まりは Python の側。ここは映して操作を返すだけ)
"use strict";

let infoLocked = false, infoValues = {}, roleMap = {};
let refs = { cols: [], rows: [], lines: [], join: null, head: [] }, refsView = null, refsTable = null;
let ledgerData = null, ledgerTable = null, layout = {};
const NUM = "#0";

// ---- 小さな部品: 確かめる窓・右クリックのメニュー・文字の幅
function ask(title, text) {
  const d = $("ask");
  d.querySelector(".title").textContent = title; d.querySelector(".text").textContent = text;
  return new Promise(res => {
    const done = v => { d.close(); res(v); };
    d.querySelector(".yes").onclick = () => done(true);
    d.querySelector(".no").onclick = () => done(false);
    d.oncancel = e => { e.preventDefault(); done(false); };
    d.showModal(); d.querySelector(".no").focus();
  });
}
function showMenu(e, items) {
  const m = $("menu");
  m.replaceChildren(...items.map(it => {
    if (it === "-") return el("hr");
    const d = el("div", { textContent: it.label, className: it.off ? "off" : "" });
    if (!it.off) d.addEventListener("click", () => { hideMenu(); it.run(); });
    return d;
  }));
  const z = parseFloat(document.documentElement.style.zoom || "1");
  m.hidden = false;
  m.style.left = Math.min(e.clientX / z, innerWidth / z - m.offsetWidth - 4) + "px";
  m.style.top = Math.min(e.clientY / z, innerHeight / z - m.offsetHeight - 4) + "px";
}
function hideMenu() { $("menu").hidden = true; }
addEventListener("mousedown", e => { if (!e.target.closest("#menu")) hideMenu(); }, true);
addEventListener("keydown", e => { if (e.key === "Escape") hideMenu(); });
const measure = (() => { const c = document.createElement("canvas").getContext("2d"); return (t, font) => { c.font = font; return c.measureText(t).width; }; })();
// 見出しが収まる幅(列の基準の幅の初期値。表が広ければ、この比で余りを配る)
const headFont = () => `${getComputedStyle(document.body).fontSize} ${app.UI.FONT_FAMILY}`;
const headNeed = (text, extra = 0) => Math.ceil(measure(text, headFont()) + extra + app.UI.TABLE_HEAD_PAD);
const sameKeys = (a, b) => Array.isArray(a) && a.length === b.length && a.every((x, i) => x === b[i]);
const badge = (text, colors) => {
  const b = el("i", { className: "badge", textContent: text });
  if (colors) { b.style.background = colors[0]; b.style.color = colors[1]; }
  return b;
};

// ---- 計測ファイル(1 項目 1 行・必須の印・空なら欄の中に説明・資料一覧で割り当てた列の名前)
function buildInfo(ui) {
  const card = $("info-card");
  card.replaceChildren();
  for (const [key, label, desc] of ui.INFO_FIELDS) {
    const req = ui.REQUIRED_FIELDS.includes(key);
    const inp = el("input", { placeholder: desc });
    inp.dataset.key = key;
    inp.addEventListener("input", () => { infoValues[key] = inp.value; markReq(); api().set_values({ [key]: inp.value }); });
    const field = el("div", { className: "field" }, inp);
    if (req) field.append(el("div", { className: "req" }));
    card.append(el("label", { textContent: label + (req ? ui.REQUIRED_MARK : "") }), field);
    if (ui.REF_ROLES.includes(key)) {
      const note = el("div", { className: "colnote" });
      note.dataset.role = key; note.hidden = true;
      card.append(el("span"), note);
    }
  }
  showInfo();
}
function markReq() {
  for (const inp of $("info-card").querySelectorAll("input")) {
    const r = inp.parentNode.querySelector(".req");
    if (r) r.style.background = app.UI.REQ_MARK_COLORS[(infoValues[inp.dataset.key] || "").trim() ? 1 : 0];
  }
}
function showInfo() {
  for (const inp of $("info-card").querySelectorAll("input")) {
    const v = infoValues[inp.dataset.key] ?? "";
    if (document.activeElement !== inp || inp.value.trim() !== v.trim()) inp.value = v;
    inp.disabled = infoLocked;
  }
  markReq();
  const [text, colors] = infoLocked ? app.UI.INFO_BADGE_TURN : app.UI.INFO_BADGE;
  const b = $("info-badge");
  b.textContent = text; b.style.background = colors[0]; b.style.color = colors[1];
}
app.on("info", d => { infoLocked = d.locked; infoValues = Object.assign({}, d.values); showInfo(); });
app.on("roles", r => {
  roleMap = r;
  for (const n of $("info-card").querySelectorAll(".colnote")) {
    const col = r[n.dataset.role];
    n.textContent = col ? app.UI.REF_COL_NOTE.replace("{col}", col) : "";
    n.hidden = !col; n.previousElementSibling.hidden = !col;
  }
  fitPanes();
});

// ---- 資料一覧
function refsColumns() {
  // 列の基準の幅: 同じ列の並びなら今の幅 → 保存した幅(列の並びが同じときだけ)→ 見出しが収まる幅。
  // 見出し(▲▼・役割のバッジ)が収まらなければ広げる。行番号の列は余りを配らない(fixed)
  const ui = app.UI, v = refsView || {}, sort = v.sort, roles = v.roles || {};
  const keys = [NUM, ...refs.cols], saved = layout.refs_col_widths || {};
  const old = sameKeys(refsTable.cols.map(c => c.key), keys) ? refsTable.widths()
            : sameKeys(saved.cols, keys) && saved.widths ? saved.widths : {};
  const col = (key, name, extra) => {
    const mark = sort && sort[0] === key ? ui.REF_SORT_MARK[sort[1] ? "true" : "false"] : "";
    const role = Object.keys(roles).find(r => roles[r] === key);
    const title = [name + mark]; if (role) title.push(badge(role));
    const need = headNeed(name + mark, role ? measure(role, "10px " + ui.FONT_FAMILY) + 20 : 0);
    const w = Number(old[key]);
    return Object.assign({ key, title, width: Math.max(w > 0 ? w : 0, need) }, extra);
  };
  return [Object.assign(col(NUM, ui.REF_NUM_HEAD, { cls: "num", align: "right", minW: ui.REF_NUM_MIN_WIDTH, fixed: true }),
                        { width: Number(old[NUM]) > 0 ? Number(old[NUM]) : ui.REF_NUM_WIDTH }),
          ...refs.cols.map(c => col(c, c, c === refs.join ? { cls: "num" } : {}))];
}
function showRefs() {
  if (!refsTable) return;
  refsTable.setColumns(refsColumns());
  const v = refsView || { view: [] };
  refsTable.setRows(v.view, n => [String(refs.lines[n]), ...refs.rows[n]]);
  const ui = app.UI;
  $("refs-file").textContent = v.file || ui.REF_NO_FILE; $("refs-file").title = v.file || "";
  $("refs-btn-filter").classList.toggle("on", !!v.on);
  $("refs-btn-join").classList.toggle("on", !!v.join);
  $("refs-on").classList.toggle("on", !!v.on);
  $("refs-on").textContent = v.on ? ui.REF_FILTER_ON : ui.REF_FILTER_OFF;
  const opts = (sel, list, cur) => {
    sel.replaceChildren(...list.map(x => el("option", { value: x, textContent: x, selected: x === cur })));
  };
  opts($("refs-target"), [ui.REF_ALL, ...refs.cols], v.target);
  if (document.activeElement !== $("refs-query")) $("refs-query").value = v.query || "";
  const j = v.join || ["", "", ""];
  opts($("join-a"), ["", ...refs.head], $("join-a").value || j[0]);
  opts($("join-b"), ["", ...refs.head], $("join-b").value || j[2]);
  if (v.join && document.activeElement !== $("join-sep") && !$("join-sep").dataset.touched) $("join-sep").value = j[1];
  joinStates();
}
function joinStates() {
  const v = refsView || {}, a = $("join-a").value, b = $("join-b").value, sep = $("join-sep").value;
  const cur = v.join;
  $("join-make").textContent = cur ? app.UI.REF_JOIN_UPDATE : app.UI.REF_JOIN_MAKE;
  $("join-make").disabled = !(refs.head.includes(a) && refs.head.includes(b)) || (cur && cur[0] === a && cur[1] === sep && cur[2] === b);
  $("join-drop").disabled = !cur;
}
app.on("refs_data", d => { refs = d; });
app.on("refs_view", v => { refsView = v; showRefs(); });
function toggleBar(which) {
  const open = $(`bar-${which}`).hidden;
  $("bar-filter").hidden = true; $("bar-join").hidden = true;
  $(`bar-${which}`).hidden = !open;
  if (open) (which === "filter" ? $("refs-query") : $("join-a")).focus();
}
let queryTimer = null;
function setupRefs(ui) {
  refsTable = new VTable($("refs-table"), {
    rowH: 24,
    onHead: key => api().refs_sort(key),
    onHeadMenu: (key, e) => {
      if (key === NUM) return;
      const now = Object.keys((refsView || {}).roles || {}).find(r => refsView.roles[r] === key);
      showMenu(e, ui.REF_ROLES.map(role => ({ label: role + (role === now ? "  ✓" : ""), run: () => api().refs_assign(key, role === now ? null : role) })));
    },
    onRowClick: n => { if ((refsView || {}).enabled !== false) api().refs_use(n); },
    onEnter: sel => { if (sel.length && (refsView || {}).enabled !== false) api().refs_use(sel[0]); },
    onResize: w => {
      layout.refs_col_widths = { cols: refsTable.cols.map(c => c.key), widths: w };
      api().save_layout({ refs_col_widths: layout.refs_col_widths });
    },
  });
  $("refs-open").addEventListener("click", () => api().refs_open());
  $("refs-btn-filter").addEventListener("click", () => toggleBar("filter"));
  $("refs-btn-join").addEventListener("click", () => toggleBar("join"));
  const search = () => api().refs_search($("refs-on").classList.contains("on"), $("refs-target").value, $("refs-query").value);
  $("refs-on").addEventListener("click", () => api().refs_search(!refsView.on, $("refs-target").value, $("refs-query").value));
  $("refs-target").addEventListener("change", search);
  $("refs-query").addEventListener("input", () => {
    if ($("refs-query").value.trim() && !refsView.on) { $("refs-on").classList.add("on"); $("refs-btn-filter").classList.add("on"); $("refs-on").textContent = ui.REF_FILTER_ON; }
    clearTimeout(queryTimer);
    queryTimer = setTimeout(() => api().refs_search(true && ($("refs-on").classList.contains("on")), $("refs-target").value, $("refs-query").value), ui.REF_FILTER_DELAY_MS);
  });
  for (const id of ["join-a", "join-b", "join-sep"]) $(id).addEventListener("input", () => { $("join-sep").dataset.touched = "1"; joinStates(); });
  for (const id of ["join-a", "join-b"]) $(id).addEventListener("change", joinStates);
  $("join-make").addEventListener("click", () => { delete $("join-sep").dataset.touched; api().refs_join($("join-a").value, $("join-sep").value, $("join-b").value); });
  $("join-drop").addEventListener("click", () => api().refs_unjoin());
}

// ---- 計測台帳(セルの編集・右クリックで 有効 / 失敗 / 削除。終わっていないセットの行は受け付けない)
function setupLedger(ui) {
  ledgerTable = new VTable($("ledger-table"), {
    rowH: 24, multi: true,
    rowClass: rid => { const r = ledgerRow(rid); return r ? r.tag : ""; },
    onRowDblClick: (rid, col) => {
      const c = ui.LEDGER_COLS[col];
      if (!c || !c[1] || ledgerRow(rid).locked || ledgerTable.sel.size !== 1) return;
      ledgerTable.edit(rid, col);
    },
    onEnter: sel => {
      const col = ui.LEDGER_COLS.findIndex(c => c[1]);
      if (sel.length === 1 && !ledgerRow(sel[0]).locked) ledgerTable.edit(sel[0], col);
    },
    onEdit: (rid, col, v) => api().ledger_cell(rid, ui.LEDGER_COLS[col][0], v),
    onDelete: sel => deleteRows(sel),
    onRowMenu: (e, sel) => {
      const off = sel.some(r => ledgerRow(r).locked);
      showMenu(e, [{ label: ui.LEDGER_MENU[0], off, run: () => api().ledger_failed(sel, false) },
                   { label: ui.LEDGER_MENU[1], off, run: () => api().ledger_failed(sel, true) }, "-",
                   { label: ui.LEDGER_MENU[2], off, run: () => deleteRows(sel) }]);
    },
    onResize: w => { layout.ledger_col_widths = w; api().save_layout({ ledger_col_widths: w }); },
  });
  // 列の基準の幅: 保存した幅(全部の列がそろっているときだけ)、無ければ見出しが収まる幅(表が広ければ、この比で余りを配る)
  const widths = layout.ledger_col_widths || {};
  const ok = ui.LEDGER_COLS.every(([name]) => Number.isInteger(widths[name]) && widths[name] >= 20 && widths[name] <= 3000);
  ledgerTable.setColumns(ui.LEDGER_COLS.map(([name, editable]) => {
    const title = editable ? name : [name, el("i", { className: "lock" })];   // 編集できない列は線の鍵の印(絵文字は色付きで出るので使わない)
    return { key: name, title, align: editable ? "left" : "center", width: ok ? widths[name] : headNeed(name, editable ? 0 : 15) };
  }));
  $("ledger-folder").addEventListener("click", () => api().open_folder());
  // 表の外のクリックで選択を外す(表の中・メニュー・確かめる窓は除く)。
  // 表は押した行を描き直すので e.target はもう画面に無いことがある。押したときの道筋(composedPath)で判断する
  addEventListener("mousedown", e => {
    const inside = e.composedPath().some(n => n instanceof Element && n.matches("#ledger-table, #menu, dialog"));
    if (!inside) ledgerTable.clear();
  });
}
const ledgerRow = rid => (ledgerData && ledgerData.byId[rid]) || { locked: true, tag: "" };
async function deleteRows(sel) {
  if (!sel.length || sel.some(r => ledgerRow(r).locked)) return;
  const nos = sel.map(r => ledgerRow(r).cells[0]);
  const shown = nos.slice(0, 5).join(", ") + (nos.length > 5 ? " …" : "");
  if (await ask("行の削除", `${sel.length} 行(No. ${shown})を台帳から削除しますか?`)) api().ledger_delete(sel);
}
app.on("ledger", d => {
  const atEnd = ledgerTable && ledgerTable.body.scrollTop + ledgerTable.body.clientHeight >= ledgerTable.body.scrollHeight - 30;
  const before = ledgerData ? ledgerData.rows.length : 0;
  d.byId = Object.fromEntries(d.rows.map(r => [r.id, r]));
  ledgerData = d;
  if (!ledgerTable) return;
  ledgerTable.setRows(d.rows.map(r => r.id), rid => d.byId[rid].cells);
  if (d.rows.length > before && (atEnd || !before)) ledgerTable.body.scrollTop = ledgerTable.body.scrollHeight;
  $("ledger-path").textContent = d.folder + (d.saved ? "" : `  ⚠未保存(${app.UI.LEDGER_FILE})`);
  $("ledger-path").title = d.folder;
});

// ---- 段の境目(3 段。高さの割合を設定に保存。窓の高さが変わっても割合のまま・最小の高さは守る)
let split = null;
function paneMins() {
  const ui = app.UI, rowH = 24;
  // 1 段目は計測ファイルのカードの下端まで(見出しの下の余白も含む)。info-col の scrollHeight は段の高さより小さくならないので使わない(縮められなかった)
  const top = $("info-card").offsetTop + $("info-card").offsetHeight - $("info-col").offsetTop + 1;
  const ledger = $("pane-ledger").querySelector(".head").offsetHeight + 32 + rowH * ui.LEDGER_MIN_ROWS + 30;
  const log = 30 + 20 * ui.LOG_ROWS;
  return [top, ledger, log];
}
function fitPanes() {
  if (!app.UI) return;
  const right = $("right"), sash = app.UI.SASH_WIDTH;
  const H = right.clientHeight - app.UI.MARGIN * 2 - sash * 2;
  if (H <= 0) return;
  const mins = paneMins();
  let [a, b] = split || (() => {
    const extra = Math.max(0, H - mins[0] - mins[1] - mins[2]), w = app.UI.RIGHT_WEIGHTS, t = w[0] + w[1] + w[2];
    const h0 = mins[0] + extra * w[0] / t, h1 = mins[1] + extra * w[1] / t;
    return [h0 / H, (h0 + h1) / H];
  })();
  let h0 = Math.max(mins[0], a * H), h1 = Math.max(mins[1], b * H - h0);
  if (H - h0 - h1 < mins[2]) { h1 = Math.max(mins[1], H - h0 - mins[2]); }
  if (H - h0 - h1 < mins[2]) { h0 = Math.max(mins[0], H - h1 - mins[2]); }
  $("pane-top").style.height = h0 + "px";
  $("pane-ledger").style.height = h1 + "px";
}
function setupSashes() {
  split = Array.isArray(layout.right_split) && layout.right_split.length === 2 ? layout.right_split.map(Number) : null;
  for (const s of document.querySelectorAll(".sash")) {
    s.addEventListener("mousedown", e => {
      e.preventDefault();
      const i = +s.dataset.i, z = parseFloat(document.documentElement.style.zoom || "1");
      const H = $("right").clientHeight - app.UI.MARGIN * 2 - app.UI.SASH_WIDTH * 2;
      const y0 = e.clientY / z, h0 = $("pane-top").offsetHeight, h1 = $("pane-ledger").offsetHeight;
      const move = m => {
        const dy = m.clientY / z - y0;
        const a = i === 0 ? h0 + dy : h0, b = i === 0 ? h0 + h1 : h0 + h1 + dy;
        split = [a / H, b / H];
        fitPanes();
      };
      const up = () => {
        removeEventListener("mousemove", move); removeEventListener("mouseup", up);
        const H2 = $("right").clientHeight - app.UI.MARGIN * 2 - app.UI.SASH_WIDTH * 2;
        split = [$("pane-top").offsetHeight / H2, ($("pane-top").offsetHeight + $("pane-ledger").offsetHeight) / H2].map(x => Math.round(x * 1e4) / 1e4);
        api().save_layout({ right_split: split });
      };
      addEventListener("mousemove", move); addEventListener("mouseup", up);
    });
  }
  new ResizeObserver(fitPanes).observe($("right"));
}

// ---- 表の下の説明(UI.REF_HINT / LBL_LEDGER_HINT: (札, ((印, 文), …)) の並び)
function renderHint(box, groups, ui) {
  box.replaceChildren(...groups.map(([target, items]) => {
    const g = el("span", { className: "grp" });
    if (target) g.append(el("span", { className: "tag " + target, textContent: ui.HINT_TARGETS[target] }));
    for (const [icon, text] of items) {
      const it = el("span", { className: "it" });
      it.append(el("i", { className: "ico " + icon }), text);
      g.append(it);
    }
    return g;
  }));
}

// ---- 始まり(app.js が見た目の値を受け取ったあと呼ぶ)
function setupRight(ui, lay) {
  layout = lay || {};
  const root = document.documentElement.style;
  root.setProperty("--info-pad-x", ui.INFO_CARD_PAD[0] + "px"); root.setProperty("--info-pad-y", ui.INFO_CARD_PAD[1] + "px");
  root.setProperty("--entry-pad-x", ui.INFO_ENTRY_PAD[0] + "px"); root.setProperty("--entry-pad-y", ui.INFO_ENTRY_PAD[1] + "px");
  for (const [k, v] of Object.entries(ui.REF_TOOL)) root.setProperty(`--tool-${k.replace("_", "-")}`, v);
  root.setProperty("--grip-w", ui.SASH_GRIP[0] + "px"); root.setProperty("--grip-h", ui.SASH_GRIP[1] + "px");
  root.setProperty("--SASH_LINE", ui.SASH_LINE[1]);
  buildInfo(ui);
  renderHint($("refs-hint"), ui.REF_HINT, ui); renderHint($("ledger-hint"), ui.LBL_LEDGER_HINT, ui);
  setupRefs(ui);
  setupLedger(ui);
  setupSashes();
  fitPanes();
}
