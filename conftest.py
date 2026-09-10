"""Test fixtures for the Notion widget bundle.

This repo is standalone: the widgets live here, not inside a Tesserae
checkout. To render them we need a real Tesserae app, so the tests borrow
one from a local checkout and stage this repo's plugin folders into the
app's ``authored`` directory — the same directory a dev push would use, and
one that is derived from ``data_root``, so each test gets its own copy and
nothing is written into the Tesserae checkout itself.

Point ``TESSERAE_SRC`` at your checkout if it isn't the sibling
``../../tesserae-upstream-fork``. Tests skip (rather than fail) when no
checkout is found, so the repo stays clonable on its own.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent
PLUGIN_FOLDERS = ("notion_core", "notion_tasks", "notion_projects")

_DEFAULT_SRC = REPO.parent.parent / "tesserae-upstream-fork"
TESSERAE_SRC = Path(os.environ.get("TESSERAE_SRC", _DEFAULT_SRC)).expanduser()

_HAVE_SRC = (TESSERAE_SRC / "app" / "app_factory.py").is_file()

if _HAVE_SRC and str(TESSERAE_SRC) not in sys.path:
    sys.path.insert(0, str(TESSERAE_SRC))


@pytest.fixture
def app(tmp_path: Path):
    if not _HAVE_SRC:
        pytest.skip(f"No Tesserae checkout at {TESSERAE_SRC}; set TESSERAE_SRC.")

    from app.app_factory import create_app

    # Stage this repo's widgets where the loader looks for pushed widgets.
    authored = tmp_path / "authored"
    authored.mkdir(parents=True, exist_ok=True)
    for folder in PLUGIN_FOLDERS:
        shutil.copytree(
            REPO / folder,
            authored / folder,
            ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc"),
        )

    return create_app(
        testing=True,
        data_root=tmp_path,
        plugins_dir=TESSERAE_SRC / "plugins",
    )


@pytest.fixture
def client(app):
    return app.test_client()
