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
from fake_notion import DS_ID, DS_ID_B, fake_urlopen  # noqa: E402
from helpers import (  # noqa: E402
    ACCOUNT_A,
    cell_data,
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
        render(client, PLUGIN, "lg", data_source=DS_ID_B)
    )
    assert data["items"][0]["project"] == "Migration epic"


# ----- grouping ----------------------------------------------------------


def _write_stale_schema(app: Flask, account_id: str, ds_id: str, props: dict) -> None:
    """Plant a schema cache missing a column the live database has.

    This is the real failure it reproduces: a relation column added in Notion
    after the cache was written stayed invisible for the cache's lifetime, so
    both the operator's explicit override and auto-detection worked off a
    column list that no longer matched the database.
    """
    import json as _json

    core_dir = Path(app.config["PLUGIN_REGISTRY"].get("notion_core").data_dir)
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / f"schema_{account_id}_{ds_id}.json").write_text(_json.dumps(props))



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


def test_grouping_is_skipped_when_the_database_has_no_project_column(
    app: Flask, client: FlaskClient
) -> None:
    """Group by Project against a database with nothing to group by must
    render the flat list, not an empty grouping or an error."""
    configure_one_account(app)
    _write_stale_schema(app, ACCOUNT_A, DS_ID, {"Name": {"type": "title"}})
    data = cell_data(render(client, PLUGIN, "lg", group_by="project"))
    assert not data.get("error"), data.get("error")
    assert "groups" not in data
    assert data["items"]


# ----- accounts ----------------------------------------------------------


def test_the_database_selects_the_workspace(app: Flask, client: FlaskClient) -> None:
    """No account is passed at all: picking a database is the whole act of
    choosing an account, because a data source id belongs to one workspace."""
    configure_two_accounts(app)
    data = cell_data(render(client, PLUGIN, "lg", data_source=DS_ID_B))
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


def test_neither_widget_declares_an_account_option(app: Flask) -> None:
    """Regression guard. The picker looked like it filtered the Database list
    and could not (the host gives choices() only the option key), and once the
    database decided the account it did nothing at all. It must not come back."""
    registry = app.config["PLUGIN_REGISTRY"]
    for plugin_id in ("notion_tasks", "notion_list", "notion_cards"):
        options = registry.get(plugin_id).manifest.get("cell_options", [])
        assert "account" not in {o["name"] for o in options}, plugin_id


# ----- stale schema cache ------------------------------------------------


def test_override_of_a_column_missing_from_a_stale_cache_self_heals(
    app: Flask, client: FlaskClient
) -> None:
    """The operator types a real column name; the cache predates it. The
    widget must re-read the schema and honour the override, not silently
    fall back to whatever auto-detection finds in the stale copy."""
    configure_one_account(app)
    _write_stale_schema(
        app, ACCOUNT_A, DS_ID, {"Name": {"type": "title"}, "Priority": {"type": "select"}}
    )
    data = cell_data(
        render(client, PLUGIN, "lg", group_by="project", project_prop="Project")
    )
    assert not data.get("error"), data.get("error")
    assert data["detected"]["project"] == "Project"
    # Grouped by the real project column, not by Priority.
    assert {g["name"] for g in data["groups"]} >= {"Tesserae", "Life admin"}


def test_an_override_naming_no_real_column_says_so(
    app: Flask, client: FlaskClient
) -> None:
    """A genuine typo must be reported, listing the columns that do exist --
    silently reading a different column is how this went unnoticed."""
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg", project_prop="Epsiodes"))
    assert "no column called 'Epsiodes'" in data["error"]
    assert "'Project'" in data["error"]


def test_group_headings_can_be_switched_off(app: Flask, client: FlaskClient) -> None:
    """Headings off must not switch grouping off: the server still buckets by
    project, so the rows stay in group order. Only the heading rows go."""
    configure_one_account(app)
    data = cell_data(
        render(client, PLUGIN, "lg", group_by="project", show_group_header=False)
    )
    assert not data.get("error"), data.get("error")
    assert [g["name"] for g in data["groups"]][0] == "Tesserae"


# ----- filtering ---------------------------------------------------------


def test_no_filter_by_default(app: Flask, client: FlaskClient) -> None:
    """The filter is opt-in: a cell that never set one is unchanged."""
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg"))
    titles = {i["title"] for i in data["items"]}
    assert "Renew the domain" in titles          # assigned to someone else
    assert data["filtered_by"] == ""


def test_filter_me_keeps_only_the_token_owners_rows(
    app: Flask, client: FlaskClient
) -> None:
    """'me' has to resolve to the human who owns the token. Notion accepts the
    literal string "me" but reads it as the BOT, which is nobody's assignee,
    so the obvious spelling would silently return nothing."""
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg", filter_person="me"))
    titles = {i["title"] for i in data["items"]}
    assert "Fix the panel refresh loop" in titles
    assert "Write the Notion widget README" in titles
    assert "Renew the domain" not in titles      # someone else's
    assert "Unfiled odd job" not in titles       # unassigned
    assert data["filtered_by"] == "me"


