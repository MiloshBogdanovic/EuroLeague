# EuroLeague player form

Streamlit app for EuroLeague team form, player windows (this + last season), H2H vs next opponent, and same-position defender matchups.

## Local run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m streamlit run app.py
```

## OddsPapi multi-book props (bet365 · 1xBet · Superbet RS)

Market cut compares milestone overs (`5+` / `10+` / …) from **OddsPapi** against **AdmiralBet** O/U half-lines. Books: **bet365**, **1xBet**, **superbet.rs** (Mozzart/MaxBet have no player props on this feed). Free tier is ~250 requests/month — refresh is **manual only** and only hits near-term fixtures.

**Note:** A successful refresh can still return `players: 0` if EuroLeague ladders are not posted yet. That is normal days before tip-off — re-run `python euro_oddspapi.py --refresh` closer to gameday to continue testing.

1. Copy [`.env.example`](.env.example) → `.env` and set your RapidAPI key (OddsPapi on RapidAPI):

```env
ODDSPAPI_API_KEY=your_rapidapi_key_here
ODDSPAPI_BASE_URL=https://odds-api1.p.rapidapi.com
ODDSPAPI_RAPIDAPI_HOST=odds-api1.p.rapidapi.com
```

2. In the app sidebar: **Refresh OddsPapi props** (and **Refresh AdmiralBet lines** locally).
3. Open **Market cut** — active board prefers `data/oddspapi_points_board.json` unless the bet365 screenshot board is newer (or `EURO_PREFER_BET365_BOARD=1`).

### OddsPapi quota workaround (bet365 screenshots)

When RapidAPI is exhausted, transcribe Player Points ladders into `import_bet365_screenshot_board.py` and run:

```powershell
python import_bet365_screenshot_board.py --prefer
```

That writes `data/bet365_points_board.json` and sets `EURO_PREFER_BET365_BOARD=1` in local `.env`. Commit + push the JSON so Cloud picks it up (fresher `fetched_at` wins over OddsPapi). Keep the import script on branch `local/bet365-screenshots` if you do not want it on `main`.

CLI smoke test:

```powershell
.\.venv\Scripts\Activate.ps1
python euro_oddspapi.py
```

## Data cache

Boxscore / metadata caches live under [`data/`](data/) (especially `player_game_logs_*.csv`). They are committed so deploys and first loads hit the EuroLeague API much less often. Missing games are still fetched and merged into the cache at runtime.

## Local odds → Streamlit Cloud (no API/scrape on the server)

**Rule:** RapidAPI (OddsPapi) and Playwright scrapes (AdmiralBet, 1x2online) run **only on your PC**.  
Cloud only **reads** committed JSON under `data/`.

| File | Source | Needed on Cloud? |
|------|--------|------------------|
| `data/oddspapi_points_board.json` | OddsPapi refresh | Yes (Market cut / API Fetch) |
| `data/admiralbet_lines.json` | AdmiralBet scrape | Yes (compare) |
| `data/1x2online_lines.json` | 1x2online scrape | Yes (compare) |
| `data/bet365_points_board.json` | Legacy screenshot | Optional fallback |
| `data/oddspapi_*.json` caches | Local quota caches | No (gitignored) |
| `data/*_capture.json` | Scrape debug dumps | No (gitignored) |

### Publish workflow

```powershell
# 1) Refresh locally (sidebar buttons, or):
python euro_oddspapi.py --refresh
python euro_1x2online.py
# AdmiralBet: use app sidebar "Refresh AdmiralBet lines"

# 2) Stage Cloud snapshots
python publish_odds_data.py
# or refresh-all then stage:
python publish_odds_data.py --refresh

# 3) Commit + push when ready
git status
git commit -m "Update odds snapshots for Streamlit Cloud"
git push
```

Streamlit Cloud redeploys from GitHub; Market cut rebuilds comparisons from those files (no keys on the server).

## Deploy on Streamlit Community Cloud (free)

1. Push this repo to GitHub (public or private).
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. **New app** → pick this repo → main file `app.py` → Deploy.
4. Wait for the build; open the public URL.
5. After local odds refreshes, run `publish_odds_data.py` → commit → push so Cloud gets new lines.

No RapidAPI key or Playwright is required on Cloud. Keep `.env` local only.
