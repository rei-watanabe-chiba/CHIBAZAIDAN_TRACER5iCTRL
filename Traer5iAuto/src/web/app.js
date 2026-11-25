// WebView2 版の主の窓。Python(web_app.py)から届いた知らせを映し、操作を Python へ返すだけ(決まりは Python の側)。
// 土台は core.js、状態の欄とボタンは status.js
"use strict";

function applyUI(ui, zoom) {
  applyBase(ui, zoom);
  $("pass").placeholder = ui.PASS_DESC;
  $("pass").size = ui.PASS_WIDTH;
  $("mini-views").replaceChildren(...[ui.MINI_NONE, ...ui.MINI_VIEWS].map(v => {
    const b = el("button", { textContent: ui.MINI_VIEW_LABELS[v], type: "button" });
    b.dataset.view = v;
    b.addEventListener("click", () => api().set_mini_view(v));
    return b;
  }));
  $("device-head").replaceChildren(...ui.DEVICE_LIST_COLS.map(([t, f]) => el("th", { textContent: t, style: `width:${f * 100}%` })));
}


// ---- 状態の欄の右(計測資料)
app.on("aside", values => {
  $("aside").replaceChildren(...app.UI.STATUS_ASIDE.map(k => {
    const lines = app.UI.STATUS_ASIDE_LINES[k] || 1;
    const d = el("div", { textContent: values[k] || "", className: lines === 1 ? "one" : "" });
    d.style.maxHeight = `${lines * 1.45}em`;
    d.title = values[k] || "";
    return d;
  }));
});

// ---- 設定(フェーズ設定をスキップ・機器PASS)。測定中は変えられない
app.on("settings", s => { $("pass").value = s.pass || ""; $("skip").checked = !!s.skip; $("mini-on").checked = !!s.mini; $("mini-top").checked = !!s.top; });
$("mini-on").addEventListener("change", () => api().set_mini_enabled($("mini-on").checked));   // 最小化時に小窓にするか(いつでも変えられる)
$("mini-top").addEventListener("change", () => api().set_mini_top($("mini-top").checked));    // 小窓を常時前面に表示するか

// ---- 小窓: 最小化すると同じ窓を小さくして、状態の欄とボタン・「常時前面に表示する」を出す(決まりは web_app.py)。
// 「常時前面に表示する」の行の右の切り替えで モード表・本体画面を足す。窓の高さは中身に合わせる(物理の px で渡す。body と innerHeight の比で全体の倍率を除く)
function fitMini() {
  if (!document.body.classList.contains("compact")) return;
  requestAnimationFrame(() => {
    const body = document.body.getBoundingClientRect(), left = $("left").getBoundingClientRect();
    const pad = left.top - body.top;
    api().fit_mini((left.height + pad * 2) * (window.innerHeight / body.height) * window.devicePixelRatio);
  });
}
app.on("compact", on => { document.body.classList.toggle("compact", !!on); if (on) fitMini(); else fitPanes(); });
app.on("mini_view", parts => {
  for (const v of app.UI.MINI_VIEWS) document.body.classList.toggle(`mini-${v}`, parts.includes(v));
  for (const b of $("mini-views").children) b.classList.toggle("on", b.dataset.view === app.UI.MINI_NONE ? !parts.length : parts.includes(b.dataset.view));
  fitMini();
});
app.on("settings_lock", on => { $("pass").disabled = on; $("skip").disabled = on; });
$("skip").addEventListener("change", () => api().set_skip_phases($("skip").checked));
$("pass").addEventListener("input", () => {
  const v = $("pass").value.replace(/[^0-9]/g, "");            // 半角数字だけ
  if (v !== $("pass").value) $("pass").value = v;
});
$("pass").addEventListener("change", () => api().set_pass($("pass").value));
$("pass").addEventListener("blur", () => api().set_pass($("pass").value));

