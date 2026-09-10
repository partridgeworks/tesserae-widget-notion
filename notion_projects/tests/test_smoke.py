"""notion_projects smoke tests: renders at every size, against a fake Notion."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fake_notion import DS_ID_B  # noqa: E402
from helpers import (  # noqa: E402
    ACCOUNT_B,
    cell_data,
    configure_one_account,
    configure_two_accounts,
    render,
)

PLUGIN = "notion_projects"


def _configure(app: Flask) -> None:
    configure_one_account(app)


def _render(client: FlaskClient, size: str, **opts: object) -> str:
    return render(client, PLUGIN, size, **opts)


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_renders_at_every_size(app: Flask, client: FlaskClient, size: str) -> None:
    _configure(app)
    body = _render(client, size)
    assert f'data-plugin="{PLUGIN}"' in body


def test_shows_active_projects_and_hides_completed(app: Flask, client: FlaskClient) -> None:
    _configure(app)
    body = _render(client, "lg")
    assert "Fix the panel refresh loop" in body
    assert "Ship the deploy script" not in body


def test_progress_is_detected_and_normalised(app: Flask, client: FlaskClient) -> None:
    """The fake Progress column holds fractions (0.75), which must reach the
    client as a 0-1 float with has_progress set, so the bar renders."""
    _configure(app)
    body = _render(client, "lg")
    assert '"has_progress": true' in body
    assert '"progress": 0.75' in body


def test_missing_token_renders_a_setup_message_not_a_crash(
    app: Flask, client: FlaskClient
) -> None:
    body = _render(client, "md")
    assert f'data-plugin="{PLUGIN}"' in body
    assert "Notion" in body


def test_account_option_selects_the_right_workspace(
    app: Flask, client: FlaskClient
) -> None:
    configure_two_accounts(app)
    data = cell_data(_render(client, "lg", account=ACCOUNT_B, data_source=DS_ID_B))
    assert data["account"] == "Personal"