def test_filter_by_the_owners_own_name_works_too(
    app: Flask, client: FlaskClient
) -> None:
    """Typing the owner's name is the same as 'me', and resolves to an id, so
    it filters server-side rather than by string comparison."""
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg", filter_person="Carl Partridge"))
    assert {i["title"] for i in data["items"]} == {
        "Fix the panel refresh loop",
        "Write the Notion widget README",
    }
    assert data["filter_incomplete"] is False


def test_filter_by_another_persons_name_matches_locally(
    app: Flask, client: FlaskClient
) -> None:
    """Nobody else's name can be resolved to an id (listing users is forbidden
    to personal access tokens), so it falls back to matching the rendered
    names — which still has to work."""
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg", filter_person="Someone Else"))
    assert {i["title"] for i in data["items"]} == {"Renew the domain"}


def test_filter_columns_are_ored(app: Flask, client: FlaskClient) -> None:
    """Two columns named, a row kept if EITHER matches. Task name carries the
    text, Owner carries the person, and each row matches only one of them."""
    configure_one_account(app)
    data = cell_data(
        render(client, PLUGIN, "lg", filter_person="Renew",
               filter_columns="Name, Owner")
    )
    assert {i["title"] for i in data["items"]} == {"Renew the domain"}


def test_filter_naming_a_missing_column_says_so(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = cell_data(
        render(client, PLUGIN, "lg", filter_person="me", filter_columns="Nope")
    )
    assert "no column called 'Nope'" in data["error"]


def test_filter_on_an_unfilterable_column_type_says_so(
    app: Flask, client: FlaskClient
) -> None:
    """A date column can't be text-matched. Saying so beats matching nothing,
    which reads exactly like a broken widget."""
    configure_one_account(app)
    data = cell_data(
        render(client, PLUGIN, "lg", filter_person="me", filter_columns="Due")
    )
    assert "is a date column" in data["error"]
    assert "Filterable columns here" in data["error"]


def test_filter_without_a_people_column_explains_itself(
    app: Flask, client: FlaskClient
) -> None:
    configure_one_account(app)
    _write_stale_schema(app, ACCOUNT_A, DS_ID, {"Name": {"type": "title"}})
    data = cell_data(render(client, PLUGIN, "lg", filter_person="me"))
    assert "no people column" in data["error"]


def test_filter_survives_completed_tasks_being_included(
    app: Flask, client: FlaskClient
) -> None:
    configure_one_account(app)
    data = cell_data(
        render(client, PLUGIN, "lg", filter_person="me", show_completed=True)
    )
    assert "Ship the deploy script" in {i["title"] for i in data["items"]}
    assert "Renew the domain" not in {i["title"] for i in data["items"]}


def test_unresolvable_me_errors_rather_than_matching_the_word(
    app: Flask, client: FlaskClient
) -> None:
    """If "me" can't be resolved to a real user it must NOT fall through to
    the local matcher, which substring-matches the rendered names — "me"
    appears in Mel, James and Carmen, so it would quietly return other
    people's work while looking like it had filtered correctly."""
    configure_one_account(app)
    core_dir = Path(app.config["PLUGIN_REGISTRY"].get("notion_core").data_dir)
    core_dir.mkdir(parents=True, exist_ok=True)
    # An owner lookup that came back empty, cached.
    (core_dir / f"owner_{ACCOUNT_A}.json").write_text("{}")

    data = cell_data(render(client, PLUGIN, "lg", filter_person="me"))
    assert "Couldn't work out who 'me' is" in data["error"]


def test_empty_result_names_the_filter(app: Flask, client: FlaskClient) -> None:
    """"Nothing open" after a filter reads as a broken widget. The payload has
    to carry what was filtered on so the cell can say which it was."""
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg", filter_person="Nobody At All"))
    assert data["empty"] is True
    assert data["filtered_by"] == "Nobody At All"


# ----- sorting -----------------------------------------------------------


def test_sort_by_a_column_replaces_the_urgency_order(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg", sort_prop="Name", sort_dir="desc"))
    assert [i["title"] for i in data["items"]] == [
        "Write the Notion widget README",
        "Unfiled odd job",
        "Renew the domain",
        "Fix the panel refresh loop",
    ]
    assert data["sorted_by"] == ["Name"]


def test_sort_by_a_column_keeps_groups_in_that_order(app: Flask, client: FlaskClient) -> None:
    """With an explicit sort the groups follow it too, rather than being
    re-ranked by urgency: the operator asked for that order, so the first
    task's project comes first. "No project" still goes last."""
    configure_one_account(app)
    data = cell_data(
        render(client, PLUGIN, "lg", group_by="project", sort_prop="Name", sort_dir="desc")
    )
    assert [g["name"] for g in data["groups"]] == ["Tesserae", "Life admin", "No project"]


def test_sort_by_a_missing_column_says_so(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = cell_data(render(client, PLUGIN, "lg", sort_prop="Nope"))
    assert "no column called 'Nope'" in data["error"]
