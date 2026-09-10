// notion_projects, Spectra list archetype. One row per active project:
// title and status on top, a progress bar underneath where the database
// tracks one. Bars are solid blocks against a sunken track — no drawn
// borders, which dither into nothing on Spectra 6.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const MAX_ROWS = { xs: 2, sm: 4, md: 6, lg: 16 };

// Same reasoning as notion_tasks: meta columns share the title's row, so each
// size keeps only what leaves the project name legible.
const META = {
  xs: { status: false, due: false, owner: false, progress: false },
  sm: { status: false, due: true, owner: false, progress: true },
  md: { status: true, due: true, owner: false, progress: true },
  lg: { status: true, due: true, owner: true, progress: true },
};

function dueLabel(item) {
  const raw = String(item.due_date || "");
  if (!raw) return "";
  const d = new Date(raw + "T00:00:00");
  if (isNaN(d)) return escapeHtml(raw);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// Fill colour tracks how close to done a project is, so a wall of bars reads
// as a heat map rather than a wall of one colour.
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
  const title = data.title || "Projects";
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="notion_projects">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>${escapeHtml(title)}</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const total = data.total ?? 0;
  const overdue = data.overdue_count ?? 0;

  if (fragment === "count") {
    shadow.innerHTML = `
      ${css}
      <style>
        .count-wrap { width:100%; height:100%; display:flex; flex-direction:column;
                      align-items:center; justify-content:center; gap:1cqmin;
                      container-type:size; }
        .count-num { font-size:38cqmin; font-weight:var(--fw-black); line-height:1;
                     font-variant-numeric:tabular-nums;
                     color:${overdue > 0 ? "var(--accent-1)" : "var(--text-primary)"}; }
        .count-label { font-size:9cqmin; letter-spacing:var(--ls-label);
                       text-transform:uppercase; color:var(--text-secondary);
                       font-weight:var(--fw-bold); }
      </style>
      <div class="w" data-widget="notion_projects">
        <div class="count-wrap">
          <div class="count-num">${total}</div>
          <div class="count-label">${overdue > 0 ? `${overdue} late` : "active"}</div>
        </div>
      </div>`;
    return;
  }

  if (data.empty) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="notion_projects">
        <div class="w-title"><i class="ph-bold ph-kanban" style="color:var(--accent-3)"></i><h3>${escapeHtml(title)}</h3></div>
        <div class="w-body" style="justify-content:center;align-items:center">
          <i class="ph-bold ph-check-circle" style="color:var(--accent-3);font-size:3em"></i>
          <p class="u-muted">${data.filtered_by
            ? `Nothing for ${escapeHtml(data.filtered_by)}.`
            : "No active projects."}</p>
        </div>
      </div>`;
    return;
  }

  const room = META[size] || META.md;
  const showProgress = opts.show_progress !== false && data.has_progress !== false && room.progress;
  const showDue = opts.show_due !== false && data.has_due !== false && room.due;
  const showOwner = opts.show_owner === true && room.owner;
  const showStatus = data.has_status !== false && room.status;

  const all = Array.isArray(data.items) ? data.items : [];
  const items = all.slice(0, MAX_ROWS[size] ?? all.length);

  const rows = items.map((item, i) => {
    const meta = [];
    if (showStatus && item.status) {
      meta.push(`<span class="chip">${escapeHtml(item.status)}</span>`);
    }
    if (showDue && item.due_date) {
      const style = item.overdue
        ? "color:var(--accent-1);font-weight:var(--fw-black)"
        : "";
      meta.push(`<span style="${style}">${dueLabel(item)}</span>`);
    }
    if (showOwner && item.owner) {
      meta.push(`<span class="u-muted">${escapeHtml(item.owner)}</span>`);
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
      <div class="proj-row ${i % 2 ? "is-zebra" : ""}">
        <div class="proj-head">
          <span class="proj-title">${escapeHtml(item.title)}</span>
          ${meta.length ? `<span class="list-meta">${meta.join(" ")}</span>` : ""}
        </div>
        ${bar}
      </div>`;
  }).join("");

  const hidden = total - items.length;
  const more = hidden > 0
    ? `<div class="proj-row"><span class="u-muted" style="font-size:var(--fs-caption)">+ ${hidden} more</span></div>`
    : "";

  const metaBits = [];
  if (size === "xs") {
    metaBits.push(overdue > 0 ? `${overdue} LATE` : `${total} ACTIVE`);
  } else {
    if (overdue > 0) metaBits.push(`${overdue} LATE`);
    metaBits.push(`${total} ACTIVE`);
  }

  shadow.innerHTML = `
    ${css}
    <style>
      .proj-row { display:flex; flex-direction:column; gap:calc(var(--space-1) / 2);
                  padding: var(--space-1) var(--space-2); }
      .proj-row.is-zebra { background: var(--surface-sunken); }
      .proj-head { display:flex; align-items:baseline; justify-content:space-between;
                   gap: var(--space-2); min-width:0; }
      .proj-title { font-weight: var(--fw-bold); overflow:hidden;
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
      .size-xs .list-title, .size-xs .proj-title {
        white-space: normal; display: -webkit-box; -webkit-line-clamp: 2;
        -webkit-box-orient: vertical; overflow: hidden;
      }
    </style>
    <div class="w size-${size}" data-widget="notion_projects">
      <div class="w-title">
        <i class="ph-bold ph-kanban" style="color:${overdue > 0 ? "var(--accent-1)" : "var(--accent-3)"}"></i>
        <h3>${escapeHtml(title)}</h3>
        <span class="w-title-meta">${metaBits.join(" · ")}</span>
      </div>
      <div class="w-body list-body">${rows}${more}</div>
    </div>`;
}
