// notion_cards. A grid of bordered cards, one per record, each stacking the
// columns the cell asked for: a checkbox for a checkbox, badges for a select
// or status, a formatted date, a number in its Notion format, a rollup's
// gathered values one per line, and text for everything else — each at the
// size the cell chose, wrapping to the lines it allowed, with or without the
// column name above it, and as far apart as the cell asked. Optionally under
// a heading per value of one column.
//
// The border is the one place this family draws a line: it is what makes a
// card a card. It is drawn at --stroke-1 (2px), the e-ink minimum, so it
// survives dithering on Spectra 6.

import {
  CSS, dateLabel, emptyCard, emptyMessage, errorCard, escapeHtml,
} from "../notion_core/static/notion-widgets.js";

const WIDGET = "notion_cards";

// xs … xl → font-size, in em of the widget's fluid base. A field's badges,
// checkbox icon and name label all size from this, so one setting moves
// the whole field.
const SIZE_EM = { xs: 0.72, s: 0.86, m: 1, l: 1.3, xl: 1.7 };

// The most columns a cell size can carry before every card is a sliver.
const COLUMN_ROOM = { xs: 1, sm: 2, md: 4, lg: 6 };

// "Space between properties", in em, added to the gap a card always has
// between its fields. The server clamps it too; this is the belt.
const MAX_FIELD_GAP = 5;

const CURRENCY = {
  dollar: "$", canadian_dollar: "CA$", australian_dollar: "A$", singapore_dollar: "S$",
  euro: "€", pound: "£", yen: "¥", rupee: "₹", won: "₩", yuan: "CN¥", real: "R$",
  franc: "CHF ", krona: "kr ", rupiah: "Rp ", ruble: "₽", peso: "$", lira: "₺",
  rand: "R", baht: "฿", dirham: "AED ", zloty: "zł ", shekel: "₪", hong_kong_dollar: "HK$",
  new_zealand_dollar: "NZ$", mexican_peso: "MX$", philippine_peso: "₱", ringgit: "RM ",
};

function numberLabel(v) {
  if (!v || typeof v.value !== "number") return "";
  if (v.format === "percent") return `${Math.round(v.value * 100)}%`;
  const text = v.value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  const symbol = CURRENCY[v.format];
  return symbol ? `${symbol}${text}` : text;
}

function dateText(v) {
  if (!v || !v.start) return "";
  const start = dateLabel(v.start);
  const end = v.end ? dateLabel(v.end) : "";
  return end && end !== start ? `${start} – ${end}` : start;
}

function fieldHtml(f, lead) {
  const em = SIZE_EM[f.size] ?? 1;
  const name = f.show_name ? `<span class="f-name">${escapeHtml(f.name)}</span>` : "";

  if (f.kind === "checkbox") {
    // The one inline field: a box with nothing next to it says nothing, so
    // the name sits on the same line rather than above.
    return `
      <div class="f f-check" style="font-size:${em}em">
        <i class="ph-bold ${f.value ? "ph-check-square" : "ph-square"}"></i>
        ${f.show_name ? `<span class="f-check-name">${escapeHtml(f.name)}</span>` : ""}
      </div>`;
  }

  let body = "";
  if (f.kind === "badge") {
    const chips = (Array.isArray(f.value) ? f.value : []).map(
      (v) => `<span class="chip">${escapeHtml(v)}</span>`,
    );
    body = chips.length ? `<span class="f-badges">${chips.join("")}</span>` : "";
  } else if (f.kind === "lines") {
    // A rollup's gathered values, one per line. Every entry gets its line:
    // the max-lines setting is a floor here, not a cap, and a card too
    // short for them all gives lines up in fit() like any wrapped text.
    const items = (Array.isArray(f.value) ? f.value : []).map((v) => escapeHtml(v));
    const lines = Math.max(items.length, Number(f.lines) || 1);
    body = items.length
      ? `<span class="f-val is-wrap" style="-webkit-line-clamp:${lines}">${items.join("<br>")}</span>`
      : "";
  } else {
    const text = f.kind === "date" ? dateText(f.value)
      : f.kind === "number" ? numberLabel(f.value)
        : escapeHtml(f.value);
    // One line ends in an ellipsis; more wrap and clamp at that many.
    const lines = Math.max(1, Number(f.lines) || 1);
    body = text
      ? lines > 1
        ? `<span class="f-val is-wrap" style="-webkit-line-clamp:${lines}">${text}</span>`
        : `<span class="f-val">${text}</span>`
      : "";
  }
  // An empty value with its name shown gets a dash, so the card's rows stay
  // in the same place from card to card. Without a name it just isn't drawn.
  if (!body && !name) return "";
  if (!body) body = `<span class="f-val u-muted">—</span>`;
  return `<div class="f ${lead ? "is-lead" : ""}" style="font-size:${em}em">${name}${body}</div>`;
}

