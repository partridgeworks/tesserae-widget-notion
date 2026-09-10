"""Shared test helpers: configure accounts, render a cell."""

from __future__ import annotations

import json
from html import unescape
from typing import Any
from unittest.mock import patch
from urllib.parse import quote

from flask import Flask
from flask.testing import FlaskClient

from fake_notion import DS_ID, TOKEN_A, TOKEN_B, fake_urlopen

ACCOUNT_A = "aaaa1111"
ACCOUNT_B = "bbbb2222"


def configure_one_account(app: Flask) -> None:
    """One account, in the multi-account shape."""
    app.config["SETTINGS_STORE"].patch_section(
        "plugins",
        {
            "notion_core": {
                "accounts_json": json.dumps([{"id": ACCOUNT_A, "name": "Work"}]),
                f"account_{ACCOUNT_A}_token_secret": TOKEN_A,
            }
        },
    )


def configure_two_accounts(app: Flask) -> None:
    app.config["SETTINGS_STORE"].patch_section(
        "plugins",
        {
            "notion_core": {
                "accounts_json": json.dumps(
                    [{"id": ACCOUNT_A, "name": "Work"}, {"id": ACCOUNT_B, "name": "Personal"}]
                ),
                f"account_{ACCOUNT_A}_token_secret": TOKEN_A,
                f"account_{ACCOUNT_B}_token_secret": TOKEN_B,
            }
        },
    )


def configure_legacy_account(app: Flask) -> None:
    """The pre-multi-account shape: one bare token, no account list."""
    app.config["SETTINGS_STORE"].patch_section(
        "plugins", {"notion_core": {"api_token_secret": TOKEN_A}}
    )


def render(client: FlaskClient, plugin: str, size: str = "md", **opts: Any) -> str:
    """Render one cell. ``/_test/render`` takes cell options as a single
    ``?opts=<json>`` blob, not as individual query parameters."""
    options: dict[str, Any] = {"data_source": DS_ID}
    options.update(opts)
    query = f"/_test/render?plugin={plugin}&size={size}&opts={quote(json.dumps(options))}"
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        resp = client.get(query)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    return unescape(resp.get_data(as_text=True))


def cell_data(body: str) -> dict[str, Any]:
    """The JSON the server handed the cell, pulled back out of the markup.

    Asserting on this rather than on rendered text keeps the data tests
    independent of the client-side layout, which changes far more often.
    """
    marker = "data-data='"
    start = body.index(marker) + len(marker)
    end = body.index("'", start)
    return json.loads(body[start:end])
