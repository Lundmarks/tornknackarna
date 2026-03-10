# Tornknäckarna · Scouting Dashboard

A scouting and match-tracking system for Tornknäckarna, competing in Dota 2 Captain's Mode on [E-Sparven](https://esparven.se). A Python bot runs daily, pulls match data and pub stats, and writes everything to GitHub Gists. A static GitHub Pages frontend reads from those Gists and presents it as a scouting report.

Live at: **[lundmarks.github.io/tornknackarna](https://lundmarks.github.io/tornknackarna/)**

---

## How it works

Each day the bot:
1. Fetches upcoming and past meetings from E-Sparven
2. Resolves opponent Steam IDs (via page scraping, then OpenDota search as fallback)
3. Parses tournament picks/bans and player performance directly from E-Sparven match data
4. Fetches pub stats and rank from OpenDota
5. Writes one secret GitHub Gist per opponent, plus an index Gist the frontend reads on load
6. Builds a self-scouting Gist for Tornknäckarna's own roster

Past meetings are frozen after first write — they won't be re-processed unless you delete `state.json`.

Hero pool and history data covers the current season plus any seasons listed in `EXTRA_SEASONS` in `bot.py`.

---

## Project structure

```
bot.py           Main orchestration loop
esparven.py      E-Sparven API client + HTML scraper
opendota.py      OpenDota API client
gist.py          GitHub Gist storage client
steam.py         Steam32/64 ID conversion
player_map.py    Persistent name → Steam ID cache (player_map.json)
index.html       GitHub Pages frontend
medals/          Rank medal images (herald.png … immortal.png)
state.json       Local run state — gist IDs, frozen flags (gitignored)
```

---

## Setup

**Requirements:** Python 3.11+, Docker (for deployment)

```bash
pip install -r requirements.txt
```

Create a `.env` file:

```
ESPARVEN_KEY=your_esparven_api_key
ESPARVEN_TEAM_ID=67
GITHUB_TOKEN=ghp_...
GITHUB_USERNAME=Lundmarks
GIST_INDEX_ID=        # filled in automatically on first run, then paste here
```

**Creating a GitHub token:**
Go to GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. Under Account permissions, set Gists to Read and write.

Run once to test:

```bash
python bot.py --run-once
```

On first run the bot will print two IDs:

```
Add to .env:       GIST_INDEX_ID=<id>
Set in index.html: OUR_TEAM_GIST_ID = '<id>'
```

Paste `GIST_INDEX_ID` into `.env` (prevents re-creating the index Gist if `state.json` is ever deleted).

Paste both IDs into the top of the `<script>` block in `index.html`:

```javascript
const INDEX_GIST_ID    = '<id from .env>';
const OUR_TEAM_GIST_ID = '<id from bot output>';
```

Then push `index.html` to GitHub.

---

## Seasons

Edit these constants at the top of `bot.py` each season:

```python
CURRENT_SEASON_ID = 67       # update to new season ID
EXTRA_SEASONS     = [64, 65] # past seasons to include; set to [] for current only
```

---

## Deployment

The bot runs in Docker on a daily schedule (06:00 server time):

```bash
docker build -t tornknackarna-bot .
docker run -d --name tornknackarna-bot --restart unless-stopped --env-file .env tornknackarna-bot
```

To force a full refresh (e.g. after schema changes or a new season):

```bash
rm state.json && python bot.py --run-once
```

Or inside Docker:

```bash
docker run --rm --env-file .env tornknackarna-bot python bot.py --run-once
```

---

## Frontend

The dashboard is a single `index.html` with no build step. It reads all data from GitHub Gists at load time.

- Dark / light / console theme, persisted in localStorage
- Per-opponent scouting reports: rank, pub winrate, CM hero pool, recent form
- Season toggle: current season only vs all tracked seasons
- Ban suggestions scored by CM winrate, games played, and delta vs pub winrate
- Match history with picks/bans, OPP/vs side labels, and W/L per game
- Tornknäckarna roster view under "Our team"

To develop locally:

```bash
python3 -m http.server 8080
```

---

## Notes

- `cdn.dota2.com` is unreliable for hero images — the frontend uses `steamcdn-a.akamaihd.net` instead
- `player_map.json` and `state.json` are gitignored; back them up if you care about confirmed Steam ID mappings
- Gists are created as secret (unlisted) but not private — anyone with the URL can read them, same as the previous JSONbin setup
- GitHub raw Gist URLs always serve the latest revision with no versioning or caching issues
