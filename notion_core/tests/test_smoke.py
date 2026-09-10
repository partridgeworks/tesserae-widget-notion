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
from tests.fake_notion import DS_ID, SCHEMA, TOKEN_A, fake_urlopen  # noqa: E402
from tests.helpers import (  # noqa: E402
    ACCOUNT_A,
    ACCOUNT_B,
    configure_legacy_account,
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


def test_legacy_single_token_is_surfaced_as_one_account(app: Flask) -> None:
    """A pre-multi-account install must keep working with nothing rewritten."""
    core = _core(app)
    configure_legacy_account(app)
    with app.app_context():
        accounts = core.accounts()
        assert len(accounts) == 1
        assert accounts[0]["name"] == "Notion"
        assert core.token_for(accounts[0]["id"]) == TOKEN_A
        assert core.default_account_id() == core.LEGACY_ACCOUNT_ID


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


def test_resolve_account_prefers_the_cells_choice(app: Flask) -> None:
    core = _core(app)
    configure_two_accounts(app)
    with app.app_context():
        assert core.resolve_account({"account": ACCOUNT_B})[0] == ACCOUNT_B


def test_resolve_account_ignores_an_id_that_no_longer_exists(app: Flask) -> None:
    core = _core(app)
    configure_one_account(app)
    with app.app_context():
        account_id, err = core.resolve_account({"account": "gone"})
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
    assert detected["due"] == "Due"
    assert detected["priority"] == "Priority"
    assert detected["project"] == "Project"
    assert detected["done"] == "Done"
    assert detected["assignee"] == "Owner"
    assert detected["progress"] == "Progress"


def test_detect_survives_a_database_with_only_a_title(app: Flask) -> None:
    core = _core(app)
    with app.app_context():
        detected = core.detect({"Name": {"type": "title"}})
    assert detected["title"] == "Name"
    assert detected["status"] == ""
    assert detected["due"] == ""
    assert detected["progress"] == ""


def test_detect_falls_back_to_type_when_no_name_hint_matches(app: Flask) -> None:
    core = _core(app)
    with app.app_context():
        detected = core.detect({"Name": {"type": "title"}, "Whenever": {"type": "date"}})
    assert detected["due"] == "Whenever"


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
        resolved = core.resolve_props(schema, {"due_prop": "Review on"})
    assert resolved["due"] == "Review on"


def test_override_naming_a_missing_column_is_ignored(app: Flask) -> None:
    core = _core(app)
    with app.app_context():
        resolved = core.resolve_props(SCHEMA, {"due_prop": "Nonexistent"})
    assert resolved["due"] == "Due"


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
