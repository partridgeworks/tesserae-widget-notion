"""A fake Notion API, good enough to render both widgets offline.

Patched over ``urllib.request.urlopen`` so no test ever touches the network.
Dispatches on method + path the way the real API does, which means the tests
exercise the actual request the widgets build rather than a stub of it.

Two workspaces are modelled so the multi-account paths are exercised: the
token on the request decides which one answers, exactly as Notion does.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any
from urllib.request import Request

# Workspace A (the account most tests use)
DS_ID = "11111111-2222-3333-4444-555555555555"
TOKEN_A = "ntn_test-token"

# Workspace B, reachable only with its own token
DS_ID_B = "99999999-8888-7777-6666-555555555555"
TOKEN_B = "ntn_second-token"

RELATED_PAGE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
# Pages a rollup reaches through the epic's own relation (Episodes →
# Bookings → Topic, where Topic is itself a relation to a Topics database).
# The last has no page behind it: an id the resolver can't turn into a name.
TOPIC_PAGE_IDS = (
    "10101010-2020-3030-4040-505050505050",
    "60606060-7070-8080-9090-a0a0a0a0a0a0",
    "deadbeef-dead-beef-dead-beefdeadbeef",
)

# The human who owns the integration token. GET /v1/users/me returns the BOT;
# the person is under bot.owner.user, and their id is the only thing Notion's
# people filter accepts.
OWNER_ID = "0000aaaa-1111-bbbb-2222-cccc3333dddd"
OWNER_NAME = "Carl Partridge"
OTHER_ID = "9999aaaa-8888-bbbb-7777-cccc6666dddd"
OTHER_NAME = "Someone Else"

SCHEMA: dict[str, Any] = {
    "Name": {"type": "title"},
    # A status's options belong to groups, and Notion sorts by group first
    # then by position within the group. The options list is deliberately
    # NOT in that order, so a widget that reads the list, or the alphabet,
    # instead of the groups gets caught.
    "Status": {"type": "status", "status": {
        "options": [
            {"id": "st-done", "name": "Done", "color": "green"},
            {"id": "st-todo", "name": "To do", "color": "default"},
            {"id": "st-prog", "name": "In progress", "color": "blue"},
        ],
        "groups": [
            {"id": "g-todo", "name": "To-do", "color": "gray", "option_ids": ["st-todo"]},
            {"id": "g-prog", "name": "In progress", "color": "blue", "option_ids": ["st-prog"]},
            {"id": "g-done", "name": "Complete", "color": "green", "option_ids": ["st-done"]},
        ],
    }},
    "Due": {"type": "date"},
    # Arranged High, Medium, Low in Notion. The alphabet says High, Low,
    # Medium, which is what the widgets showed before they read the options.
    "Priority": {"type": "select", "select": {"options": [
        {"id": "pr-high", "name": "High", "color": "red"},
        {"id": "pr-med", "name": "Medium", "color": "yellow"},
        {"id": "pr-low", "name": "Low", "color": "gray"},
    ]}},
    "Project": {"type": "multi_select"},
    "Progress": {"type": "number", "number": {"format": "percent"}},
    "Owner": {"type": "people"},
    "Collaborators": {"type": "people"},
    "Done": {"type": "checkbox"},
    # Read only by the cards widget, which shows any column it is pointed
    # at: plain text, a link, and a timestamp Notion maintains itself.
    "Notes": {"type": "rich_text"},
    "Link": {"type": "url"},
    "Created": {"type": "created_time"},
}

# Workspace B uses a relation for its project column, so the relation branch
# of project_label / relation_titles gets exercised too.
SCHEMA_B: dict[str, Any] = {
    "Task": {"type": "title"},
    "Stage": {"type": "status"},
    "Deadline": {"type": "date"},
    "Epic": {"type": "relation"},
    # A rollup over the epic's tags: Notion hands back one entry per
    # related page, each a whole multi-select, i.e. a list of lists.
    "Topics": {"type": "rollup"},
    # A rollup over a relation on the epic: entries are relations, i.e.
    # page ids, which only mean something once resolved to titles.
    "Guests": {"type": "rollup"},
}


def _page(
    page_id: str,
    name: str,
    status: str,
    due: str,
    priority: str,
    projects: list[str],
    progress: float | None = None,
    done: bool = False,
    assignee: list[dict[str, str]] | None = None,
    collaborators: list[dict[str, str]] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "Name": {"type": "title", "title": [{"plain_text": name}]},
        "Status": {"type": "status", "status": {"name": status}},
        "Due": {"type": "date", "date": {"start": due} if due else None},
        "Priority": {"type": "select", "select": {"name": priority} if priority else None},
        # Multi-valued on purpose: the widget has to pick one, not stringify
        # the list.
        "Project": {
            "type": "multi_select",
            "multi_select": [{"name": p} for p in projects],
        },
        "Progress": {"type": "number", "number": progress},
        "Owner": {"type": "people", "people": assignee or []},
        "Collaborators": {"type": "people", "people": collaborators or []},
        "Done": {"type": "checkbox", "checkbox": done},
        "Notes": {"type": "rich_text", "rich_text": [{"plain_text": notes}] if notes else []},
        "Link": {"type": "url", "url": f"https://example.test/{page_id}"},
        "Created": {"type": "created_time", "created_time": "2026-09-01T09:30:00.000Z"},
    }
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://www.notion.so/{page_id}",
        "properties": props,
    }


# Deliberately mixed: one overdue, one undated, one already complete, and two
# distinct projects — so sort order, the completed filter and grouping all
# have something to bite on. "Fix the panel refresh loop" carries TWO
# projects, which is the multi-select case.
ME = [{"object": "user", "id": OWNER_ID, "name": OWNER_NAME}]
THEM = [{"object": "user", "id": OTHER_ID, "name": OTHER_NAME}]

PAGES = [
    _page(
        "p1", "Fix the panel refresh loop", "In progress", "2020-01-02",
        "High", ["Tesserae", "Home lab"], 0.75, assignee=ME, notes="Flickers every 3rd push",
    ),
    _page(
        "p2", "Write the Notion widget README", "To do", "2035-06-01",
        "Medium", ["Tesserae"], 0.4, assignee=ME,
    ),
    # Assigned to somebody else: the thing a filter has to remove.
    _page("p3", "Renew the domain", "To do", "", "Low", ["Life admin"], 0.1, assignee=THEM),
    _page(
        "p4", "Ship the deploy script", "Done", "2020-02-02",
        "High", ["Tesserae"], 1.0, True, assignee=ME,
    ),
    # No project and nobody assigned.
    _page("p5", "Unfiled odd job", "To do", "2035-07-01", "", [], 0.0, collaborators=ME),
]

PAGES_B = [
    {
        "object": "page",
        "id": "b1",
        "url": "https://www.notion.so/b1",
        "properties": {
            "Task": {"type": "title", "title": [{"plain_text": "Second workspace task"}]},
            "Stage": {"type": "status", "status": {"name": "To do"}},
            "Deadline": {"type": "date", "date": {"start": "2035-01-01"}},
            "Epic": {"type": "relation", "relation": [{"id": RELATED_PAGE_ID}]},
            "Topics": {
                "type": "rollup",
                "rollup": {
                    "type": "array",
                    "function": "show_original",
                    "array": [
                        {"type": "multi_select", "multi_select": [
                            {"name": "Hardware"}, {"name": "E-ink"},
                        ]},
                        {"type": "multi_select", "multi_select": [
                            {"name": "E-ink"}, {"name": "Firmware"},
                        ]},
                    ],
                },
            },
            "Guests": {
                "type": "rollup",
                "rollup": {
                    "type": "array",
                    "function": "show_original",
                    "array": [
                        {"type": "relation", "has_more": False, "relation": [
                            {"id": TOPIC_PAGE_IDS[0]}, {"id": TOPIC_PAGE_IDS[1]},
                        ]},
                        {"type": "relation", "has_more": False, "relation": [
                            {"id": TOPIC_PAGE_IDS[1]}, {"id": TOPIC_PAGE_IDS[2]},
                        ]},
                    ],
                },
            },
        },
    }
]

RELATED_PAGE = {
    "object": "page",
    "id": RELATED_PAGE_ID,
    "properties": {"Name": {"type": "title", "title": [{"plain_text": "Migration epic"}]}},
}

TOPIC_PAGES = {
    TOPIC_PAGE_IDS[0]: {
        "object": "page",
        "id": TOPIC_PAGE_IDS[0],
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "Ada Lovelace"}]}},
    },
    TOPIC_PAGE_IDS[1]: {
        "object": "page",
        "id": TOPIC_PAGE_IDS[1],
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "Grace Hopper"}]}},
    },
}

SEARCH_A = [
    {
        "object": "data_source",
        "id": DS_ID,
        "title": [{"plain_text": "Work Tracker"}],
        "parent": {"database_id": "db-1"},
    }
]
SEARCH_B = [
    {
        "object": "data_source",
        "id": DS_ID_B,
        "title": [{"plain_text": "Personal Tracker"}],
        "parent": {"database_id": "db-2"},
    }
]


class _Resp:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _forbidden(url: str) -> urllib.error.HTTPError:
    """``GET /v1/users`` as a personal access token really does 403."""
    body = json.dumps({
        "object": "error", "code": "restricted_resource",
        "message": "Personal access tokens cannot list users.",
    }).encode()
    return urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(body))


def _not_found(url: str) -> urllib.error.HTTPError:
    """What Notion really returns when a token asks for a database in another
    workspace: 404 with ``object_not_found``, indistinguishable from a
    database that was simply never shared with the integration."""
    body = json.dumps({"object": "error", "code": "object_not_found"}).encode()
    return urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(body))


def _clause_matches(page: dict[str, Any], clause: dict[str, Any]) -> bool:
    """The subset of Notion's filter grammar these widgets emit."""
    if "or" in clause:
        return any(_clause_matches(page, c) for c in clause["or"])
    name = clause.get("property")
    raw = (page.get("properties") or {}).get(name) or {}
    if "people" in clause:
        wanted = clause["people"].get("contains")
        return any(u.get("id") == wanted for u in (raw.get("people") or []))
    for kind in ("title", "rich_text", "select", "status"):
        if kind in clause:
            cond = clause[kind]
            if kind in ("title", "rich_text"):
                text = "".join(n.get("plain_text", "") for n in (raw.get(kind) or []))
            else:
                text = ((raw.get(kind) or {}) or {}).get("name") or ""
            if "contains" in cond:
                return cond["contains"].lower() in text.lower()
            if "equals" in cond:
                return cond["equals"] == text
    return True


