"""notion_core unit tests.

The core has no cell of its own, so these exercise the two things the
widgets depend on and that a schema change upstream would silently break:
property auto-detection, and the value normalisation for every Notion
property type the family reads.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.fake_notion import DS_ID, SCHEMA, fake_urlopen  # noqa: E402


def _core(app: Flask):
    return app.config["PLUGIN_REGISTRY"].get("notion_core").server_module


def _configure(app: Flask) -> None:
    app.config["SETTINGS_STORE"].patch_section(
        "plugins", {"notion_core": {"api_token": "ntn_test-token"}}
    )


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
    """Every role but the title should come back empty rather than guessing,
    so a minimal database renders as a plain list instead of erroring."""
    core = _core(app)
    with app.app_context():
        detected = core.detect({"Name": {"type": "title"}})
    assert detected["title"] == "Name"
    assert detected["status"] == ""
    assert detected["due"] == ""
    assert detected["progress"] == ""


def test_detect_falls_back_to_type_when_no_name_hint_matches(app: Flask) -> None:
    """A date column called something unguessable is still the due date:
    type is the primary filter, the name hints only break ties."""
    core = _core(app)
    with app.app_context():
        detected = core.detect(
            {"Name": {"type": "title"}, "Whenever": {"type": "date"}}
        )
    assert detected["due"] == "Whenever"


def test_explicit_override_beats_detection(app: Flask) -> None:
    core = _core(app)
    schema = {
        "Name": {"type": "title"},
        "Due": {"type": "date"},
        "Review on": {"type": "date"},
    }
    with app.app_context():
        resolved = core.resolve_props(schema, {"due_prop": "Review on"})
    assert resolved["due"] == "Review on"


def test_override_naming_a_missing_column_is_ignored(app: Flask) -> None:
    """A typo should fall back to the detected column, not blank the field."""
    core = _core(app)
    with app.app_context():
        resolved = core.resolve_props(SCHEMA, {"due_prop": "Nonexistent"})
    assert resolved["due"] == "Due"


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
            "Task description": {"type": "title", "title": [{"plain_text": "Renamed"}]},
        }
    }
    with app.app_context():
        assert core.page_title(page) == "Renamed"


def test_choices_lists_shared_databases(app: Flask) -> None:
    core = _core(app)
    _configure(app)
    with app.app_context(), patch("urllib.request.urlopen", side_effect=fake_urlopen):
        options = core.choices("data_sources")
    assert options == [{"value": DS_ID, "label": "Work Tracker"}]


def test_choices_without_a_token_explains_itself(app: Flask) -> None:
    """An empty dropdown is impossible to diagnose from the editor, so the
    unconfigured case returns a row that says what to do."""
    core = _core(app)
    with app.app_context():
        options = core.choices("data_sources")
    assert len(options) == 1
    assert "Notion Core" in options[0]["label"]


def test_admin_page_renders(app: Flask, client) -> None:
    _configure(app)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        resp = client.get("/plugins/notion_core/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Work Tracker" in body