function cardHtml(card) {
  const inner = card.fields.map((f, i) => fieldHtml(f, i === 0)).join("");
  return `<div class="card">${inner || `<span class="f-val">${escapeHtml(card.title)}</span>`}</div>`;
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const size = ctx?.cell?.size || "md";
  const title = data.title || "Cards";

  if (data.error) {
    errorCard(shadow, WIDGET, title, data.error);
    return;
  }
  if (data.empty) {
    emptyCard(shadow, WIDGET, {
      icon: "ph-cards",
      title,
      message: emptyMessage(data, "Nothing to show."),
    });
    return;
  }

  const cards = Array.isArray(data.cards) ? data.cards : [];
  const cols = Math.max(1, Math.min(Number(data.columns) || 2, COLUMN_ROOM[size] ?? 6));
  const fieldGap = Math.max(0, Math.min(MAX_FIELD_GAP, Number(data.field_gap) || 0));
  // With no grouping the whole thing is one unnamed section. A heading is a
  // full-width grid item, so every group starts on a fresh row.
  const sections = Array.isArray(data.groups) && data.groups.length
    ? data.groups.map((g) => ({ name: g.name, cards: Array.isArray(g.cards) ? g.cards : [] }))
    : [{ name: null, cards }];

  const grid = sections.map((section) => {
    const head = section.name === null ? "" : `
      <div class="group-head">
        <span class="group-name">${escapeHtml(section.name)}</span>
        <span class="group-count">${section.cards.length}</span>
      </div>`;
    return head + section.cards.map(cardHtml).join("");
  }).join("");

  const total = data.total ?? cards.length;
  const metaAll = `${total} ${total === 1 ? "RECORD" : "RECORDS"}`;

  shadow.innerHTML = `
    ${CSS}
    <style>
      .cards { flex:1 1 auto; min-height:0; display:grid;
               grid-template-columns: repeat(${cols}, minmax(0, 1fr));
               grid-auto-rows: minmax(0, 1fr);
               gap: var(--space-2); }
      .card { position:relative; border: var(--stroke-1, 2px) solid var(--edge);
              border-radius: 0.5em; padding: var(--space-2) var(--space-3);
              display:flex; flex-direction:column; justify-content:flex-start;
              gap: calc(var(--space-1) + ${fieldGap}em);
              min-width:0; min-height:0; overflow:hidden; }
      /* Headings read as headings through weight, case and colour, not a
         rule, and they span the grid so each group starts a new row. */
      .group-head { grid-column: 1 / -1; display:flex; align-items:baseline;
                    justify-content:space-between; gap: var(--space-2);
                    padding: var(--space-1) var(--space-1) 0;
                    font-size: var(--fs-caption); font-weight: var(--fw-black);
                    letter-spacing: var(--ls-label);
                    text-transform: var(--label-transform, uppercase);
                    color: var(--text-secondary); }
      .group-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .group-count { flex:0 0 auto; color: var(--text-muted); }
      .f { display:flex; flex-direction:column; gap:0.1em; min-width:0; line-height:1.25; }
      .f-name { font-size:0.7em; font-weight:var(--fw-bold); letter-spacing:var(--ls-label);
                text-transform: var(--label-transform, uppercase); color:var(--text-muted);
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .f-val { font-weight:var(--fw-semi); overflow:hidden; text-overflow:ellipsis;
               white-space:nowrap; }
      .f-val.is-wrap { white-space:normal; display:-webkit-box;
                       -webkit-box-orient:vertical; overflow-wrap:anywhere; }
      .is-lead .f-val { font-weight:var(--fw-bold); }
      .f-check { flex-direction:row; align-items:center; gap:0.35em; }
      .f-check .ph-bold { font-size:1.25em; color:var(--text-primary); line-height:1; }
      .f-check-name { font-weight:var(--fw-semi); overflow:hidden;
                      text-overflow:ellipsis; white-space:nowrap; }
      .f-badges { display:flex; flex-wrap:wrap; gap:0.25em; }
      .chip { background: var(--surface-sunken); color: var(--text-secondary);
              border-radius: var(--pill-radius, 999px); padding: 0 0.5em;
              font-size: 0.85em; font-weight: var(--fw-bold); white-space:nowrap;
              text-transform: var(--label-transform, none); }
    </style>
    <div class="w size-${size}" data-widget="${WIDGET}">
      <div class="w-title">
        <i class="ph-bold ph-cards" style="color:var(--accent-3)"></i>
        <h3>${escapeHtml(title)}</h3>
        <span class="w-title-meta">${size === "xs" ? "" : metaAll}</span>
      </div>
      <div class="w-body cards">${grid}</div>
    </div>`;

  // Every card row gets an equal share of what the headings leave, so a
  // card can be shorter than its content. Two passes, both whole-item:
  // first, a row shorter than one line of text holds nothing legible, so
  // sections are walked in order and cards (then whole sections) beyond
  // what fits are hidden, with the title meta saying "N OF M"; second,
  // inside each drawn card, fields from the first that doesn't fit are
  // hidden rather than sliced mid-glyph. Runs again once the stylesheet
  // and fonts are in, because metrics move.
  const link = shadow.querySelector("link[rel=stylesheet]");
  const fit = () => {
    if (link && !link.sheet) return;
    const body = shadow.querySelector(".cards");
    const items = [...body.children];
    for (const el of items) el.style.display = "";
    body.style.gridTemplateRows = "";

    const style = getComputedStyle(body);
    const fontPx = parseFloat(style.fontSize) || 16;
    const gapPx = parseFloat(style.rowGap) || 0;
    const minRow = fontPx * 2.4;
    let remaining = body.clientHeight;
    const tracks = [];
    let drawn = 0;
    let exhausted = false;

    for (const section of sections) {
      const nodes = items.splice(0, section.cards.length + (section.name === null ? 0 : 1));
      const head = section.name === null ? null : nodes.shift();
      const rows = Math.ceil(nodes.length / cols);
      const headCost = head ? head.offsetHeight + gapPx : 0;
      let draw = 0;
      if (!exhausted && remaining - headCost >= minRow) {
        const roomRows = Math.floor((remaining - headCost + gapPx) / (minRow + gapPx));
        draw = Math.min(rows, roomRows);
      }
      if (draw === 0) {
        exhausted = true;
        for (const el of nodes) el.style.display = "none";
        if (head) head.style.display = "none";
        continue;
      }
      remaining -= headCost + draw * (minRow + gapPx);
      if (head) tracks.push("auto");
      tracks.push(`repeat(${draw}, minmax(0, 1fr))`);
      for (const el of nodes.slice(draw * cols)) el.style.display = "none";
      drawn += Math.min(nodes.length, draw * cols);
      if (draw < rows) exhausted = true;
    }
    body.style.gridTemplateRows = tracks.join(" ");
    const meta = shadow.querySelector(".w-title-meta");
    if (meta && size !== "xs") meta.textContent = drawn < total ? `${drawn} OF ${total}` : metaAll;

    for (const card of body.querySelectorAll(".card")) {
      if (card.style.display === "none") continue;
      // clientHeight ends at the inner edge of the border: a field may run
      // into the bottom padding and still be whole, so that is the limit.
      const room = card.clientHeight;
      const fields = [...card.querySelectorAll(":scope > .f")];
      for (const f of fields) {
        f.style.display = "";
        const wrap = f.querySelector(".is-wrap");
        if (wrap) {
          wrap.dataset.lines ??= wrap.style.webkitLineClamp;
          wrap.style.webkitLineClamp = wrap.dataset.lines;
        }
      }
      const fits = (el) => el.offsetTop + el.offsetHeight <= room + 0.5;
      // A wrapping field gives up lines one at a time before it is judged;
      // the lead is never hidden, only shortened, since a card with no
      // first field says nothing.
      const settle = (f) => {
        const wrap = f.querySelector(".is-wrap");
        let lines = wrap ? parseInt(wrap.style.webkitLineClamp, 10) || 1 : 1;
        while (!fits(f) && wrap && lines > 1) {
          lines -= 1;
          wrap.style.webkitLineClamp = String(lines);
        }
        return fits(f);
      };
      if (fields[0]) settle(fields[0]);
      let overflowed = false;
      for (const f of fields.slice(1)) {
        if (overflowed || !settle(f)) { overflowed = true; f.style.display = "none"; }
      }
    }
  };
  fit();
  if (link) link.addEventListener("load", fit, { once: true });
  if (document.fonts?.ready) document.fonts.ready.then(fit);
}
