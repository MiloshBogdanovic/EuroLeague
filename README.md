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
3. Open **Market cut** — active board prefers `data/oddspapi_points_board.json`; legacy `data/bet365_points_board.json` is screenshot fallback only.

CLI smoke test:

```powershell
.\.venv\Scripts\Activate.ps1
python euro_oddspapi.py
```

## Data cache

Boxscore / metadata caches live under [`data/`](data/) (especially `player_game_logs_*.csv`). They are committed so deploys and first loads hit the EuroLeague API much less often. Missing games are still fetched and merged into the cache at runtime.

## Deploy on Streamlit Community Cloud (free)

1. Push this repo to GitHub (public or private).
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. **New app** → pick this repo → main file `app.py` → Deploy.
4. Wait for the build; open the public URL.

No API keys or secrets are required.
