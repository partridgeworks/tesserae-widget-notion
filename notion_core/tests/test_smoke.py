"""notion_core unit tests.

The core has no cell of its own, so these exercise what the widgets depend on
and what an upstream change would silently break: account resolution and
storage, property auto-detection, the value normalisation for every Notion
property type the family reads, and the admin page that owns configuration.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fake_notion import DS_ID, DS_ID_B, SCHEMA, TOKEN_A, fake_urlopen  # noqa: E402
from helpers import (  # noqa: E402
    ACCOUNT_A,
    ACCOUNT_B,
    configure_one_account,
    configure_two_accounts,
)


def _core(app: Flask):
    return app.config["PLUGIN_REGISTRY"].get("notion_core").server_module


def _section(app: Flask) -> dict:
    return app.config["SETTINGS_STORE"].get_section("plugins")["notion_core"]


# ----- accounts ----------------------------------------------------------


def test_accounts_round_trip(app: Flask) -> None:
    core = _core(app)
    configure_two_accounts(app)
    with app.app_context():
        names = [a["name"] for a in core.accounts()]
        assert names == ["Work", "Personal"]
        assert core.token_for(ACCOUNT_A) == TOKEN_A
        assert core.is_configured() is True


def test_an_account_without_a_token_is_not_usable(app: Flask) -> None:
    """Half-configured accounts must not be offered to cells, or a widget
    would pick one and fail at render time."""
    core = _core(app)
    app.config["SETTINGS_STORE"].patch_section(
        "plugins", {"notion_core": {"accounts_json": json.dumps([{"id": "x1", "name": "Empty"}])}}
    )
    with app.app_context():
        assert core.accounts()
        assert core.configured_accounts() == []
        assert core.is_configured() is False
        assert "no token yet" in (core.config_error() or "")


def test_the_database_decides_the_account(app: Flask) -> None:
    """A data source id belongs to exactly one workspace, so its owner wins;
    there is no per-cell account option to contradict it."""
    core = _core(app)
    configure_two_accounts(app)
    with app.app_context(), patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert core.resolve_account({"data_source": DS_ID_B})[0] == ACCOUNT_B
        assert core.resolve_account({"data_source": DS_ID})[0] == ACCOUNT_A


def test_resolve_account_without_a_database_uses_the_first_account(app: Flask) -> None:
    core = _core(app)
    configure_two_accounts(app)
    with app.app_context():
        account_id, err = core.resolve_account({})
    assert account_id == ACCOUNT_A
    assert err is None


def test_resolve_account_reports_when_nothing_is_configured(app: Flask) -> None:
    core = _core(app)
    with app.app_context():
        account_id, err = core.resolve_account({})
    assert account_id == ""
    assert "No Notion account" in (err or "")


# ----- settings persistence ----------------------------------------------


def test_admin_add_account_persists_name_and_token(app: Flask, client) -> None:
    core = _core(app)
    client.post("/plugins/notion_core/accounts", data={"name": "Work", "token": TOKEN_A})
    with app.app_context():
        accounts = core.accounts()
        assert [a["name"] for a in accounts] == ["Work"]
        assert core.token_for(accounts[0]["id"]) == TOKEN_A


def test_admin_save_renames_without_clearing_the_token(app: Flask, client) -> None:
    """The form renders token inputs empty, so a blank one has to mean "keep
    what's stored" — otherwise renaming an account would silently wipe it."""
    core = _core(app)
    client.post("/plugins/notion_core/accounts", data={"name": "Work", "token": TOKEN_A})
    with app.app_context():
        account_id = core.accounts()[0]["id"]

    client.post(
        "/plugins/notion_core/save",
        data={f"name_{account_id}": "Renamed", f"token_{account_id}": ""},
    )
    with app.app_context():
        assert core.accounts()[0]["name"] == "Renamed"
        assert core.token_for(account_id) == TOKEN_A


