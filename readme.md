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
2. Resolves opponent Steam IDs via page scraping, with OpenDota search as fallback
3. Parses tournament picks, bans, and player performance from E-Sparven match data
4. Fetches pub stats, rank, and hero pool data from OpenDota
5. Computes draft tendencies — most picked, most banned, and draft order patterns per team
6. Generates ticker snippets summarising key intel for each opponent
7. Writes one secret GitHub Gist per opponent, a self-scouting Gist for Tornknäckarna, and an index Gist the frontend reads on load

Past meetings are frozen after first write and skipped on subsequent runs unless `state.json` is deleted. Upcoming matches are re-scouted on every run.

---

## Project structure

```
bot.py            Main orchestration loop
esparven.py       E-Sparven API client and HTML scraper
opendota.py       OpenDota API client
gist.py           GitHub Gist storage client
steam.py          Steam32/64 ID conversion utilities
player_map.py     Persistent name to Steam ID cache (player_map.json)
index.html        GitHub Pages frontend (single file, no build step)
favicon.svg       Site favicon
medals/           Rank medal images (herald.png through immortal.png)
media/            Static assets
state.json        Local run state — gist IDs, frozen flags (gitignored)
player_map.json   Steam ID cache (gitignored)
Dockerfile        Container definition for server deployment
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

# Optional: Discord notifications and scheduled events — see "Discord integration" below
DISCORD_WEBHOOK_URL=
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
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
python bot.py --run-once --no-opendota   # skip OpenDota API calls (fast debug mode)
python bot.py --force-next-event         # admin debug: force-create a Discord event for the soonest upcoming match, then exit
```

`--no-opendota` skips all per-player OpenDota calls (rank, winrate, recent matches, pub heroes). Tournament heroes are still built from E-Sparven match data. Useful for testing without waiting on API rate limits.

`--force-next-event` bypasses the normal 12-day creation window (see "Discord integration" below) to immediately create an event for the soonest upcoming match — a no-op if there's no upcoming match or it already has an event. Useful for verifying the Discord bot setup without waiting for a match to enter the window naturally.

---

## Discord integration

Both pieces are optional and independently gated — set only `DISCORD_WEBHOOK_URL` for notifications, only `DISCORD_BOT_TOKEN`/`DISCORD_GUILD_ID` for scheduled events, or both. Leave the relevant variable(s) unset to disable.

**Webhook notifications** (`webhook.py`, `DISCORD_WEBHOOK_URL`) — posts to a channel via an incoming webhook:
- New upcoming match detected, with top ban targets
- A scheduled event was created for a match (see below), linking to it
- Match day reminder, once per match

**Scheduled events** (`discord_events.py`, `DISCORD_BOT_TOKEN` + `DISCORD_GUILD_ID`) — creates a native Discord Guild Scheduled Event once a match is 12 days out or closer (`DISCORD_EVENT_WINDOW_DAYS` in `bot.py`), with a cover image composited from both teams' logos when available. If E-Sparven later reschedules the match, the existing event is retimed in place — never deleted or recreated. Once the match is played, the event is marked Completed rather than left stale. **Ready check:** players confirm attendance by clicking **Interested** on the event itself — this is stated in both the event description and the webhook announcement.

This needs a separate bot application (not the webhook above) with the **Manage Events** permission in the target server:
1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot → copy the token into `DISCORD_BOT_TOKEN`
2. OAuth2 → URL Generator → scope `bot`, permission `Manage Events` → open the generated URL to invite it to your server
3. Enable Developer Mode in Discord (User Settings → Advanced), right-click the server icon → Copy Server ID → `DISCORD_GUILD_ID`

---

## Season configuration

Update these two constants at the top of `bot.py` at the start of each season:

```python
CURRENT_SEASON_ID = 77
EXTRA_SEASONS     = [6, 17, 27, 43, 60, 67]  # older seasons to include; set to [] for current only
```

After changing season, delete `state.json` and re-run to regenerate all gists.

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

The bot runs in Docker on a daily schedule at 06:00 UTC.

```bash
# Build
docker build -t tornknackarna-scouter .

# Run with persistent state
docker run -d \
  --name tornknackarna-scouter \
  --restart unless-stopped \
  -v $(pwd)/state.json:/app/state.json \
  -v $(pwd)/player_map.json:/app/player_map.json \
  --env-file .env \
  tornknackarna-scouter
```

**Useful commands:**

```bash
# View logs
docker logs -f tornknackarna-scouter

# Run manually inside container
docker exec tornknackarna-scouter python bot.py --run-once --no-opendota

# Full rebuild
docker stop tornknackarna-scouter
docker rm tornknackarna-scouter
docker build -t tornknackarna-scouter .
docker run -d --name tornknackarna-scouter --restart unless-stopped \
  -v $(pwd)/state.json:/app/state.json \
  -v $(pwd)/player_map.json:/app/player_map.json \
  --env-file .env tornknackarna-scouter
```

The `-v` flags are bind mounts — they link files on your host to files inside the container so that state survives rebuilds.

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

Steam IDs are resolved automatically where possible. For players with no numeric ID on E-Sparven, add them manually to `player_map.json`:

```json
{
  "PlayerName": { "account_id": 12345678, "confirmed": true }
}
```

`player_map.json` is gitignored — back it up alongside `state.json`.

---

## Notes

- Gists are secret (unlisted) but not private — anyone with the URL can read them
- Deleting `state.json` causes everything to be regenerated from scratch — update `.env` and `index.html` with the new IDs afterwards
- `OPENDOTA_DELAY = 2.5` seconds between OpenDota API calls to stay within rate limits
- Hero images use `steamcdn-a.akamaihd.net` — `cdn.dota2.com` is unreliable
