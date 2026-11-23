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

Past meetings are frozen after first write and skipped on subsequent runs unless `state.json` is deleted. Upcoming matches are re-scouted on every run.

Between those runs, a light sync every 30 minutes (`SYNC_INTERVAL_MINUTES`) checks E-Sparven for new matches and reschedules, updates Discord events and ready checks, and refreshes the Discord scouting cards. It makes no OpenDota calls and writes no gists.

---

## Project structure

```
bot.py               Main orchestration loop
esparven.py          E-Sparven API client and HTML scraper
opendota.py          OpenDota API client
gist.py              GitHub Gist storage client
webhook.py           Discord match notifications, sent by the bot account
discord_events.py    Discord Guild Scheduled Event creation/reschedule/completion
ready_bot.py         Always-on Discord service: ready check buttons, alerts, slash commands
ready_check.py       Ready check rules and SQLite storage
scout_cards.py       Opponent player cards from E-Sparven profiles, cached in the ready check database
scout_ui.py          Discord embeds, dropdown and rank emojis for the scouting cards
steam.py             Steam32/64 ID conversion utilities
player_map.py        Persistent name to Steam ID cache (player_map.json)
index.html           GitHub Pages frontend (single file, no build step)
favicon.svg          Site favicon
medals/              Rank medal images (herald.png through immortal.png)
media/               Static assets
state.json           Local run state — gist IDs, frozen flags, Discord event IDs (gitignored)
player_map.json      Steam ID cache (gitignored)
data/                Ready check database: votes, history, attendance (gitignored)
Dockerfile           Container definition for server deployment
docker-compose.yml   Compose services `scout` and `ready`
```

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

### Scouting cards

Every ready check has a **Scout** button, and the scouting lives behind it so the channel gets no extra messages:

- **Scout** shows the opponent roster privately: rank badge, record and KDA this season, and main position per player, plus E-Sparven's win prediction when it shows one. A dropdown below opens any player's full card, also private.
- A **thread** on the ready check message holds the roster and one card per player for anyone who wants to read them together. The bot edits these messages when the data changes, so the thread doesn't grow.

A card shows rank, league career (games, record, seasons), this season's record, table placing, playoffs, K/D/A, GPM, XPM, kill participation, positions and heroes, the player's most played heroes across all seasons, their three previous seasons, and links to E-Sparven, OpenDota and Stratz.

The data comes from E-Sparven's public match page and player profiles (`/match/{id}`, `/spelare/{id}`), built by the `scout` service on its 30-minute sync and stored in the ready check database. Profiles are cached for 6 hours. Rank badges are application emojis that the bot uploads from `medals/` the first time it starts, so there's nothing to upload by hand. The same images are used in the dropdown, the roster lines and each card's thumbnail. `RANK_BADGE_URL` overrides the thumbnail.

**Admin slash commands** (replies are visible only to you):

```
/readycheck-stats [season]
/readycheck-debug [players_in] [hours_to_kickoff] [preview] [in_voice]
/scout-debug [team] [view]
```

`/readycheck-stats` shows matches answered, late drops and no-shows per player. `/readycheck-debug` runs a throwaway sandbox that saves nothing and pings nobody. Set `preview` to `reschedule`, `short_roster` or `voice` to preview those alerts.

`/scout-debug` builds scouting cards live for any team in the league (the `team` option autocompletes) and shows them privately. Without `team` it shows the next opponent's stored cards. `view: dropdown` (default) is the Scout button's view, and `view: thread` shows every message the scouting thread would hold. Nothing is saved or posted. Building a team takes about 10 seconds.

