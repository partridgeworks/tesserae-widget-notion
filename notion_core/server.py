"""notion_core — shared Notion connection for the notion_* widget family.

No widget cell of its own. The sibling ``notion_tasks`` / ``notion_list`` /
``notion_cards`` widgets reach in through the plugin registry and call ``accounts``,
``data_sources``, ``schema``, ``query`` and ``choices`` so all of them share
one set of credentials, one discovery cache, and one property-detection pass.

Built against **Notion-Version 2025-09-03**, where a *database* contains one
or more *data sources* and queries address the data source rather than the
database (``POST /v1/data_sources/{id}/query``). The older
``/v1/databases/{id}/query`` path is deprecated and breaks the moment a
database gains a second source, so this family only speaks the new shape.

Several Notion accounts (separate workspaces, or several integrations on one
workspace) can be configured, each with a friendly name and its own token.
Every widget cell picks which account it reads from.

Configuration lives on this plugin's own admin page, ``/plugins/notion_core/``,
NOT in the manifest's ``settings`` block. A static settings array cannot
express "a variable number of accounts, each with a name and a secret", so
the whole thing is one hand-built form instead. The manifest declares no
settings at all, which keeps configuration in exactly one place.

On-disk shape, under ``plugins.notion_core`` in settings.json:

    accounts_json                '[{"id": "a1b2c3d4", "name": "Work"}]'
    account_<id>_token_secret    encrypted per-account token
    notion_version               optional Notion-Version override

Caches, in this plugin's ``data_dir``:

    data_sources_<account>.json  that account's databases (TTL 1 h)
    schema_<account>_<ds>.json   one data source's property schema (TTL 1 h)

Imports are deliberately limited to the standard library and Flask: nothing
here depends on Tesserae's own modules.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

API_BASE = "https://api.notion.com/v1"
API_HOST = "api.notion.com"
DEFAULT_NOTION_VERSION = "2025-09-03"
USER_AGENT = "tesserae/0.1 (+notion_core)"
HTTP_TIMEOUT_S = 15

DISCOVERY_TTL_S = 3600
# Deliberately shorter than discovery. A stale *list* of databases is a
# cosmetic annoyance in a dropdown; a stale *schema* silently changes which
# column a widget reads, because detection runs against whatever this cache
# holds. That cost an afternoon: a relation column added after the cache was
# written was invisible for an hour, so grouping fell back to the first
# select column it could see (Priority) with nothing said.
SCHEMA_TTL_S = 900

# Notion pages a search/query 100 at a time. Cap the walk so a huge database
# can't turn one render into a hundred round-trips; widgets show a handful of
# rows and sort server-side within what they fetched.
PAGE_SIZE = 100
MAX_PAGES = 5

ACCOUNTS_FIELD = "accounts_json"
VERSION_FIELD = "notion_version"

ERR_NO_ACCOUNTS = (
    "No Notion account configured yet. Add one on the Notion Core admin page "
    "(Widgets → Notion Core → admin page)."
)
ERR_BAD_TOKEN = (
    "Notion rejected the integration token — copy it again from "
    "notion.so/my-integrations."
)
ERR_NO_ACCESS = (
    "Notion can't see that database. Open it in Notion, then ••• → "
    "Connections → add your integration."
)


# ----- settings ---------------------------------------------------------


def _settings() -> dict[str, Any]:
    store = current_app.config["SETTINGS_STORE"]
    section = store.get_section("plugins") or {}
    return section.get("notion_core") or {}


def _store() -> Any:
    return current_app.config["SETTINGS_STORE"]


def notion_version() -> str:
    return (_settings().get(VERSION_FIELD) or "").strip() or DEFAULT_NOTION_VERSION


def notion_version_raw() -> str:
    """The stored override exactly as typed ("" meaning "use the default").

    Distinct from ``notion_version()``, which resolves the default in. Saving
    must not turn an empty override into a pinned literal, or the install
    would stop following the default when this plugin is upgraded.
    """
    return str(_settings().get(VERSION_FIELD) or "").strip()


def _token_field(account_id: str) -> str:
    return f"account_{account_id}_token"


def accounts() -> list[dict[str, str]]:
    """Configured accounts as ``[{"id", "name"}]``, in display order."""
    raw = _settings().get(ACCOUNTS_FIELD)
    if not raw:
        return []
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [
                {"id": str(e.get("id") or ""), "name": str(e.get("name") or "")}
                for e in parsed
                if isinstance(e, dict) and e.get("id")
            ]
    return []


def token_for(account_id: str) -> str:
    """That account's integration token, or "" if it has none."""
    if not account_id:
        return ""
    s = _settings()
    field = _token_field(account_id)
    return (s.get(f"{field}_secret") or s.get(field) or "").strip()


def account_name(account_id: str) -> str:
    for entry in accounts():
        if entry["id"] == account_id:
            return entry["name"] or account_id
    return account_id


def configured_accounts() -> list[dict[str, str]]:
    """Only the accounts that actually have a token."""
    return [a for a in accounts() if token_for(a["id"])]


def is_configured() -> bool:
    return bool(configured_accounts())


def default_account_id() -> str:
    """The account a cell gets when it doesn't name one.

    With exactly one configured account this is simply that account, which
    is what makes the per-cell account picker redundant on a single-account
    install.
    """
    usable = configured_accounts()
    return usable[0]["id"] if usable else ""


def config_error() -> str | None:
    """Why Notion can't be used, precise enough to act on, or None.

    A token that was configured and has since become undecryptable (the
    encryption key changed under a recreated container or a restored data
    folder) reads back as an empty string, which is indistinguishable from
    "never set" unless we ask the store. The difference is "set this up"
    versus "type it in again".
    """
    if is_configured():
        return None
    with contextlib.suppress(Exception):
        unreadable = _store().unreadable_secrets("plugins", "notion_core")
        if any(str(k).endswith("_token_secret") for k in unreadable):
            return (
                "Your stored Notion token can no longer be decrypted (the "
                "encryption key changed); re-enter it on the Notion Core admin page."
            )
    if accounts():
        return "That Notion account has no token yet. Add one on the Notion Core admin page."
    return ERR_NO_ACCOUNTS