def test_admin_remove_account_erases_its_token(app: Flask, client) -> None:
    """Dropping the account from the list is not enough: the store merges, so
    the secret would otherwise sit on disk belonging to nothing."""
    core = _core(app)
    configure_two_accounts(app)
    client.post(f"/plugins/notion_core/accounts/{ACCOUNT_B}/remove")
    with app.app_context():
        assert [a["id"] for a in core.accounts()] == [ACCOUNT_A]
        assert core.token_for(ACCOUNT_B) == ""
        assert core.token_for(ACCOUNT_A) == TOKEN_A


def test_two_accounts_keep_separate_tokens(app: Flask, client) -> None:
    core = _core(app)
    client.post("/plugins/notion_core/accounts", data={"name": "One", "token": "ntn_one"})
    client.post("/plugins/notion_core/accounts", data={"name": "Two", "token": "ntn_two"})
    with app.app_context():
        first, second = core.accounts()
        assert core.token_for(first["id"]) == "ntn_one"
        assert core.token_for(second["id"]) == "ntn_two"


def test_blank_version_override_is_not_pinned(app: Flask, client) -> None:
    """Saving an empty override must leave the default free to move when the
    plugin is upgraded, not freeze today's value into settings.json."""
    core = _core(app)
    configure_one_account(app)
    client.post("/plugins/notion_core/save", data={"notion_version": ""})
    with app.app_context():
        assert core.notion_version_raw() == ""
        assert core.notion_version() == core.DEFAULT_NOTION_VERSION


def test_version_override_is_honoured(app: Flask, client) -> None:
    core = _core(app)
    configure_one_account(app)
    client.post("/plugins/notion_core/save", data={"notion_version": "2022-06-28"})
    with app.app_context():
        assert core.notion_version() == "2022-06-28"


def test_token_is_stored_under_a_secret_suffixed_key(app: Flask, client) -> None:
    """The `_secret` convention is what makes disk-grepping show which values
    are sensitive, and what gets them encrypted."""
    client.post("/plugins/notion_core/accounts", data={"name": "Work", "token": TOKEN_A})
    stored = _section(app)
    assert any(k.startswith("account_") and k.endswith("_token_secret") for k in stored)


# ----- detection ---------------------------------------------------------


def test_detect_maps_every_role_from_the_fake_schema(app: Flask) -> None:
    core = _core(app)
    with app.app_context():
        detected = core.detect(SCHEMA)
    assert detected["title"] == "Name"
    assert detected["status"] == "Status"
    assert detected["date"] == "Due"
    assert detected["priority"] == "Priority"
    assert detected["project"] == "Project"
    assert detected["done"] == "Done"
    assert detected["person"] == "Owner"
    assert detected["progress"] == "Progress"


def test_detect_survives_a_database_with_only_a_title(app: Flask) -> None:
    core = _core(app)
    with app.app_context():
        detected = core.detect({"Name": {"type": "title"}})
    assert detected["title"] == "Name"
    assert detected["status"] == ""
    assert detected["date"] == ""
    assert detected["progress"] == ""


def test_detect_falls_back_to_type_when_no_name_hint_matches(app: Flask) -> None:
    core = _core(app)
    with app.app_context():
        detected = core.detect({"Name": {"type": "title"}, "Whenever": {"type": "date"}})
    assert detected["date"] == "Whenever"


def test_detect_finds_a_project_column_of_any_supported_type(app: Flask) -> None:
    core = _core(app)
    with app.app_context():
        for kind in ("relation", "select", "multi_select"):
            detected = core.detect({"Name": {"type": "title"}, "Project": {"type": kind}})
            assert detected["project"] == "Project", kind


def test_explicit_override_beats_detection(app: Flask) -> None:
    core = _core(app)
    schema = {"Name": {"type": "title"}, "Due": {"type": "date"}, "Review on": {"type": "date"}}
    with app.app_context():
        resolved = core.resolve_props(schema, {"date_prop": "Review on"})
    assert resolved["date"] == "Review on"


def test_override_naming_a_missing_column_is_ignored(app: Flask) -> None:
    core = _core(app)
    with app.app_context():
        resolved = core.resolve_props(SCHEMA, {"date_prop": "Nonexistent"})
    assert resolved["date"] == "Due"


