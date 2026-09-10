"""notion_tasks: renders at every size, groups by project, honours accounts."""

from __future__ import annotations

import sys
from html import unescape
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.fake_notion import DS_ID_B, fake_urlopen  # noqa: E402
from tests.helpers import (  # noqa: E402
    ACCOUNT_A,
    ACCOUNT_B,
    cell_data,
    configure_legacy_account,
    configure_one_account,
    configure_two_accounts,
    render,
)

PLUGIN = "notion_tasks"


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_renders_at_every_size(app: Flask, client: FlaskClient, size: str) -> None:
    configure_one_account(app)
    assert f'data-plugin="{PLUGIN}"' in render(client, PLUGIN, size)


def test_shows_task_titles_and_hides_completed(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    body = render(client, PLUGIN, "lg")
    assert "Fix the panel refresh loop" in body
    assert "Renew the domain" in body
    assert "Ship the deploy script" not in body


def test_show_completed_option_includes_done_tasks(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    assert "Ship the deploy script" in render(client, PLUGIN, "lg", show_completed=True)


def test_missing_token_renders_a_setup_message_not_a_crash(
    app: Flask, client: FlaskClient
) -> None:
    body = render(client, PLUGIN, "md")
    assert f'data-plugin="{PLUGIN}"' in body
    assert "Notion" in body


def test_missing_database_option_asks_for_one(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        resp = client.get(f"/_test/render?plugin={PLUGIN}&size=md")
    assert resp.status_code == 200
    assert "Pick a Notion database" in unescape(resp.get_data(as_text=True))


# ----- multi-valued project column ---------------------------------------


def test_multi_select_project_takes_the_first_value(app: Flask, client: FlaskClient) -> None:
    """The fake task carries TWO projects. The widget must pick one, not
    stringify the list into "['Tesserae', 'Home lab']"."""
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg"))
    row = next(i for i in data["items"] if i["title"] == "Fix the panel refresh loop")
    assert row["project"] == "Tesserae"
    assert "[" not in row["project"]


def test_relation_project_resolves_to_the_related_page_title(
    app: Flask, client: FlaskClient
) -> None:
    """Workspace B's project column is a relation, so the id has to be
    resolved to the related page's title."""
    configure_two_accounts(app)
    data = cell_data(
        render(client, PLUGIN, "lg", account=ACCOUNT_B, data_source=DS_ID_B)
    )
    assert data["items"][0]["project"] == "Migration epic"


# ----- grouping ----------------------------------------------------------


def test_grouping_is_off_by_default(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    assert "groups" not in cell_data(render(client, PLUGIN, "lg"))


def test_group_by_project_buckets_and_names_the_groups(
    app: Flask, client: FlaskClient
) -> None:
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg", group_by="project"))
    names = [g["name"] for g in data["groups"]]
    assert "Tesserae" in names
    assert "Life admin" in names
    # An unfiled task gets its own bucket, forced last.
    assert names[-1] == "No project"
    tesserae = next(g for g in data["groups"] if g["name"] == "Tesserae")
    assert {i["title"] for i in tesserae["items"]} == {
        "Fix the panel refresh loop",
        "Write the Notion widget README",
    }
    assert tesserae["overdue_count"] == 1


def test_group_order_puts_the_most_urgent_project_first(
    app: Flask, client: FlaskClient
) -> None:
    """Group order follows the most urgent task in each group, so the project
    needing attention stays at the top of the cell. "Tesserae" owns the only
    overdue task, so it leads."""
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg", group_by="project"))
    assert data["groups"][0]["name"] == "Tesserae"

    # The header markup itself lives in client.js and only exists once a
    # browser has run it, so it is verified by tools/shoot.py rather than
    # here: this response carries the data, not the rendered shadow DOM.


def test_grouping_falls_back_when_there_is_no_project_column(
    app: Flask, client: FlaskClient
) -> None:
    """Pointing the project override at a column that doesn't exist must not
    invent an empty grouping; the flat list still renders."""
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg", group_by="project", project_prop="Nope"))
    # The override is ignored, so detection still finds the real column.
    assert data["groups"]


# ----- accounts ----------------------------------------------------------


def test_account_option_selects_the_right_workspace(
    app: Flask, client: FlaskClient
) -> None:
    configure_two_accounts(app)
    data = cell_data(render(client, PLUGIN, "lg", account=ACCOUNT_B, data_source=DS_ID_B))
    assert data["account"] == "Personal"
    assert data["items"][0]["title"] == "Second workspace task"


def test_single_account_is_used_without_the_cell_naming_it(
    app: Flask, client: FlaskClient
) -> None:
    """The picker is pointless with one account, so a cell that stores no
    account at all must still render."""
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg"))
    assert data["account"] == "Work"
    assert not data.get("error")


def test_stale_account_id_falls_back_to_the_database_owner(
    app: Flask, client: FlaskClient
) -> None:
    """A cell pointing at a deleted account should degrade to the account
    that owns its database rather than break."""
    configure_two_accounts(app)
    data = cell_data(
        render(client, PLUGIN, "lg", account="deleted-account", data_source=DS_ID_B)
    )
    assert data["account"] == "Personal"


def test_legacy_single_token_install_still_renders(app: Flask, client: FlaskClient) -> None:
    """Upgrading from the single-token version must not break placed cells."""
    configure_legacy_account(app)
    data = cell_data(render(client, PLUGIN, "lg"))
    assert data["account"] == "Notion"
    assert not data.get("error")
    assert data["items"]


def test_account_choices_hint_when_there_is_only_one(app: Flask) -> None:
    configure_one_account(app)
    core = app.config["PLUGIN_REGISTRY"].get("notion_core").server_module
    with app.app_context():
        options = core.choices("accounts")
    assert len(options) == 1
    assert "only account" in options[0]["label"]
    assert options[0]["value"] == ACCOUNT_A


def test_mismatched_account_and_database_still_renders(
    app: Flask, client: FlaskClient
) -> None:
    """The two pickers are resolved independently by the host, so a cell can
    hold an account from one workspace and a database from another. A Notion
    data source id belongs to exactly one workspace, so the database is the
    unambiguous signal and must win -- otherwise the query 404s and the cell
    blames the user for not sharing a database they already shared."""
    configure_two_accounts(app)
    data = cell_data(
        render(client, PLUGIN, "lg", account=ACCOUNT_A, data_source=DS_ID_B)
    )
    assert not data.get("error"), data.get("error")
    assert data["account"] == "Personal"
    assert data["items"][0]["title"] == "Second workspace task"
