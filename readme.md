# Tornknäckarna · Scouting Dashboard

<p align="center">
  <img src="favicon.svg" width="80" height="80" alt="Radar">
</p>

A scouting and match intelligence system for Tornknäckarna, competing in Dota 2 Captain's Mode on [E-Sparven](https://esparven.se). A Python bot runs daily, analyses opponent and team data, and writes to GitHub Gists. A static GitHub Pages frontend reads from those Gists and presents scouting reports, draft analysis, and match history.

Live at: **[lundmarks.github.io/tornknackarna](https://lundmarks.github.io/tornknackarna/)**

---

## How it works

Each day the bot:

1. Fetches upcoming and past meetings from E-Sparven
2. Updates Discord first (ready checks, events, match-day reminders), so a scouting failure can't block them
3. Parses tournament picks, bans, and player performance from E-Sparven match data
4. Fetches pub stats, rank, and hero pool data from OpenDota
5. Computes draft tendencies — most picked, most banned, and draft order patterns per team
6. Generates ticker snippets summarising key intel for each opponent
7. Writes one secret GitHub Gist per opponent, a self-scouting Gist for Tornknäckarna, and an index Gist the frontend reads on load

Past meetings are frozen after first write and skipped on subsequent runs unless `data/state.json` is deleted. Upcoming matches are re-scouted on every run.

Between those runs, a light sync every 30 minutes (`SYNC_INTERVAL_MINUTES`) checks E-Sparven for new matches and reschedules, updates Discord events and ready checks, and refreshes the Discord scouting cards. It makes no OpenDota calls and writes no gists.

---

## Project structure

```
bot.py               Entry point of the scout service: full run, light sync, scheduler
config.py            Settings from .env
seasons.py           Current season from E-Sparven; which seasons count for stats
discord_sync.py      Discord events, ready check queueing, reminders, scouting cards
presence.py          The bot's status line: countdown, ready count, live, next season
scouting.py          Full scouting run: one gist per opponent, our team and the index
players.py           Steam ID lookup and per-player data (OpenDota, league heroes, career)
analysis.py          Stats from E-Sparven match data: draft tendencies, hero pools, history
esparven.py          E-Sparven API client and page parsers
opendota.py          OpenDota API client
gist.py              GitHub Gist storage client
webhook.py           Channel notifications, sent by the bot account
discord_events.py    Discord scheduled events: create, retime, complete
ready_bot.py         Always-on Discord service: ready check, Scout button, slash commands
ready_check.py       Ready check rules and SQLite storage
scout_cards.py       Opponent player cards from E-Sparven profiles
scout_ui.py          Discord embeds, dropdown and rank emojis for the scouting cards
player_map.py        Steam ID cache per player (data/player_map.json)
jsonfile.py          Crash-safe JSON files in data/
output.py            Console logging for bot.py
steam.py             Steam32/64 ID conversion
index.html           GitHub Pages dashboard (single file, no build step)
medals/              Rank badge images, also uploaded as the bot's rank emojis
tests/               pytest suite, with saved E-Sparven pages in tests/fixtures/
data/                Runtime state (gitignored): state.json, player_map.json, ready_check.db
Dockerfile           Container definition
docker-compose.yml   Compose services `scout` and `ready`
```

Everything the bot remembers lives in `data/`:

- `state.json`: gist IDs, frozen flags and Discord event IDs per match
- `player_map.json`: Steam IDs per player
- `ready_check.db`: votes, history, attendance, scouting cards and the status snapshot

Files are written by writing a temporary file and renaming it, so a crash can't leave a half-written file behind.

---

## Setup

**Requirements:** Python 3.11+, Docker (optional, for deployment)

```bash
pip install -r requirements.txt
```

**Create a `.env` file:**

```
ESPARVEN_KEY=your_esparven_api_key
ESPARVEN_TEAM_ID=67
GITHUB_TOKEN=ghp_...
GITHUB_USERNAME=your_github_username
GIST_INDEX_ID=        # leave blank on first run, paste in after

# Optional: Discord bot — see "Discord integration" below
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
DISCORD_READY_CHANNEL_ID=
DISCORD_ROLE_ID=
```

**Creating a GitHub token:**
GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. Under Account permissions, set Gists to Read and write.

**First run:**

```bash
python bot.py --run-once
```

At the end of the first run, the bot prints the new gist IDs:

```
Add to .env:       GIST_INDEX_ID=<id>
Set in index.html: OUR_TEAM_GIST_ID = '<id>'
```

Paste `GIST_INDEX_ID` into `.env`. Then update the two constants at the top of the `<script>` block in `index.html`:

```javascript
const INDEX_GIST_ID    = '<id from .env>';
const OUR_TEAM_GIST_ID = '<id from bot output>';
```

Push `index.html` to GitHub to deploy.

---

## Bot flags

```bash
python bot.py --run-once                 # run once then exit
python bot.py --sync-once                # light match/reschedule/scouting-card sync, then exit
python bot.py --run-once --no-opendota   # skip OpenDota API calls (fast debug mode)
python bot.py --force-next-event         # admin debug: force-create a Discord event for the soonest upcoming match, then exit
```

`--no-opendota` skips all per-player OpenDota calls (rank, winrate, recent matches, pub heroes). Tournament heroes are still built from E-Sparven match data. Useful for testing without waiting on API rate limits.

`--force-next-event` bypasses the normal 12-day creation window (see "Discord integration" below) to immediately create an event for the soonest upcoming match — a no-op if there's no upcoming match or it already has an event. Useful for verifying the Discord bot setup without waiting for a match to enter the window naturally.

---

## Discord integration

Everything is posted by one bot account. Set `DISCORD_BOT_TOKEN` and `DISCORD_READY_CHANNEL_ID` for the ready check and notifications, and `DISCORD_GUILD_ID` for scheduled events. Set `DISCORD_ROLE_ID` to @-mention a role. Leave a variable unset to disable that part.

**Match notifications** (`webhook.py`):
- New upcoming match detected, with top ban targets when scouting data is available
- Match day reminder, once per match, tagging the role

**Scheduled events** (`discord_events.py`) — creates a native Discord event once a match is 12 days out or closer (`DISCORD_EVENT_WINDOW_DAYS` in `bot.py`), with a cover image from both teams' logos when available. If E-Sparven reschedules the match, the event is retimed in place, never deleted or recreated. Once the match is played, the event is marked Completed. The event is informational only.

### Ready check

`ready_bot.py` runs as the always-on `ready` service and posts a ready check when a match is within 12 days. Players click **I'm in** or **Can't make it**, and clicking their current answer clears it. Anyone who can see the message can vote, and voting closes at kick-off. **History** shows that match's audit log privately to whoever clicks it.

| Event | When | What happens |
|---|---|---|
| Late drop | An "in" player leaves within 30 hours of kick-off | Alert after 60 seconds, cancelled if they click back in. Pings the role only if 5 or fewer were in before the drop |
| Back in | A player with a sent alert returns | Short message, no ping |
| Reschedule | E-Sparven reports a new time | All answers reset, and the role is pinged to vote again |
| Short roster | Fewer than 5 are in at 48h and 24h before kick-off | The role is pinged with how many are missing |
| Voice check | 10 minutes before kick-off | Passes silently if at least 5 "in" players are in voice. Otherwise pings those missing |
| Attendance | At kick-off | With 5 in voice, extra players count as bench. With fewer, "in" players not in voice count as no-shows |

The voice check counts any voice channel the bot can see, except the server's AFK channel. Only players who clicked **I'm in** count, so stand-ins must vote too. Reschedules are detected within 30 minutes by the light sync.

### Bot status

The bot's status line under its name in the member list shows what's coming up, updated within 15 seconds:

| When | Status |
|---|---|
| From kick-off for 3 hours | 🔴 Live vs Team BIGMAN |
| On match day | Match day: vs Team BIGMAN at 19:00 |
| Before that | Next: vs Team BIGMAN · Sun 19:00 |
| No matches scheduled | Next season starts 17 Jan · sign-ups open |

While a ready check is posted and fewer than five have said they're in, the countdown adds the count, e.g. "· 3/5 ready". E-Sparven fills in results afterwards, so "Live" is the time window, not a score. The `scout` service stores the upcoming matches and the next season's start (from E-Sparven's season page) on every sync, and the `ready` service shows them.

### Scouting cards

Every ready check has a **Scout** button, and the scouting lives behind it so the channel gets no extra messages:

- **Scout** shows the opponent roster privately: rank badge, record and KDA this season, and main position per player, plus E-Sparven's win prediction when it shows one. A dropdown below opens any player's full card, also private.
- A **thread** on the ready check message holds the roster and one card per player for anyone who wants to read them together. The bot edits these messages when the data changes, so the thread doesn't grow.

A card shows rank, league career (games, record, seasons), this season's record, table placing, playoffs, K/D/A, GPM, XPM, kill participation, positions and heroes, the player's most played heroes across all seasons, their three previous seasons, and links to E-Sparven, OpenDota and Stratz.

The data comes from E-Sparven's public match page and player profiles (`/match/{id}`, `/spelare/{id}`), built by the `scout` service on its 30-minute sync and stored in the ready check database. Profiles are cached for 6 hours. Rank badges are application emojis that the bot uploads from `medals/` the first time it starts, so there's nothing to upload by hand. The same images are used in the dropdown, the roster lines and each card's thumbnail. `RANK_BADGE_URL` overrides the thumbnail.

**Admin slash commands** (replies are visible only to you):

```
/readycheck-stats [season]
/debug ready-check [players_in] [hours_to_kickoff] [preview] [in_voice]
/debug scout [team] [view]
/debug status [scenario] [players_in]
/debug run [job] [opendota] [days_out]
```

`/readycheck-stats` shows matches answered, late drops and no-shows per player. `/debug ready-check` runs a throwaway sandbox that saves nothing and pings nobody. Set `preview` to `reschedule`, `short_roster` or `voice` to preview those alerts.

`/debug scout` builds scouting cards live for any team in the league (the `team` option autocompletes) and shows them privately. Without `team` it shows the next opponent's stored cards. `view: dropdown` (default) is the Scout button's view, and `view: thread` shows every message the scouting thread would hold. Nothing is saved or posted. Building a team takes about 10 seconds.

`/debug status` shows the bot's status line right now, or previews it for a made-up countdown, match day, live match or off-season, without changing the real status.

`/debug run` runs the real `sync`, `full` or `next_event` job on live data and privately lists what it would do. It works on a throwaway copy of the state files and blocks every Discord and gist write, so nothing is posted, pinged, saved or changed. The same mode is available as `python bot.py --sync-once --silent`. `job: new_match` instead shows the messages a new match would produce, privately and with example data. Set `days_out` above 12 to see a match outside the ready check window.

### Bot setup

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot → copy the token into `DISCORD_BOT_TOKEN`. No privileged intents are needed.
2. OAuth2 → URL Generator → scopes `bot` and `applications.commands`, permission `Manage Events` → open the generated URL to invite it to your server.
3. Enable Developer Mode in Discord (User Settings → Advanced), then copy the server ID to `DISCORD_GUILD_ID`, the channel ID to `DISCORD_READY_CHANNEL_ID`, and the role ID to `DISCORD_ROLE_ID`.
4. In the ready channel, allow the bot **View Channel**, **Send Messages**, **Embed Links** and **Read Message History**, plus **Mention All Roles** unless the role is mentionable. For the scouting thread, also allow **Create Public Threads** and **Send Messages in Threads**.
5. Allow **View Channel** on every voice channel the team uses, or players there will look absent.

---

## Season configuration

Nothing to edit when a new season starts. The bot takes the season from E-Sparven: the season of our upcoming matches, or else the season E-Sparven marks as active. Played matches on the dashboard are the current season's, and the ticker only shows lines for matches on the dashboard, so last season drops out on its own.

Hero pools and draft stats use every league season. One-off cups are left out: seasons 48 (Glitched Dota 2 First Blood) and 65 (Defense of the Mellandays). Two optional `.env` settings change this:

```bash
CURRENT_SEASON_ID=87      # force a season instead of asking E-Sparven
EXCLUDED_SEASONS=48,65    # seasons left out of stats (add new cups here)
```

Don't delete `data/state.json` between seasons. It holds the index and our-team gist IDs that `index.html` points at, and deleting it creates new gists, which breaks the live dashboard until the IDs are updated.

---

## Fresh start

To wipe all state and regenerate everything from scratch:

```bash
docker compose down
sudo rm data/state.json data/player_map.json
docker compose up -d
```

Then update `.env` and `index.html` with the new gist IDs printed in the `scout` log at the end of the first run. Delete `data/ready_check.db` too only if you also want to lose ready check history and attendance.

---

## Deployment

The bot runs in Docker via Compose with two services:

- `scout` (`tornknackarna-scout-1`) runs twice daily at 06:00 and 18:00 UTC (`RUN_TIMES` in `.env`), plus once immediately whenever the container starts. A light sync runs every 30 minutes in between (`SYNC_INTERVAL_MINUTES`, `0` turns it off).
- `ready` (`tornknackarna-ready-1`) stays connected to Discord and handles the ready check.

```bash
# Build and start (detached)
docker compose up -d --build

# Update only the Discord service, without a full scouting run
docker compose up -d --build ready
```

`data/` is bind-mounted (see `docker-compose.yml`), so state survives rebuilds. `.env` is loaded via `env_file`, so it must exist alongside `docker-compose.yml` before starting. `.dockerignore` keeps `.env` and all state out of the image itself.

**Upgrading from the old layout:** `state.json` and `player_map.json` used to live next to the code. On its first start, the bot copies them into `data/` if they aren't there yet. Add read-only mounts for the old files to `docker-compose.yml` if you still need that copy. Once `data/state.json` and `data/player_map.json` exist, the old files are only a backup.

**Useful commands:**

```bash
# View logs
docker logs -f tornknackarna-scout-1
docker logs -f tornknackarna-ready-1

# Restart (e.g. after editing data/state.json)
docker compose restart scout

# Apply .env changes (restart alone doesn't reload them)
docker compose up -d

# Run manually inside the container
docker exec tornknackarna-scout-1 python bot.py --run-once --no-opendota

# Rebuild and start after code changes
docker compose up -d

# Check whether it's actually running — restart policy is `unless-stopped`,
# which does NOT auto-restart after a manual `docker stop`
docker ps -a
```

---

## Frontend

The dashboard is a single `index.html` with no build step or dependencies. All data is fetched from GitHub Gists at load time.

**Sidebar:**
- Upcoming and past matches for the current season
- Tornknäckarna roster shortcut (password protected)
- Spinning 3D logo with drag-to-rotate and match countdown timer
- Logo rotation speed increases as the next match approaches
- Live ticker feed with bot-generated intel snippets

**Match panel:**
- Opponent roster with rank medals, pub winrate, recent form, and CM hero pool
- League career from E-Sparven on each player card: record, seasons played, and the last four seasons with placing, KDA and main position. Players without OpenDota data use E-Sparven's rank
- Ban suggestions scored by CM winrate, games played, delta vs pub winrate, and recency — grouped into Priority, Moderate, and Situational tiers
- Draft presence: most picked and most banned heroes overall
- Draft order tendencies: first ban and first pick spotlights, plus full slot-by-slot breakdown
- Match history with picks, bans, side labels, and result per game
- Season toggle between current season and all tracked seasons

**Our team panel** (password protected):
- Tornknäckarna roster with the same player cards and hero pool data
- Draft presence and order tendencies for our own picks and bans
- Full match history

**Themes:** Dark, Light, Console — persisted in localStorage. The Console theme includes a green 3D spinning logo and a boot sequence animation.

**Local development:**

```bash
python3 -m http.server 8080
```

---

## Player ID mapping

New players get their Steam ID automatically. The API's roster gives each player an `applicationUserID`, and E-Sparven's public match page (`/match/{id}`) links each player's profile by that same ID next to their OpenDota link. The bot joins the two, so it never has to match names. Players who haven't registered a numeric ID on E-Sparven stay unknown.

IDs are saved in `data/player_map.json`, keyed by `applicationUserID`, so a player who renames themselves keeps their Steam ID:

```json
{
  "d88f7630-526d-4b73-871e-bd6703175329": {
    "name": "PlayerName", "account_id": 12345678, "confirmed": false, "source": "scraped"
  }
}
```

To fix a player by hand, add an entry keyed by their current in-game name with `"confirmed": true`. The bot moves it under their ID the next time it sees them, and never overwrites confirmed entries:

```json
{
  "PlayerName": { "account_id": 12345678, "confirmed": true }
}
```

Older files keyed by name are moved over the same way. `RESOLVE_NEW_PLAYER_IDS` in `config.py` turns the lookup off. The OpenDota name search fallback stays off (`OPENDOTA_NAME_SEARCH`), since it can pick the wrong account.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite needs no network or credentials. It covers the E-Sparven parsers (against saved pages in `tests/fixtures/`), the ready check rules, the player map, the draft and hero statistics, reminder timing, Discord event completion, season detection and the scouting cards. If E-Sparven changes its pages, save a fresh copy of the page into `tests/fixtures/` and update the parser until the tests pass again.

---

## Notes

- Gists are secret (unlisted) but not private. The dashboard's gist IDs are in `index.html`, so anyone who opens the page source can read the data, including the "Our team" panel behind the password. The password only keeps casual visitors out.
- The dashboard escapes every string from the gists when it loads them, because player and team names come from E-Sparven and anyone can choose their own
- Deleting `data/state.json` causes everything to be regenerated from scratch — update `.env` and `index.html` with the new IDs afterwards
- `OPENDOTA_DELAY = 2.5` seconds between OpenDota API calls to stay within rate limits
- Hero images use `steamcdn-a.akamaihd.net` — `cdn.dota2.com` is unreliable