# ----- property reading --------------------------------------------------


def test_is_done_reads_both_status_names_and_a_checkbox(app: Flask) -> None:
    core = _core(app)
    assert core.is_done("Done") is True
    assert core.is_done("Completed") is True
    assert core.is_done("In progress") is False
    assert core.is_done("", True) is True
    assert core.is_done("", False) is False


def test_prop_normalises_each_notion_property_type(app: Flask) -> None:
    core = _core(app)
    page = {
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "Hello "}, {"plain_text": "world"}]},
            "Status": {"type": "status", "status": {"name": "In progress"}},
            "Tags": {"type": "multi_select", "multi_select": [{"name": "a"}, {"name": "b"}]},
            "Due": {"type": "date", "date": {"start": "2026-09-10"}},
            "Done": {"type": "checkbox", "checkbox": True},
            "Count": {"type": "number", "number": 42},
            "Owner": {"type": "people", "people": [{"name": "Carl"}]},
            "Link": {"type": "relation", "relation": [{"id": "abc"}]},
            "Empty": {"type": "select", "select": None},
        }
    }
    with app.app_context():
        assert core.prop(page, "Name") == "Hello world"
        assert core.prop(page, "Status") == "In progress"
        assert core.prop(page, "Tags") == ["a", "b"]
        assert core.prop(page, "Due") == {"start": "2026-09-10", "end": ""}
        assert core.prop(page, "Done") is True
        assert core.prop(page, "Count") == 42
        assert core.prop(page, "Owner") == ["Carl"]
        assert core.prop(page, "Link") == ["abc"]
        assert core.prop(page, "Empty") == ""
        assert core.prop(page, "NotThere") is None


def test_rollup_relation_ids_reads_the_page_ids_a_rollup_gathers(app: Flask) -> None:
    core = _core(app)
    page = {
        "properties": {
            "Guests": {"type": "rollup", "rollup": {"type": "array", "array": [
                {"type": "relation", "relation": [{"id": "p1"}, {"id": "p2"}]},
                {"type": "multi_select", "multi_select": [{"name": "not a page"}]},
                {"type": "relation", "relation": [{"id": "p2"}]},
            ]}},
            "Count": {"type": "rollup", "rollup": {"type": "number", "number": 3}},
            "Epic": {"type": "relation", "relation": [{"id": "p9"}]},
        }
    }
    with app.app_context():
        assert core.rollup_relation_ids(page, "Guests") == ["p1", "p2", "p2"]
        assert core.rollup_relation_ids(page, "Count") == []
        assert core.rollup_relation_ids(page, "Epic") == []
        assert core.rollup_relation_ids(page, "NotThere") == []
        # The rollup value itself still normalises to the ids, nested.
        assert core.prop(page, "Guests") == [["p1", "p2"], ["not a page"], ["p2"]]


def test_page_title_finds_the_title_by_type_not_by_name(app: Flask) -> None:
    """Notion lets you rename the title column, so matching on "Name" is the
    one thing guaranteed to break on someone else's workspace."""
    core = _core(app)
    page = {
        "properties": {
            "Task description": {"type": "title", "title": [{"plain_text": "Renamed"}]}
        }
    }
    with app.app_context():
        assert core.page_title(page) == "Renamed"


def test_project_label_picks_the_first_of_a_multi_valued_column(app: Flask) -> None:
    core = _core(app)
    page = {
        "properties": {
            "Project": {
                "type": "multi_select",
                "multi_select": [{"name": "Alpha"}, {"name": "Beta"}],
            }
        }
    }
    with app.app_context():
        assert core.project_label(page, "Project", "multi_select", {}) == "Alpha"


def test_project_label_resolves_relation_ids_through_the_name_map(app: Flask) -> None:
    core = _core(app)
    page = {"properties": {"Epic": {"type": "relation", "relation": [{"id": "p9"}, {"id": "p8"}]}}}
    with app.app_context():
        assert core.project_label(page, "Epic", "relation", {"p9": "First epic"}) == "First epic"
        # An unresolved id must not leak into the UI as a raw UUID.
        assert core.project_label(page, "Epic", "relation", {}) == ""


