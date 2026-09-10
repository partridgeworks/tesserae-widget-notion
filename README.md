# Notion widgets for Tesserae

Open tasks and active projects from [Notion](https://www.notion.so), rendered
onto an e-ink panel. For [Tesserae](https://github.com/dmellok/tesserae), the
self-hosted e-ink dashboard companion.

A bundle of three plugin folders:

| Folder | Kind | What it does |
|---|---|---|
| `notion_core` | data | One or more Notion accounts (name + token), database discovery, column detection. No cell of its own — it *is* the admin page. |
| `notion_tasks` | widget | Open tasks, overdue first, with due date, status and project, optionally grouped under project headings. |
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
…); type the Notion property name exactly. The admin page's *Inspect columns*
shows what was detected.

**The project column can be multi-valued.** It may be a `relation` pointing at
several pages, a `multi_select` with several tags ticked, or a plain `select`.
A row and a group heading each need one label, so the first entry wins, in
Notion's own order — the order you see in Notion. Relation ids are resolved to
the related page's title (capped at 24 lookups per render, since each costs a
request); anything unresolved shows no project rather than a raw UUID.

## Grouping tasks by project

*Notion, Tasks* has a **Group by** option, defaulting to **No grouping**. Set
it to **Project** and the list gains a heading row per project, with that
group's overdue count on the right.

- Group order follows the most urgent task in each group, so the project
  needing attention stays at the top of the cell.
- Tasks with no project collect under **No project**, forced last.
- The project name drops out of each row's meta — the heading directly above
  already says it.
- Headings cost vertical room, so grouped mode uses a smaller row budget and
  drops the "+ N more" line (the title bar's "4 OPEN" states the same total).
  A group whose heading would be the last thing to fit is skipped entirely: a
  heading with nothing under it is worse than no heading.
- Grouping is ignored at `xs`, where a heading plus one task is the whole cell.

Grouping applies to the tasks that survive the *Max tasks shown* limit, not to
the whole database — the cell shows what it shows, so grouping anything else
would advertise groups whose tasks never appear.

## Setup

Everything is configured in one place: the **Notion Core admin page**, at
**Widgets → Notion Core → admin page** (`/plugins/notion_core/`). There is
nothing to set under Settings.

1. Create an internal integration at
   [notion.so/my-integrations](https://www.notion.so/my-integrations) and copy
   its token (starts with `ntn_` or `secret_`). Read access is all these
   widgets use.
2. On the admin page, give the account a **friendly name** and paste the
   **token**, then Save.
3. **Share each database with the integration** — this is the step everyone
   misses. Open the database in Notion, then **••• → Connections** and add
   your integration. A database that isn't shared does not appear and produces
   no error; the API simply cannot see it.
4. Add a *Notion, Tasks* or *Notion, Projects* cell and pick the database from
   the dropdown.

The admin page doubles as the diagnostic: it lists the databases each account
can see, and *Inspect columns* shows the detected mapping for one.

### Several Notion accounts

Use **Add another account** for a second workspace, or a second integration on
the same one. Each account keeps its own friendly name and token, and each
widget cell has a **Notion account** option choosing which one it reads.

With a single account there is nothing to choose: the picker shows that one
account marked *(only account)* and it is used whether or not the cell stores
it. Tesserae's cell editor has no mechanism for hiding an option conditionally
— there is no `visible_if` in the plugin schema — so the field is still
rendered; it simply has no decision to make.

When several accounts are configured, the **Database** dropdown lists every
account's databases prefixed with the account name (`Work · Roadmap`). It has
to: the host hands `choices()` only the option key, never the cell's other
values, so the list cannot be filtered to the account the cell picked. If the
two ever disagree, the database wins, because it names exactly one account.

Removing an account erases its token from disk (both the encrypted and any
legacy plaintext key) and drops its cached discovery. Cells pointing at it
fall back to a remaining account rather than breaking.

### Upgrading from 0.1.x

Nothing to do. A single-token install is read as one account named "Notion" and
keeps working untouched; the old value is only rewritten into the new shape
when you next save the admin form. Placed cells keep rendering throughout.

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
enforces that at the socket layer.

Tokens are written through the settings store's secret convention, so each
lands encrypted under `account_<id>_token_secret` and disk-grepping shows which
values are sensitive. The admin form renders token inputs **empty**: a blank
one means "keep what's stored", so a stored secret is never round-tripped
through the browser, and renaming an account cannot wipe its token. Nothing is
written outside each plugin's own `data_dir`.

On-disk shape under `plugins.notion_core`:

```
accounts_json              '[{"id": "a1b2c3d4", "name": "Work"}]'
account_<id>_token_secret  encrypted per-account token
notion_version             optional Notion-Version override ("" = default)
api_token_secret           0.1.x installs, read-only
```

## Development

```sh
# Render every widget and variant at every size against a fake Notion.
# No token, no network. Includes the grouped task list.
python tools/shoot.py            # -> screenshots/<plugin><variant>-<size>.png
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

**Verified against a fake Notion API:** renders at xs/sm/md/lg in light and
dark, grouped and ungrouped; multi-valued project columns (`multi_select` and
`relation`) resolve to one label; two accounts keep separate tokens; a 0.1.x
single-token install still renders; removing an account erases its token.
58 tests.

**Verified against a real workspace:** database discovery, column detection and
task/project rendering (0.1.0). The multi-account paths and grouping are so far
only exercised against the fake API.

## License

MIT. See [LICENSE](LICENSE).

Tesserae itself is AGPL-3.0-or-later, and MIT is compatible with it: combine
this widget into a Tesserae deployment and the combined work is AGPL, while
these files stay reusable under MIT. The catalog
[accepts permissive licences](https://docs.tesserae.ink/dev/publishing-a-widget/)
on exactly that basis. Nothing here derives from Tesserae's source — the
plugin code imports only the standard library and Flask.