def resolve_account(options: dict[str, Any]) -> tuple[str, str | None]:
    """Which account a cell reads from → ``(account_id, error)``.

    **The selected database decides.** A Notion data source id belongs to
    exactly one workspace, so once a database is picked there is nothing left
    to infer; there is deliberately no per-cell account option (``choices()``
    is handed only the option key, never the cell's other values, so a
    database list could never be filtered to a chosen account anyway). With
    no database picked yet, the first configured account stands in so the
    cell can at least say what is wrong.
    """
    usable = configured_accounts()
    if not usable:
        return "", config_error() or ERR_NO_ACCOUNTS
    owner = account_for_data_source(str(options.get("data_source") or "").strip())
    return (owner or usable[0]["id"]), None


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


def _safe(value: str) -> str:
    return "".join(c for c in str(value) if c.isalnum() or c in "-_")[:64]


# ----- HTTP -------------------------------------------------------------


def _request(
    account_id: str, method: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[Any | None, str | None]:
    """One Notion API call as ``account_id`` → (parsed_json, friendly_error).

    Never raises: every caller in this family returns ``{"error": ...}``
    rather than letting an exception reach the renderer.
    """
    tok = token_for(account_id)
    if not tok:
        return None, config_error() or ERR_NO_ACCOUNTS

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
    account_id: str, path: str, payload: dict[str, Any], *, max_pages: int = MAX_PAGES
) -> tuple[list[dict[str, Any]] | None, str | None, bool]:
    """Walk a Notion cursor-paginated POST endpoint up to ``max_pages``.

    The third element is True when Notion still had more rows at the cap.
    That matters for filtering: rows dropped locally out of a truncated fetch
    mean the widget cannot claim to be showing everything that matches.
    """
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    truncated = False
    for _ in range(max_pages):
        page_payload = dict(payload, page_size=PAGE_SIZE)
        if cursor:
            page_payload["start_cursor"] = cursor
        data, err = _request(account_id, "POST", path, page_payload)
        if err or not isinstance(data, dict):
            return None, err or "Notion returned an unexpected response.", False
        results = data.get("results")
        if isinstance(results, list):
            out.extend(r for r in results if isinstance(r, dict))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor") or None
        if not cursor:
            break
        truncated = True
    else:
        truncated = bool(cursor)
    return out, None, truncated


# ----- rich text --------------------------------------------------------


def plain_text(rich: Any) -> str:
    """Flatten a Notion rich-text array to a plain string."""
    if isinstance(rich, str):
        return rich
    if not isinstance(rich, list):
        return ""
    parts = [str(node.get("plain_text") or "") for node in rich if isinstance(node, dict)]
    return "".join(parts).strip()


# ----- discovery --------------------------------------------------------


