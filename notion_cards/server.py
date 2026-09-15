"""notion_cards — records from any Notion database, laid out as a grid of cards.

The generic member of the family. Tasks and List each know what their rows
mean; this one knows nothing about the database except what the cell tells
it: up to five columns to show on every card, each at a chosen size, with
or without its name. What a column looks like is inferred from its Notion
type — a checkbox draws a checkbox, a select or status draws a badge, a
date is formatted as a date, a rollup that gathers several values lists
them one per line, and anything else is text at the chosen size.

Rows can be filtered by up to three column-and-condition pairs (``Status is
Done``, ``Due is before today``, ``Owner is not empty``), all of which must
hold; narrowed to one person the same way the sibling widgets are; sorted by
any column; capped; and grouped under a heading per value of one column.

Never raises: returns ``{"error": "friendly message"}``.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

from flask import current_app

PLUGIN_ID = "notion_cards"
DEFAULT_TITLE = "Cards"
MAX_FIELDS = 5
SIZES = ("xs", "s", "m", "l", "xl")
DEFAULT_SIZE = "m"
DEFAULT_LINES = 1
MAX_LINES = 8
# Extra white space between a card's fields, in em, on top of the small gap
# the card always draws. Cell option "field_gap".
DEFAULT_FIELD_GAP = 0.0
MAX_FIELD_GAP = 5.0
ERR_NO_CORE = (
    "The Notion Core plugin isn't installed. Install the whole Notion bundle, "
    "not just this widget."
)
ERR_NO_DATABASE = "Pick a Notion database in this cell's settings."

# A Notion page id, with or without its hyphens. A rollup over a relation
# gathers these rather than names; one is never worth printing.
_PAGE_ID = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$")

# Notion types → how the card draws them. Anything not listed is text.
_BADGE_TYPES = ("select", "status", "multi_select")
_DATE_TYPES = ("date", "created_time", "last_edited_time")


def _core() -> Any:
    registry = current_app.config.get("PLUGIN_REGISTRY")
    entry = registry.get("notion_core") if registry is not None else None
    return entry.server_module if entry is not None else None


def choices(name: str) -> list[dict[str, str]]:
    """The host calls ``choices()`` only on the declaring plugin, so
    re-export the core's resolver."""
    core = _core()
    if core is None:
        return [{"value": "", "label": "Notion Core plugin is missing"}]
    resolver = getattr(core, "choices", None)
    return list(resolver(name)) if callable(resolver) else []


def field_settings(options: dict[str, Any]) -> list[dict[str, Any]]:
    """The columns a cell asked for → ``[{"name", "size", "lines", "show_name"}]``.

    Slots are read in order and blank ones skipped, so leaving Property 2
    empty and filling Property 3 still gives two fields, in that order.
    """
    out: list[dict[str, Any]] = []
    for i in range(1, MAX_FIELDS + 1):
        name = str(options.get(f"prop{i}") or "").strip()
        if not name:
            continue
        size = str(options.get(f"prop{i}_size") or DEFAULT_SIZE).strip().lower()
        out.append({
            "name": name,
            "size": size if size in SIZES else DEFAULT_SIZE,
            "lines": _read_int(options, f"prop{i}_lines", DEFAULT_LINES, MAX_LINES),
            "show_name": bool(options.get(f"prop{i}_show_name")),
        })
    return out


def _read_int(options: dict[str, Any], key: str, default: int, hi: int) -> int:
    """Local twin of ``core.read_int``: ``field_settings`` runs before the
    core is looked up, and is unit-tested without one."""
    try:
        value = int(options.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(hi, value))


def read_field_gap(options: dict[str, Any]) -> float:
    """The "Space between properties" slider, in em, clamped to its range."""
    try:
        value = float(options.get("field_gap", DEFAULT_FIELD_GAP))
    except (TypeError, ValueError):
        value = DEFAULT_FIELD_GAP
    if math.isnan(value):  # "nan" parses; it must not reach the stylesheet
        value = DEFAULT_FIELD_GAP
    return max(0.0, min(MAX_FIELD_GAP, value))


def display_kind(prop_type: str, value: Any = None) -> str:
    """How a column is drawn, from its Notion type.

    A formula or rollup has no type of its own until it produces a value,
    so those are judged by the value on the row instead: a formula that
    yields true/false draws as a checkbox, one that yields a number as a
    number, and so on. A rollup that gathers values from related pages
    yields a list — of names, of tags, of whole multi-selects — and is
    drawn as lines of text, one entry per line.
    """
    if prop_type == "checkbox":
        return "checkbox"
    if prop_type in _BADGE_TYPES:
        return "badge"
    if prop_type in _DATE_TYPES:
        return "date"
    if prop_type == "number":
        return "number"
    if prop_type in ("formula", "rollup"):
        if isinstance(value, bool):
            return "checkbox"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, dict):
            return "date"
        if isinstance(value, list):
            return "lines"
    return "text"


