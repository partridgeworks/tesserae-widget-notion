"""Render both widgets at every cell size and save PNGs.

The smoke tests prove the server half: the right data reaches the cell. They
say nothing about the half that matters on a panel, because `client.js` runs
in a browser. This boots a real Tesserae with the bundle staged, serves it,
and screenshots `/_test/render` at xs/sm/md/lg through the same headless
Chromium the production renderer uses.

    python tools/shoot.py [--out screenshots] [--theme spectra] [--style standard]

Notion is faked (tests/fake_notion.py), so this needs no token and no
network. Set TESSERAE_SRC if your Tesserae checkout isn't the sibling
../../tesserae-upstream-fork.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from wsgiref.simple_server import WSGIRequestHandler, make_server

REPO = Path(__file__).resolve().parent.parent
PLUGIN_FOLDERS = ("notion_core", "notion_tasks", "notion_projects")
SIZES = ("xs", "sm", "md", "lg")
# Must match app/composer.py's SIZE_DIMENSIONS so the screenshot viewport is
# the cell, not a scaled approximation of it.
DIMENSIONS = {"xs": (180, 180), "sm": (380, 240), "md": (640, 400), "lg": (1200, 800)}

TESSERAE_SRC = Path(
    os.environ.get("TESSERAE_SRC", REPO.parent.parent / "tesserae-upstream-fork")
).expanduser()
sys.path.insert(0, str(TESSERAE_SRC))
sys.path.insert(0, str(REPO))

from tests.fake_notion import DS_ID, fake_urlopen  # noqa: E402


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args: object) -> None:  # noqa: A002
        pass


def build_app(data_root: Path):
    from app.app_factory import create_app

    authored = data_root / "authored"
    authored.mkdir(parents=True, exist_ok=True)
    for folder in PLUGIN_FOLDERS:
        shutil.copytree(
            REPO / folder,
            authored / folder,
            ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc"),
        )
    app = create_app(testing=True, data_root=data_root, plugins_dir=TESSERAE_SRC / "plugins")
    app.config["SETTINGS_STORE"].patch_section(
        "plugins", {"notion_core": {"api_token": "ntn_fake-token"}}
    )
    return app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="screenshots")
    ap.add_argument("--theme", default="spectra")
    ap.add_argument("--style", default="standard")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()

    # --out may be absolute (a scratch dir) or relative to the repo.
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        app = build_app(Path(tmp))
        server = make_server("127.0.0.1", args.port, app, handler_class=_QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{args.port}"

        from playwright.sync_api import sync_playwright

        # The fake Notion has to be active in THIS process, because the
        # widgets' fetch() runs inside the server we just started here.
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), sync_playwright() as p:
            browser = p.chromium.launch()
            for plugin in ("notion_tasks", "notion_projects"):
                for size in SIZES:
                    w, h = DIMENSIONS[size]
                    opts = quote(json.dumps({"data_source": DS_ID}))
                    url = (
                        f"{base}/_test/render?plugin={plugin}&size={size}"
                        f"&theme={args.theme}&style={args.style}&opts={opts}"
                    )
                    page = browser.new_page(viewport={"width": w, "height": h})
                    page.on("console", lambda m: (
                        print(f"    console[{m.type}] {m.text}")
                        if m.type in ("error", "warning") else None))
                    page.on("pageerror", lambda e: print(f"    pageerror: {e}"))
                    page.goto(url, wait_until="networkidle")
                    # Widget modules mount asynchronously; wait for the cell to
                    # actually paint rather than racing the screenshot.
                    # composer.js attaches the shadow root to `.cell-content`
                    # (falling back to `.cell`), not to the [data-plugin] node.
                    page.wait_for_function(
                        "() => {"
                        " const cell = document.querySelector('.cell');"
                        " if (!cell) return false;"
                        " const host = cell.querySelector('.cell-content') || cell;"
                        " return !!host.shadowRoot && host.shadowRoot.childElementCount > 0;"
                        "}",
                        timeout=15000,
                    )
                    dest = out_dir / f"{plugin}-{size}.png"
                    page.screenshot(path=str(dest))
                    label = dest.relative_to(REPO) if dest.is_relative_to(REPO) else dest
                    print(f"  {label}  ({w}x{h})")
                    page.close()
            browser.close()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
