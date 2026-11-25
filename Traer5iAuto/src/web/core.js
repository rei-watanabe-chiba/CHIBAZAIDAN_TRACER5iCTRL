// WebView2 版の画面の土台: 知らせの受け取り(app.receive / app.on)・Python の呼び出し・見た目の値(applyBase)。
// Python → 画面: app.receive([[種類, 中身], ...]) / 画面 → Python: pywebview.api.…(中身の処理は Python の「画面の糸」で順に行う)
"use strict";

// 蓋のセンサーの F24 の連打(押したままの間 約 30 ms おき)は、画面のキーの処理へ渡さない(LID_SENSOR.md)
for (const t of ["keydown", "keyup", "keypress"]) {
  addEventListener(t, e => { if (e.key === ((app.UI && app.UI.SENSOR_KEY) || "F24")) e.stopImmediatePropagation(); }, true);
}

const app = {
  UI: null,                 // gui_config.UI の値(Python の hello で受け取る)
  handlers: {},
  stats: { received: 0, order: [] },   // テスト用
  state: {},                // 最後に届いた中身(テスト用: button・status・modes・device_list など)
  on(kind, fn) { (this.handlers[kind] ||= []).push(fn); },
  receive(msgs) {
    for (const [kind, data] of msgs) {
      this.stats.received++;
      this.state[kind] = data;
      for (const fn of this.handlers[kind] || []) {
        try { fn(data); } catch (e) { report(e); }
      }
    }
  },
};
const api = () => window.pywebview.api;

function report(e) {
  try { api().log(String(e && e.stack || e)); } catch (_) { /* まだつながっていない */ }
}
window.addEventListener("error", e => report(e.error || e.message));
window.addEventListener("unhandledrejection", e => report(e.reason));

const $ = id => document.getElementById(id);
const el = (tag, props = {}, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };

// ---- UI の値: 色(#rrggbb)・数(px)を CSS の変数 --名前 に、文言を data-ui の要素に
function ptToPx(font) { return Array.isArray(font) && typeof font[1] === "number" ? `${Math.round(font[1] * 4 / 3)}px` : null; }

function applyBase(ui, zoom) {
  const root = document.documentElement.style;
  for (const [k, v] of Object.entries(ui)) {
    if (typeof v === "string" && /^#[0-9a-f]{6}$/i.test(v)) root.setProperty(`--${k}`, v);
    else if (typeof v === "number") { root.setProperty(`--${k}`, `${v}px`); root.setProperty(`--${k}-n`, String(v)); }
  }
  const pair = (name, [a, b], unit = "px") => { root.setProperty(`--${name}-x`, a + unit); root.setProperty(`--${name}-y`, b + unit); };
  pair("status-pad", ui.STATUS_PAD); pair("dock-pad", ui.MODE_DOCK_PAD);
  root.setProperty("--split-l", ui.STATUS_SPLIT[0]); root.setProperty("--split-r", ui.STATUS_SPLIT[1]);
  root.setProperty("--sel-bg", ui.MODE_SEL[0]); root.setProperty("--sel-fg", ui.MODE_SEL[1]);
  root.setProperty("--DEVICE_LIST_PAD", `${ui.DEVICE_LIST_PAD[0]}px`);
  root.setProperty("--danger-icon", ui.DANGER.icon_fg);
  root.setProperty("--FONT_FAMILY", `"${ui.FONT_FAMILY}"`);
  root.setProperty("--FONT_SEMIBOLD", `"${ui.FONT_SEMIBOLD}"`);
  root.setProperty("--font-size", ptToPx(ui.FONT_LOG) || "12px");
  root.setProperty("--font-title", ptToPx(ui.FONT_TITLE) || "12px");
  document.documentElement.style.zoom = String(zoom);         // 全体の倍率(UI.ZOOMS。画面の高さで選ぶ)
  document.title = ui.WEB_TITLE;
  for (const e of document.querySelectorAll("[data-ui]")) e.textContent = ui[e.dataset.ui] ?? "";
}
