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

SCHEMA: dict[str, Any] = {
    "Name": {"type": "title"},
    "Status": {"type": "status"},
    "Due": {"type": "date"},
    "Priority": {"type": "select"},
    "Project": {"type": "multi_select"},
    "Progress": {"type": "number"},
    "Owner": {"type": "people"},
    "Done": {"type": "checkbox"},
}

# Workspace B uses a relation for its project column, so the relation branch
# of project_label / relation_titles gets exercised too.
SCHEMA_B: dict[str, Any] = {
    "Task": {"type": "title"},
    "Stage": {"type": "status"},
    "Deadline": {"type": "date"},
    "Epic": {"type": "relation"},
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
        "Owner": {"type": "people", "people": [{"name": "Carl"}]},
        "Done": {"type": "checkbox", "checkbox": done},
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
PAGES = [
    _page(
        "p1", "Fix the panel refresh loop", "In progress", "2020-01-02",
        "High", ["Tesserae", "Home lab"], 0.75,
    ),
    _page(
        "p2", "Write the Notion widget README", "To do", "2035-06-01",
        "Medium", ["Tesserae"], 0.4,
    ),
    _page("p3", "Renew the domain", "To do", "", "Low", ["Life admin"], 0.1),
    _page("p4", "Ship the deploy script", "Done", "2020-02-02", "High", ["Tesserae"], 1.0, True),
    # No project at all: must land in the "No project" group, last.
    _page("p5", "Unfiled odd job", "To do", "2035-07-01", "", [], 0.0),
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
        },
    }
]

RELATED_PAGE = {
    "object": "page",
    "id": RELATED_PAGE_ID,
    "properties": {"Name": {"type": "title", "title": [{"plain_text": "Migration epic"}]}},
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


def _not_found(url: str) -> urllib.error.HTTPError:
    """What Notion really returns when a token asks for a database in another
    workspace: 404 with ``object_not_found``, indistinguishable from a
    database that was simply never shared with the integration."""
    body = json.dumps({"object": "error", "code": "object_not_found"}).encode()
    return urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(body))


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
            return _Resp({"object": "list", "results": pages, "has_more": False})

    if "/v1/pages/" in url:
        if RELATED_PAGE_ID in url:
            return _Resp(RELATED_PAGE)
        return _Resp(PAGES[0])
    raise AssertionError(f"fake_notion got an unexpected request: {method} {url}")
