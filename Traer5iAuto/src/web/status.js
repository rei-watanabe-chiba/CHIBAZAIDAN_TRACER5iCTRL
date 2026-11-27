// 状態の欄とメインのボタン(小窓にしたときも同じ要素を出す)
"use strict";

// ---- 状態の欄(タイトル・説明・進捗バー。照射中は 2 色で明滅)
let blink = null;
app.on("status", s => {
  const box = $("status");
  box.style.background = s.bg; box.style.color = s.fg;
  const t = $("status-title");                      // 記号: 「ico-…」は線の絵(app.css)、ほかは文字
  if (s.icon && s.icon.startsWith("ico-")) t.replaceChildren(el("i", { className: "ico " + s.icon }), s.title);
  else t.textContent = s.icon ? `${s.icon} ${s.title}` : s.title;
  clearInterval(blink); blink = null;
  $("status-title").style.color = "";
  if (s.blink) {
    let n = 0;
    blink = setInterval(() => { $("status-title").style.color = s.blink[++n % 2]; }, s.blink_ms);
  }
});
app.on("detail", text => { $("status-detail").textContent = text || ""; });
app.on("progress", p => { $("status-bar").hidden = !p.show; $("status-bar").firstChild.style.width = `${Math.max(0, Math.min(1, p.value)) * 100}%`; });

// ---- メインのボタン・「終了」
function setButton(b, enabled, text, color) {
  if (text != null) b.textContent = text;
  if (color != null) b.dataset.color = color;
  b.disabled = !enabled;
  b.style.setProperty("--c", enabled ? b.dataset.color : app.UI.MAIN_BUTTON.busy[1]);
}
app.on("button", b => setButton($("btn-main"), b.enabled, b.text, b.color));
app.on("end_button", b => { $("btn-end").hidden = !b.show; setButton($("btn-end"), b.enabled, app.UI.MAIN_BUTTON.end_turn[0], app.UI.MAIN_BUTTON.end_turn[1]); });
$("btn-main").addEventListener("click", () => api().main());
$("btn-end").addEventListener("click", () => api().end_turn());