def data_sources(
    account_id: str, *, refresh: bool = False
) -> tuple[list[dict[str, str]] | None, str | None]:
    """One account's data sources → [{id, title, database_id}].

    Cached for an hour: this drives an edit-time dropdown and the widgets'
    account lookups, neither of which needs minute-fresh data, and Notion's
    search endpoint is comparatively slow.
    """
    if not account_id:
        return None, ERR_NO_ACCOUNTS
    cache = data_dir() / f"data_sources_{_safe(account_id)}.json"
    if not refresh:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            if cache.exists() and time.time() - cache.stat().st_mtime < DISCOVERY_TTL_S:
                loaded = json.loads(cache.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    return loaded, None

    results, err, _ = _paginated(
        account_id,
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
        parent = item.get("parent") or item.get("database_parent") or {}
        found.append(
            {
                "id": ds_id,
                "title": plain_text(item.get("title")) or "Untitled",
                "database_id": str(parent.get("database_id") or "")
                if isinstance(parent, dict)
                else "",
            }
        )
    found.sort(key=lambda d: d["title"].lower())

    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(found), encoding="utf-8")
    return found, None


def all_data_sources(*, refresh: bool = False) -> list[dict[str, str]]:
    """Every account's data sources, each tagged with its account.

    Best-effort: an account whose token has gone bad contributes nothing
    rather than breaking the dropdown for the accounts that still work.
    """
    out: list[dict[str, str]] = []
    for entry in configured_accounts():
        found, err = data_sources(entry["id"], refresh=refresh)
        if err or not found:
            continue
        for ds in found:
            out.append(
                {
                    "id": ds["id"],
                    "title": ds["title"],
                    "account_id": entry["id"],
                    "account_name": entry["name"] or entry["id"],
                }
            )
    return out


def account_for_data_source(ds_id: str) -> str:
    """Which account owns a data source, from the discovery caches."""
    if not ds_id:
        return ""
    for entry in configured_accounts():
        found, err = data_sources(entry["id"])
        if err or not found:
            continue
        if any(ds["id"] == ds_id for ds in found):
            return entry["id"]
    return ""


def schema(
    account_id: str, ds_id: str, *, refresh: bool = False
) -> tuple[dict[str, Any] | None, str | None]:
    """One data source's property schema → {prop_name: {"type": ...}}."""
    if not ds_id:
        return None, "No database selected."
    cache = data_dir() / f"schema_{_safe(account_id)}_{_safe(ds_id)}.json"
    if not refresh:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            if cache.exists() and time.time() - cache.stat().st_mtime < SCHEMA_TTL_S:
                loaded = json.loads(cache.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return loaded, None

    data, err = _request(account_id, "GET", f"/data_sources/{ds_id}")
    if err or not isinstance(data, dict):
        return None, err or "Couldn't read that database's properties."
    props = data.get("properties")
    props = props if isinstance(props, dict) else {}

    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(props), encoding="utf-8")
    return props, None


def page(account_id: str, page_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch one page by id. Used to resolve relation targets to titles."""
    if not page_id:
        return None, "No page id."
    data, err = _request(account_id, "GET", f"/pages/{page_id}")
    if err or not isinstance(data, dict):
        return None, err or "Notion returned an unexpected response."
    return data, None


def query(
    account_id: str,
    ds_id: str,
    *,
    filter_: dict[str, Any] | None = None,
    sorts: list[dict[str, Any]] | None = None,
    max_pages: int = MAX_PAGES,
) -> tuple[list[dict[str, Any]] | None, str | None, bool]:
    """Query a data source for pages → ``(pages, error, truncated)``."""
    if not ds_id:
        return None, "No database selected.", False
    payload: dict[str, Any] = {}
    if filter_:
        payload["filter"] = filter_
    if sorts:
        payload["sorts"] = sorts
    return _paginated(account_id, f"/data_sources/{ds_id}/query", payload, max_pages=max_pages)


# ----- property reading -------------------------------------------------


def prop(page_obj: dict[str, Any], name: str) -> Any:
    """Read one property off a page and normalise it to a plain Python value.

    Returns "" / None / [] rather than raising for a missing or empty
    property, so callers can treat every property as optional.
    """
    props = page_obj.get("properties")
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
    # A formula's result comes wrapped as {"type": "string", "string": ...}
    # / {"type": "boolean", "boolean": ...}: the two scalar shapes that are
    # not also top-level property types.
    if kind == "string":
        return str(value or "")
    if kind == "boolean":
        return bool(value)
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
            return [_prop_value(v) for v in value.get("array") or [] if isinstance(v, dict)]
        return _prop_value(value)
    if kind in ("created_time", "last_edited_time"):
        return str(value or "")
    return None


def prop_type(schema_props: dict[str, Any] | None, name: str) -> str:
    """The declared Notion type of a property, or "" if unknown."""
    if not isinstance(schema_props, dict) or not name:
        return ""
    spec = schema_props.get(name)
    return str(spec.get("type") or "") if isinstance(spec, dict) else ""


def page_title(page_obj: dict[str, Any], schema_props: dict[str, Any] | None = None) -> str:
    """The page's title, found by property *type* rather than by name.

    Notion lets you rename the title column to anything ("Task", "Project",
    "Name"), so matching on the name is the one thing guaranteed to break on
    someone else's workspace.
    """
    props = page_obj.get("properties")
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
                return str(prop(page_obj, name) or "")
    return ""


# ----- property auto-detection ------------------------------------------

# Name hints, checked in order, lowercased substring match. Type is always
# the primary filter; these only break ties between same-typed columns.
_HINTS: dict[str, tuple[str, ...]] = {
    "status": ("status", "state", "stage", "progress"),
    "date": ("due", "deadline", "date", "when", "target"),
    "priority": ("priority", "urgency", "importance"),
    "project": ("project", "epic", "parent", "area", "initiative"),
    "done": ("done", "complete", "completed", "finished", "checked"),
    "person": ("assignee", "owner", "person", "responsible"),
    "progress": ("progress", "complete", "completion", "percent"),
}

# A project column can be a relation to a Projects database, a plain select,
# or a multi_select where several projects are ticked at once.
PROJECT_TYPES = ("relation", "select", "multi_select")


def _pick(schema_props: dict[str, Any], types: tuple[str, ...], hints: tuple[str, ...]) -> str:
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
    """Guess which properties carry status / date / priority / project / done.

    Type first, name second. Returns "" for anything absent, and every
    consumer treats a missing mapping as "don't show that bit" — so a
    database with no date column renders without dates rather than
    erroring. Roles are generic (``date``, ``person``): what a widget calls
    them ("due", "assignee", "owner") is that widget's business.
    """
    props = schema_props if isinstance(schema_props, dict) else {}
    return {
        "title": _pick(props, ("title",), ()),
        "status": _pick(props, ("status", "select"), _HINTS["status"]),
        "date": _pick(props, ("date",), _HINTS["date"]),
        "priority": _pick(props, ("select", "status", "number"), _HINTS["priority"]),
        "project": _pick(props, PROJECT_TYPES, _HINTS["project"]),
        "done": _pick(props, ("checkbox",), _HINTS["done"]),
        "person": _pick(props, ("people",), _HINTS["person"]),
        "progress": _pick(props, ("number", "formula", "rollup"), _HINTS["progress"]),
    }


def unmatched_overrides(
    schema_props: dict[str, Any] | None, options: dict[str, Any]
) -> list[tuple[str, str]]:
    """``(role, typed_name)`` for every ``*_prop`` override naming a column
    this schema does not contain.

    Silently ignoring these was the bug: an operator who typed a real column
    name that the cached schema had not caught up with got a different column
    entirely, chosen by auto-detection, with no indication anything had been
    overridden at all.
    """
    props = schema_props if isinstance(schema_props, dict) else {}
    out: list[tuple[str, str]] = []
    for key in detect(None):
        typed = str(options.get(f"{key}_prop") or "").strip()
        if typed and typed not in props:
            out.append((key, typed))
    return out


def unknown_column_message(typed: str, schema_props: dict[str, Any] | None) -> str:
    """A column-not-found message that names what IS there.

    Listing the real columns turns "it ignored me" into a one-glance fix,
    including for the case this was written for: an emoji-prefixed name typed
    without its emoji.
    """
    names = sorted(schema_props or {})
    shown = ", ".join(f"'{n}'" for n in names[:12])
    if len(names) > 12:
        shown += f", … ({len(names)} in total)"
    return (
        f"This database has no column called '{typed}'. "
        f"Available columns: {shown or '(none)'}."
    )


def resolve_props(schema_props: dict[str, Any] | None, options: dict[str, Any]) -> dict[str, str]:
    """Auto-detection, with any explicit ``*_prop`` cell option winning.

    An override naming a property that doesn't exist is ignored here, but
    callers must not let that pass silently: check ``unmatched_overrides``
    first and surface it. Quietly falling back to a detected column means
    the cell reads something the operator never asked for.
    """
    detected = detect(schema_props)
    props = schema_props if isinstance(schema_props, dict) else {}
    for key in list(detected):
        override = str(options.get(f"{key}_prop") or "").strip()
        if override and override in props:
            detected[key] = override
    return detected


# ----- project names ----------------------------------------------------


def project_label(
    page_obj: dict[str, Any],
    prop_name: str,
    kind: str,
    relation_names: dict[str, str],
) -> str:
    """One display name for a page's project column.

    The column can legitimately hold several projects — a ``multi_select``
    with more than one tag ticked, or a ``relation`` pointing at several
    pages. Grouping and the row meta both need a single label, so the first
    non-empty entry wins; the order is Notion's own, which is the order
    shown in the Notion UI.
    """
    if not prop_name:
        return ""
    value = prop(page_obj, prop_name)
    if value is None:
        return ""
    if isinstance(value, list):
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            # A relation yields page ids; every other list type yields names.
            if kind == "relation":
                resolved = relation_names.get(text, "").strip()
                if resolved:
                    return resolved
                continue
            return text
        return ""
    return str(value).strip()


def relation_titles(
    account_id: str,
    pages: list[dict[str, Any]],
    prop_name: str,
    kind: str,
    *,
    limit: int = 24,
) -> dict[str, str]:
    """Resolve relation-target page ids to titles → {page_id: title}.

    Only meaningful for a ``relation`` column; every other type already
    carries names. Each id costs one request, so the walk is capped: past
    the cap those rows fall back to no project name rather than turning one
    render into dozens of round-trips.
    """
    if not prop_name or kind != "relation":
        return {}
    ids: list[str] = []
    for page_obj in pages:
        value = prop(page_obj, prop_name)
        if isinstance(value, list):
            ids.extend(str(v) for v in value if v)
    names: dict[str, str] = {}
    for page_id in list(dict.fromkeys(ids))[:limit]:
        data, err = page(account_id, page_id)
        if err or data is None:
            continue
        names[page_id] = page_title(data) or ""
    return names


# ----- filtering --------------------------------------------------------

# Types a server-side Notion filter can express directly. Everything else is
# matched locally after the fetch.
_FILTER_TEXT_TYPES = ("title", "rich_text", "url", "email", "phone_number")
_FILTER_EQUALS_TYPES = ("select", "status")
# Filterable at all, server-side or locally. A date or number column can't be
# "contains"-matched meaningfully, and saying so beats matching nothing.
FILTERABLE_TYPES = (
    "people", "multi_select", *_FILTER_TEXT_TYPES, *_FILTER_EQUALS_TYPES,
)

OWNER_TTL_S = 3600
# What a user types to mean "whoever owns this Notion token".
ME_ALIASES = ("me", "myself", "mine")


def split_columns(raw: Any) -> list[str]:
    """"Assignee, Collaborators" → ["Assignee", "Collaborators"]."""
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def token_owner(account_id: str) -> dict[str, str]:
    """The *person* who owns this integration token → ``{"id", "name"}``.

    ``GET /v1/users`` is forbidden to personal access tokens ("Personal access
    tokens cannot list users"), so a person's name cannot be resolved to the
    user id that Notion's ``people`` filter demands. This endpoint is the way
    in: it returns the *bot*, whose ``bot.owner.user`` is the human who
    created the integration.

    Note the trap it avoids. Notion accepts the literal ``"me"`` in a people
    filter, but "me" is the **bot**, which is never anybody's assignee — so
    the obvious spelling silently matches nothing. Filtering by the owner's
    real id is what actually works.
    """
    cache = data_dir() / f"owner_{_safe(account_id)}.json"
    with contextlib.suppress(OSError, json.JSONDecodeError):
        if cache.exists() and time.time() - cache.stat().st_mtime < OWNER_TTL_S:
            loaded = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded

    data, err = _request(account_id, "GET", "/users/me")
    owner: dict[str, str] = {}
    if not err and isinstance(data, dict):
        bot = data.get("bot")
        person = (bot or {}).get("owner", {}).get("user") if isinstance(bot, dict) else None
        if isinstance(person, dict) and person.get("id"):
            owner = {"id": str(person["id"]), "name": str(person.get("name") or "")}
        elif data.get("type") == "person" and data.get("id"):
            owner = {"id": str(data["id"]), "name": str(data.get("name") or "")}
    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(owner), encoding="utf-8")
    return owner


def resolve_person(account_id: str, wanted: str) -> str:
    """A Notion user id for ``wanted``, or "" if it can't be resolved.

    Resolves "me" (and the owner's own name) to the token owner. Any other
    name has no lookup available — see ``token_owner`` — so it falls through
    to local matching on the rendered names.
    """
    name = str(wanted or "").strip()
    if not name:
        return ""
    owner = token_owner(account_id)
    if not owner:
        return ""
    if name.lower() in ME_ALIASES:
        return owner["id"]
    if owner.get("name") and name.lower() == owner["name"].strip().lower():
        return owner["id"]
    return ""


def person_filter_error(account_id: str, wanted: str) -> str | None:
    """Reject a "me" that could not be resolved to a real user.

    Without this, an unresolvable "me" falls through to the local matcher and
    is substring-matched against rendered names — so it quietly returns every
    row assigned to a Mel, a James or a Carmen. An error is the only honest
    answer: "me" is a claim about identity, not a search term.
    """
    if str(wanted or "").strip().lower() in ME_ALIASES and not resolve_person(account_id, wanted):
        return (
            "Couldn't work out who 'me' is — Notion didn't report an owner for "
            "this integration. Re-check the token on the Notion Core admin page, "
            "or type the person's name as Notion spells it."
        )
    return None


def filter_column_error(schema_props: dict[str, Any] | None, columns: list[str]) -> str | None:
    """Reject filter columns that can't be matched, naming the usable ones.

    A date or number column would quietly match nothing, which reads exactly
    like a broken widget.
    """
    props = schema_props if isinstance(schema_props, dict) else {}
    for name in columns:
        if name not in props:
            return unknown_column_message(name, props)
        kind = prop_type(props, name)
        if kind not in FILTERABLE_TYPES:
            usable = sorted(n for n in props if prop_type(props, n) in FILTERABLE_TYPES)
            return (
                f"'{name}' is a {kind} column, which can't be filtered by text. "
                f"Filterable columns here: {', '.join(repr(u) for u in usable) or '(none)'}."
            )
    return None


def build_filter(
    schema_props: dict[str, Any] | None, columns: list[str], wanted: str, person_id: str
) -> tuple[dict[str, Any] | None, bool]:
    """A Notion filter for ``wanted`` across ``columns`` → (filter, complete).

    ``complete`` is True only when every column was expressed server-side. It
    is what tells the caller whether a truncated fetch can still be trusted:
    Notion filters before paging, so a server-side filter never loses rows to
    the page cap, while a local one can.
    """
    props = schema_props if isinstance(schema_props, dict) else {}
    clauses: list[dict[str, Any]] = []
    complete = True
    for name in columns:
        kind = prop_type(props, name)
        if kind == "people":
            # Only an id works here; a name is rejected outright by the API.
            if person_id:
                clauses.append({"property": name, "people": {"contains": person_id}})
            else:
                complete = False
        elif kind in _FILTER_TEXT_TYPES:
            clauses.append({"property": name, kind: {"contains": wanted}})
        elif kind in _FILTER_EQUALS_TYPES:
            clauses.append({"property": name, kind: {"equals": wanted}})
        else:
            # multi_select rejects the whole query with a 400 when the value
            # isn't an existing option, so it is matched locally instead.
            complete = False

    if not clauses:
        return None, False
    if len(clauses) == 1:
        return clauses[0], complete
    return {"or": clauses}, complete


def _column_text(page_obj: dict[str, Any], name: str, kind: str) -> tuple[str, list[str]]:
    """One column rendered for local matching → (text, people_ids)."""
    value = prop(page_obj, name)
    ids: list[str] = []
    if kind == "people":
        raw = (page_obj.get("properties") or {}).get(name) or {}
        ids = [
            str(u.get("id") or "")
            for u in (raw.get("people") or [])
            if isinstance(u, dict) and u.get("id")
        ]
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v), ids
    if isinstance(value, dict):
        return str(value.get("start") or ""), ids
    return str(value or ""), ids


def row_matches(
    page_obj: dict[str, Any],
    schema_props: dict[str, Any] | None,
    columns: list[str],
    wanted: str,
    person_id: str = "",
) -> bool:
    """Does this page match the filter? OR across ``columns``.

    Case-insensitive substring on the rendered text, plus an exact id match
    for people columns, so "me" works even where the display name doesn't.
    """
    needle = str(wanted or "").strip().lower()
    if not needle and not person_id:
        return True
    props = schema_props if isinstance(schema_props, dict) else {}
    for name in columns:
        text, ids = _column_text(page_obj, name, prop_type(props, name))
        if person_id and person_id in ids:
            return True
        if needle and needle in text.lower():
            return True
    return False


# ----- shared widget plumbing ------------------------------------------
#
# The display widgets (tasks, list, cards) all walk the same road from a
# cell's options to a queried, filtered, sorted set of pages; only the row
# shape each one paints differs. Everything on the common road lives here,
# so a fix lands in all of them at once and a new view starts from the same
# place the existing ones do.


def read_int(options: dict[str, Any], key: str, default: int, *, lo: int = 1,
             hi: int | None = None) -> int:
    """An integer cell option, clamped, with the default for anything unparseable."""
    try:
        value = int(options.get(key, default))
    except (TypeError, ValueError):
        value = default
    value = max(lo, value)
    return min(hi, value) if hi is not None else value


def result_cache(
    data_dir: Path, fingerprint: Any, refresh_min: int
) -> tuple[dict[str, Any] | None, Path]:
    """The cached result for this exact configuration, if fresh → (result, path).

    The fingerprint must contain every option that changes the rows, or
    editing that option would keep serving the previous configuration's
    rows until the refresh interval expired.
    """
    with contextlib.suppress(OSError):
        data_dir.mkdir(parents=True, exist_ok=True)
    slug = hashlib.sha1(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:12]
    path = data_dir / f"result_{slug}.json"
    with contextlib.suppress(OSError, json.JSONDecodeError):
        if path.exists() and time.time() - path.stat().st_mtime < refresh_min * 60:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                return cached, path
    return None, path


def override_names(options: dict[str, Any]) -> list[str]:
    """Every column name typed into a ``*_prop`` auto-detect override."""
    return [
        typed
        for key in detect(None)
        if (typed := str(options.get(f"{key}_prop") or "").strip())
    ]


def schema_with_columns(
    account_id: str, ds_id: str, required: list[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """The data source's schema, guaranteed to contain every ``required`` column.

    A named column that is missing is nearly always a schema this cache has
    not caught up with -- a column added minutes ago -- rather than a typo, so
    the schema is re-read once before blaming the operator. If it really
    isn't there the error names the columns that do exist. Falling through
    to auto-detection instead would mean silently reading a column nobody
    asked for.
    """
    schema_props, err = schema(account_id, ds_id)
    if err or schema_props is None:
        return None, err or "Couldn't read that database."
    wanted = [name for name in dict.fromkeys(required) if name]
    missing = [name for name in wanted if name not in schema_props]
    if missing:
        fresh, fresh_err = schema(account_id, ds_id, refresh=True)
        if fresh is not None and not fresh_err:
            schema_props = fresh
            missing = [name for name in wanted if name not in schema_props]
    if missing:
        return None, unknown_column_message(missing[0], schema_props)
    return schema_props, None


# -- person filter --------------------------------------------------------


def person_filter(
    account_id: str,
    schema_props: dict[str, Any] | None,
    options: dict[str, Any],
    default_column: str,
) -> tuple[dict[str, Any], str | None]:
    """Resolve a cell's "Only show items for" options → (filter, error).

    The returned filter carries ``person`` (what was typed, "" for none),
    ``columns``, ``person_id``, ``notion_filter`` (to hand to ``query``) and
    ``complete`` (True when Notion could apply every clause itself).

    A blank ``filter_person`` means "no filter", which is the pre-0.5
    behaviour and stays the default: a cell that never set one is unchanged.
    """
    person = str(options.get("filter_person") or "").strip()
    columns = split_columns(options.get("filter_columns"))
    empty = {
        "person": "", "columns": [], "person_id": "", "notion_filter": None, "complete": True,
    }
    if not person:
        return empty, None
    if not columns:
        # Default to the detected people column, which is what "assigned to
        # me" means in every database that has one.
        columns = [default_column] if default_column else []
    if not columns:
        return empty, (
            "This database has no people column to filter on. Name the "
            "columns to match in the cell's Filter columns option."
        )
    col_err = filter_column_error(schema_props, columns)
    if col_err:
        return empty, col_err
    person_err = person_filter_error(account_id, person)
    if person_err:
        return empty, person_err
    person_id = resolve_person(account_id, person)
    notion_filter, complete = build_filter(schema_props, columns, person, person_id)
    return {
        "person": person,
        "columns": columns,
        "person_id": person_id,
        "notion_filter": notion_filter,
        "complete": complete,
    }, None


def apply_person_filter(
    pages: list[dict[str, Any]], schema_props: dict[str, Any] | None, flt: dict[str, Any]
) -> list[dict[str, Any]]:
    """Drop the rows a person filter rejects; a no-op for the empty filter."""
    if not flt.get("person"):
        return pages
    return [
        p for p in pages
        if row_matches(p, schema_props, flt["columns"], flt["person"], flt["person_id"])
    ]


# -- sorting --------------------------------------------------------------

# Property types Notion will sort by itself. Sorting server-side matters
# because the fetch is capped: it decides WHICH rows come back, not just
# their order. Everything else is sorted locally, after the fetch.
SORT_SERVER_TYPES = frozenset({
    "title", "rich_text", "number", "select", "status", "date", "checkbox",
    "url", "email", "phone_number", "unique_id", "created_time", "last_edited_time",
})


SORT_SLOTS = 2


def sort_settings(options: dict[str, Any]) -> list[tuple[str, bool]]:
    """The cell's sort options → ``[(column, descending), ...]``, primary first.

    Slots are ``sort_*`` and ``sort2_*``. An empty list means the widget's
    own default order. A blank primary with a filled secondary still sorts,
    by the secondary alone: whatever was typed is what the operator meant.
    """
    out: list[tuple[str, bool]] = []
    for i in range(1, SORT_SLOTS + 1):
        prefix = "sort" if i == 1 else f"sort{i}"
        column = str(options.get(f"{prefix}_prop") or "").strip()
        if not column:
            continue
        direction = str(options.get(f"{prefix}_dir") or "asc").strip().lower()
        out.append((column, direction.startswith("desc")))
    return out


def notion_sorts(
    schema_props: dict[str, Any] | None, sorts: list[tuple[str, bool]]
) -> list[dict[str, str]] | None:
    """A ``sorts`` payload for ``query``, or None when Notion can't help.

    Only the leading run of columns Notion can sort is sent: once a column
    has to be sorted locally, sorting the fetch by anything after it would
    change which rows come back without making the order right.
    """
    out: list[dict[str, str]] = []
    for column, descending in sorts:
        if prop_type(schema_props, column) not in SORT_SERVER_TYPES:
            break
        out.append({"property": column, "direction": "descending" if descending else "ascending"})
    return out or None


def sort_value(page_obj: dict[str, Any], name: str) -> tuple[int, Any] | None:
    """One property as a comparable key, or None when it is empty.

    Notion values of different types can't be compared with each other (a
    formula column can yield a number on one row and text on another), so
    the key leads with a type rank: booleans, then numbers, then dates,
    then text. A list sorts by its first entry, in Notion's own order.
    """
    value = prop(page_obj, name)
    while isinstance(value, list):
        value = value[0] if value else None
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    if isinstance(value, dict):
        start = str(value.get("start") or "")
        return (2, start) if start else None
    return (3, str(value).strip().lower())


def _sort_once(
    pages: list[dict[str, Any]], column: str, descending: bool
) -> list[dict[str, Any]]:
    keyed = [(sort_value(p, column), p) for p in pages]
    valued = [(k, p) for k, p in keyed if k is not None]
    empties = [p for k, p in keyed if k is None]
    valued.sort(key=lambda pair: pair[0], reverse=descending)
    return [p for _, p in valued] + empties


def sort_pages(
    pages: list[dict[str, Any]], sorts: list[tuple[str, bool]]
) -> list[dict[str, Any]]:
    """Stable multi-column sort. Rows with no value in a column go last for
    that column either way: a blank isn't the biggest or the smallest, it's
    the least interesting. Applied secondary-first so ties in the primary
    keep the secondary's order.
    """
    for column, descending in reversed(sorts):
        pages = _sort_once(pages, column, descending)
    return list(pages)


# -- condition filter -----------------------------------------------------

# One column, one condition, one value: the generic filter the cards widget
# offers. Matched locally after the fetch, so it works for every property
# type and never trips Notion's per-type filter grammar.
CONDITIONS: dict[str, str] = {
    "equals": "is",
    "not_equals": "is not",
    "one_of": "is one of",
    "not_one_of": "is not one of",
    "contains": "contains",
    "not_contains": "does not contain",
    "is_empty": "is empty",
    "is_not_empty": "is not empty",
    "gt": "is greater than / after",
    "gte": "is at least / on or after",
    "lt": "is less than / before",
    "lte": "is at most / on or before",
}
_TRUE_WORDS = ("true", "yes", "checked", "done", "1", "on")


def _condition_text(value: Any) -> str:
    """A property value flattened to the text the operator would type."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return str(value.get("start") or "")
    if isinstance(value, list):
        return ", ".join(_condition_text(v) for v in value if v not in (None, ""))
    return str(value)


def _is_empty(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, dict):
        return not value.get("start")
    return value is None or value == "" or value == []


def _number(text: str) -> float | None:
    with contextlib.suppress(ValueError, TypeError):
        return float(str(text).strip().rstrip("%"))
    return None


def condition_matches(value: Any, op: str, wanted: str, *, today: str = "") -> bool:
    """Does one property value satisfy ``<op> <wanted>``?

    Case-insensitive on text. Numbers compare numerically when both sides
    parse; dates compare as ISO strings, so ``"2026-09"`` matches a whole
    month and the word ``today`` stands for today's date. A checkbox
    matches ``yes``/``no`` (and their usual synonyms). ``one_of`` takes a
    comma-separated list and holds when any entry of the value (any tag of
    a multi-select) is in it.
    """
    op = str(op or "equals").strip().lower()
    if op == "is_empty":
        return _is_empty(value)
    if op == "is_not_empty":
        return not _is_empty(value)

    needle = str(wanted or "").strip()
    if needle.lower() == "today" and today:
        needle = today
    haystack = _condition_text(value)
    if isinstance(value, bool):
        haystack = "yes" if value else "no"
        needle = "yes" if needle.lower() in _TRUE_WORDS else "no"
    items = (
        [_condition_text(v) for v in value if v not in (None, "")]
        if isinstance(value, list) else [haystack]
    )

    if op == "equals":
        return any(item.lower() == needle.lower() for item in items)
    if op == "not_equals":
        return not any(item.lower() == needle.lower() for item in items)
    if op in ("one_of", "not_one_of"):
        wanted_set = {part.lower() for part in split_columns(needle)}
        hit = any(item.lower() in wanted_set for item in items)
        return hit if op == "one_of" else not hit
    if op == "contains":
        return needle.lower() in haystack.lower()
    if op == "not_contains":
        return needle.lower() not in haystack.lower()

    if op in ("gt", "gte", "lt", "lte"):
        if _is_empty(value):
            return False
        left: Any
        right: Any
        left_n, right_n = _number(haystack), _number(needle)
        if left_n is not None and right_n is not None:
            left, right = left_n, right_n
        elif isinstance(value, dict):
            # A date against a shorter prefix ("2026-09"): compare at the
            # precision the operator typed, so "before 2026-09" means
            # "before September", not "before 2026-09-00T00:00".
            left, right = haystack[: len(needle)], needle
        else:
            left, right = haystack.lower(), needle.lower()
        if op == "gt":
            return left > right
        if op == "gte":
            return left >= right
        if op == "lt":
            return left < right
        return left <= right
    return True


# How many column-condition-value filters a cell can stack. They AND: a
# row has to satisfy every one that names a column.
CONDITION_SLOTS = 3


def condition_settings(options: dict[str, Any]) -> list[tuple[str, str, str]]:
    """The cell's condition-filter options → ``[(column, op, value), ...]``.

    Slots are ``filter_*``, ``filter2_*``, ``filter3_*``. A slot with no
    column is skipped, so a blank second filter doesn't disable the third.
    An unknown op falls back to ``equals`` rather than matching everything.
    """
    out: list[tuple[str, str, str]] = []
    for i in range(1, CONDITION_SLOTS + 1):
        prefix = "filter" if i == 1 else f"filter{i}"
        column = str(options.get(f"{prefix}_prop") or "").strip()
        if not column:
            continue
        op = str(options.get(f"{prefix}_op") or "equals").strip().lower()
        out.append((
            column,
            op if op in CONDITIONS else "equals",
            str(options.get(f"{prefix}_value") or "").strip(),
        ))
    return out


def apply_conditions(
    pages: list[dict[str, Any]],
    conditions: list[tuple[str, str, str]],
    *,
    today: str = "",
) -> list[dict[str, Any]]:
    """Keep the rows that satisfy every condition."""
    for column, op, wanted in conditions:
        pages = [p for p in pages if condition_matches(prop(p, column), op, wanted, today=today)]
    return pages


# ----- status semantics -------------------------------------------------

# Notion's `status` type carries a group ("To-do" / "In progress" / "Complete")
# but a plain `select` used as a status does not, so fall back to name matching.
_DONE_NAMES = (
    "done", "complete", "completed", "shipped", "archived", "cancelled",
    "canceled", "closed", "won't do", "wont do",
)


def is_done(status_value: str, checkbox_value: Any = None) -> bool:
    if checkbox_value is True:
        return True
    name = str(status_value or "").strip().lower()
    return any(name == d or name.startswith(d) for d in _DONE_NAMES)


# ----- cell-option choices ----------------------------------------------


def choices(name: str) -> list[dict[str, str]]:
    """Dropdown contents for ``choices_from`` in this plugin AND, by
    delegation, in the sibling widgets.

    The host only ever calls ``choices()`` on the plugin whose manifest
    declares the option, so each widget re-exports this via the registry.
    Errors render as a single explanatory row rather than an empty picker,
    which is impossible to diagnose from the editor.
    """
    if name == "data_sources":
        return _data_source_choices()
    return []


def _data_source_choices() -> list[dict[str, str]]:
    if not is_configured():
        return [{"value": "", "label": "No Notion account configured yet"}]
    found = all_data_sources()
    if not found:
        return [
            {
                "value": "",
                "label": "No databases shared with the integration yet (Notion: ••• → Connections)",
            }
        ]
    # This is the only account-bearing control a cell has: the account name
    # in the label is how the operator tells two workspaces' databases apart,
    # and picking a row selects that account (see `resolve_account`). With one
    # account the prefix would be noise.
    multi = len(configured_accounts()) > 1
    return [
        {
            "value": ds["id"],
            "label": f"{ds['account_name']} · {ds['title']}" if multi else ds["title"],
        }
        for ds in found
    ]


# ----- admin page -------------------------------------------------------


def _settings_fields(account_ids: list[str]) -> list[dict[str, Any]]:
    """The field list handed to ``update_for_namespace``.

    The store only persists keys it finds here, which is exactly what makes
    a variable number of accounts storable: the list is built per save from
    the accounts being saved.
    """
    fields: list[dict[str, Any]] = [
        {"name": ACCOUNTS_FIELD, "type": "string", "label": "Accounts"},
        {"name": VERSION_FIELD, "type": "string", "label": "Notion-Version"},
    ]
    for account_id in account_ids:
        fields.append(
            {
                "name": _token_field(account_id),
                "type": "string",
                "label": f"Token for {account_id}",
                "secret": True,
            }
        )
    return fields


def _persist(entries: list[dict[str, str]], tokens: dict[str, str], version: str) -> None:
    """Persist the account list, any changed tokens, and the version override.

    ``tokens`` carries only the accounts whose token is actually changing;
    the store merges rather than replaces, so an untouched account keeps the
    token it already has. That is what lets the form render token inputs
    empty instead of round-tripping secrets through the browser.
    """
    account_ids = [e["id"] for e in entries]
    values: dict[str, Any] = {
        ACCOUNTS_FIELD: json.dumps(entries),
        VERSION_FIELD: version,
    }
    values.update({_token_field(k): v for k, v in tokens.items()})
    _store().update_for_namespace(
        "plugins", "notion_core", values, _settings_fields(account_ids + list(tokens))
    )


def _erase_token(account_id: str) -> None:
    """Remove an account's token from disk entirely.

    Blanking it through ``update_for_namespace`` is not enough: a token can
    sit under either ``account_<id>_token_secret`` or the pre-encryption
    ``account_<id>_token``, and the store merges, so writing "" to one leaves
    the other readable. Deleting both is the only way it actually goes.

    ``raw_section`` is deliberate: it hands back the section with secrets
    still wrapped, so the other accounts' tokens are written back as
    ciphertext. Reading through ``get_section`` would decrypt them, and
    patching that back would rewrite every other token in plaintext.
    """
    store = _store()
    raw = store.raw_section("plugins")
    item = dict(raw.get("notion_core") or {})
    field = _token_field(account_id)
    item.pop(f"{field}_secret", None)
    item.pop(field, None)
    store.patch_section("plugins", {**raw, "notion_core": item})


def _forget_caches(account_id: str) -> None:
    directory = data_dir()
    with contextlib.suppress(OSError):
        for path in directory.glob(f"data_sources_{_safe(account_id)}.json"):
            path.unlink()
        for path in directory.glob(f"schema_{_safe(account_id)}_*.json"):
            path.unlink()


def blueprint() -> Blueprint:
    """``/plugins/notion_core/`` — every bit of Notion configuration.

    This page owns the settings rather than the manifest's ``settings``
    block, because that block is a static array and cannot express "a
    variable number of accounts, each with a friendly name and a secret".
    Keeping the version override here too means there is exactly one place
    to look.

    It also earns its keep as a diagnostic: setting this family up fails in
    two ways that are indistinguishable from the cell editor — a bad token,
    and a good token on a database nobody shared with the integration. This
    page tells them apart, per account, and shows the detected property
    mapping so you can see what the widgets will read before placing a cell.
    """
    bp = Blueprint("notion_core_admin", __name__, template_folder="templates")

    @bp.get("/")
    def index() -> str:
        refresh = request.args.get("refresh") == "1"
        inspect = (request.args.get("inspect") or "").strip()

        panes = []
        for entry in accounts():
            account_id = entry["id"]
            has_token = bool(token_for(account_id))
            listed: list[dict[str, str]] = []
            error = ""
            if has_token:
                found, err = data_sources(account_id, refresh=refresh)
                error = err or ""
                listed = found or []
            panes.append(
                {
                    "id": account_id,
                    "name": entry["name"],
                    "has_token": has_token,
                    "data_sources": listed,
                    "error": error,
                }
            )

        detail = None
        if inspect:
            owner = account_for_data_source(inspect)
            if owner:
                props, err = schema(owner, inspect, refresh=refresh)
                if props is not None and not err:
                    found, _ = data_sources(owner)
                    detail = {
                        "id": inspect,
                        "account_name": account_name(owner),
                        "title": next(
                            (d["title"] for d in (found or []) if d["id"] == inspect), inspect
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
            panes=panes,
            detail=detail,
            notion_version=notion_version(),
            version_override=notion_version_raw(),
            default_version=DEFAULT_NOTION_VERSION,
            configured=is_configured(),
        )

    @bp.post("/accounts")
    def add_account() -> Any:
        entries = accounts()
        # Random rather than sequential: the id becomes part of a settings
        # key, so it must stay stable across renames and never collide with
        # an id a deleted account used to hold.
        new_id = secrets.token_hex(4)
        name = (request.form.get("name") or "").strip() or f"Notion {len(entries) + 1}"
        token = (request.form.get("token") or "").strip()
        entries.append({"id": new_id, "name": name})
        _persist(entries, {new_id: token} if token else {}, notion_version_raw())
        flash(f"Added account '{name}'.", "ok")
        return redirect(url_for("notion_core_admin.index"))

    @bp.post("/save")
    def save() -> Any:
        entries = accounts()
        tokens: dict[str, str] = {}
        for entry in entries:
            account_id = entry["id"]
            name = (request.form.get(f"name_{account_id}") or "").strip()
            if name:
                entry["name"] = name
            # Token inputs render empty: blank means "leave it alone", so a
            # save that only renames an account never round-trips a secret
            # through the browser.
            token = (request.form.get(f"token_{account_id}") or "").strip()
            if token:
                tokens[account_id] = token
                _forget_caches(account_id)
        version = (request.form.get("notion_version") or "").strip()
        _persist(entries, tokens, version)
        flash("Saved.", "ok")
        return redirect(url_for("notion_core_admin.index"))

    @bp.post("/accounts/<account_id>/remove")
    def remove_account(account_id: str) -> Any:
        entries = [e for e in accounts() if e["id"] != account_id]
        # Order matters: rewrite the account list first, then erase the token.
        # `_persist` merges, so erasing first would let the list write put
        # nothing back but would leave a window where the account is gone and
        # its secret is not.
        _persist(entries, {}, notion_version_raw())
        _erase_token(account_id)
        _forget_caches(account_id)
        flash("Account removed.", "ok")
        return redirect(url_for("notion_core_admin.index"))

    return bp
