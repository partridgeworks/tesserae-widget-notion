"""notion_list smoke tests: renders at every size, against a fake Notion."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fake_notion import DS_ID_B  # noqa: E402
from helpers import (  # noqa: E402
    cell_data,
    configure_one_account,
    configure_two_accounts,
    render,
)

PLUGIN = "notion_list"


def _configure(app: Flask) -> None:
    configure_one_account(app)


def _render(client: FlaskClient, size: str, **opts: object) -> str:
    return render(client, PLUGIN, size, **opts)


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_renders_at_every_size(app: Flask, client: FlaskClient, size: str) -> None:
    _configure(app)
    body = _render(client, size)
    assert f'data-plugin="{PLUGIN}"' in body


def test_shows_open_rows_and_hides_completed(app: Flask, client: FlaskClient) -> None:
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


def test_the_database_selects_the_workspace(app: Flask, client: FlaskClient) -> None:
    configure_two_accounts(app)
    data = cell_data(_render(client, "lg", data_source=DS_ID_B))
    assert data["account"] == "Personal"


# ----- filtering ---------------------------------------------------------


def test_filter_me_keeps_only_the_owners_rows(
    app: Flask, client: FlaskClient
) -> None:
    _configure(app)
    data = cell_data(_render(client, "lg", filter_person="me"))
    titles = {i["title"] for i in data["items"]}
    assert "Fix the panel refresh loop" in titles
    assert "Renew the domain" not in titles
    assert data["filtered_by"] == "me"


def test_filter_ors_assignee_and_collaborators(app: Flask, client: FlaskClient) -> None:
    """The shape this was built for: mine if I am the assignee OR a
    collaborator. "Unfiled odd job" is only reachable through the second
    column, so it proves the OR rather than the first clause alone."""
    _configure(app)
    one = cell_data(_render(client, "lg", filter_person="me", filter_columns="Owner"))
    both = cell_data(
        _render(client, "lg", filter_person="me", filter_columns="Owner, Collaborators")
    )
    assert "Unfiled odd job" not in {i["title"] for i in one["items"]}
    assert "Unfiled odd job" in {i["title"] for i in both["items"]}
    assert "Renew the domain" not in {i["title"] for i in both["items"]}


# ----- sorting -----------------------------------------------------------


def test_default_order_is_overdue_first_then_by_date(app: Flask, client: FlaskClient) -> None:
    _configure(app)
    data = cell_data(_render(client, "lg"))
    assert [i["title"] for i in data["items"]][:2] == [
        "Fix the panel refresh loop",          # overdue
        "Write the Notion widget README",      # soonest dated
    ]
    assert data["sorted_by"] == []


def test_sort_by_a_column_replaces_the_default_order(app: Flask, client: FlaskClient) -> None:
    _configure(app)
    asc = cell_data(_render(client, "lg", sort_prop="Name"))
    assert [i["title"] for i in asc["items"]] == [
        "Fix the panel refresh loop",
        "Renew the domain",
        "Unfiled odd job",
        "Write the Notion widget README",
    ]
    assert asc["sorted_by"] == ["Name"]
    desc = cell_data(_render(client, "lg", sort_prop="Name", sort_dir="desc"))
    assert [i["title"] for i in desc["items"]] == list(reversed([i["title"] for i in asc["items"]]))


def test_sort_by_a_number_column_descending(app: Flask, client: FlaskClient) -> None:
    _configure(app)
    data = cell_data(_render(client, "lg", sort_prop="Progress", sort_dir="desc"))
    assert [i["progress"] for i in data["items"]] == [0.75, 0.4, 0.1, 0.0]


def test_sort_by_a_missing_column_says_so(app: Flask, client: FlaskClient) -> None:
    _configure(app)
    data = cell_data(_render(client, "lg", sort_prop="Priorty"))
    assert "no column called 'Priorty'" in data["error"]
    assert "'Priority'" in data["error"]


def test_changing_the_sort_direction_is_not_served_from_cache(
    app: Flask, client: FlaskClient
) -> None:
    """Direction is not a ``*_prop`` option, so it has to be fingerprinted
    on its own or flipping it would serve the previous order for a whole
    refresh interval."""
    _configure(app)
    first = cell_data(_render(client, "lg", sort_prop="Name", sort_dir="asc"))
    second = cell_data(_render(client, "lg", sort_prop="Name", sort_dir="desc"))
    assert first["items"][0]["title"] != second["items"][0]["title"]
