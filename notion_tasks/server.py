"""notion_tasks — open tasks from a Notion data source.

All Notion access goes through the ``notion_core`` sibling (credentials for
every configured account, one discovery cache, one property-detection pass);
this module turns whatever schema the user's database happens to have into
the flat row shape ``client.js`` paints.

Never raises: returns ``{"error": "friendly message"}`` so the cell renders
an error card instead of a stack trace.

Caches ``result_<slug>.json`` in this plugin's ``data_dir``, TTL = the cell's
Refresh option.
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

# Tasks with no project still have to land somewhere when grouping is on.
NO_PROJECT_LABEL = "No project"

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
    if isinstance(value, bool):
        return 0
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
    page_obj: dict[str, Any],
    core: Any,
    props: dict[str, str],
    schema_props: dict[str, Any],
    project_kind: str,
    relation_names: dict[str, str],
    today: str,
) -> dict[str, Any]:
    title = core.page_title(page_obj, schema_props) or "Untitled"

    status = str(core.prop(page_obj, props["status"]) or "") if props["status"] else ""
    checkbox = core.prop(page_obj, props["done"]) if props["done"] else None
    done = core.is_done(status, checkbox)

    due_raw = core.prop(page_obj, props["due"]) if props["due"] else None
    due_date = ""
    if isinstance(due_raw, dict):
        due_date = str(due_raw.get("start") or "")[:10]

    # Handles every project column shape, including the multi-valued ones:
    # a multi_select with several tags ticked, or a relation pointing at
    # several pages. First non-empty entry wins.
    project = core.project_label(page_obj, props["project"], project_kind, relation_names)

    assignee = ""
    if props["assignee"]:
        people = core.prop(page_obj, props["assignee"])
        if isinstance(people, list) and people:
            assignee = str(people[0] or "")

    priority_value = core.prop(page_obj, props["priority"]) if props["priority"] else None

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
        "url": str(page_obj.get("url") or ""),
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


def _group(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket tasks by project → ``[{"name", "items", "overdue_count"}]``.

    Group order follows the most urgent task in each group, using the same
    sort key the flat list uses, so the project needing attention stays at
    the top of the cell. Tasks with no project collect into one group that
    is forced last: an unfiled task is the least interesting kind.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        buckets.setdefault(item["project"] or NO_PROJECT_LABEL, []).append(item)

    groups = [
        {
            "name": name,
            "items": rows,
            "overdue_count": sum(1 for r in rows if r["overdue"]),
        }
        for name, rows in buckets.items()
    ]
    groups.sort(key=lambda g: (g["name"] == NO_PROJECT_LABEL, _sort_key(g["items"][0])))
    return groups


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    title = str(options.get("title") or "").strip() or "Tasks"
    ds_id = str(options.get("data_source") or "").strip()

    core = _core()
    if core is None:
        return {"error": ERR_NO_CORE, "title": title}
    account_id, err = core.resolve_account(options)
    if err:
        return {"error": err, "title": title}
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
    group_by = str(options.get("group_by") or "none").strip().lower()

    data_dir = Path(ctx.get("data_dir") or ".")
    with contextlib.suppress(OSError):
        data_dir.mkdir(parents=True, exist_ok=True)
    overrides = {k: v for k, v in sorted(options.items()) if k.endswith("_prop")}
    fingerprint = json.dumps(
        [account_id, ds_id, limit, show_completed, group_by, overrides], sort_keys=True
    )
    slug = hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
    result_path = data_dir / f"result_{slug}.json"

    now = int(time.time())
    if result_path.exists() and now - int(result_path.stat().st_mtime) < refresh_min * 60:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            cached = json.loads(result_path.read_text(encoding="utf-8"))
            cached["title"] = title
            return cached  # type: ignore[no-any-return]

    schema_props, err = core.schema(account_id, ds_id)
    if err or schema_props is None:
        return {"error": err or "Couldn't read that database.", "title": title}
    # An override that doesn't match is nearly always a schema this cache has
    # not caught up with -- a column added minutes ago -- rather than a typo.
    # Re-read once before blaming the operator, then say so plainly if it
    # really isn't there. Falling through to auto-detection instead means
    # silently reading a column nobody asked for.
    missing = core.unmatched_overrides(schema_props, options)
    if missing:
        fresh, fresh_err = core.schema(account_id, ds_id, refresh=True)
        if fresh is not None and not fresh_err:
            schema_props = fresh
            missing = core.unmatched_overrides(schema_props, options)
    if missing:
        return {
            "error": core.unknown_column_message(missing[0][1], schema_props),
            "title": title,
        }
    props = core.resolve_props(schema_props, options)
    project_kind = core.prop_type(schema_props, props["project"])

    # Sort server-side by due date when the database has one, so that the
    # capped page walk returns the soonest tasks rather than an arbitrary
    # slice. Completion filtering happens locally: a "done" state can live in
    # a status, a select or a checkbox, and building a Notion filter for the
    # right one is far more fragile than dropping rows after the fact.
    sorts = [{"property": props["due"], "direction": "ascending"}] if props["due"] else None
    pages, err = core.query(account_id, ds_id, sorts=sorts)
    if err or pages is None:
        return {"error": err or "Couldn't load tasks from Notion.", "title": title}

    relation_names = core.relation_titles(account_id, pages, props["project"], project_kind)
    today = date.today().isoformat()
    items = [
        _row(p, core, props, schema_props, project_kind, relation_names, today) for p in pages
    ]
    if not show_completed:
        items = [i for i in items if not i["done"]]
    items.sort(key=_sort_key)
    shown = items[:limit]

    result: dict[str, Any] = {
        "title": title,
        "items": shown,
        "total": len(items),
        "shown": len(shown),
        "overdue_count": sum(1 for i in items if i["overdue"]),
        "today_count": sum(1 for i in items if i["today"]),
        "empty": not items,
        "has_due": bool(props["due"]),
        "has_project": bool(props["project"]),
        "has_status": bool(props["status"]),
        "group_by": group_by,
        "account": core.account_name(account_id),
        "detected": props,
        "fetched_at": now,
    }
    # Group the rows that survived the limit, not the whole set: the cell
    # shows `shown`, so grouping anything else would advertise groups whose
    # tasks never appear.
    if group_by == "project" and props["project"]:
        result["groups"] = _group(shown)
    with contextlib.suppress(OSError):
        result_path.write_text(json.dumps(result), encoding="utf-8")
    return result
