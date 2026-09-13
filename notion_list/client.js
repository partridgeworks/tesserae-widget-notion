// notion_list, Spectra list archetype. One row per record: title and status
// on top, a progress bar underneath where the database tracks one. Bars are
// solid blocks against a sunken track — no drawn borders, which dither into
// nothing on Spectra 6.

import {
  CSS, countTile, dateLabel, emptyCard, emptyMessage, errorCard, escapeHtml,
} from "../notion_core/static/notion-widgets.js";

const WIDGET = "notion_list";
const MAX_ROWS = { xs: 2, sm: 4, md: 6, lg: 16 };

// Same reasoning as notion_tasks: meta columns share the title's row, so each
// size keeps only what leaves the title legible.
const META = {
  xs: { status: false, date: false, person: false, progress: false },
  sm: { status: false, date: true, person: false, progress: true },
  md: { status: true, date: true, person: false, progress: true },
  lg: { status: true, date: true, person: true, progress: true },
};

// Fill colour tracks how close to done a row is, so a wall of bars reads as
// a heat map rather than a wall of one colour.
function barColor(p) {
  if (p >= 0.99) return "var(--accent-3)";
  if (p >= 0.5) return "var(--accent-2)";
  return "var(--accent-4)";
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const fragment = ctx?.cell?.fragment ?? ctx?.fragment ?? "full";
  const opts = ctx?.cell?.options || {};
  const size = ctx?.cell?.size || "md";
  const title = data.title || "List";

  if (data.error) {
    errorCard(shadow, WIDGET, title, data.error);
    return;
  }

  const total = data.total ?? 0;
  const overdue = data.overdue_count ?? 0;

  if (fragment === "count") {
    countTile(shadow, WIDGET, {
      number: total,
      label: overdue > 0 ? `${overdue} late` : "rows",
      alarm: overdue > 0,
    });
    return;
  }

  if (data.empty) {
    emptyCard(shadow, WIDGET, {
      icon: "ph-list-bullets",
      title,
      message: emptyMessage(data, "Nothing to show."),
    });
    return;
  }

  const room = META[size] || META.md;
  const showProgress = opts.show_progress !== false && data.has_progress !== false && room.progress;
  const showDate = opts.show_date !== false && data.has_date !== false && room.date;
  const showPerson = opts.show_person === true && room.person;
  const showStatus = data.has_status !== false && room.status;

  const all = Array.isArray(data.items) ? data.items : [];
  const items = all.slice(0, MAX_ROWS[size] ?? all.length);

  const rows = items.map((item, i) => {
    const meta = [];
    if (showStatus && item.status) {
      meta.push(`<span class="chip">${escapeHtml(item.status)}</span>`);
    }
    if (showDate && item.date) {
      const style = item.overdue
        ? "color:var(--accent-1);font-weight:var(--fw-black)"
        : "";
      meta.push(`<span style="${style}">${dateLabel(item.date)}</span>`);
    }
    if (showPerson && item.person) {
      meta.push(`<span class="u-muted">${escapeHtml(item.person)}</span>`);
    }

    const hasBar = showProgress && typeof item.progress === "number";
    const pct = hasBar ? Math.round(item.progress * 100) : 0;
    const bar = hasBar
      ? `<div class="bar-row">
           <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${barColor(item.progress)}"></div></div>
           <span class="bar-pct">${pct}%</span>
         </div>`
      : "";

    return `
      <div class="row ${i % 2 ? "is-zebra" : ""}">
        <div class="row-head">
          <span class="row-title">${escapeHtml(item.title)}</span>
          ${meta.length ? `<span class="list-meta">${meta.join(" ")}</span>` : ""}
        </div>
        ${bar}
      </div>`;
  }).join("");

  const hidden = total - items.length;
  const more = hidden > 0
    ? `<div class="row"><span class="u-muted" style="font-size:var(--fs-caption)">+ ${hidden} more</span></div>`
    : "";

  const metaBits = [];
  if (size === "xs") {
    metaBits.push(overdue > 0 ? `${overdue} LATE` : `${total} ROWS`);
  } else {
    if (overdue > 0) metaBits.push(`${overdue} LATE`);
    metaBits.push(`${total} ROWS`);
  }

  shadow.innerHTML = `
    ${CSS}
    <style>
      .row { display:flex; flex-direction:column; gap:calc(var(--space-1) / 2);
             padding: var(--space-1) var(--space-2); }
      .row.is-zebra { background: var(--surface-sunken); }
      .row-head { display:flex; align-items:baseline; justify-content:space-between;
                  gap: var(--space-2); min-width:0; }
      .row-title { font-weight: var(--fw-bold); overflow:hidden;
                   text-overflow:ellipsis; white-space:nowrap; min-width:0; }
      .list-meta { display:inline-flex; align-items:center; gap: var(--space-1);
                   font-size: var(--fs-caption); color: var(--text-secondary);
                   white-space: nowrap; flex: 0 0 auto; }
      .chip { background: var(--surface); color: var(--text-secondary);
              border-radius: var(--pill-radius, 999px);
              padding: 0 var(--space-1); font-size: var(--fs-caption);
              text-transform: var(--label-transform, none); }
      .is-zebra .chip { background: var(--bg); }
      .bar-row { display:flex; align-items:center; gap: var(--space-1); }
      .bar-track { flex:1 1 auto; height:0.5em; background: var(--surface);
                   border-radius: var(--pill-radius, 999px); overflow:hidden; }
      .is-zebra .bar-track { background: var(--bg); }
      .bar-fill { height:100%; border-radius: var(--pill-radius, 999px); }
      .bar-pct { flex:0 0 auto; font-size: var(--fs-caption);
                 color: var(--text-secondary); font-variant-numeric: tabular-nums; }
      .size-xs .row-title {
        white-space: normal; display: -webkit-box; -webkit-line-clamp: 2;
        -webkit-box-orient: vertical; overflow: hidden;
      }
    </style>
    <div class="w size-${size}" data-widget="${WIDGET}">
      <div class="w-title">
        <i class="ph-bold ph-list-bullets" style="color:${overdue > 0 ? "var(--accent-1)" : "var(--accent-3)"}"></i>
        <h3>${escapeHtml(title)}</h3>
        <span class="w-title-meta">${metaBits.join(" · ")}</span>
      </div>
      <div class="w-body list-body">${rows}${more}</div>
    </div>`;
}
