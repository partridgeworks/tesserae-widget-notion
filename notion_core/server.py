"""notion_core — shared Notion connection for the notion_* widget family.

No widget cell of its own. The sibling ``notion_tasks`` / ``notion_projects``
widgets reach in through the plugin registry and call ``data_sources``,
``schema``, ``query`` and ``choices`` so all of them share one integration
token, one discovery cache, and one property-detection pass.

Built against **Notion-Version 2025-09-03**, where a *database* contains one
or more *data sources* and queries address the data source rather than the
database (``POST /v1/data_sources/{id}/query``). The older
``/v1/databases/{id}/query`` path is deprecated and breaks the moment a
database gains a second source, so this family only speaks the new shape.

Config lives in Settings → Widgets → Notion Core: an internal integration
token from https://www.notion.so/my-integrations. Each database must also be
shared with that integration (Notion: ••• → Connections → your integration),
otherwise it simply will not appear — the API returns no error for a database
the integration cannot see, it just isn't in the results.

Caches, in this plugin's ``data_dir``:
  data_sources.json     id → title/database map (TTL 1 h)
  schema_<id>.json      one data source's property schema (TTL 1 h)
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, render_template, request

API_BASE = "https://api.notion.com/v1"
API_HOST = "api.notion.com"
DEFAULT_NOTION_VERSION = "2025-09-03"
USER_AGENT = "tesserae/0.1 (+notion_core)"
HTTP_TIMEOUT_S = 15

DISCOVERY_TTL_S = 3600
SCHEMA_TTL_S = 3600

# Notion pages a search/query 100 at a time. Cap the walk so a huge database
# can't turn one render into a hundred round-trips; widgets show a handful of
# rows and sort server-side within what they fetched.
PAGE_SIZE = 100
MAX_PAGES = 5

ERR_NO_TOKEN = (
    "Add your Notion integration token in Settings → Widgets → Notion Core "
    "(create one at notion.so/my-integrations)."
)
ERR_BAD_TOKEN = (
    "Notion rejected the integration token — copy it again from "
    "notion.so/my-integrations."
)
ERR_NO_ACCESS = (
    "Notion can't see that database. Open it in Notion, then ••• → "
    "Connections → add your integration."
)


# ----- settings -------------------------------------------------------


def _settings() -> dict[str, Any]:
    store = current_app.config["SETTINGS_STORE"]
    section = store.get_section("plugins") or {}
    return section.get("notion_core") or {}


def token() -> str:
    """The integration token. Stored as ``api_token_secret`` on disk per the
    settings_store secret convention; the plain key is the pre-encryption
    fallback."""
    s = _settings()
    return (s.get("api_token_secret") or s.get("api_token") or "").strip()


def notion_version() -> str:
    return (_settings().get("notion_version") or "").strip() or DEFAULT_NOTION_VERSION


def is_configured() -> bool:
    return bool(token())


def config_error() -> str | None:
    """The reason Notion can't be used, precise enough to act on, or None.

    A token that was configured and has since become undecryptable (the
    encryption key changed under a recreated container or a restored data
    folder) reads back as an empty string, which is indistinguishable from
    "never set" unless we ask the store. The difference is "set this up"
    versus "type it in again"."""
    if token():
        return None
    with contextlib.suppress(Exception):
        store = current_app.config["SETTINGS_STORE"]
        if "api_token_secret" in store.unreadable_secrets("plugins", "notion_core"):
            return (
                "Your Notion token can no longer be decrypted (the encryption "
                "key changed); re-enter it in Settings → Widgets → Notion Core."
            )
    return ERR_NO_TOKEN


def data_dir() -> Path:
    """This plugin's own cache directory, resolved through the registry so it
    is correct whether we were called from our own blueprint or from a
    sibling widget's ``fetch()``."""
    registry = current_app.config.get("PLUGIN_REGISTRY")
    entry = registry.get("notion_core") if registry is not None else None
    path = Path(entry.data_dir) if entry is not None else Path(".")
    with contextlib.suppress(OSError):
        path.mkdir(parents=True, exist_ok=True)
    return path


# ----- HTTP -----------------------------------------------------------


def _request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[Any | None, str | None]:
    """One Notion API call → (parsed_json, friendly_error).

    Never raises: every caller in this family returns ``{"error": ...}``
    rather than letting an exception reach the renderer.
    """
    tok = token()
    if not tok:
        return None, config_error() or ERR_NO_TOKEN

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {tok}",
            "Notion-Version": notion_version(),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as err:
        return None, _http_error_message(err)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None, "Couldn't reach Notion right now."


