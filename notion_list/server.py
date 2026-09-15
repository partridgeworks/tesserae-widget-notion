"""notion_list — rows from any Notion database, one line each.

Sibling of ``notion_tasks``; same core, different question. Tasks answer
"what do I do next"; a list answers "what's in this database and how is
each row getting on": status, a date, a person, and a progress bar where
the database tracks one. Nothing here assumes the rows are projects, or
tasks, or anything else: any database with a title column renders, and
every other column is optional.

Never raises: returns ``{"error": "friendly message"}``.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

from flask import current_app

PLUGIN_ID = "notion_list"
DEFAULT_TITLE = "List"
ERR_NO_CORE = (
    "The Notion Core plugin isn't installed. Install the whole Notion bundle, "
    "not just this widget."
)
ERR_NO_DATABASE = "Pick a Notion database in this cell's settings."


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


def _progress(value: Any) -> float | None:
    """Normalise a progress column to 0.0-1.0, or None if there isn't one.

    Notion percent-formatted numbers come back as a fraction (0.4 = 40%)
    while a plain number column is usually typed as 0-100, and a rollup
    "percent complete" can be either. Anything above 1 is treated as a
    percentage; that misreads a genuine 1%-as-1 case, which is a far rarer
    database than one storing whole percentages.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, list):
        numbers = [v for v in value if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not numbers:
            return None
        value = sum(numbers) / len(numbers)
    if not isinstance(value, (int, float)):
        return None
    fraction = float(value) / 100.0 if value > 1 else float(value)
    return max(0.0, min(1.0, fraction))


def _row(
    page: dict[str, Any],
    core: Any,
    props: dict[str, str],
    schema_props: dict[str, Any],
    today: str,
) -> dict[str, Any]:
    status = str(core.prop(page, props["status"]) or "") if props["status"] else ""
    checkbox = core.prop(page, props["done"]) if props["done"] else None
    done = core.is_done(status, checkbox)

    date_raw = core.prop(page, props["date"]) if props["date"] else None
    date = ""
    if isinstance(date_raw, dict):
        date = str(date_raw.get("start") or "")[:10]

    person = ""
    if props["person"]:
        people = core.prop(page, props["person"])
        if isinstance(people, list) and people:
            person = str(people[0] or "")

    progress = _progress(core.prop(page, props["progress"])) if props["progress"] else None

    return {
        "title": core.page_title(page, schema_props) or "Untitled",
        "status": status,
        "done": done,
        "date": date,
        "overdue": bool(date) and not done and date < today,
        "person": person,
        "progress": progress,
        "url": str(page.get("url") or ""),
    }


def _sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """The default order: overdue first, then dated by soonest, then
    furthest along, then title.

    Progress descending puts nearly-finished rows above barely-started
    ones, which is the order you want when deciding what to push over the
    line this week. A cell that names a sort column skips this entirely.
    """
    return (
        0 if item["overdue"] else 1,
        item["date"] or "9999-12-31",
        -(item["progress"] if item["progress"] is not None else -1),
        item["title"].lower(),
    )


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

    limit = core.read_int(options, "limit", 6)
    refresh_min = core.read_int(options, "refresh_min", 15)
    show_completed = bool(options.get("show_completed"))
    sorts = core.sort_settings(options)
    # Read here, not where they are used: the cache fingerprint below is
    # built before the schema is fetched, and must include the filter or
    # changing it would serve rows from the previous one.
    filter_person = str(options.get("filter_person") or "").strip()
    filter_columns = core.split_columns(options.get("filter_columns"))
    overrides = {k: v for k, v in sorted(options.items()) if k.endswith("_prop")}

    cached, result_path = core.result_cache(
        Path(ctx.get("data_dir") or "."),
        [account_id, ds_id, limit, show_completed, overrides, sorts,
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

    # Sort server-side so the capped fetch returns the rows the cell will
    # show rather than an arbitrary slice: by the chosen column when Notion
    # can, else by date, which is what the default order leads with.
    notion_sorts = core.notion_sorts(schema_props, sorts)
    if notion_sorts is None and props["date"]:
        notion_sorts = [{"property": props["date"], "direction": "ascending"}]
    pages, err, truncated = core.query(
        account_id, ds_id, filter_=flt["notion_filter"], sorts=notion_sorts
    )
    if err or pages is None:
        return {"error": err or "Couldn't load rows from Notion.", "title": title}

    pages = core.apply_person_filter(pages, schema_props, flt)
    pages = core.sort_pages(pages, sorts, schema_props)
    today = date.today().isoformat()
    items = [_row(p, core, props, schema_props, today) for p in pages]
    if not show_completed:
        items = [i for i in items if not i["done"]]
    if not sorts:
        items.sort(key=_sort_key)

    tracked = [i["progress"] for i in items if i["progress"] is not None]
    result = {
        "title": title,
        "items": items[:limit],
        "total": len(items),
        "shown": min(len(items), limit),
        "overdue_count": sum(1 for i in items if i["overdue"]),
        "empty": not items,
        "has_date": bool(props["date"]),
        "has_status": bool(props["status"]),
        "has_progress": bool(props["progress"]) and bool(tracked),
        "account": core.account_name(account_id),
        "avg_progress": (sum(tracked) / len(tracked)) if tracked else None,
        "filtered_by": flt["person"],
        # True when rows were dropped locally out of a fetch that hit the
        # page cap, so matches beyond it were never seen. A server-side
        # filter runs before paging, so it never has this problem.
        "filter_incomplete": bool(flt["person"]) and not flt["complete"] and truncated,
        "sorted_by": [c for c, _ in sorts],
        "detected": props,
        "fetched_at": int(time.time()),
    }
    with contextlib.suppress(OSError):
        result_path.write_text(json.dumps(result), encoding="utf-8")
    return result
