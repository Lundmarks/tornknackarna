# CLAUDE.md

Notes for Claude sessions working on this repo. Keep this file current: it is
the handoff if a session stops mid-task.

## What this is

Scouting and match tooling for Tornknäckarna (Dota 2 Captain's Mode on
E-Sparven, team ID 67). Two Docker Compose services on the owner's home server:

- `scout` runs `bot.py`: full scouting runs at 06:00/18:00 UTC (OpenDota +
  GitHub Gists for the GitHub Pages dashboard `index.html`), plus a light sync
  every 30 minutes (new matches, reschedules, Discord events, ready checks,
  scouting cards).
- `ready` runs `ready_bot.py`: always-on Discord bot (button ready check,
  alerts, Scout button and thread, admin slash commands).

They share the bind-mounted, gitignored `data/` folder: `ready_check.db`,
`state.json` and `player_map.json`. See `readme.md` for user-facing docs.

## Module map

- `bot.py` entry point: `run()` (full), `sync()` (light), scheduler, CLI flags
- `config.py` settings from env; `required()` for secrets
- `seasons.py` current season from E-Sparven; cups excluded from stats
- `discord_sync.py` events, ready check queueing, reminders, scouting cards,
  and `update_presence_snapshot()` for the status line
- `presence.py` bot status text (pure `status_text()`); snapshot in the
  `bot_status` table, written by scout, read by ready every 15 s
- `scouting.py` gist pipeline (`Scouting` context dataclass)
- `players.py` Steam ID resolution and the one `build_players()`
- `analysis.py` pure stats from jsonMatchData (draft, heroes, history)
- `player_map.py` `PlayerMap`, keyed by applicationUserID (migrates names)
- `jsonfile.py` atomic JSON writes, `data_path()` for files in `data/`
- `esparven.py` API client and page parsers; `opendota.py`, `gist.py`
- `webhook.py` channel posts via bot REST (returns bool: sent or not)
- `discord_events.py` scheduled events
- `ready_bot.py`, `ready_check.py` button ready check service and rules
- `scout_cards.py`, `scout_ui.py` scouting card data and Discord views
- `index.html` dashboard (GitHub Pages, from `main`)

## Tests

`pip install -r requirements-dev.txt`, then `python -m pytest`. No network:
E-Sparven pages are saved in `tests/fixtures/` (trimmed to `<main>`), and
`tests/conftest.py` points `DATA_DIR` at a temp folder. When E-Sparven changes
its pages, save a fresh copy there and update the parser and the tests.

## Conventions

- User-facing text (Discord, readme) in plain English, no em dashes, no emojis
  in prose (Discord embed titles already use a few; keep that style).
- E-Sparven API: header `X-ESPARVEN-API-KEY`. `inGameAccount` is always empty,
  meeting rosters have `tier: None` (use `api/team/{id}` for ranks).
- Steam IDs come from the public match page `/match/{id}`, joined on
  `applicationUserID` (the uuid in `/spelare/{uuid}` links). See `esparven.py`.
- `matchDate` strings from E-Sparven are Europe/Stockholm wall time without an
  offset.
- Never commit `.env`, `state.json`, `player_map.json` or `data/`.
- Work on the branch the session is told to use; PR into `main`. GitHub Pages
  serves `index.html` from `main`.

## Status of the code audit (Nov 2026)

Audit items 1, 2, 3, 4, 6, 7, 8, 10, 11, 12 and 13 are done on PR #2
(branch `claude/upbeat-cannon-3i5gvk`):

1 night-time match-day ping, 2 dashboard XSS, 3 `.dockerignore`, 4 season
from the API, 6 `load_dotenv()` order, 7 atomic state writes, 8 event
completion retries, 10 player map keyed by ID, 11 `bot.py` split, 12 tests,
13 `httpx` only.

Notes on what's done:
- State files moved to `data/` (`jsonfile.data_path`) because atomic rename
  fails on single-file bind mounts. Old files are copied over once; compose
  still mounts them read-only for that migration.
- Match-day reminder: `discord_sync.match_day_reminder_due()`, from 09:00
  Stockholm time.
- Season: `EXTRA_SEASONS`/`CURRENT_SEASON_ID` constants replaced by
  `seasons.py`; `EXCLUDED_SEASONS` env (default 48,65 cups) keeps stats the same.
- Dashboard XSS: `fetchGist()` runs `sanitizeData()` on every gist, so all
  strings arrive HTML-escaped. Use `unescL()` for textContent and `escL()` on
  values read back from the DOM (dataset, textContent). Gist and meeting IDs
  used in inline onclick handlers are validated (hex / digits), because
  entities are decoded inside attributes before the JS runs.
- Ticker lines only come from matches listed on the dashboard, so the old
  season's lines disappear without clearing state by hand.
- Notifications only clear their "sent" flags when Discord accepted the post.

Bot status line (Nov 2026, after the audit): countdown, ready count only
while fewer than 5 are in, "Live" for 3 h from kick-off (no score: E-Sparven
results come in afterwards), next season off-season. Not wanted: a result
line after matches, and reading members' presence (no Presence intent).

Still open, not requested yet: 5 (dashboard password gate is cosmetic), 9 (legacy
reaction ready check fallback), 14-20 (stale comments, dead code, old
E-Sparven URLs, User-Agent, naming, container user/WAL, minified index.html).
