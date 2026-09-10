"""notion_tasks — open tasks from a Notion data source.

All Notion access goes through the ``notion_core`` sibling (one token, one
discovery cache, one property-detection pass); this module turns whatever
schema the user's database happens to have into the flat row shape
``client.js`` paints.

Never raises: returns ``{"error": "friendly message"}`` so the cell renders
an error card instead of a stack trace.

Caches ``result_<slug>.json`` in this plugin's ``data_dir``, TTL =
the cell's Refresh option.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

from flask import current_app

PLUGIN_ID = "notion_tasks"
ERR_NO_CORE = (
    "The Notion Core plugin isn't installed. Install the whole Notion bundle, "
    "not just this widget."
)
ERR_NO_DATABASE = "Pick a Notion database in this cell's settings."

# Notion `select` priorities are free text, so map the names people actually
# use onto a sortable rank. Anything unrecognised sorts below the named ones
# rather than above, so an unmapped value can't jump the queue.
_PRIORITY_RANK: dict[str, int] = {
    "urgent": 4, "critical": 4, "p0": 4, "highest": 4,
    "high": 3, "p1": 3,
    "medium": 2, "med": 2, "normal": 2, "p2": 2,
    "low": 1, "p3": 1, "lowest": 1, "p4": 1,
}


def _core() -> Any:
    """The notion_core server module, or None if the bundle is half-installed."""
    registry = current_app.config.get("PLUGIN_REGISTRY")
    entry = registry.get("notion_core") if registry is not None else None
    return entry.server_module if entry is not None else None


def choices(name: str) -> list[dict[str, str]]:
    """The host only calls ``choices()`` on the plugin that declares the
    option, never on a sibling, so re-export the core's resolver."""
    core = _core()
    if core is None:
        return [{"value": "", "label": "Notion Core plugin is missing"}]
    resolver = getattr(core, "choices", None)
    return list(resolver(name)) if callable(resolver) else []


def _priority_rank(value: Any) -> int:
    if isinstance(value, (int, float)):
        # A numeric priority column: higher number = more urgent, clamped
        # into the same 0-4 band as the named ranks so both sort together.
        return max(0, min(4, int(value)))
    name = str(value or "").strip().lower()
    if not name:
        return 0
    if name in _PRIORITY_RANK:
        return _PRIORITY_RANK[name]
    # "P1 — Urgent", "High priority", etc.
    for key, rank in _PRIORITY_RANK.items():
        if name.startswith(key):
            return rank
    return 0


def _priority_label(value: Any) -> str:
    """Display text for the priority column, numeric or named."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return str(value or "")


def _row(
    page: dict[str, Any],
    core: Any,
    props: dict[str, str],
    schema_props: dict[str, Any],
    project_names: dict[str, str],
    today: str,
) -> dict[str, Any]:
    title = core.page_title(page, schema_props) or "Untitled"

    status = str(core.prop(page, props["status"]) or "") if props["status"] else ""
    checkbox = core.prop(page, props["done"]) if props["done"] else None
    done = core.is_done(status, checkbox)

    due_raw = core.prop(page, props["due"]) if props["due"] else None
    due_date = ""
    if isinstance(due_raw, dict):
        due_date = str(due_raw.get("start") or "")[:10]

    project = ""
    if props["project"]:
        value = core.prop(page, props["project"])
        if isinstance(value, list):
            # A relation gives page ids; resolve to titles where we have them.
            names = [project_names.get(v, "") for v in value]
            project = next((n for n in names if n), "")
        elif value:
            project = str(value)

    assignee = ""
    if props["assignee"]:
        people = core.prop(page, props["assignee"])
        if isinstance(people, list) and people:
            assignee = str(people[0] or "")

    priority_value = core.prop(page, props["priority"]) if props["priority"] else None

    return {
        "title": title,
        "status": status,
        "done": done,
        "due_date": due_date,
        "overdue": bool(due_date) and not done and due_date < today,
        "today": bool(due_date) and due_date == today,
        "project": project,
        "assignee": assignee,
        "priority": _priority_label(priority_value),
        "priority_rank": _priority_rank(priority_value),
        "url": str(page.get("url") or ""),
    }


def _sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Overdue first, then soonest due, then priority, then title.

    Undated tasks sort after every dated one (the sentinel date), which is
    what you want on a wall panel: the things with a deadline are the things
    that need looking at.
    """
    return (
        0 if item["overdue"] else 1,
        item["due_date"] or "9999-12-31",
        -item["priority_rank"],
        item["title"].lower(),
    )


