const CSRF = document.querySelector('meta[name=csrf]')?.content;
async function api(url, data) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF}, body: JSON.stringify(data || {})});
  const j = await r.json().catch(() => ({error: 'Unexpected server response'}));
  if (!r.ok) throw new Error(j.error || 'Request failed');
  return j;
}
function busy(btn, on, label) {
  if (on) { btn.dataset.l = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spin"></span> ' + (label || 'Working…'); }
  else { btn.disabled = false; btn.innerHTML = btn.dataset.l; }
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function ringHTML(v, s = 96) { const c = v >= 75 ? 'hi' : v >= 50 ? 'mid' : 'lo'; return `<div class="ring ${c}" style="--v:${v};--s:${s}px"><span>${v}</span></div>`; }
