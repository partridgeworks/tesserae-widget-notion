// notion_tasks, Spectra list archetype. Zebra rows of open Notion tasks:
// a priority-coloured dot, the task title, then due + project meta on the
// right. Overdue due-text goes accent-1 bold so the panel reads at a glance
// from across the room. "count" fragment = one big open-task number.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// priority_rank: 4 urgent … 1 low, 0 unset. Accent-1 is the alarm colour,
// so it is reserved for urgent/high and for overdue dates.
const PRIO_COLOR = {
  4: "var(--accent-1)",
  3: "var(--accent-1)",
  2: "var(--accent-2)",
  1: "var(--accent-4)",
  0: "var(--text-muted)",
};

const MAX_ROWS = { xs: 2, sm: 5, md: 8, lg: 20 };

// Meta columns compete with the task title for the same row. Each size keeps
// only what still leaves the title readable: at xs the title is the whole
// point, so everything else goes rather than eliding "Renew the domain" down
// to "Re...". Cell options can still switch a column off, never on.
const META = {
  xs: { status: false, due: false, project: false },
  sm: { status: false, due: true, project: false },
  md: { status: false, due: true, project: true },
  lg: { status: true, due: true, project: true },
};

function dueLabel(item) {
  const raw = String(item.due_date || "");
  if (!raw) return "";
  if (item.today) return "today";
  const d = new Date(raw + "T00:00:00");
  if (isNaN(d)) return escapeHtml(raw);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  // Panels canvas passes the fragment on the cell; older hosts put it at the
  // top level. Read both so a fragment placement works either way.
  const fragment = ctx?.cell?.fragment ?? ctx?.fragment ?? "full";
  const opts = ctx?.cell?.options || {};
  const size = ctx?.cell?.size || "md";
  const title = data.title || "Tasks";
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="notion_tasks">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>${escapeHtml(title)}</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const overdue = data.overdue_count ?? 0;
  const total = data.total ?? 0;

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
      <div class="w" data-widget="notion_tasks">
        <div class="count-wrap">
          <div class="count-num">${total}</div>
          <div class="count-label">${overdue > 0 ? `${overdue} overdue` : "open tasks"}</div>
        </div>
      </div>`;
    return;
  }

  if (data.empty) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="notion_tasks">
        <div class="w-title"><i class="ph-bold ph-list-checks" style="color:var(--accent-3)"></i><h3>${escapeHtml(title)}</h3></div>
        <div class="w-body" style="justify-content:center;align-items:center">
          <i class="ph-bold ph-check-circle" style="color:var(--accent-3);font-size:3em"></i>
          <p class="u-muted">Nothing open.</p>
        </div>
      </div>`;
    return;
  }

  const room = META[size] || META.md;
  const showDue = opts.show_due !== false && data.has_due !== false && room.due;
  const showProject = opts.show_project !== false && data.has_project !== false && room.project;
  const showStatus = opts.show_status !== false && data.has_status !== false && room.status;

  const all = Array.isArray(data.items) ? data.items : [];
  const items = all.slice(0, MAX_ROWS[size] ?? all.length);

  const rows = items.map((item, i) => {
    const dot = PRIO_COLOR[item.priority_rank] || PRIO_COLOR[0];
    const meta = [];

    if (showStatus && item.status) {
      meta.push(`<span class="chip">${escapeHtml(item.status)}</span>`);
    }
    if (showDue && item.due_date) {
      const style = item.overdue
        ? "color:var(--accent-1);font-weight:var(--fw-black)"
        : item.today
          ? "color:var(--text-primary);font-weight:var(--fw-bold)"
          : "";
      meta.push(`<span style="${style}">${dueLabel(item)}</span>`);
    }
    if (showProject && item.project) {
      meta.push(`<span class="u-muted">${escapeHtml(item.project)}</span>`);
    }

    const icon = item.done ? "ph-check-circle" : "ph-circle";
    return `
      <div class="list-row ${i % 2 ? "is-zebra" : ""}">
        <div class="list-lead">
          <i class="ph-bold ${icon}" style="color:${item.done ? "var(--text-muted)" : dot}"></i>
          <span class="list-title" ${item.done ? 'style="color:var(--text-muted)"' : ""}>${escapeHtml(item.title)}</span>
        </div>
        ${meta.length ? `<span class="list-meta">${meta.join(" ")}</span>` : ""}
      </div>`;
  }).join("");

  const hidden = total - items.length;
  const more = hidden > 0
    ? `<div class="list-row"><span class="u-muted" style="font-size:var(--fs-caption)">+ ${hidden} more</span></div>`
    : "";

  // The header meta wraps to two lines at xs and steals a whole row, so
  // there it collapses to the single number worth knowing.
  const metaBits = [];
  if (size === "xs") {
    metaBits.push(overdue > 0 ? `${overdue} LATE` : `${total} OPEN`);
  } else {
    if (overdue > 0) metaBits.push(`${overdue} OVERDUE`);
    metaBits.push(`${total} OPEN`);
  }

  shadow.innerHTML = `
    ${css}
    <style>
      .list-meta { display:inline-flex; align-items:center; gap: var(--space-1);
                   font-size: var(--fs-caption); color: var(--text-secondary);
                   white-space: nowrap; flex: 0 0 auto; }
      .list-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .size-xs .list-title, .size-xs .proj-title {
        white-space: normal; display: -webkit-box; -webkit-line-clamp: 2;
        -webkit-box-orient: vertical; overflow: hidden;
      }
      .chip { background: var(--surface-sunken); color: var(--text-secondary);
              border-radius: var(--pill-radius, 999px);
              padding: 0 var(--space-1); font-size: var(--fs-caption);
              text-transform: var(--label-transform, none); }
    </style>
    <div class="w size-${size}" data-widget="notion_tasks">
      <div class="w-title">
        <i class="ph-bold ph-list-checks" style="color:${overdue > 0 ? "var(--accent-1)" : "var(--accent-3)"}"></i>
        <h3>${escapeHtml(title)}</h3>
        <span class="w-title-meta">${metaBits.join(" · ")}</span>
      </div>
      <div class="w-body list-body">${rows}${more}</div>
    </div>`;
}
