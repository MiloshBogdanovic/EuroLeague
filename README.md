# EuroLeague player form

Streamlit app for EuroLeague team form, player windows (this + last season), H2H vs next opponent, and same-position defender matchups.

## Local run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m streamlit run app.py
```

## Data cache

Boxscore / metadata caches live under [`data/`](data/) (especially `player_game_logs_*.csv`). They are committed so deploys and first loads hit the EuroLeague API much less often. Missing games are still fetched and merged into the cache at runtime.

## Deploy on Streamlit Community Cloud (free)

1. Push this repo to GitHub (public or private).
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. **New app** → pick this repo → main file `app.py` → Deploy.
4. Wait for the build; open the public URL.

No API keys or secrets are required.