def test_project_label_is_empty_for_an_empty_column(app: Flask) -> None:
    core = _core(app)
    page = {"properties": {"Project": {"type": "multi_select", "multi_select": []}}}
    with app.app_context():
        assert core.project_label(page, "Project", "multi_select", {}) == ""
        assert core.project_label(page, "", "multi_select", {}) == ""


# ----- shared plumbing: sorting + conditions -----------------------------


def test_sort_pages_puts_blank_values_last_in_both_directions(app: Flask) -> None:
    core = _core(app)
    rows = [
        {"properties": {"N": {"type": "number", "number": 2}}},
        {"properties": {"N": {"type": "number", "number": None}}},
        {"properties": {"N": {"type": "number", "number": 10}}},
    ]
    asc = [core.prop(p, "N") for p in core.sort_pages(rows, [("N", False)])]
    desc = [core.prop(p, "N") for p in core.sort_pages(rows, [("N", True)])]
    assert asc == [2, 10, None]
    assert desc == [10, 2, None]


def test_sort_value_never_compares_apples_with_oranges(app: Flask) -> None:
    """A formula column can yield a number on one row and text on another;
    Python would raise comparing them, so the key leads with a type rank."""
    core = _core(app)
    rows = [
        {"properties": {"F": {"type": "formula", "formula": {"type": "string", "string": "b"}}}},
        {"properties": {"F": {"type": "formula", "formula": {"type": "number", "number": 3}}}},
        {"properties": {"F": {"type": "formula", "formula": {"type": "boolean", "boolean": True}}}},
        {"properties": {"F": {"type": "date", "date": {"start": "2026-01-01"}}}},
    ]
    ordered = [core.prop(p, "F") for p in core.sort_pages(rows, [("F", False)])]
    assert ordered == [True, 3, {"start": "2026-01-01", "end": ""}, "b"]


def test_sort_by_a_list_column_uses_its_first_entry(app: Flask) -> None:
    core = _core(app)
    rows = [
        {"properties": {"T": {"type": "multi_select", "multi_select": [{"name": "zeta"}]}}},
        {"properties": {"T": {"type": "multi_select", "multi_select": []}}},
        {"properties": {"T": {"type": "multi_select",
                              "multi_select": [{"name": "Alpha"}, {"name": "omega"}]}}},
    ]
    assert [core.prop(p, "T") for p in core.sort_pages(rows, [("T", False)])] == [
        ["Alpha", "omega"], ["zeta"], [],
    ]


def _select_row(name: str) -> dict:
    return {"properties": {"S": {"type": "select", "select": {"name": name} if name else None}}}


def test_sort_by_a_select_column_follows_notions_option_order(app: Flask) -> None:
    """The option arrangement in Notion is the order Notion sorts and groups
    by, so it is the order the widgets sort by: Today before This Week
    before Next Week, not the alphabet's Next, This M, This W."""
    core = _core(app)
    schema = {"S": {"type": "select", "select": {"options": [
        {"id": "1", "name": "Today"}, {"id": "2", "name": "This Week"},
        {"id": "3", "name": "Next Week"}, {"id": "4", "name": "This Month"},
    ]}}}
    rows = [_select_row(n) for n in
            ("This Month", "", "Next Week", "Today", "Someday", "This Week")]
    asc = [core.prop(p, "S") for p in core.sort_pages(rows, [("S", False)], schema)]
    # An option the schema doesn't list (added since the layout was cached)
    # goes after every listed one; blanks stay last either way.
    assert asc == ["Today", "This Week", "Next Week", "This Month", "Someday", ""]
    desc = [core.prop(p, "S") for p in core.sort_pages(rows, [("S", True)], schema)]
    assert desc == ["Someday", "This Month", "Next Week", "This Week", "Today", ""]
    # Without a schema there is nothing better than the alphabet.
    alpha = [core.prop(p, "S") for p in core.sort_pages(rows, [("S", False)])]
    assert alpha == ["Next Week", "Someday", "This Month", "This Week", "Today", ""]