// ---- 測定モード表(行のモード欄は選択欄、フェーズは行ごとの入力欄。下のドックに 追加 / 削除 / 合計)
let modeSel = new Set(), modeData = null;
function renderModes(m) {
  modeData = m;
  const ids = new Set(m.rows.map(r => r.id));
  modeSel = new Set([...modeSel].filter(i => ids.has(i)));
  if (m.rows.some(r => r.tag === "run")) modeSel.clear();
  const tbody = $("modes").querySelector("tbody");
  const focused = document.activeElement;
  const keep = focused && focused.dataset && focused.dataset.rid ? [focused.dataset.rid, focused.dataset.k, focused.selectionStart] : null;
  tbody.replaceChildren(...m.rows.map(r => {
    const tr = el("tr", { className: modeSel.has(r.id) ? "sel" : "" });
    tr.dataset.rid = r.id;
    const colors = r.tag ? app.UI.MODE_ROW_COLORS[r.tag] : null;
    if (colors && !modeSel.has(r.id)) { tr.style.background = colors[0]; tr.style.color = colors[1]; }
    const sel = el("select", { disabled: m.locked });
    for (const a of m.apps) sel.append(el("option", { value: a, textContent: a, selected: a === r.app }));
    if (!m.apps.includes(r.app)) sel.append(el("option", { value: r.app, textContent: r.app, selected: true }));
    sel.addEventListener("change", () => api().mode_set(r.id, sel.value));
    const ph = el("td", { className: "ph" });
    r.phases.forEach((v, k) => {
      if (k) ph.append(el("span", { className: "plus", textContent: "+" }));
      const inp = el("input", { value: v, disabled: m.locked, className: r.bad[k] ? "bad" : "" });
      inp.dataset.rid = r.id; inp.dataset.k = k;
      const vals = () => [...ph.querySelectorAll("input")].map(i => i.value);
      inp.addEventListener("input", () => api().mode_phases(r.id, vals(), false));
      inp.addEventListener("change", () => api().mode_phases(r.id, vals(), true));
      inp.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
        if (e.key === "Escape") { e.preventDefault(); api().mode_revert(r.id); inp.blur(); }
      });
      ph.append(inp);
    });
    tr.append(el("td", { className: "no", textContent: r.no }), el("td", {}, sel), ph);
    return tr;
  }));
  if (keep) {
    const i = tbody.querySelector(`input[data-rid="${keep[0]}"][data-k="${keep[1]}"]`);
    if (i) { i.focus(); try { i.setSelectionRange(keep[2], keep[2]); } catch (_) {} }
  }
  $("mode-total").textContent = m.total;
  $("mode-add").classList.toggle("off", m.locked);
  $("mode-del").classList.toggle("off", m.locked || !modeSel.size);
}
app.on("modes", renderModes);
$("modes").addEventListener("mousedown", e => {
  if (e.target.closest("select, input")) return;
  const tr = e.target.closest("tbody tr");
  if (!tr) { modeSel.clear(); } else if (e.ctrlKey) { modeSel.has(tr.dataset.rid) ? modeSel.delete(tr.dataset.rid) : modeSel.add(tr.dataset.rid); }
  else modeSel = new Set([tr.dataset.rid]);
  renderModes(modeData);
});
$("modes").addEventListener("keydown", e => {
  if (e.target.closest("input, select")) return;
  if (e.key === "Delete") $("mode-del").click();
  if (e.key === "Escape") { modeSel.clear(); renderModes(modeData); }
});
$("mode-add").addEventListener("click", async () => {
  if (modeData.locked) return;
  const order = modeData.rows.map(r => r.id).filter(i => modeSel.has(i));
  const id = await api().mode_add(order.length ? order[order.length - 1] : null);
  if (id) { modeSel = new Set([id]); renderModes(modeData); }
});
$("mode-del").addEventListener("click", () => { if (!modeData.locked && modeSel.size) api().mode_delete([...modeSel]); });

// ---- ログ・アラート
let alerts = 0;
function addLine(box, text) {
  const e = $(box);
  const stick = e.scrollTop + e.clientHeight >= e.scrollHeight - 4;
  e.append(document.createTextNode(text + "\n"));
  while (e.childNodes.length > (box === "alert" ? 100 : 300)) e.firstChild.remove();
  if (stick) e.scrollTop = e.scrollHeight;
}
function showTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("on", b.dataset.tab === name);
  $("log").hidden = name !== "log"; $("alert").hidden = name !== "alert";
}
const hhmm = () => new Date().toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
app.on("log", text => addLine("log", `${hhmm()} ${text}`));
app.on("alert", ([title, desc]) => {
  addLine("alert", `${hhmm()} ⚠ ${title}` + (desc ? `\n      ${desc}` : ""));
  alerts++;
  $("tab-alert").textContent = `⚠ ${app.UI.TAB_ALERTS} (${alerts})`;
  showTab("alert");
});
app.on("select_log", () => showTab("log"));
for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));

