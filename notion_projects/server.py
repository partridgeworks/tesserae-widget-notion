"""notion_projects — active projects from a Notion data source.

Sibling of ``notion_tasks``; same core, different question. Tasks answer
"what do I do next"; projects answer "what's in flight and how far along".
So this one leads with status and progress rather than due dates, and keeps
completed work out of the way by default.

Never raises: returns ``{"error": "friendly message"}``.
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

PLUGIN_ID = "notion_projects"
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

    due_raw = core.prop(page, props["due"]) if props["due"] else None
    due_date = ""
    if isinstance(due_raw, dict):
        due_date = str(due_raw.get("start") or "")[:10]

    owner = ""
    if props["assignee"]:
        people = core.prop(page, props["assignee"])
        if isinstance(people, list) and people:
            owner = str(people[0] or "")

    progress = _progress(core.prop(page, props["progress"])) if props["progress"] else None

    return {
        "title": core.page_title(page, schema_props) or "Untitled",
        "status": status,
        "done": done,
        "due_date": due_date,
        "overdue": bool(due_date) and not done and due_date < today,
        "owner": owner,
        "progress": progress,
        "url": str(page.get("url") or ""),
    }


def _sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Overdue first, then dated by soonest, then furthest along, then title.

    Progress descending puts nearly-finished projects above barely-started
    ones, which is the order you want when deciding what to push over the
    line this week.
    """
    return (
        0 if item["overdue"] else 1,
        item["due_date"] or "9999-12-31",
        -(item["progress"] if item["progress"] is not None else -1),
        item["title"].lower(),
    )


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    title = str(options.get("title") or "").strip() or "Projects"
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
        limit = max(1, int(options.get("limit", 6)))
    except (TypeError, ValueError):
        limit = 6
    try:
        refresh_min = max(1, int(options.get("refresh_min", 15)))
    except (TypeError, ValueError):
        refresh_min = 15

    show_completed = bool(options.get("show_completed"))

    data_dir = Path(ctx.get("data_dir") or ".")
    with contextlib.suppress(OSError):
        data_dir.mkdir(parents=True, exist_ok=True)
    overrides = {k: v for k, v in sorted(options.items()) if k.endswith("_prop")}
    fingerprint = json.dumps(
        [account_id, ds_id, limit, show_completed, overrides], sort_keys=True
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
    props = core.resolve_props(schema_props, options)

    sorts = (
        [{"property": props["due"], "direction": "ascending"}] if props["due"] else None
    )
    pages, err = core.query(account_id, ds_id, sorts=sorts)
    if err or pages is None:
        return {"error": err or "Couldn't load projects from Notion.", "title": title}

    today = date.today().isoformat()
    items = [_row(p, core, props, schema_props, today) for p in pages]
    if not show_completed:
        items = [i for i in items if not i["done"]]
    items.sort(key=_sort_key)

    tracked = [i["progress"] for i in items if i["progress"] is not None]
    result = {
        "title": title,
        "items": items[:limit],
        "total": len(items),
        "shown": min(len(items), limit),
        "overdue_count": sum(1 for i in items if i["overdue"]),
        "empty": not items,
        "has_due": bool(props["due"]),
        "has_status": bool(props["status"]),
        "has_progress": bool(props["progress"]) and bool(tracked),
        "account": core.account_name(account_id),
        "avg_progress": (sum(tracked) / len(tracked)) if tracked else None,
        "detected": props,
        "fetched_at": now,
    }
    with contextlib.suppress(OSError):
        result_path.write_text(json.dumps(result), encoding="utf-8")
    return result
