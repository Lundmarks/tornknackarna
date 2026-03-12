# Tornknäckarna · Scouting Dashboard

A scouting and match intelligence system for Tornknäckarna, competing in Dota 2 Captain's Mode on [E-Sparven](https://esparven.se). A Python bot runs daily, analyses opponent and team data, and writes to GitHub Gists. A static GitHub Pages frontend reads from those Gists and presents scouting reports, draft analysis, and match history.

Live at: **[lundmarks.github.io/tornknackarna](https://lundmarks.github.io/tornknackarna/)**

<p align="center">
  <img src="favicon.svg" width="80" height="80" alt="Radar">
</p>

---

## How it works

Each day the bot:

1. Fetches upcoming and past meetings from E-Sparven
2. Resolves opponent Steam IDs via page scraping, with OpenDota search as fallback
3. Parses tournament picks, bans, and player performance from E-Sparven match data
4. Fetches pub stats, rank, and hero pool data from OpenDota
5. Computes draft tendencies — most picked and most banned heroes per team
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
medals/           Rank medal images (herald.png through immortal.png)
media/            Static assets (password-image.png etc.)
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
```

**Creating a GitHub token:**
GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. Under Account permissions, set Gists to Read and write.

**First run:**

```bash
python bot.py --run-once
```

At the end of the first run, the bot prints:

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

## Season configuration

Update these two constants at the top of `bot.py` at the start of each season:

```python
CURRENT_SEASON_ID = 67       # the active E-Sparven season ID
EXTRA_SEASONS     = [64, 65] # older seasons to include in hero pool data
                             # set to [] for current season only
```

After changing season, delete `state.json` and re-run to regenerate all bins.

---

## Deployment

The bot runs in Docker on a daily schedule at 06:00 server time.

```bash
# Build
docker build -t tornknackarna-scouter .
```


---

## Frontend

The dashboard is a single `index.html` with no build step or dependencies. All data is fetched from GitHub Gists at load time.

**Sidebar:**
- Upcoming and past matches for the current season
- Tornknäckarna roster shortcut (password protected)
- Spinning 3D logo with drag-to-rotate and match countdown timer
- Live ticker feed with bot-generated intel snippets

**Match panel:**
- Opponent roster with rank medals, pub winrate, recent form, and CM hero pool
- Ban suggestions scored by CM winrate, games played, delta vs pub winrate, and recency — grouped into Priority, Moderate, and Situational tiers
- Draft presence section showing most picked and most banned heroes
- Match history with picks, bans, side labels, and result per game
- Season toggle between current season and all tracked seasons

**Our team panel**
- Own team roster with the same player cards and hero pool data
- Draft presence for our own picks and bans
- Full match history

**Themes:** Dark, Light, Console — persisted in localStorage. The Console theme includes a green 3D spinning logo and boot sequence animation.

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