// ---- 本体画面: 届いた画面を映し、クリックを機器の px にして返す。つながっていない間は一覧・「接続」・案内の文
const screen = $("screen"), ctx = screen.getContext("2d");
app.frames = 0;
app.on("frame", f => {
  if (!f) { ctx.fillStyle = app.UI.DEVICE_BG; ctx.fillRect(0, 0, screen.width, screen.height); return; }
  const img = new Image();
  img.onload = () => {
    if (screen.width !== f.w || screen.height !== f.h) { screen.width = f.w; screen.height = f.h; }
    ctx.drawImage(img, 0, 0);
    app.frames++;
  };
  img.src = "data:image/png;base64," + f.png;
});
let manual = true;
app.on("manual", on => { manual = on; screen.style.cursor = on ? "pointer" : "default"; });
screen.addEventListener("click", e => {
  if (!manual || !$("device-overlay").hidden) return;
  const r = screen.getBoundingClientRect();
  api().click(Math.floor((e.clientX - r.left) * screen.width / r.width), Math.floor((e.clientY - r.top) * screen.height / r.height));
});
app.on("device_overlay", on => { $("device-overlay").hidden = !on; });
app.on("device_wait", on => { $("screen-wait").hidden = !on; });
app.on("device_list", d => {
  const rows = d.rows.map(r => {
    const tr = el("tr", { className: r.key === d.selected ? "sel" : "" },
      ...["serial", "host", "state"].map(k => el("td", { textContent: r[k], title: r[k] })));
    tr.addEventListener("click", () => api().select_device(r.key));
    tr.addEventListener("dblclick", () => api().connect());
    return tr;
  });
  while (rows.length < app.UI.DEVICE_LIST_ROWS) rows.push(el("tr", {}, el("td", { colSpan: 3 })));
  $("device-rows").replaceChildren(...rows);
  $("btn-connect").disabled = !d.can_connect;
  $("device-msg").textContent = d.message;
});
$("btn-connect").addEventListener("click", () => api().connect());

// ---- 照射前の確認: 安全確認をすべてチェックするまで「照射開始」は押せない。Enter・Esc はキャンセル。
//      照射してよいかは Python の側でも必ず確かめる(Controller.confirm_answer)
const dlg = $("confirm");
app.on("confirm", c => {
  dlg.querySelector(".icon").textContent = c.icon;
  dlg.querySelector(".lead").textContent = c.lead;
  const ok = dlg.querySelector(".ok");
  ok.textContent = c.ok;
  const boxes = c.checks.map(t => el("input", { type: "checkbox" }));
  dlg.querySelector(".checks").replaceChildren(...c.checks.map((t, i) => el("label", {}, boxes[i], el("span", { textContent: t }))));
  const update = () => { ok.disabled = !boxes.every(b => b.checked); };
  boxes.forEach(b => b.addEventListener("change", update));
  update();
  let answered = false;
  const answer = yes => {
    if (answered) return;
    answered = true;
    dlg.close();
    api().confirm(yes && boxes.every(b => b.checked), boxes.map(b => b.checked));
  };
  ok.onclick = () => answer(true);
  dlg.querySelector(".cancel").onclick = () => answer(false);
  dlg.onkeydown = e => { if (e.key === "Enter" || e.key === "Escape") { e.preventDefault(); answer(false); } };
  dlg.oncancel = e => { e.preventDefault(); answer(false); };
  dlg.showModal();
  dlg.querySelector(".cancel").focus();
});
const msg = $("message");
app.on("message", m => {
  msg.querySelector(".title").textContent = m.title; msg.querySelector(".text").textContent = m.text;
  msg.querySelector(".close").onclick = () => msg.close();
  msg.showModal();
});

// ---- テスト用の知らせ(順番どおりに届くか)
app.on("test", n => app.stats.order.push(n));

// ---- 始まり: 見た目の値を受け取ってから、知らせを受け取れることを Python に伝える
window.addEventListener("pywebviewready", async () => {
  try {
    const h = await api().hello();
    app.UI = h.ui;
    applyUI(h.ui, h.zoom);
    setupRight(h.ui, h.layout);                                   // 右の列(right.js)
    await api().ready();
  } catch (e) { report(e); }
});