def _http_error_message(err: urllib.error.HTTPError) -> str:
    """Turn a Notion HTTP error into something a user can act on.

    Notion puts a machine-readable ``code`` in the body; it distinguishes
    "your token is wrong" from "your token is fine but this database was
    never shared with the integration", which are the two failures people
    actually hit and which both surface as 404-ish confusion otherwise.
    """
    code = ""
    with contextlib.suppress(Exception):
        code = str(json.loads(err.read().decode("utf-8")).get("code") or "")

    if err.code in (401, 403) or code == "unauthorized":
        return ERR_BAD_TOKEN
    if err.code == 404 or code == "object_not_found":
        return ERR_NO_ACCESS
    if err.code == 429 or code == "rate_limited":
        return "Notion is rate-limiting us; the widget will catch up shortly."
    if err.code == 400 and code == "validation_error":
        return "Notion rejected the query — the selected database may have changed."
    return f"Notion returned HTTP {err.code}."


def _paginated(
    path: str, payload: dict[str, Any], *, max_pages: int = MAX_PAGES
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Walk a Notion cursor-paginated POST endpoint up to ``max_pages``."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        page_payload = dict(payload, page_size=PAGE_SIZE)
        if cursor:
            page_payload["start_cursor"] = cursor
        data, err = _request("POST", path, page_payload)
        if err or not isinstance(data, dict):
            return None, err or "Notion returned an unexpected response."
        results = data.get("results")
        if isinstance(results, list):
            out.extend(r for r in results if isinstance(r, dict))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor") or None
        if not cursor:
            break
    return out, None


# ----- rich text ------------------------------------------------------


def plain_text(rich: Any) -> str:
    """Flatten a Notion rich-text array to a plain string."""
    if isinstance(rich, str):
        return rich
    if not isinstance(rich, list):
        return ""
    parts = [
        str(node.get("plain_text") or "")
        for node in rich
        if isinstance(node, dict)
    ]
    return "".join(parts).strip()


# ----- discovery ------------------------------------------------------


def data_sources(*, refresh: bool = False) -> tuple[list[dict[str, str]] | None, str | None]:
    """Every data source the integration can see → [{id, title, database_id}].

    Cached for an hour: this drives an edit-time dropdown and a widget's
    project-name lookups, neither of which needs minute-fresh data, and
    Notion's search endpoint is comparatively slow.
    """
    cache = data_dir() / "data_sources.json"
    if not refresh:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            if cache.exists() and time.time() - cache.stat().st_mtime < DISCOVERY_TTL_S:
                loaded = json.loads(cache.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    return loaded, None

    results, err = _paginated(
        "/search",
        {
            "filter": {"property": "object", "value": "data_source"},
            "sort": {"timestamp": "last_edited_time", "direction": "descending"},
        },
    )
    if err or results is None:
        return None, err or "Couldn't list your Notion databases."

    found: list[dict[str, str]] = []
    for item in results:
        if item.get("object") != "data_source":
            continue
        ds_id = str(item.get("id") or "")
        if not ds_id:
            continue
        title = plain_text(item.get("title")) or "Untitled"
        parent = item.get("parent") or item.get("database_parent") or {}
        found.append(
            {
                "id": ds_id,
                "title": title,
                "database_id": str(parent.get("database_id") or "")
                if isinstance(parent, dict)
                else "",
            }
        )
    found.sort(key=lambda d: d["title"].lower())

    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(found), encoding="utf-8")
    return found, None


def schema(ds_id: str, *, refresh: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    """One data source's property schema → {prop_name: {"type": ...}}."""
    if not ds_id:
        return None, "No database selected."
    safe = "".join(c for c in ds_id if c.isalnum() or c in "-_")[:64]
    cache = data_dir() / f"schema_{safe}.json"
    if not refresh:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            if cache.exists() and time.time() - cache.stat().st_mtime < SCHEMA_TTL_S:
                loaded = json.loads(cache.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return loaded, None

    data, err = _request("GET", f"/data_sources/{ds_id}")
    if err or not isinstance(data, dict):
        return None, err or "Couldn't read that database's properties."
    props = data.get("properties")
    props = props if isinstance(props, dict) else {}

    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(props), encoding="utf-8")
    return props, None


def page(page_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch one page by id. Used to resolve relation targets to titles."""
    if not page_id:
        return None, "No page id."
    data, err = _request("GET", f"/pages/{page_id}")
    if err or not isinstance(data, dict):
        return None, err or "Notion returned an unexpected response."
    return data, None


def query(
    ds_id: str,
    *,
    filter_: dict[str, Any] | None = None,
    sorts: list[dict[str, Any]] | None = None,
    max_pages: int = MAX_PAGES,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Query a data source for pages."""
    if not ds_id:
        return None, "No database selected."
    payload: dict[str, Any] = {}
    if filter_:
        payload["filter"] = filter_
    if sorts:
        payload["sorts"] = sorts
    return _paginated(f"/data_sources/{ds_id}/query", payload, max_pages=max_pages)


# ----- property reading -----------------------------------------------


def prop(page: dict[str, Any], name: str) -> Any:
    """Read one property off a page and normalise it to a plain Python value.

    Returns "" / None / [] rather than raising for a missing or empty
    property, so callers can treat every property as optional.
    """
    props = page.get("properties")
    if not isinstance(props, dict) or not name:
        return None
    raw = props.get(name)
    if not isinstance(raw, dict):
        return None
    return _prop_value(raw)


def _prop_value(raw: dict[str, Any]) -> Any:
    kind = raw.get("type") or ""
    value = raw.get(kind)

    if kind in ("title", "rich_text"):
        return plain_text(value)
    if kind in ("select", "status"):
        return str(value.get("name") or "") if isinstance(value, dict) else ""
    if kind == "multi_select":
        return [str(v.get("name") or "") for v in value or [] if isinstance(v, dict)]
    if kind == "date":
        if not isinstance(value, dict):
            return None
        return {"start": value.get("start") or "", "end": value.get("end") or ""}
    if kind == "checkbox":
        return bool(value)
    if kind == "number":
        return value
    if kind == "url":
        return str(value or "")
    if kind == "people":
        return [str(v.get("name") or "") for v in value or [] if isinstance(v, dict)]
    if kind == "relation":
        return [str(v.get("id") or "") for v in value or [] if isinstance(v, dict)]
    if kind == "unique_id":
        if not isinstance(value, dict):
            return ""
        prefix = str(value.get("prefix") or "")
        number = value.get("number")
        return f"{prefix}-{number}" if prefix else str(number or "")
    if kind == "formula":
        # A formula resolves to one of four scalar shapes; unwrap to the one
        # it actually produced rather than guessing from the property name.
        return _prop_value(value) if isinstance(value, dict) else None
    if kind == "rollup":
        if not isinstance(value, dict):
            return None
        if value.get("type") == "array":
            return [
                _prop_value(v) for v in value.get("array") or [] if isinstance(v, dict)
            ]
        return _prop_value(value)
    if kind in ("created_time", "last_edited_time"):
        return str(value or "")
    return None


def page_title(page: dict[str, Any], schema_props: dict[str, Any] | None = None) -> str:
    """The page's title, found by property *type* rather than by name.

    Notion lets you rename the title column to anything ("Task", "Project",
    "Name"), so matching on the name is the one thing guaranteed to break on
    someone else's workspace.
    """
    props = page.get("properties")
    if not isinstance(props, dict):
        return ""
    for raw in props.values():
        if isinstance(raw, dict) and raw.get("type") == "title":
            text = plain_text(raw.get("title"))
            if text:
                return text
    if schema_props:
        for name, spec in schema_props.items():
            if isinstance(spec, dict) and spec.get("type") == "title":
                return str(prop(page, name) or "")
    return ""


# ----- property auto-detection ----------------------------------------

# Name hints, checked in order, lowercased substring match. Type is always
# the primary filter; these only break ties between same-typed columns.
_HINTS: dict[str, tuple[str, ...]] = {
    "status": ("status", "state", "stage", "progress"),
    "due": ("due", "deadline", "date", "when", "target"),
    "priority": ("priority", "urgency", "importance"),
    "project": ("project", "epic", "parent", "area", "initiative"),
    "done": ("done", "complete", "completed", "finished", "checked"),
    "assignee": ("assignee", "owner", "person", "responsible"),
    "progress": ("progress", "complete", "completion", "percent"),
}


def _pick(
    schema_props: dict[str, Any], types: tuple[str, ...], hints: tuple[str, ...]
) -> str:
    """Best property of one of ``types``, preferring a name hint match."""
    candidates = [
        name
        for name, spec in schema_props.items()
        if isinstance(spec, dict) and spec.get("type") in types
    ]
    if not candidates:
        return ""
    for hint in hints:
        for name in candidates:
            if hint in name.lower():
                return name
    return candidates[0]


def detect(schema_props: dict[str, Any] | None) -> dict[str, str]:
    """Guess which properties carry status / due / priority / project / done.

    Type first, name second. Returns "" for anything absent, and every
    consumer treats a missing mapping as "don't show that bit" — so a
    database with no due-date column simply renders without due dates
    rather than erroring.
    """
    props = schema_props if isinstance(schema_props, dict) else {}
    return {
        "title": _pick(props, ("title",), ()),
        "status": _pick(props, ("status", "select"), _HINTS["status"]),
        "due": _pick(props, ("date",), _HINTS["due"]),
        "priority": _pick(props, ("select", "status", "number"), _HINTS["priority"]),
        "project": _pick(props, ("relation", "select"), _HINTS["project"]),
        "done": _pick(props, ("checkbox",), _HINTS["done"]),
        "assignee": _pick(props, ("people",), _HINTS["assignee"]),
        "progress": _pick(props, ("number", "formula", "rollup"), _HINTS["progress"]),
    }


def resolve_props(schema_props: dict[str, Any] | None, options: dict[str, Any]) -> dict[str, str]:
    """Auto-detection, with any explicit ``*_prop`` cell option winning.

    An override naming a property that doesn't exist is ignored rather than
    honoured — a typo should degrade to the detected column, not blank the
    field with no explanation.
    """
    detected = detect(schema_props)
    props = schema_props if isinstance(schema_props, dict) else {}
    for key in list(detected):
        override = str(options.get(f"{key}_prop") or "").strip()
        if override and override in props:
            detected[key] = override
    return detected


# ----- status semantics -----------------------------------------------

# Notion's `status` type carries a group ("To-do" / "In progress" / "Complete")
# but a plain `select` used as a status does not, so fall back to name matching.
_DONE_NAMES = ("done", "complete", "completed", "shipped", "archived", "cancelled",
               "canceled", "closed", "won't do", "wont do")


def is_done(status_value: str, checkbox_value: Any = None) -> bool:
    if checkbox_value is True:
        return True
    name = str(status_value or "").strip().lower()
    return any(name == d or name.startswith(d) for d in _DONE_NAMES)


# ----- cell-option choices --------------------------------------------


def choices(name: str) -> list[dict[str, str]]:
    """Dropdown contents for ``choices_from`` in this plugin AND, by
    delegation, in the sibling widgets.

    The host only ever calls ``choices()`` on the plugin whose manifest
    declares the option, so each widget re-exports this via the registry.
    Errors render as a single explanatory row rather than an empty picker,
    which is impossible to diagnose from the editor.
    """
    if name != "data_sources":
        return []
    if not is_configured():
        return [{"value": "", "label": "Set your Notion token in Settings → Widgets → Notion Core"}]
    found, err = data_sources()
    if err or found is None:
        return [{"value": "", "label": err or "Couldn't reach Notion"}]
    if not found:
        return [
            {
                "value": "",
                "label": "No databases shared with the integration yet (Notion: ••• → Connections)",
            }
        ]
    return [{"value": d["id"], "label": d["title"]} for d in found]


# ----- admin page -----------------------------------------------------


def blueprint() -> Blueprint:
    """``/plugins/notion_core/`` — connection status and database discovery.

    Setting this family up fails in two places that look identical from the
    cell editor: a bad token, and a good token on a database nobody shared
    with the integration. This page tells them apart, and lists the detected
    property mapping per database so you can see what the widgets will use
    before you place one.
    """
    bp = Blueprint("notion_core_admin", __name__, template_folder="templates")

    @bp.get("/")
    def index() -> str:
        refresh = request.args.get("refresh") == "1"
        inspect = (request.args.get("inspect") or "").strip()

        error = "" if is_configured() else (config_error() or ERR_NO_TOKEN)
        found: list[dict[str, str]] = []
        if is_configured():
            listed, err = data_sources(refresh=refresh)
            if err:
                error = err
            else:
                found = listed or []

        detail: dict[str, Any] | None = None
        if inspect and is_configured():
            props, err = schema(inspect, refresh=refresh)
            if err:
                error = error or err
            elif props is not None:
                detail = {
                    "id": inspect,
                    "title": next(
                        (d["title"] for d in found if d["id"] == inspect), inspect
                    ),
                    "properties": sorted(
                        (
                            (name, str(spec.get("type") or "?"))
                            for name, spec in props.items()
                            if isinstance(spec, dict)
                        ),
                        key=lambda pair: pair[0].lower(),
                    ),
                    "detected": detect(props),
                }

        return render_template(
            "notion_core/index.html",
            configured=is_configured(),
            data_sources=found,
            detail=detail,
            error=error,
            notion_version=notion_version(),
        )

    return bp