### Bot setup

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot → copy the token into `DISCORD_BOT_TOKEN`. No privileged intents are needed.
2. OAuth2 → URL Generator → scopes `bot` and `applications.commands`, permission `Manage Events` → open the generated URL to invite it to your server.
3. Enable Developer Mode in Discord (User Settings → Advanced), then copy the server ID to `DISCORD_GUILD_ID`, the channel ID to `DISCORD_READY_CHANNEL_ID`, and the role ID to `DISCORD_ROLE_ID`.
4. In the ready channel, allow the bot **View Channel**, **Send Messages**, **Embed Links** and **Read Message History**, plus **Mention All Roles** unless the role is mentionable. For the scouting thread, also allow **Create Public Threads** and **Send Messages in Threads**.
5. Allow **View Channel** on every voice channel the team uses, or players there will look absent.

---

## Season configuration

Update these two constants at the top of `bot.py` at the start of each season:

```python
CURRENT_SEASON_ID = 77
EXTRA_SEASONS     = [6, 17, 27, 43, 60, 67]  # older seasons to include; set to [] for current only
```

After changing season, **don't delete `state.json`** — it stores `index_bin_id`/`our_team_bin_id`, and deleting it mints brand-new gists that orphan the IDs hardcoded in `index.html`, breaking the live dashboard until someone updates them manually. Instead:

1. Back up `state.json` and `player_map.json` (e.g. into a gitignored `backups/` folder) before making changes.
2. Bump `CURRENT_SEASON_ID`, and move the *old* current season into `EXTRA_SEASONS` rather than dropping it, to keep historical stats intact.
3. Clear only `state["meetings"]` (the per-meeting freeze/ticker/Discord-event cache) to drop stale data from the old season — leave `index_bin_id`/`our_team_bin_id` untouched.

---

## Fresh start

To wipe all state and regenerate everything from scratch:

```bash
sudo rm state.json player_map.json
echo '{}' > state.json
echo '{}' > player_map.json
python bot.py --run-once --no-opendota
```

Then update `.env` and `index.html` with the new gist IDs printed at the end of the run.

---

## Deployment

The bot runs in Docker via Compose with two services:

- `scout` (`tornknackarna-scout-1`) runs twice daily at 06:00 and 18:00 UTC (`RUN_TIMES` in `bot.py`), plus once immediately whenever the container starts. A light sync runs every 30 minutes in between (`SYNC_INTERVAL_MINUTES`, `0` turns it off).
- `ready` (`tornknackarna-ready-1`) stays connected to Discord and handles the ready check.

```bash
# Build and start (detached)
docker compose up -d --build

# Update only the Discord service, without a full scouting run
docker compose up -d --build ready
```

`state.json`, `player_map.json` and `data/` are bind-mounted (see `docker-compose.yml`) so state survives rebuilds. `.env` is loaded via `env_file`, so it must exist alongside `docker-compose.yml` before starting.

**Useful commands:**

```bash
# View logs
docker logs -f tornknackarna-scout-1
docker logs -f tornknackarna-ready-1

# Restart (e.g. after editing state.json)
docker compose restart scout

# Apply .env changes (restart alone doesn't reload them)
docker compose up -d

# Run manually inside the container
docker exec tornknackarna-scout-1 python bot.py --run-once --no-opendota

# Full rebuild after code changes
docker compose up -d --build

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

Found IDs are saved to `player_map.json` as unconfirmed. Entries marked `"confirmed": true` are never overwritten, so you can still fix a player by hand:

```json
{
  "PlayerName": { "account_id": 12345678, "confirmed": true }
}
```

`RESOLVE_NEW_PLAYER_IDS` in `bot.py` turns the lookup off. The old OpenDota name search fallback stays off (`OPENDOTA_NAME_SEARCH`), since it can pick the wrong account. `player_map.json` is gitignored, so back it up alongside `state.json`.

---

## Notes

- Gists are secret (unlisted) but not private — anyone with the URL can read them
- Deleting `state.json` causes everything to be regenerated from scratch — update `.env` and `index.html` with the new IDs afterwards
- `OPENDOTA_DELAY = 2.5` seconds between OpenDota API calls to stay within rate limits
- Hero images use `steamcdn-a.akamaihd.net` — `cdn.dota2.com` is unreliable
