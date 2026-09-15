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

# What "Group by" can bucket on -- each a row field fed by a detected (or
# overridden) column, so a cell that names its own Status column groups by
# that one -- and the label for a task with nothing in that column.
GROUP_COLUMNS: dict[str, str] = {
    "project": NO_PROJECT_LABEL,
    "status": "No status",
    "priority": "No priority",
}

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

    due_raw = core.prop(page_obj, props["date"]) if props["date"] else None
    due_date = ""
    if isinstance(due_raw, dict):
        due_date = str(due_raw.get("start") or "")[:10]

    # Handles every project column shape, including the multi-valued ones:
    # a multi_select with several tags ticked, or a relation pointing at
    # several pages. First non-empty entry wins.
    project = core.project_label(page_obj, props["project"], project_kind, relation_names)

    assignee = ""
    if props["person"]:
        people = core.prop(page_obj, props["person"])
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


def _group(
    items: list[dict[str, Any]],
    column: str,
    *,
    order: dict[str, int] | None = None,
    sorted_by_column: bool = False,
    keep_order: bool = False,
) -> list[dict[str, Any]]:
    """Bucket tasks by one row field → ``[{"name", "items", "overdue_count"}]``.

    ``column`` is a key of ``GROUP_COLUMNS``. Tasks with nothing in it
    collect into one group forced last: an unfiled task is the least
    interesting kind. The rest are ordered by the first of these that
    applies:

    - ``sorted_by_column``: the cell sorted its rows by this very column,
      so the groups come out in order of first appearance -- ascending or
      descending, whichever the operator asked for.
    - ``order``, the column's option arrangement in Notion (a select or a
      status): that arrangement, the way a Notion board grouped by the
      column shows it, whatever the rows themselves are sorted by.
    - ``keep_order``, a cell sorted by some other column: first appearance,
      so the groups follow the sort rather than being re-ranked.
    - Otherwise the most urgent task in each group, using the flat list's
      sort key, so the project needing attention stays at the top of the
      cell. A priority column with no arrangement to follow (a number)
      leads with the highest priority instead.
    """
    empty_label = GROUP_COLUMNS[column]
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        buckets.setdefault(item[column] or empty_label, []).append(item)

    groups = [
        {
            "name": name,
            "items": rows,
            "overdue_count": sum(1 for r in rows if r["overdue"]),
        }
        for name, rows in buckets.items()
    ]

    def rank(group: dict[str, Any]) -> tuple[Any, ...]:
        if group["name"] == empty_label:
            return (1,)
        first = group["items"][0]
        if sorted_by_column:
            return (0,)
        if order is not None:
            return (0, order.get(group["name"].strip().lower(), len(order)), _sort_key(first))
        if keep_order:
            return (0,)
        if column == "priority":
            return (0, -first["priority_rank"], _sort_key(first))
        return (0, _sort_key(first))

    groups.sort(key=rank)
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

    limit = core.read_int(options, "limit", 8)
    refresh_min = core.read_int(options, "refresh_min", 15)
    show_completed = bool(options.get("show_completed"))
    group_by = str(options.get("group_by") or "none").strip().lower()
    sorts = core.sort_settings(options)
    # Read here, not where they are used: the cache fingerprint below is
    # built before the schema is fetched, and must include the filter or
    # changing it would serve rows from the previous one.
    filter_person = str(options.get("filter_person") or "").strip()
    filter_columns = core.split_columns(options.get("filter_columns"))
    overrides = {k: v for k, v in sorted(options.items()) if k.endswith("_prop")}

    cached, result_path = core.result_cache(
        Path(ctx.get("data_dir") or "."),
        [account_id, ds_id, limit, show_completed, group_by, overrides, sorts,
         filter_person, filter_columns],
        refresh_min,
    )
    if cached is not None:
        cached["title"] = title
        return cached

    schema_props, err = core.schema_with_columns(
        account_id, ds_id, [*core.override_names(options), *(c for c, _ in sorts), *filter_columns]
    )
    if err or schema_props is None:
        return {"error": err or "Couldn't read that database.", "title": title}
    props = core.resolve_props(schema_props, options)

    flt, err = core.person_filter(account_id, schema_props, options, props["person"])
    if err:
        return {"error": err, "title": title}
    project_kind = core.prop_type(schema_props, props["project"])

    # Sort server-side so the capped page walk returns the rows the cell
    # will show rather than an arbitrary slice: by the chosen column when
    # Notion can sort it, else by due date, which the default order leads
    # with. Completion filtering happens locally: a "done" state can live
    # in a status, a select or a checkbox, and building a Notion filter for
    # the right one is far more fragile than dropping rows after the fact.
    notion_sorts = core.notion_sorts(schema_props, sorts)
    if notion_sorts is None and props["date"]:
        notion_sorts = [{"property": props["date"], "direction": "ascending"}]
    pages, err, truncated = core.query(
        account_id, ds_id, filter_=flt["notion_filter"], sorts=notion_sorts
    )
    if err or pages is None:
        return {"error": err or "Couldn't load tasks from Notion.", "title": title}

    relation_names = core.relation_titles(account_id, pages, props["project"], project_kind)
    pages = core.apply_person_filter(pages, schema_props, flt)
    pages = core.sort_pages(pages, sorts, schema_props)
    today = date.today().isoformat()
    items = [
        _row(p, core, props, schema_props, project_kind, relation_names, today) for p in pages
    ]
    if not show_completed:
        items = [i for i in items if not i["done"]]
    if not sorts:
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
        "has_due": bool(props["date"]),
        "has_project": bool(props["project"]),
        "has_status": bool(props["status"]),
        "group_by": group_by,
        "account": core.account_name(account_id),
        "filtered_by": flt["person"],
        # True when rows were dropped locally out of a fetch that hit the
        # page cap, so matches beyond it were never seen. A server-side
        # filter runs before paging, so it never has this problem.
        "filter_incomplete": bool(flt["person"]) and not flt["complete"] and truncated,
        "sorted_by": [c for c, _ in sorts],
        "detected": props,
        "fetched_at": int(time.time()),
    }
    # Group the rows that survived the limit, not the whole set: the cell
    # shows `shown`, so grouping anything else would advertise groups whose
    # tasks never appear. No groups at all when the database has no such
    # column: the flat list, not an empty grouping or an error.
    group_column = props.get(group_by, "") if group_by in GROUP_COLUMNS else ""
    if group_column:
        result["groups"] = _group(
            shown,
            group_by,
            order=core.option_order(schema_props, group_column),
            sorted_by_column=bool(sorts) and sorts[0][0] == group_column,
            keep_order=bool(sorts),
        )
    with contextlib.suppress(OSError):
        result_path.write_text(json.dumps(result), encoding="utf-8")
    return result
