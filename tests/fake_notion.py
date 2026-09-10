"""A fake Notion API, good enough to render both widgets offline.

Patched over ``urllib.request.urlopen`` so no test ever touches the network.
Dispatches on method + path the way the real API does, which means the tests
exercise the actual request the widgets build rather than a stub of it.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request

DS_ID = "11111111-2222-3333-4444-555555555555"

SCHEMA: dict[str, Any] = {
    "Name": {"type": "title"},
    "Status": {"type": "status"},
    "Due": {"type": "date"},
    "Priority": {"type": "select"},
    "Project": {"type": "select"},
    "Progress": {"type": "number"},
    "Owner": {"type": "people"},
    "Done": {"type": "checkbox"},
}


def _page(
    page_id: str,
    name: str,
    status: str,
    due: str,
    priority: str,
    project: str,
    progress: float | None = None,
    done: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "Name": {"type": "title", "title": [{"plain_text": name}]},
        "Status": {"type": "status", "status": {"name": status}},
        "Due": {"type": "date", "date": {"start": due} if due else None},
        "Priority": {"type": "select", "select": {"name": priority} if priority else None},
        "Project": {"type": "select", "select": {"name": project} if project else None},
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


# Deliberately mixed: one overdue, one undated, one already complete, so the
# sort order and the completed-filter both have something to bite on.
PAGES = [
    _page(
        "p1", "Fix the panel refresh loop", "In progress", "2020-01-02",
        "High", "Tesserae", 0.75,
    ),
    _page("p2", "Write the Notion widget README", "To do", "2035-06-01", "Medium", "Tesserae", 0.4),
    _page("p3", "Renew the domain", "To do", "", "Low", "Life admin", 0.1),
    _page("p4", "Ship the deploy script", "Done", "2020-02-02", "High", "Tesserae", 1.0, done=True),
]

SEARCH_RESULTS = [
    {
        "object": "data_source",
        "id": DS_ID,
        "title": [{"plain_text": "Work Tracker"}],
        "parent": {"database_id": "db-1"},
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


def fake_urlopen(req: Request, *args: object, **kwargs: object) -> _Resp:
    url = req.full_url if isinstance(req, Request) else str(req)
    method = req.get_method() if isinstance(req, Request) else "GET"

    if url.endswith("/v1/search"):
        return _Resp({"object": "list", "results": SEARCH_RESULTS, "has_more": False})
    if method == "GET" and "/v1/data_sources/" in url:
        return _Resp({"object": "data_source", "id": DS_ID, "properties": SCHEMA})
    if method == "POST" and url.endswith("/query"):
        return _Resp({"object": "list", "results": PAGES, "has_more": False})
    if "/v1/pages/" in url:
        return _Resp(PAGES[0])
    raise AssertionError(f"fake_notion got an unexpected request: {method} {url}")
