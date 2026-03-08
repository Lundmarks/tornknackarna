# Tornknäckarna · Scouting Dashboard

A scouting and match-tracking system for Tornknäckarna, competing in Dota 2 Captain's Mode on [E-Sparven](https://esparven.se). A Python bot runs daily, pulls match data and pub stats, and writes everything to JSONbin. A static GitHub Pages frontend reads from those bins and presents it as a scouting report.

Live at: **[lundmarks.github.io/tornknackarna](https://lundmarks.github.io/tornknackarna/)**

---

## How it works

Each day the bot:
1. Fetches upcoming and past meetings from E-Sparven
2. Resolves opponent Steam IDs (via page scraping, then OpenDota search as fallback)
3. Parses tournament picks/bans and player performance directly from E-Sparven match data
4. Fetches pub stats and rank from OpenDota
5. Writes one JSONbin per opponent, plus an index bin the frontend reads on load
6. Builds a self-scouting bin for Tornknäckarna's own roster

Past meetings are frozen after first write — they won't be touched again unless you delete `state.json`.

---

## Project structure

```
bot.py           Main orchestration loop
esparven.py      E-Sparven API client + HTML scraper
opendota.py      OpenDota API client
jsonbin.py       JSONbin.io client
steam.py         Steam32/64 ID conversion
player_map.py    Persistent name → Steam ID cache (player_map.json)
index.html       GitHub Pages frontend
medals/          Rank medal images (herald.png … immortal.png)
state.json       Local run state — bin IDs, frozen flags (gitignored)
```

---

## Setup

**Requirements:** Python 3.11+, Docker (for deployment)

```bash
pip install -r requirements.txt
```

Create a `.env` file:

```
ESPARVEN_KEY=your_key
ESPARVEN_TEAM_ID=67
JSONBIN_KEY=your_key
JSONBIN_INDEX_BIN_ID=   # filled in automatically on first run
```

Run once to test:

```bash
python bot.py --run-once
```

On first run the bot will print the index bin ID — paste it into `index.html` as `INDEX_BIN_ID`, and the self-scouting bin ID as `OUR_TEAM_BIN_ID`.

---

## Deployment

The bot runs in Docker on a daily schedule (06:00 server time):

```bash
docker build -t tornknackarna-bot .
docker run -d --name tornknackarna-bot --restart unless-stopped --env-file .env tornknackarna-bot
```

To force a full refresh (e.g. after schema changes):

```bash
rm state.json
docker run --rm --env-file .env tornknackarna-bot python bot.py --run-once
```

---

## Frontend

The dashboard is a single `index.html` with no build step. It reads all data from JSONbin at load time.

- Dark mode by default, light/dark toggle persisted in localStorage
- Per-opponent scouting reports: rank, pub winrate, CM hero pool, recent form
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
- JSONbin free tier allows 10,000 requests/month — at one daily run with a typical schedule that's well within limits