def test_sort_by_a_status_column_walks_its_groups(app: Flask) -> None:
    """Notion orders a status by group (To-do, In progress, Complete), then
    by position inside the group; the options list itself is in no
    particular order. An option in no group comes after the grouped ones."""
    core = _core(app)
    schema = {"S": {"type": "status", "status": {
        "options": [
            {"id": "c", "name": "Shipped"}, {"id": "a", "name": "Backlog"},
            {"id": "b", "name": "Building"}, {"id": "d", "name": "Blocked"},
            {"id": "a2", "name": "Ready"},
        ],
        "groups": [
            {"id": "g1", "name": "To-do", "option_ids": ["a", "a2"]},
            {"id": "g2", "name": "In progress", "option_ids": ["b"]},
            {"id": "g3", "name": "Complete", "option_ids": ["c"]},
        ],
    }}}
    rows = [{"properties": {"S": {"type": "status", "status": {"name": n}}}}
            for n in ("Shipped", "Blocked", "Building", "Ready", "Backlog")]
    ordered = [core.prop(p, "S") for p in core.sort_pages(rows, [("S", False)], schema)]
    assert ordered == ["Backlog", "Ready", "Building", "Shipped", "Blocked"]


def test_option_order_is_only_for_option_columns(app: Flask) -> None:
    core = _core(app)
    schema = {
        "T": {"type": "title"},
        "M": {"type": "multi_select", "multi_select": {"options": [
            {"id": "1", "name": "Zeta"}, {"id": "2", "name": "Alpha"},
        ]}},
        "Bare": {"type": "select"},
    }
    assert core.option_order(schema, "T") is None
    assert core.option_order(None, "M") is None
    assert core.option_order(schema, "M") == {"zeta": 0, "alpha": 1}
    # A select the cached layout lists no options for still sorts, by text.
    assert core.option_order(schema, "Bare") is None
    rows = [
        {"properties": {"M": {"type": "multi_select", "multi_select": [{"name": "Alpha"}]}}},
        {"properties": {"M": {"type": "multi_select",
                              "multi_select": [{"name": "Zeta"}, {"name": "Alpha"}]}}},
    ]
    # A list still sorts by its first entry, now ranked by the arrangement.
    assert [core.prop(p, "M") for p in core.sort_pages(rows, [("M", False)], schema)] == [
        ["Zeta", "Alpha"], ["Alpha"],
    ]


def test_notion_sorts_only_for_types_notion_can_sort(app: Flask) -> None:
    core = _core(app)
    schema = {"Due": {"type": "date"}, "Epic": {"type": "relation"}}
    assert core.notion_sorts(schema, [("Due", True)]) == [
        {"property": "Due", "direction": "descending"}
    ]
    assert core.notion_sorts(schema, [("Epic", False)]) is None
    assert core.notion_sorts(schema, []) is None
    # Only the leading sortable run goes to Notion: a local primary means
    # the secondary can't be sent either.
    assert core.notion_sorts(schema, [("Due", False), ("Epic", False)]) == [
        {"property": "Due", "direction": "ascending"}
    ]
    assert core.notion_sorts(schema, [("Epic", False), ("Due", False)]) is None


def test_sort_settings_reads_direction_loosely(app: Flask) -> None:
    core = _core(app)
    assert core.sort_settings({"sort_prop": " Due ", "sort_dir": "desc"}) == [("Due", True)]
    assert core.sort_settings({"sort_prop": "Due", "sort_dir": "Descending"}) == [("Due", True)]
    assert core.sort_settings({"sort_prop": "Due"}) == [("Due", False)]
    assert core.sort_settings({}) == []
    assert core.sort_settings(
        {"sort_prop": "Status", "sort2_prop": "Name", "sort2_dir": "desc"}
    ) == [("Status", False), ("Name", True)]
    assert core.sort_settings({"sort2_prop": "Name"}) == [("Name", False)]


