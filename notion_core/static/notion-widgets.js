// notion_core/static/notion-widgets.js — the parts every notion_* cell paints
// the same way: the stylesheet link, HTML escaping, date labels, and the
// whole-cell states (error, empty, a big-number count tile).
//
// Widgets import it relatively, `../notion_core/static/notion-widgets.js`,
// which resolves against /plugins/<widget>/client.js to
// /plugins/notion_core/static/notion-widgets.js — the host serves any file
// under a plugin's static/ folder, and Notion Core is already a hard
// requirement of every widget in the family.

export const CSS = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// "2026-09-12" → "Sep 12"; a different year adds it ("Sep 12, 2025"); a
// datetime keeps its time ("Sep 12, 14:30"). Anything unparseable is shown
// as typed rather than as "Invalid Date". Already escaped.
export function dateLabel(iso, { today = false } = {}) {
  const raw = String(iso || "");
  if (!raw) return "";
  if (today) return "today";
  const hasTime = raw.includes("T");
  const d = new Date(hasTime ? raw : raw + "T00:00:00");
  if (isNaN(d)) return escapeHtml(raw);
  const opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  if (hasTime) { opts.hour = "2-digit"; opts.minute = "2-digit"; }
  return d.toLocaleDateString(undefined, opts);
}

export function errorCard(shadow, widget, title, message) {
  shadow.innerHTML = `
    ${CSS}
    <div class="w" data-widget="${widget}">
      <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>${escapeHtml(title)}</h3></div>
      <div class="w-body"><p class="u-muted">${escapeHtml(message)}</p></div>
    </div>`;
}

export function emptyCard(shadow, widget, { icon, title, message }) {
  shadow.innerHTML = `
    ${CSS}
    <div class="w" data-widget="${widget}">
      <div class="w-title"><i class="ph-bold ${icon}" style="color:var(--accent-3)"></i><h3>${escapeHtml(title)}</h3></div>
      <div class="w-body" style="justify-content:center;align-items:center">
        <i class="ph-bold ph-check-circle" style="color:var(--accent-3);font-size:3em"></i>
        <p class="u-muted">${escapeHtml(message)}</p>
      </div>
    </div>`;
}

// What the "count" fragment paints: one number that reads from across the
// room, and a one-word label under it. `alarm` colours the number accent-1.
export function countTile(shadow, widget, { number, label, alarm }) {
  shadow.innerHTML = `
    ${CSS}
    <style>
      .count-wrap { width:100%; height:100%; display:flex; flex-direction:column;
                    align-items:center; justify-content:center; gap:1cqmin;
                    container-type:size; }
      .count-num { font-size:38cqmin; font-weight:var(--fw-black); line-height:1;
                   font-variant-numeric:tabular-nums;
                   color:${alarm ? "var(--accent-1)" : "var(--text-primary)"}; }
      .count-label { font-size:9cqmin; letter-spacing:var(--ls-label);
                     text-transform:uppercase; color:var(--text-secondary);
                     font-weight:var(--fw-bold); }
    </style>
    <div class="w" data-widget="${widget}">
      <div class="count-wrap">
        <div class="count-num">${Number(number) || 0}</div>
        <div class="count-label">${escapeHtml(label)}</div>
      </div>
    </div>`;
}

// The empty-state sentence for a filtered cell. "Nothing open" after a
// filter reads as a broken widget; naming what was filtered on is the
// difference between "all done" and "nothing here is assigned to that
// person".
export function emptyMessage(data, fallback) {
  if (data.filtered_by) return `Nothing for ${data.filtered_by}.`;
  if (data.condition) return `Nothing matches: ${data.condition}.`;
  return fallback;
}