def _apply_filter(pages: list[dict[str, Any]], flt: Any) -> list[dict[str, Any]]:
    if not isinstance(flt, dict):
        return pages
    return [p for p in pages if _clause_matches(p, flt)]


def fake_urlopen(req: Request, *args: object, **kwargs: object) -> _Resp:
    url = req.full_url if isinstance(req, Request) else str(req)
    method = req.get_method() if isinstance(req, Request) else "GET"
    auth = req.get_header("Authorization", "") if isinstance(req, Request) else ""
    workspace_b = TOKEN_B in auth

    if url.endswith("/v1/search"):
        results = SEARCH_B if workspace_b else SEARCH_A
        return _Resp({"object": "list", "results": results, "has_more": False})

    if "/v1/data_sources/" in url:
        # A token only sees its own workspace. Asking workspace B for
        # workspace A's data source 404s, exactly as the real API does --
        # which is what makes an account/database mismatch a real failure
        # rather than a cosmetic one.
        owned = DS_ID_B if workspace_b else DS_ID
        if owned not in url:
            raise _not_found(url)
        if method == "GET":
            schema = SCHEMA_B if workspace_b else SCHEMA
            return _Resp({"object": "data_source", "id": owned, "properties": schema})
        if url.endswith("/query"):
            pages = PAGES_B if workspace_b else PAGES
            payload = json.loads(req.data.decode()) if getattr(req, "data", None) else {}
            pages = _apply_filter(pages, payload.get("filter"))
            return _Resp({"object": "list", "results": pages, "has_more": False})

    if url.endswith("/v1/users/me"):
        # Mirrors the real shape: a bot, with the human under bot.owner.user.
        return _Resp({
            "object": "user", "id": "bot-id", "name": "Tesserae-test", "type": "bot",
            "bot": {"owner": {"type": "user", "user": {
                "object": "user", "id": OWNER_ID, "name": OWNER_NAME}}},
        })
    if url.endswith("/v1/users") or "/v1/users?" in url:
        # Notion refuses this for personal access tokens.
        raise _forbidden(url)
    if "/v1/pages/" in url:
        if RELATED_PAGE_ID in url:
            return _Resp(RELATED_PAGE)
        for page_id, topic_page in TOPIC_PAGES.items():
            if page_id in url:
                return _Resp(topic_page)
        if TOPIC_PAGE_IDS[2] in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(
                json.dumps({"object": "error", "code": "object_not_found",
                            "message": "Could not find page."}).encode()))
        return _Resp(PAGES[0])
    raise AssertionError(f"fake_notion got an unexpected request: {method} {url}")