def _project_names(core: Any, pages: list[dict[str, Any]], props: dict[str, str]) -> dict[str, str]:
    """Resolve relation-target page ids to their titles.

    Only called when the project column is a relation. One extra request per
    distinct related database would be ideal; in practice the relation points
    at a single Projects database, and the widget shows a handful of rows, so
    fetching the related pages individually is capped hard and skipped
    entirely when it would cost more than a few calls.
    """
    if not props["project"]:
        return {}
    ids: list[str] = []
    for page in pages:
        value = core.prop(page, props["project"])
        if isinstance(value, list):
            ids.extend(v for v in value if v)
    unique = list(dict.fromkeys(ids))[:12]
    names: dict[str, str] = {}
    for page_id in unique:
        data, err = core.page(page_id)
        if err or data is None:
            continue
        names[page_id] = core.page_title(data) or ""
    return names


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    title = str(options.get("title") or "").strip() or "Tasks"
    ds_id = str(options.get("data_source") or "").strip()

    core = _core()
    if core is None:
        return {"error": ERR_NO_CORE, "title": title}
    if not core.is_configured():
        return {"error": core.config_error() or "Notion isn't configured.", "title": title}
    if not ds_id:
        return {"error": ERR_NO_DATABASE, "title": title}

    try:
        limit = max(1, int(options.get("limit", 8)))
    except (TypeError, ValueError):
        limit = 8
    try:
        refresh_min = max(1, int(options.get("refresh_min", 15)))
    except (TypeError, ValueError):
        refresh_min = 15

    show_completed = bool(options.get("show_completed"))

    data_dir = Path(ctx.get("data_dir") or ".")
    with contextlib.suppress(OSError):
        data_dir.mkdir(parents=True, exist_ok=True)
    overrides = {k: v for k, v in sorted(options.items()) if k.endswith("_prop")}
    fingerprint = json.dumps([ds_id, limit, show_completed, overrides], sort_keys=True)
    slug = hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
    result_path = data_dir / f"result_{slug}.json"

    now = int(time.time())
    if result_path.exists() and now - int(result_path.stat().st_mtime) < refresh_min * 60:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            cached = json.loads(result_path.read_text(encoding="utf-8"))
            cached["title"] = title
            return cached  # type: ignore[no-any-return]

    schema_props, err = core.schema(ds_id)
    if err or schema_props is None:
        return {"error": err or "Couldn't read that database.", "title": title}
    props = core.resolve_props(schema_props, options)

    # Sort server-side by due date when the database has one, so that the
    # capped page walk returns the soonest tasks rather than an arbitrary
    # slice. Completion filtering happens locally: a "done" state can live in
    # a status, a select or a checkbox, and building a Notion filter for the
    # right one is far more fragile than dropping rows after the fact.
    sorts = (
        [{"property": props["due"], "direction": "ascending"}] if props["due"] else None
    )
    pages, err = core.query(ds_id, sorts=sorts)
    if err or pages is None:
        return {"error": err or "Couldn't load tasks from Notion.", "title": title}

    project_names = _project_names(core, pages, props)
    today = date.today().isoformat()
    items = [_row(p, core, props, schema_props, project_names, today) for p in pages]
    if not show_completed:
        items = [i for i in items if not i["done"]]
    items.sort(key=_sort_key)

    result = {
        "title": title,
        "items": items[:limit],
        "total": len(items),
        "shown": min(len(items), limit),
        "overdue_count": sum(1 for i in items if i["overdue"]),
        "today_count": sum(1 for i in items if i["today"]),
        "empty": not items,
        "has_due": bool(props["due"]),
        "has_project": bool(props["project"]),
        "has_status": bool(props["status"]),
        "detected": props,
        "fetched_at": now,
    }
    with contextlib.suppress(OSError):
        result_path.write_text(json.dumps(result), encoding="utf-8")
    return result
