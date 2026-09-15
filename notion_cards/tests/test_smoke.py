"""notion_cards: any column, drawn by type, filtered and sorted, against a fake Notion."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fake_notion import DS_ID_B  # noqa: E402
from helpers import cell_data, configure_one_account, configure_two_accounts, render  # noqa: E402

PLUGIN = "notion_cards"
REPO = Path(__file__).resolve().parents[2]


def _cards(client: FlaskClient, **opts: object) -> dict:
    return cell_data(render(client, PLUGIN, "lg", **opts))


def _card(data: dict, title: str) -> dict:
    return next(c for c in data["cards"] if c["title"] == title)


def _field(card: dict, name: str) -> dict:
    return next(f for f in card["fields"] if f["name"] == name)


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_renders_at_every_size(app: Flask, client: FlaskClient, size: str) -> None:
    configure_one_account(app)
    assert f'data-plugin="{PLUGIN}"' in render(client, PLUGIN, size)


def test_missing_token_renders_a_setup_message_not_a_crash(
    app: Flask, client: FlaskClient
) -> None:
    body = render(client, PLUGIN, "md")
    assert f'data-plugin="{PLUGIN}"' in body
    assert "Notion" in body


# ----- what a card shows -------------------------------------------------


def test_with_no_properties_chosen_each_card_shows_the_title(
    app: Flask, client: FlaskClient
) -> None:
    """A fresh cell has nothing configured; blank cards would look broken."""
    configure_one_account(app)
    data = _cards(client)
    assert not data.get("error"), data.get("error")
    assert data["total"] == 5                        # nothing hidden: no done filter here
    card = _card(data, "Renew the domain")
    assert [f["name"] for f in card["fields"]] == ["Name"]
    assert card["fields"][0]["kind"] == "text"
    assert card["fields"][0]["value"] == "Renew the domain"


def test_each_property_is_drawn_by_its_notion_type(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(
        client,
        prop1="Name", prop2="Status", prop3="Due", prop4="Progress", prop5="Done",
    )
    card = _card(data, "Fix the panel refresh loop")
    kinds = {f["name"]: f["kind"] for f in card["fields"]}
    assert kinds == {
        "Name": "text", "Status": "badge", "Due": "date", "Progress": "number", "Done": "checkbox",
    }
    assert _field(card, "Status")["value"] == ["In progress"]
    assert _field(card, "Due")["value"] == {"start": "2020-01-02", "end": ""}
    # The schema's number format travels with the value, so the client can
    # show 75% rather than 0.75.
    assert _field(card, "Progress")["value"] == {"value": 0.75, "format": "percent"}
    assert _field(card, "Done")["value"] is False
    assert _field(_card(data, "Ship the deploy script"), "Done")["value"] is True


def test_multi_select_people_text_url_and_timestamps(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(
        client, prop1="Project", prop2="Owner", prop3="Notes", prop4="Link", prop5="Created",
    )
    card = _card(data, "Fix the panel refresh loop")
    assert _field(card, "Project") == {
        "name": "Project", "type": "multi_select", "kind": "badge",
        "value": ["Tesserae", "Home lab"], "size": "m", "lines": 1, "show_name": False,
    }
    assert _field(card, "Owner")["kind"] == "text"
    assert _field(card, "Owner")["value"] == "Carl Partridge"
    assert _field(card, "Notes")["value"] == "Flickers every 3rd push"
    assert _field(card, "Link")["value"] == "https://example.test/p1"
    assert _field(card, "Created")["kind"] == "date"
    assert _field(card, "Created")["value"]["start"].startswith("2026-09-01T")


def test_a_relation_property_shows_the_related_pages_title(
    app: Flask, client: FlaskClient
) -> None:
    configure_two_accounts(app)
    data = _cards(client, data_source=DS_ID_B, prop1="Epic")
    assert data["cards"][0]["fields"][0]["value"] == "Migration epic"


def test_a_rollup_of_several_values_is_drawn_as_lines(
    app: Flask, client: FlaskClient
) -> None:
    """A rollup over a multi-select is a list of lists; it used to crash
    the text flattener ("unhashable type: 'list'"). Now every value is a
    line of its own, flattened and de-duplicated."""
    configure_two_accounts(app)
    data = _cards(client, data_source=DS_ID_B, prop1="Task", prop2="Topics")
    assert "error" not in data
    field = _field(data["cards"][0], "Topics")
    assert field["type"] == "rollup"
    assert field["kind"] == "lines"
    assert field["value"] == ["Hardware", "E-ink", "Firmware"]


def test_a_rollup_that_reaches_a_relation_shows_titles_not_ids(
    app: Flask, client: FlaskClient
) -> None:
    """Episodes → Bookings → Guest, where Guest is itself a relation: the
    rollup gathers page ids. Each is resolved to its title; one that can't
    be (a page the token can't see) is dropped rather than printed."""
    configure_two_accounts(app)
    data = _cards(client, data_source=DS_ID_B, prop1="Task", prop2="Guests")
    field = _field(data["cards"][0], "Guests")
    assert field["kind"] == "lines"
    assert field["value"] == ["Ada Lovelace", "Grace Hopper"]
    # Grouping on it files the card under the first resolved name.
    grouped = _cards(client, data_source=DS_ID_B, prop1="Task", group_prop="Guests")
    assert [g["name"] for g in grouped["groups"]] == ["Ada Lovelace"]


def test_a_rollup_can_be_grouped_and_sorted_on(app: Flask, client: FlaskClient) -> None:
    configure_two_accounts(app)
    data = _cards(client, data_source=DS_ID_B, prop1="Task", group_prop="Topics",
                  sort_prop="Topics")
    assert "error" not in data
    assert [g["name"] for g in data["groups"]] == ["Hardware"]


def test_size_and_show_name_travel_with_each_field(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(
        client,
        prop1="Name", prop1_size="xl", prop1_show_name=False,
        prop2="Status", prop2_size="xs", prop2_show_name=True,
        prop3="Due", prop3_size="huge",                      # nonsense → default
    )
    card = data["cards"][0]
    assert [(f["name"], f["size"], f["show_name"]) for f in card["fields"]] == [
        ("Name", "xl", False), ("Status", "xs", True), ("Due", "m", False),
    ]


def test_max_lines_defaults_to_one_and_is_clamped(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(
        client, prop1="Name", prop2="Notes", prop2_lines=3, prop3="Link", prop3_lines="99",
    )
    assert [f["lines"] for f in data["cards"][0]["fields"]] == [1, 3, 8]


def test_space_between_properties_reaches_the_cell_clamped(
    app: Flask, client: FlaskClient
) -> None:
    configure_one_account(app)
    assert _cards(client, prop1="Name")["field_gap"] == 0
    assert _cards(client, prop1="Name", field_gap="1.25")["field_gap"] == 1.25
    assert _cards(client, prop1="Name", field_gap=9)["field_gap"] == 5
    assert _cards(client, prop1="Name", field_gap=-2)["field_gap"] == 0
    assert _cards(client, prop1="Name", field_gap="wide")["field_gap"] == 0


def test_blank_property_slots_are_skipped_in_order(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, prop1="", prop2="Status", prop3="", prop4="Name")
    assert [f["name"] for f in data["cards"][0]["fields"]] == ["Status", "Name"]


def test_a_property_that_does_not_exist_says_so(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, prop1="Name", prop2="Stauts")
    assert "no column called 'Stauts'" in data["error"]
    assert "'Status'" in data["error"]


# ----- layout ------------------------------------------------------------


def test_limit_and_columns_reach_the_cell(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, limit=2, columns=3)
    assert len(data["cards"]) == 2
    assert data["shown"] == 2
    assert data["total"] == 5
    assert data["columns"] == 3


def test_limit_and_columns_are_clamped(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, limit=0, columns=99)
    assert len(data["cards"]) == 1
    assert data["columns"] == 6


# ----- filtering ---------------------------------------------------------


def test_filter_is_on_a_select_column(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, filter_prop="Status", filter_op="equals", filter_value="to do")
    assert {c["title"] for c in data["cards"]} == {
        "Write the Notion widget README", "Renew the domain", "Unfiled odd job",
    }
    assert data["condition"] == "Status is to do"


def test_filter_is_not_hides_completed_rows(app: Flask, client: FlaskClient) -> None:
    """The cards widget has no "include completed" switch: this is how you
    get the same effect, and it works on any column."""
    configure_one_account(app)
    data = _cards(client, filter_prop="Status", filter_op="not_equals", filter_value="Done")
    assert "Ship the deploy script" not in {c["title"] for c in data["cards"]}
    assert data["total"] == 4


def test_filter_contains_on_a_multi_select(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, filter_prop="Project", filter_op="contains", filter_value="tesserae")
    assert {c["title"] for c in data["cards"]} == {
        "Fix the panel refresh loop", "Write the Notion widget README", "Ship the deploy script",
    }


def test_filter_is_one_of_a_list_of_values(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(
        client, filter_prop="Status", filter_op="one_of", filter_value="In Progress, done",
    )
    assert {c["title"] for c in data["cards"]} == {
        "Fix the panel refresh loop", "Ship the deploy script",
    }
    assert data["condition"] == "Status is one of In Progress, done"
    rest = _cards(
        client, filter_prop="Status", filter_op="not_one_of", filter_value="In Progress, done",
    )
    assert rest["total"] == 3


def test_filter_is_empty_and_is_not_empty(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    empty = _cards(client, filter_prop="Project", filter_op="is_empty")
    assert {c["title"] for c in empty["cards"]} == {"Unfiled odd job"}
    assert empty["condition"] == "Project is empty"
    full = _cards(client, filter_prop="Due", filter_op="is_not_empty")
    assert "Renew the domain" not in {c["title"] for c in full["cards"]}
    assert full["total"] == 4


def test_filter_compares_numbers_and_dates(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    ahead = _cards(client, filter_prop="Progress", filter_op="gt", filter_value="0.5")
    assert {c["title"] for c in ahead["cards"]} == {
        "Fix the panel refresh loop", "Ship the deploy script",
    }
    past = _cards(client, filter_prop="Due", filter_op="lt", filter_value="today")
    assert {c["title"] for c in past["cards"]} == {
        "Fix the panel refresh loop", "Ship the deploy script",
    }


def test_filter_on_a_checkbox(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, filter_prop="Done", filter_op="equals", filter_value="yes")
    assert {c["title"] for c in data["cards"]} == {"Ship the deploy script"}


def test_filter_naming_a_missing_column_says_so(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, filter_prop="Stage", filter_op="equals", filter_value="x")
    assert "no column called 'Stage'" in data["error"]


def test_filter_that_matches_nothing_names_itself(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, filter_prop="Status", filter_op="equals", filter_value="Blocked")
    assert data["empty"] is True
    assert data["condition"] == "Status is Blocked"


def test_three_filters_must_all_hold(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(
        client,
        filter_prop="Status", filter_op="not_equals", filter_value="Done",
        filter2_prop="Project", filter2_op="contains", filter2_value="Tesserae",
        filter3_prop="Progress", filter3_op="gte", filter3_value="0.5",
    )
    assert [c["title"] for c in data["cards"]] == ["Fix the panel refresh loop"]
    assert data["condition"] == (
        "Status is not Done and Project contains Tesserae "
        "and Progress is at least / on or after 0.5"
    )


def test_a_blank_middle_filter_does_not_disable_the_third(
    app: Flask, client: FlaskClient
) -> None:
    configure_one_account(app)
    data = _cards(client, filter3_prop="Done", filter3_op="equals", filter3_value="yes")
    assert {c["title"] for c in data["cards"]} == {"Ship the deploy script"}


def test_a_second_filter_naming_a_missing_column_says_so(
    app: Flask, client: FlaskClient
) -> None:
    configure_one_account(app)
    data = _cards(client, filter2_prop="Sttaus", filter2_op="equals", filter2_value="x")
    assert "no column called 'Sttaus'" in data["error"]


def test_person_filter_works_here_too(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, filter_person="me")
    assert {c["title"] for c in data["cards"]} == {
        "Fix the panel refresh loop", "Write the Notion widget README", "Ship the deploy script",
    }
    assert data["filtered_by"] == "me"


def test_person_and_condition_filters_combine(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(
        client, filter_person="me",
        filter_prop="Status", filter_op="not_equals", filter_value="Done",
    )
    assert {c["title"] for c in data["cards"]} == {
        "Fix the panel refresh loop", "Write the Notion widget README",
    }


# ----- sorting -----------------------------------------------------------


def test_sort_by_any_column_either_way(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    desc = _cards(client, sort_prop="Progress", sort_dir="desc")
    assert [c["title"] for c in desc["cards"]][:2] == [
        "Ship the deploy script", "Fix the panel refresh loop",
    ]
    asc = _cards(client, sort_prop="Name")
    assert [c["title"] for c in asc["cards"]][0] == "Fix the panel refresh loop"
    assert asc["sorted_by"] == ["Name"]


def test_second_sort_breaks_ties_in_the_first(app: Flask, client: FlaskClient) -> None:
    """Status sorts in Notion's group order (To-do, In progress, Complete),
    not alphabetically; the second sort settles the ties within a status."""
    configure_one_account(app)
    data = _cards(client, sort_prop="Status", sort2_prop="Name", sort2_dir="desc")
    assert [c["title"] for c in data["cards"]] == [
        "Write the Notion widget README",     # To do, Z→A
        "Unfiled odd job",
        "Renew the domain",
        "Fix the panel refresh loop",         # In progress
        "Ship the deploy script",             # Done
    ]
    assert data["sorted_by"] == ["Status", "Name"]


def test_a_second_sort_naming_a_missing_column_says_so(
    app: Flask, client: FlaskClient
) -> None:
    configure_one_account(app)
    data = _cards(client, sort_prop="Status", sort2_prop="Nmae")
    assert "no column called 'Nmae'" in data["error"]


def test_sort_by_a_missing_column_says_so(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, sort_prop="Nope")
    assert "no column called 'Nope'" in data["error"]


def test_changing_a_property_is_not_served_from_cache(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    first = _cards(client, prop1="Name")
    second = _cards(client, prop1="Status")
    assert first["cards"][0]["fields"][0]["name"] == "Name"
    assert second["cards"][0]["fields"][0]["name"] == "Status"


# ----- grouping ----------------------------------------------------------


def test_group_by_a_status_column(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, group_prop="Status", sort_prop="Name")
    assert data["group_by"] == "Status"
    groups = {g["name"]: [c["title"] for c in g["cards"]] for g in data["groups"]}
    assert groups == {
        "In progress": ["Fix the panel refresh loop"],
        "To do": ["Renew the domain", "Unfiled odd job", "Write the Notion widget README"],
        "Done": ["Ship the deploy script"],
    }
    # A status's groups come in Notion's arrangement (To-do, In progress,
    # Complete), not in the order the sort happens to surface them.
    assert [g["name"] for g in data["groups"]] == ["To do", "In progress", "Done"]


def test_sorting_by_the_grouped_column_itself_orders_the_groups(
    app: Flask, client: FlaskClient
) -> None:
    """Sorted by the status column, descending, the groups follow the
    cards: that is the one case where the sort outranks the arrangement."""
    configure_one_account(app)
    data = _cards(client, group_prop="Status", sort_prop="Status", sort_dir="desc")
    assert [g["name"] for g in data["groups"]] == ["Done", "In progress", "To do"]


def test_grouping_files_a_multi_valued_column_under_its_first_value_and_empties_last(
    app: Flask, client: FlaskClient
) -> None:
    configure_one_account(app)
    data = _cards(client, group_prop="Project", sort_prop="Name")
    names = [g["name"] for g in data["groups"]]
    assert names == ["Tesserae", "Life admin", "No Project"]
    tesserae = next(g for g in data["groups"] if g["name"] == "Tesserae")
    # "Fix the panel refresh loop" is tagged Tesserae AND Home lab: first wins.
    assert "Fix the panel refresh loop" in {c["title"] for c in tesserae["cards"]}
    assert {c["title"] for c in data["groups"][-1]["cards"]} == {"Unfiled odd job"}


def test_grouping_respects_the_limit(app: Flask, client: FlaskClient) -> None:
    """Groups are built from the cards that will be drawn, not the whole
    set, so a heading never advertises cards that don't appear."""
    configure_one_account(app)
    data = _cards(client, group_prop="Status", sort_prop="Name", limit=2)
    assert sum(len(g["cards"]) for g in data["groups"]) == 2
    assert data["total"] == 5


def test_group_by_a_relation_uses_the_related_title(app: Flask, client: FlaskClient) -> None:
    configure_two_accounts(app)
    data = _cards(client, data_source=DS_ID_B, group_prop="Epic")
    assert [g["name"] for g in data["groups"]] == ["Migration epic"]


def test_no_grouping_by_default(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client)
    assert "groups" not in data
    assert data["group_by"] == ""


def test_group_by_a_missing_column_says_so(app: Flask, client: FlaskClient) -> None:
    configure_one_account(app)
    data = _cards(client, group_prop="Stauts")
    assert "no column called 'Stauts'" in data["error"]


# ----- manifest ----------------------------------------------------------


def test_manifest_declares_five_property_slots_with_size_and_name_switches() -> None:
    manifest = json.loads((REPO / "notion_cards" / "plugin.json").read_text())
    names = [o["name"] for o in manifest["cell_options"]]
    for i in range(1, 6):
        slot = [names.index(f"prop{i}{suffix}") for suffix in ("", "_size", "_lines", "_show_name")]
        assert slot == sorted(slot)
    sizes = next(o for o in manifest["cell_options"] if o["name"] == "prop1_size")
    assert [c["value"] for c in sizes["choices"]] == ["xs", "s", "m", "l", "xl"]
    assert "prop6" not in names
    gap = next(o for o in manifest["cell_options"] if o["name"] == "field_gap")
    assert (gap["type"], gap["default"], gap["min"], gap["max"]) == ("slider", 0, 0, 5)
    for required in ("columns", "limit", "field_gap", "group_prop",
                     "filter_prop", "filter_op", "filter_value",
                     "filter2_prop", "filter2_op", "filter2_value",
                     "filter3_prop", "filter3_op", "filter3_value",
                     "sort_prop", "sort_dir", "sort2_prop", "sort2_dir",
                     "filter_person", "filter_columns"):
        assert required in names, required


def test_display_kind_judges_formulas_by_their_value(app: Flask) -> None:
    mod = app.config["PLUGIN_REGISTRY"].get(PLUGIN).server_module
    assert mod.display_kind("checkbox") == "checkbox"
    assert mod.display_kind("status") == "badge"
    assert mod.display_kind("multi_select") == "badge"
    assert mod.display_kind("date") == "date"
    assert mod.display_kind("last_edited_time") == "date"
    assert mod.display_kind("number") == "number"
    assert mod.display_kind("title") == "text"
    assert mod.display_kind("people") == "text"
    assert mod.display_kind("formula", True) == "checkbox"
    assert mod.display_kind("formula", 3.5) == "number"
    assert mod.display_kind("rollup", {"start": "2026-01-01"}) == "date"
    assert mod.display_kind("rollup", ["a", ["b", "c"]]) == "lines"
    assert mod.display_kind("rollup", 7) == "number"
    assert mod.display_kind("formula", "text") == "text"
