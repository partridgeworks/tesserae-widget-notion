"""notion_tasks smoke tests: renders at every size, against a fake Notion."""

from __future__ import annotations

import json
import sys
from html import unescape
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest
from flask import Flask
from flask.testing import FlaskClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.fake_notion import DS_ID, fake_urlopen  # noqa: E402

PLUGIN = "notion_tasks"


def _configure(app: Flask) -> None:
    app.config["SETTINGS_STORE"].patch_section(
        "plugins", {"notion_core": {"api_token": "ntn_test-token"}}
    )


def _render(client: FlaskClient, size: str, **opts: object) -> str:
    """Render one cell. ``/_test/render`` takes cell options as a single
    ``?opts=<json>`` blob, not as individual query parameters."""
    options: dict[str, object] = {"data_source": DS_ID}
    options.update(opts)
    query = (
        f"/_test/render?plugin={PLUGIN}&size={size}"
        f"&opts={quote(json.dumps(options))}"
    )
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        resp = client.get(query)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    return unescape(resp.get_data(as_text=True))


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_renders_at_every_size(app: Flask, client: FlaskClient, size: str) -> None:
    _configure(app)
    body = _render(client, size)
    assert f'data-plugin="{PLUGIN}"' in body


def test_shows_task_titles_and_hides_completed(app: Flask, client: FlaskClient) -> None:
    """The fake set has three open tasks and one Done; the Done one must not
    reach the cell, and the overdue one must be present."""
    _configure(app)
    body = _render(client, "lg")
    assert "Fix the panel refresh loop" in body
    assert "Renew the domain" in body
    assert "Ship the deploy script" not in body


def test_show_completed_option_includes_done_tasks(app: Flask, client: FlaskClient) -> None:
    _configure(app)
    body = _render(client, "lg", show_completed=True)
    assert "Ship the deploy script" in body


def test_missing_token_renders_a_setup_message_not_a_crash(
    app: Flask, client: FlaskClient
) -> None:
    """No token configured: the cell should explain itself rather than 500."""
    body = _render(client, "md")
    assert f'data-plugin="{PLUGIN}"' in body
    assert "Notion" in body


def test_missing_database_option_asks_for_one(app: Flask, client: FlaskClient) -> None:
    _configure(app)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        resp = client.get(f"/_test/render?plugin={PLUGIN}&size=md")
    assert resp.status_code == 200
    assert "Pick a Notion database" in unescape(resp.get_data(as_text=True))
