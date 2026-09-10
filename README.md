# Notion widgets for Tesserae

Open tasks and active projects from [Notion](https://www.notion.so), rendered
onto an e-ink panel. For [Tesserae](https://github.com/dmellok/tesserae), the
self-hosted e-ink dashboard companion.

A bundle of three plugin folders:

| Folder | Kind | What it does |
|---|---|---|
| `notion_core` | data | Holds the integration token, discovers your databases, detects which columns mean what. No cell of its own; has an admin page. |
| `notion_tasks` | widget | Open tasks, overdue first, with due date, status and project. |
| `notion_projects` | widget | Active projects with status, target date and a progress bar. |

## It adapts to your database, you don't adapt to it

Notion databases are all shaped differently, so these widgets work out the
schema instead of demanding one. Column **type** is the primary signal and the
column **name** only breaks ties, which means a title column called "Task",
a status column called "Stage" and a date column called "Whenever" are all
found correctly.

Detected roles: title, status, due date, priority, project, done checkbox,
assignee, progress. Anything missing is simply not shown — a database with no
due-date column renders as a plain list rather than an error. If a guess is
wrong, every role has a per-cell override (*Status column*, *Due date column*,
…); type the Notion property name exactly. **Settings → Widgets → Notion Core**
shows what was detected for each database.

## Setup

1. Create an internal integration at
   [notion.so/my-integrations](https://www.notion.so/my-integrations) and copy
   its token (starts with `ntn_` or `secret_`). Read access is all these
   widgets use.
2. Paste it into **Settings → Widgets → Notion Core**.
3. **Share each database with the integration** — this is the step everyone
   misses. Open the database in Notion, then **••• → Connections** and add
   your integration. A database that isn't shared does not appear and produces
   no error; the API simply cannot see it.
4. Add a *Notion, Tasks* or *Notion, Projects* cell and pick the database from
   the dropdown.

Visit `/plugins/notion_core/` on your Tesserae for a connection check, the list
of databases the integration can see, and the detected column mapping for each.

## API version

Built against `Notion-Version: 2025-09-03`, where a database contains one or
more *data sources* and queries address the data source
(`POST /v1/data_sources/{id}/query`). The older `/v1/databases/{id}/query` path
is deprecated and breaks as soon as a database gains a second source, so this
bundle only speaks the new shape. The header is overridable in Notion Core's
settings if Notion ever tells you otherwise.

## What it fetches

Both widgets query up to 500 rows (5 pages of 100) per refresh, sorted
server-side by due date where the database has one, then filter and sort
locally. Completion state is filtered in Python rather than through a Notion
filter, because "done" can live in a `status`, a `select` or a `checkbox` and
building the right filter for each is far more fragile than dropping rows
afterwards.

Results are cached in the plugin's data directory for the cell's Refresh
interval (5 / 15 / 60 min), and the database list and schema for an hour each.
Relation-valued project columns cost one extra request per distinct related
page, capped at 12.

## Networking and settings

```jsonc
"requires": ["network:api.notion.com", "settings:plugin/notion_core"]
```

`api.notion.com` is the only host any of these three contact, and Tesserae
enforces that at the socket layer. The token is stored with `secret: true`, so
it lands encrypted as `api_token_secret` and is never echoed back to the edit
form. Nothing is written outside each plugin's own `data_dir`.

## Development

```sh
# Render both widgets at every size against a fake Notion, no token needed
python tools/shoot.py            # -> screenshots/<plugin>-<size>.png
python tools/shoot.py --theme dark

# Smoke tests
python -m pytest -q
ruff check .
```

Both need a Tesserae checkout for the app itself. The sibling
`../../tesserae-upstream-fork` is assumed; set `TESSERAE_SRC` otherwise. Tests
skip rather than fail when no checkout is found.

Deploy to the LAN server with
`~/Source/Repos/tesserae/scripts/deploy-widget-wyse1.sh` — see
[docs/developing-widgets.md](../../docs/developing-widgets.md).

## Status

Pre-1.0 and unpublished. Not in the
[community catalog](https://github.com/dmellok/tesserae-widgets) yet — no
Notion widget exists there, so it is a candidate.

**Verified:** renders correctly at xs/sm/md/lg in light and dark themes,
against a fake Notion API. **Not yet verified against a real Notion
workspace** — the 2025-09-03 data-source endpoints are built from the
published reference, not from a live call.

## License

AGPL-3.0-or-later, matching Tesserae. See [LICENSE](LICENSE).