def test_second_sort_breaks_ties_and_keeps_empties_last(app: Flask) -> None:
    core = _core(app)
    def row(status, n):
        return {"properties": {
            "S": {"type": "select", "select": {"name": status} if status else None},
            "N": {"type": "number", "number": n},
        }}
    rows = [row("b", 1), row("a", 2), row("", 9), row("b", 3), row("a", None)]
    ordered = core.sort_pages(rows, [("S", False), ("N", True)])
    assert [(core.prop(p, "S"), core.prop(p, "N")) for p in ordered] == [
        ("a", 2), ("a", None), ("b", 3), ("b", 1), ("", 9),
    ]


def test_condition_matches_text_case_insensitively(app: Flask) -> None:
    core = _core(app)
    assert core.condition_matches("In Progress", "equals", "in progress")
    assert not core.condition_matches("In Progress", "not_equals", "in progress")
    assert core.condition_matches("Fix the panel", "contains", "PANEL")
    assert core.condition_matches("Fix the panel", "not_contains", "domain")


def test_condition_matches_lists_by_any_entry(app: Flask) -> None:
    """A multi_select "is Tesserae" should hold when Tesserae is one of the
    tags, not only when it is the only one."""
    core = _core(app)
    assert core.condition_matches(["Tesserae", "Home lab"], "equals", "tesserae")
    assert not core.condition_matches(["Tesserae", "Home lab"], "not_equals", "tesserae")
    assert core.condition_matches(["Tesserae", "Home lab"], "contains", "home")


def test_condition_one_of_takes_a_comma_separated_list(app: Flask) -> None:
    core = _core(app)
    wanted = "This Week, next week ,This Quarter"
    assert core.condition_matches("Next Week", "one_of", wanted)
    assert core.condition_matches("this quarter", "one_of", wanted)
    assert not core.condition_matches("Someday", "one_of", wanted)
    assert core.condition_matches("Someday", "not_one_of", wanted)
    assert not core.condition_matches("This Week", "not_one_of", wanted)
    # A multi-select matches on any of its tags; an empty value is in no list.
    assert core.condition_matches(["Backlog", "This Week"], "one_of", wanted)
    assert not core.condition_matches([], "one_of", wanted)
    assert core.condition_matches(None, "not_one_of", wanted)


def test_condition_matches_numbers_numerically(app: Flask) -> None:
    core = _core(app)
    assert core.condition_matches(0.75, "gt", "0.5")
    assert core.condition_matches(10, "gt", "9")          # not "1" < "9" as text
    assert core.condition_matches(0.75, "gte", "0.75")
    assert core.condition_matches(0.4, "lt", "0.5")
    assert core.condition_matches(0.4, "lte", "0.4")
    assert not core.condition_matches(None, "gt", "0")


def test_condition_matches_dates_as_iso_prefixes(app: Flask) -> None:
    core = _core(app)
    due = {"start": "2026-09-12", "end": ""}
    assert core.condition_matches(due, "lt", "2026-10")
    assert core.condition_matches(due, "equals", "2026-09-12")
    assert core.condition_matches(due, "lte", "today", today="2026-09-12")
    assert not core.condition_matches(due, "lt", "today", today="2026-09-12")
    assert core.condition_matches(due, "gte", "2026-09")


def test_condition_matches_checkboxes_by_yes_and_no(app: Flask) -> None:
    core = _core(app)
    assert core.condition_matches(True, "equals", "yes")
    assert core.condition_matches(True, "equals", "checked")
    assert core.condition_matches(False, "equals", "no")
    assert core.condition_matches(False, "is_empty", "")
    assert core.condition_matches(True, "is_not_empty", "")


def test_condition_is_empty_understands_every_shape(app: Flask) -> None:
    core = _core(app)
    for empty in (None, "", [], {"start": "", "end": ""}):
        assert core.condition_matches(empty, "is_empty", ""), empty
        assert not core.condition_matches(empty, "is_not_empty", ""), empty
    assert core.condition_matches("x", "is_not_empty", "")
    assert core.condition_matches(0, "is_not_empty", "")