def _text(value: Any, relation_names: dict[str, str]) -> str:
    """Any normalised property value flattened to one line of text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        return str(value.get("start") or "")
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _text(item, relation_names)
            if not text:
                continue
            # A relation yields page ids; show the related page's title and
            # drop ids that couldn't be resolved rather than printing them.
            # An item can itself be a list (a rollup over a multi-select),
            # which can't be looked up, so the str check comes first.
            if isinstance(item, str) and item in relation_names:
                text = relation_names[item]
            elif relation_names and isinstance(item, str) and len(item) >= 32:
                continue
            parts.append(text)
        return ", ".join(parts)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _lines(value: Any, relation_names: dict[str, str]) -> list[str]:
    """A rollup's gathered values as lines of text, one per entry.

    Notion returns one entry per related page, and an entry can hold a
    whole multi-select or relation, so nesting is flattened: every leaf
    value is its own line. A leaf that is a page id (the rollup reaches a
    relation) becomes that page's title, or is dropped if it couldn't be
    resolved — an id tells the reader nothing. Repeats are dropped too —
    three bookings sharing a topic is one topic — and so are blanks.
    """
    out: list[str] = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, list):
            texts = _lines(item, relation_names)
        elif isinstance(item, str) and item in relation_names:
            texts = [relation_names[item]]
        elif isinstance(item, str) and _PAGE_ID.match(item):
            texts = []
        else:
            texts = [_text(item, {}).strip()]
        for text in texts:
            if text and text not in out:
                out.append(text)
    return out


def _display_value(
    value: Any, kind: str, spec: dict[str, Any], relation_names: dict[str, str]
) -> Any:
    """The value in the shape ``client.js`` draws for that kind."""
    if kind == "checkbox":
        return bool(value)
    if kind == "badge":
        if isinstance(value, list):
            return [str(v) for v in value if v]
        return [str(value)] if value else []
    if kind == "date":
        if isinstance(value, dict):
            return {"start": str(value.get("start") or ""), "end": str(value.get("end") or "")}
        return {"start": str(value or ""), "end": ""}
    if kind == "number":
        number = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        fmt = spec.get("number", {}).get("format", "") if isinstance(spec, dict) else ""
        return {"value": number, "format": str(fmt or "")}
    if kind == "lines":
        return _lines(value, relation_names)
    return _text(value, relation_names)


def _card(
    page: dict[str, Any],
    core: Any,
    fields: list[dict[str, Any]],
    schema_props: dict[str, Any],
    relation_names: dict[str, dict[str, str]],
) -> dict[str, Any]:
    out_fields = []
    for field in fields:
        name = field["name"]
        spec = schema_props.get(name) if isinstance(schema_props.get(name), dict) else {}
        prop_type = core.prop_type(schema_props, name)
        value = core.prop(page, name)
        kind = display_kind(prop_type, value)
        out_fields.append({
            "name": name,
            "type": prop_type,
            "kind": kind,
            "value": _display_value(value, kind, spec, relation_names.get(name, {})),
            "size": field["size"],
            "lines": field["lines"],
            "show_name": field["show_name"],
        })
    return {
        "title": core.page_title(page, schema_props) or "Untitled",
        "url": str(page.get("url") or ""),
        "fields": out_fields,
    }


def _condition_label(core: Any, conditions: list[tuple[str, str, str]]) -> str:
    """"Status is Done and Due is before today" — for the empty-state message."""
    parts = []
    for column, op, value in conditions:
        verb = core.CONDITIONS.get(op, op)
        parts.append(
            f"{column} {verb}" if op in ("is_empty", "is_not_empty") else f"{column} {verb} {value}"
        )
    return " and ".join(parts)


def _group_label(
    page: dict[str, Any], core: Any, column: str, relation_names: dict[str, str]
) -> str:
    """One heading for a card's group column, "" when it is empty.

    A multi-valued column (several tags, several related pages, a rollup
    of either) can only put the card under one heading, so the first entry
    wins, in Notion's own order, as it does for the tasks widget's project
    grouping.
    """
    value = core.prop(page, column)
    if isinstance(value, list):
        leaves = _lines(value, relation_names)
        value = leaves[0] if leaves else None
    return _text(value, {}).strip()


def _group(
    cards: list[dict[str, Any]], labels: list[str], column: str
) -> list[dict[str, Any]]:
    """Bucket cards by label, in order of first appearance so a sort is
    respected; cards with nothing in the column collect last."""
    empty_name = f"No {column}"
    buckets: dict[str, list[dict[str, Any]]] = {}
    for card, label in zip(cards, labels, strict=True):
        buckets.setdefault(label or empty_name, []).append(card)
    groups = [{"name": name, "cards": rows} for name, rows in buckets.items()]
    groups.sort(key=lambda g: g["name"] == empty_name)
    return groups


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    title = str(options.get("title") or "").strip() or DEFAULT_TITLE
    ds_id = str(options.get("data_source") or "").strip()

    core = _core()
    if core is None:
        return {"error": ERR_NO_CORE, "title": title}
    account_id, err = core.resolve_account(options)
    if err:
        return {"error": err, "title": title}
    if not ds_id:
        return {"error": ERR_NO_DATABASE, "title": title}

    limit = core.read_int(options, "limit", 6, hi=48)
    columns = core.read_int(options, "columns", 2, hi=6)
    field_gap = read_field_gap(options)
    refresh_min = core.read_int(options, "refresh_min", 15)
    fields = field_settings(options)
    sorts = core.sort_settings(options)
    conditions = core.condition_settings(options)
    group_prop = str(options.get("group_prop") or "").strip()
    filter_person = str(options.get("filter_person") or "").strip()
    filter_columns = core.split_columns(options.get("filter_columns"))

    cached, result_path = core.result_cache(
        Path(ctx.get("data_dir") or "."),
        [account_id, ds_id, limit, columns, field_gap, fields, sorts,
         conditions, group_prop, filter_person, filter_columns],
        refresh_min,
    )
    if cached is not None:
        cached["title"] = title
        return cached

    schema_props, err = core.schema_with_columns(
        account_id, ds_id,
        [*(f["name"] for f in fields), *(c for c, _ in sorts), group_prop,
         *(c[0] for c in conditions), *filter_columns],
    )
    if err or schema_props is None:
        return {"error": err or "Couldn't read that database.", "title": title}
    detected = core.detect(schema_props)
    if not fields:
        # A fresh cell with nothing chosen yet still has to show something
        # recognisable: the title column, as a heading.
        fields = [{
            "name": detected["title"], "size": DEFAULT_SIZE,
            "lines": DEFAULT_LINES, "show_name": False,
        }]
        fields = [f for f in fields if f["name"]]

    flt, err = core.person_filter(account_id, schema_props, options, detected["person"])
    if err:
        return {"error": err, "title": title}

    pages, err, truncated = core.query(
        account_id, ds_id,
        filter_=flt["notion_filter"],
        sorts=core.notion_sorts(schema_props, sorts),
    )
    if err or pages is None:
        return {"error": err or "Couldn't load records from Notion.", "title": title}

    today = date.today().isoformat()
    pages = core.apply_person_filter(pages, schema_props, flt)
    pages = core.apply_conditions(pages, conditions, today=today)
    pages = core.sort_pages(pages, sorts, schema_props)
    shown = pages[:limit]

    # Relation columns hold page ids, and so does a rollup that reaches a
    # relation; resolve the ones on the cards that will actually be drawn,
    # one request per distinct page, capped.
    relation_names = {
        name: core.relation_titles(account_id, shown, name, core.prop_type(schema_props, name))
        for name in {*(f["name"] for f in fields), group_prop}
        if name and core.prop_type(schema_props, name) in ("relation", "rollup")
    }
    cards = [_card(p, core, fields, schema_props, relation_names) for p in shown]

    result = {
        "title": title,
        "cards": cards,
        "total": len(pages),
        "shown": len(cards),
        "columns": columns,
        "field_gap": field_gap,
        "empty": not pages,
        "account": core.account_name(account_id),
        "filtered_by": flt["person"],
        # A condition filter is always local, so with one in force a capped
        # fetch may have left matches unseen.
        "filter_incomplete": bool(
            truncated and (conditions or (flt["person"] and not flt["complete"]))
        ),
        "condition": _condition_label(core, conditions),
        "sorted_by": [c for c, _ in sorts],
        "group_by": group_prop,
        "fields": fields,
        "fetched_at": int(time.time()),
    }
    if group_prop:
        labels = [_group_label(p, core, group_prop, relation_names.get(group_prop, {}))
                  for p in shown]
        result["groups"] = _group(cards, labels, group_prop)
    with contextlib.suppress(OSError):
        result_path.write_text(json.dumps(result), encoding="utf-8")
    return result