def test_condition_settings_reads_three_slots_and_skips_blanks(app: Flask) -> None:
    core = _core(app)
    assert core.condition_settings({
        "filter_prop": "Status", "filter_op": "not_equals", "filter_value": "Done",
        "filter2_prop": "", "filter2_op": "contains", "filter2_value": "ignored",
        "filter3_prop": "Due", "filter3_op": "bogus", "filter3_value": "today",
    }) == [("Status", "not_equals", "Done"), ("Due", "equals", "today")]
    assert core.condition_settings({}) == []


def test_read_int_clamps_and_defaults(app: Flask) -> None:
    core = _core(app)
    assert core.read_int({"n": "7"}, "n", 3) == 7
    assert core.read_int({"n": "seven"}, "n", 3) == 3
    assert core.read_int({}, "n", 3) == 3
    assert core.read_int({"n": 0}, "n", 3) == 1
    assert core.read_int({"n": 99}, "n", 3, hi=48) == 48


def test_schema_with_columns_rereads_a_stale_cache_before_erroring(app: Flask) -> None:
    core = _core(app)
    configure_one_account(app)
    with app.app_context():
        core.data_dir().mkdir(parents=True, exist_ok=True)
        (core.data_dir() / f"schema_{ACCOUNT_A}_{DS_ID}.json").write_text(
            json.dumps({"Name": {"type": "title"}})
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            props, err = core.schema_with_columns(ACCOUNT_A, DS_ID, ["Status"])
            assert err is None and "Status" in props
            props, err = core.schema_with_columns(ACCOUNT_A, DS_ID, ["Nope"])
    assert props is None
    assert "no column called 'Nope'" in err


# ----- choices + admin page ----------------------------------------------


def test_choices_lists_shared_databases(app: Flask) -> None:
    core = _core(app)
    configure_one_account(app)
    with app.app_context(), patch("urllib.request.urlopen", side_effect=fake_urlopen):
        options = core.choices("data_sources")
    assert options == [{"value": DS_ID, "label": "Work Tracker"}]


def test_data_source_labels_name_the_account_when_there_are_several(app: Flask) -> None:
    """`choices()` can't see which account the cell picked, so the label is
    the only thing keeping a multi-account list legible."""
    core = _core(app)
    configure_two_accounts(app)
    with app.app_context(), patch("urllib.request.urlopen", side_effect=fake_urlopen):
        labels = [o["label"] for o in core.choices("data_sources")]
    assert "Work · Work Tracker" in labels
    assert "Personal · Personal Tracker" in labels


def test_choices_without_a_token_explains_itself(app: Flask) -> None:
    """An empty dropdown is impossible to diagnose from the editor, so the
    unconfigured case returns a row that says what to do."""
    core = _core(app)
    with app.app_context():
        options = core.choices("data_sources")
    assert len(options) == 1
    assert "No Notion account" in options[0]["label"]


def test_admin_page_renders_a_pane_per_account(app: Flask, client) -> None:
    configure_two_accounts(app)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        resp = client.get("/plugins/notion_core/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Work" in body
    assert "Personal" in body
    assert "Work Tracker" in body
    assert "Add another account" in body


def test_admin_page_never_echoes_a_stored_token(app: Flask, client) -> None:
    """Tokens go in but never come back out: the form renders empty inputs so
    a secret is not round-tripped through the browser on every page view."""
    configure_one_account(app)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        body = client.get("/plugins/notion_core/").get_data(as_text=True)
    assert TOKEN_A not in body


def test_admin_page_without_any_account_explains_setup(app: Flask, client) -> None:
    resp = client.get("/plugins/notion_core/")
    assert resp.status_code == 200
    assert "my-integrations" in resp.get_data(as_text=True)


def test_there_is_no_accounts_dropdown_any_more(app: Flask) -> None:
    """The per-cell account picker was removed: the selected database names
    exactly one workspace, so a second control could only ever disagree with
    it. An unknown key must return nothing rather than resurrect one."""
    core = _core(app)
    configure_two_accounts(app)
    with app.app_context():
        assert core.choices("accounts") == []
