"""Shared EuroLeague helpers for form / team-coach analysis."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from euroleague_api.boxscore_data import BoxScoreData
from euroleague_api.game_metadata import GameMetadata
from euroleague_api.player_stats import PlayerStats
from euroleague_api.schedule import Schedule

# Repo-relative so Streamlit Cloud / any cwd still finds committed caches.
DATA_DIR = Path(__file__).resolve().parent / "data"
REQUEST_SLEEP_SEC = 0.4
MAX_RETRIES = 6


def normalize_player_id(value) -> str:
    """Align boxscore ids (e.g. 'P003469' / 13369 / 13369.0) with stats codes ('003469')."""
    if value is None:
        return ""
    if isinstance(value, float):
        if np.isnan(value):
            return ""
        if value == int(value):
            value = int(value)
    if isinstance(value, (int, np.integer)):
        return str(int(value)).zfill(6)
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        text = text[:-2]
    if len(text) >= 2 and text[0].upper() == "P" and text[1:].isdigit():
        text = text[1:]
    text = text.strip()
    if text.isdigit():
        return text.zfill(6)
    return text


def _boxscore_with_retry(box: BoxScoreData, season: int, gamecode: int):
    """Fetch one boxscore with backoff on HTTP 429."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(REQUEST_SLEEP_SEC)
            return box.get_players_boxscore_stats(season, gamecode)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc)
            if "429" in msg or "Too Many Requests" in msg:
                wait = min(60.0, (2**attempt) + 1.0)
                time.sleep(wait)
                continue
            raise
    assert last_exc is not None
    raise last_exc


def parse_minutes(value) -> float:
    """Convert 'MM:SS' boxscore minutes to float minutes."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return np.nan
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "dnp"}:
        return np.nan
    if ":" not in text:
        try:
            return float(text)
        except ValueError:
            return np.nan
    minutes, seconds = text.split(":", 1)
    return int(minutes) + int(seconds) / 60.0


def load_schedule(season: int) -> pd.DataFrame:
    schedule = Schedule("E").get_schedule(season)
    schedule = schedule.copy()
    schedule["game"] = schedule["game"].astype(int)
    schedule["played_bool"] = schedule["played"].astype(str).str.lower().eq("true")
    schedule["date_parsed"] = pd.to_datetime(schedule["date"], format="%b %d, %Y", errors="coerce")
    return schedule


def next_rival(schedule: pd.DataFrame, team: str) -> tuple[str | None, pd.Series | None]:
    """Return next unplayed opponent code for team, plus the schedule row."""
    team = team.upper()
    upcoming = schedule[
        (~schedule["played_bool"])
        & ((schedule["homecode"].astype(str).str.upper() == team) | (schedule["awaycode"].astype(str).str.upper() == team))
    ].sort_values("date_parsed")
    if upcoming.empty:
        return None, None
    row = upcoming.iloc[0]
    home = str(row["homecode"]).upper()
    away = str(row["awaycode"]).upper()
    rival = away if home == team else home
    return rival, row


def filter_player_vs_opponent(player_logs: pd.DataFrame, opponent: str) -> pd.DataFrame:
    """Filter already-loaded player logs to games vs one opponent code."""
    if player_logs.empty or "Opponent" not in player_logs.columns:
        return pd.DataFrame()
    opp = opponent.upper()
    out = player_logs[player_logs["Opponent"].astype(str).str.upper() == opp].copy()
    return out.sort_values("date")


def _clean_boxscore(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out = out[
        out["Player"].notna()
        & (out["Player"].str.lower() != "total")
        & (out["Player"].str.lower() != "team")
    ]
    if "Player_ID" in out.columns:
        out["Player_ID"] = out["Player_ID"].map(normalize_player_id)
    for col in ("Team", "home_team", "away_team"):
        if col in out.columns:
            out[col] = out[col].astype(str).str.strip()
    out["MinutesFloat"] = out["Minutes"].map(parse_minutes)
    out["IsStarter"] = out["IsStarter"].fillna(0).astype(int)
    out["FGM3"] = pd.to_numeric(out.get("FieldGoalsMade3"), errors="coerce")
    out["FGA3"] = pd.to_numeric(out.get("FieldGoalsAttempted3"), errors="coerce")
    out["Points"] = pd.to_numeric(out.get("Points"), errors="coerce")
    out["Valuation"] = pd.to_numeric(out.get("Valuation"), errors="coerce")
    out["TotalRebounds"] = pd.to_numeric(out.get("TotalRebounds"), errors="coerce")
    out["PIR_per_min"] = out["Valuation"] / out["MinutesFloat"].replace(0, np.nan)
    return out


def fetch_played_boxscores(
    season: int,
    max_games: int | None = None,
    *,
    cache: bool = True,
    quiet: bool = False,
) -> pd.DataFrame:
    """Pull player boxscores for played games; optionally reuse CSV cache."""
    DATA_DIR.mkdir(exist_ok=True)
    cache_path = DATA_DIR / f"player_game_logs_{season}.csv"
    schedule = load_schedule(season)
    played = schedule[schedule["played_bool"]].sort_values("date_parsed")
    if max_games is not None:
        played = played.head(max_games)

    if cache and cache_path.exists() and max_games is None:
        cached = pd.read_csv(cache_path, parse_dates=["date"])
        cached_games = set(cached["Gamecode"].astype(int).unique())
        needed = set(played["game"].astype(int))
        if needed.issubset(cached_games):
            if not quiet:
                print(f"  cache hit: {cache_path} ({len(cached)} rows)")
            return _ensure_derived(cached)

    box = BoxScoreData("E")
    frames: list[pd.DataFrame] = []
    for _, game in played.iterrows():
        gamecode = int(game["game"])
        try:
            df = _boxscore_with_retry(box, season, gamecode)
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"  skip box {season}/{gamecode}: {exc}")
            continue
        df = df.copy()
        df["date"] = game["date_parsed"]
        df["round"] = game["round"]
        df["home_team"] = game["homecode"]
        df["away_team"] = game["awaycode"]
        frames.append(df)
        if not quiet:
            print(f"  box {season}/{gamecode}: {game['hometeam']} vs {game['awayteam']}")

    if not frames:
        return pd.DataFrame()

    out = _clean_boxscore(pd.concat(frames, ignore_index=True))
    if cache and max_games is None:
        out.to_csv(cache_path, index=False)
    return out


def _ensure_derived(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "Player_ID" in out.columns:
        out["Player_ID"] = out["Player_ID"].map(normalize_player_id)
    if "MinutesFloat" not in out.columns:
        out["MinutesFloat"] = out["Minutes"].map(parse_minutes)
    if "FGM3" not in out.columns:
        out["FGM3"] = pd.to_numeric(out.get("FieldGoalsMade3"), errors="coerce")
    if "FGA3" not in out.columns:
        out["FGA3"] = pd.to_numeric(out.get("FieldGoalsAttempted3"), errors="coerce")
    out["Points"] = pd.to_numeric(out.get("Points"), errors="coerce")
    out["Valuation"] = pd.to_numeric(out.get("Valuation"), errors="coerce")
    if "TotalRebounds" in out.columns:
        out["TotalRebounds"] = pd.to_numeric(out.get("TotalRebounds"), errors="coerce")
    out["IsStarter"] = out.get("IsStarter", 0)
    out["IsStarter"] = out["IsStarter"].fillna(0).astype(int)
    out["PIR_per_min"] = out["Valuation"] / out["MinutesFloat"].replace(0, np.nan)
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
    return out


def fetch_game_metadata(season: int, *, cache: bool = True, quiet: bool = False) -> pd.DataFrame:
    """Coach-aware game metadata for played games."""
    DATA_DIR.mkdir(exist_ok=True)
    cache_path = DATA_DIR / f"game_metadata_{season}.csv"
    schedule = load_schedule(season)
    played = schedule[schedule["played_bool"]].sort_values("date_parsed")

    if cache and cache_path.exists():
        cached = pd.read_csv(cache_path)
        if set(played["game"].astype(int)).issubset(set(cached["Gamecode"].astype(int))):
            if not quiet:
                print(f"  cache hit: {cache_path}")
            return cached

    gm = GameMetadata("E")
    frames: list[pd.DataFrame] = []
    for _, game in played.iterrows():
        gamecode = int(game["game"])
        try:
            df = gm.get_game_metadata(season, gamecode)
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"  skip meta {season}/{gamecode}: {exc}")
            continue
        frames.append(df)
        if not quiet:
            print(f"  meta {season}/{gamecode}")

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    if cache:
        out.to_csv(cache_path, index=False)
    return out


def season_player_averages(season: int) -> pd.DataFrame:
    """Per-game traditional averages. Falls back to boxscore aggregates if API is empty."""
    ps = PlayerStats("E")
    trad = ps.get_player_stats_single_season("traditional", season, statistic_mode="PerGame")
    if trad is not None and not trad.empty and "player.code" in trad.columns:
        out = trad[
            [
                "player.code",
                "player.name",
                "player.team.code",
                "gamesPlayed",
                "minutesPlayed",
                "pointsScored",
                "threePointersMade",
                "threePointersAttempted",
                "threePointersPercentage",
                "assists",
                "totalRebounds",
                "pir",
            ]
        ].rename(
            columns={
                "player.code": "Player_ID",
                "player.name": "Player",
                "player.team.code": "Team",
                "gamesPlayed": "gp",
                "minutesPlayed": "min_avg",
                "pointsScored": "pts_avg",
                "threePointersMade": "fgm3_avg",
                "threePointersAttempted": "fga3_avg",
                "threePointersPercentage": "fg3_pct",
                "assists": "ast_avg",
                "totalRebounds": "reb_avg",
                "pir": "pir_avg",
            }
        )
        out["Player_ID"] = out["Player_ID"].map(normalize_player_id)
        out["pir_per_min_avg"] = out["pir_avg"] / out["min_avg"].replace(0, np.nan)
        out["source"] = "api"
        return out

    logs = fetch_played_boxscores(season, cache=True, quiet=True)
    if logs.empty:
        return pd.DataFrame()
    return season_stats_from_logs(logs, season)


def season_stats_from_logs(logs: pd.DataFrame, season: int | None = None) -> pd.DataFrame:
    """Build per-game averages from player-game boxscores."""
    df = logs.copy()
    if "TotalRebounds" not in df.columns and "totalRebounds" in df.columns:
        df["TotalRebounds"] = df["totalRebounds"]
    df["TotalRebounds"] = pd.to_numeric(df.get("TotalRebounds"), errors="coerce")
    df = df[df["MinutesFloat"].fillna(0) > 0]
    g = df.groupby(["Player_ID", "Player", "Team"], as_index=False).agg(
        gp=("Gamecode", "nunique"),
        min_avg=("MinutesFloat", "mean"),
        pts_avg=("Points", "mean"),
        fgm3_avg=("FGM3", "mean"),
        fga3_avg=("FGA3", "mean"),
        reb_avg=("TotalRebounds", "mean"),
        pir_avg=("Valuation", "mean"),
        min_tot=("MinutesFloat", "sum"),
        pir_tot=("Valuation", "sum"),
    )
    g["fg3_pct"] = np.where(g["fga3_avg"] > 0, 100 * g["fgm3_avg"] / g["fga3_avg"], np.nan)
    g["pir_per_min_avg"] = g["pir_tot"] / g["min_tot"].replace(0, np.nan)
    g["ast_avg"] = np.nan
    g["source"] = "boxscore"
    if season is not None:
        g["Season"] = season
    return g.drop(columns=["min_tot", "pir_tot"])


def fetch_club_roster(season: int, club_code: str, *, active_only: bool = True) -> pd.DataFrame:
    """Club people roster with position (Guard/Forward/Center)."""
    import requests

    club = club_code.upper().strip()
    url = f"https://api-live.euroleague.net/v2/competitions/E/seasons/E{season}/clubs/{club}/people"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    rows = []
    club_name = None
    for item in r.json():
        if club_name is None and (item.get("club") or {}).get("name"):
            club_name = item["club"]["name"]
        if item.get("typeName") != "Player":
            continue
        if active_only and not item.get("active", True):
            continue
        person = item.get("person") or {}
        pos_code = item.get("position")
        pos_name = item.get("positionName") or POSITION_CODE_TO_NAME.get(str(pos_code), "Unknown")
        rows.append(
            {
                "Season": season,
                "Team": club,
                "TeamName": club_name or club,
                "Player_ID": normalize_player_id(person.get("code")),
                "Player": person.get("name") or person.get("alias"),
                "position": int(pos_code) if pos_code is not None else None,
                "positionName": pos_name,
                "dorsal": item.get("dorsal"),
                "active": bool(item.get("active", True)),
            }
        )
    return pd.DataFrame(rows)


POSITION_CODE_TO_NAME = {"1": "Guard", "2": "Forward", "3": "Center"}
POSITION_ALIASES = {
    "g": "Guard",
    "guard": "Guard",
    "guards": "Guard",
    "1": "Guard",
    "f": "Forward",
    "forward": "Forward",
    "forwards": "Forward",
    "2": "Forward",
    "c": "Center",
    "center": "Center",
    "centers": "Center",
    "3": "Center",
}


def normalize_position_name(value) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "Unknown"
    text = str(value).strip()
    if text in POSITION_CODE_TO_NAME.values():
        return text
    return POSITION_ALIASES.get(text.lower(), text)


def player_position(season: int, team: str, player_id: str) -> dict | None:
    roster = fetch_club_roster(season, team, active_only=False)
    hit = roster[roster["Player_ID"].astype(str) == normalize_player_id(player_id)]
    if hit.empty:
        return None
    row = hit.iloc[0]
    return {
        "Player_ID": row["Player_ID"],
        "Player": row["Player"],
        "Team": row["Team"],
        "position": row["position"],
        "positionName": normalize_position_name(row["positionName"]),
    }


def same_position_rivals(
    season: int,
    rival_team: str,
    position_name: str,
    *,
    active_only: bool = True,
) -> pd.DataFrame:
    roster = fetch_club_roster(season, rival_team, active_only=active_only)
    if roster.empty:
        return roster
    pos = normalize_position_name(position_name)
    return roster[roster["positionName"].map(normalize_position_name) == pos].copy()


def traditional_defense_features(season: int) -> pd.DataFrame:
    """Traditional stats used as a defensive-role proxy (no official defender flag)."""
    ps = PlayerStats("E")
    trad = ps.get_player_stats_single_season("traditional", season, statistic_mode="PerGame")
    if trad is None or trad.empty or "player.code" not in trad.columns:
        return pd.DataFrame()
    out = trad[
        [
            "player.code",
            "player.name",
            "player.team.code",
            "gamesPlayed",
            "gamesStarted",
            "minutesPlayed",
            "steals",
            "blocks",
            "defensiveRebounds",
        ]
    ].rename(
        columns={
            "player.code": "Player_ID",
            "player.name": "Player",
            "player.team.code": "Team",
            "gamesPlayed": "gp",
            "gamesStarted": "gs",
            "minutesPlayed": "min_avg",
            "steals": "stl_avg",
            "blocks": "blk_avg",
            "defensiveRebounds": "dreb_avg",
        }
    )
    out["Player_ID"] = out["Player_ID"].map(normalize_player_id)
    out["start_rate"] = out["gs"] / out["gp"].replace(0, np.nan)
    out["stocks_per_min"] = (out["stl_avg"] + out["blk_avg"]) / out["min_avg"].replace(0, np.nan)
    out["dreb_per_min"] = out["dreb_avg"] / out["min_avg"].replace(0, np.nan)
    # Higher = more likely primary on-ball / positional defender
    out["defender_score"] = (
        out["start_rate"].fillna(0) * 0.45
        + (out["min_avg"].fillna(0) / 40.0) * 0.35
        + out["stocks_per_min"].fillna(0) * 8.0
        + out["dreb_per_min"].fillna(0) * 2.0
    )
    return out


def likely_positional_defenders(
    season: int,
    rival_team: str,
    position_name: str,
    *,
    top_n: int = 3,
    min_minutes: float = 10.0,
) -> pd.DataFrame:
    """
    Heuristic: same-position rivals who start / play heavy minutes / rack up
    steals+blocks+dreb. EuroLeague has no official 'defender' tag.
    """
    rivals = same_position_rivals(season, rival_team, position_name, active_only=True)
    if rivals.empty:
        return rivals
    feats = traditional_defense_features(season)
    if feats.empty:
        # early season / empty API: use that team's cached boxscores only
        try:
            team_logs = load_team_season_logs(season, rival_team, quiet=True)
        except Exception:
            team_logs = pd.DataFrame()
        if team_logs.empty:
            rivals["defender_score"] = np.nan
            rivals["likely_defender"] = False
            return rivals
        agg = (
            team_logs.groupby("Player_ID", as_index=False)
            .agg(
                min_avg=("MinutesFloat", "mean"),
                gp=("Gamecode", "nunique"),
                start_rate=("IsStarter", "mean"),
            )
        )
        merged = rivals.merge(agg, on="Player_ID", how="left")
        merged["defender_score"] = merged["start_rate"].fillna(0) * 0.5 + (merged["min_avg"].fillna(0) / 40.0) * 0.5
    else:
        merged = rivals.merge(feats, on="Player_ID", how="left", suffixes=("", "_feat"))
        if "Player_feat" in merged.columns:
            merged["Player"] = merged["Player"].fillna(merged["Player_feat"])
        if "min_avg" not in merged.columns and "min_avg_feat" in merged.columns:
            merged["min_avg"] = merged["min_avg_feat"]

    merged = merged[merged["min_avg"].fillna(0) >= min_minutes].copy()
    if merged.empty:
        return merged
    merged = merged.sort_values("defender_score", ascending=False)
    merged["likely_defender"] = False
    top_idx = merged.head(top_n).index
    merged.loc[top_idx, "likely_defender"] = True
    keep = [
        "Season",
        "Team",
        "Player_ID",
        "Player",
        "positionName",
        "min_avg",
        "gp",
        "start_rate",
        "stl_avg",
        "blk_avg",
        "dreb_avg",
        "defender_score",
        "likely_defender",
    ]
    return merged[[c for c in keep if c in merged.columns]]


def load_logs_for_seasons(
    seasons: list[int],
    *,
    team: str | None = None,
    quiet: bool = False,
) -> pd.DataFrame:
    """
    Load played boxscores for multiple seasons.
    If a season cache is missing and team is set, only fetch that team's games
    (much faster than pulling the whole league).
    """
    frames = []
    for season in seasons:
        cache_path = DATA_DIR / f"player_game_logs_{season}.csv"
        if cache_path.exists():
            if not quiet:
                print(f"  loading game logs {season} (cache)…")
            logs = fetch_played_boxscores(season, cache=True, quiet=True)
        elif team:
            schedule = load_schedule(season)
            played = schedule[schedule["played_bool"]]
            team_u = team.upper()
            games = played[
                (played["homecode"].astype(str).str.upper() == team_u)
                | (played["awaycode"].astype(str).str.upper() == team_u)
            ]["game"].astype(int).tolist()
            if not quiet:
                print(f"  fetching {len(games)} {team_u} games in {season} (no full-season cache yet)…")
            logs = fetch_boxscores_for_games(season, games, quiet=quiet)
        else:
            if not quiet:
                print(f"  loading game logs {season}…")
            logs = fetch_played_boxscores(season, cache=True, quiet=quiet)
        if logs.empty:
            continue
        logs = attach_opponent(logs)
        logs["Season"] = season
        frames.append(logs)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def player_game_log(logs: pd.DataFrame, player_id: str) -> pd.DataFrame:
    pid = normalize_player_id(player_id)
    out = logs[logs["Player_ID"].astype(str) == pid].copy()
    return out.sort_values("date")


def summarize_player_windows(player_logs: pd.DataFrame) -> pd.DataFrame:
    """
    Combined this+last season averages plus last-3 / last-5 windows.
    Metrics: points, 3PT made/attempted, PIR per minute.
    """
    if player_logs.empty:
        return pd.DataFrame()
    logs = player_logs.sort_values("date").copy()
    logs = logs[logs["MinutesFloat"].fillna(0) > 0]

    def _window(df: pd.DataFrame, label: str) -> dict:
        if df.empty:
            return {
                "window": label,
                "games": 0,
                "pts_avg": np.nan,
                "fgm3_avg": np.nan,
                "fga3_avg": np.nan,
                "fg3_pct": np.nan,
                "pir_per_min": np.nan,
                "min_avg": np.nan,
                "reb_avg": np.nan,
            }
        min_tot = df["MinutesFloat"].sum()
        pir_tot = df["Valuation"].sum()
        fga = df["FGA3"].sum()
        fgm = df["FGM3"].sum()
        reb = pd.to_numeric(df.get("TotalRebounds"), errors="coerce")
        return {
            "window": label,
            "games": len(df),
            "pts_avg": df["Points"].mean(),
            "fgm3_avg": df["FGM3"].mean(),
            "fga3_avg": df["FGA3"].mean(),
            "fg3_pct": (100 * fgm / fga) if fga else np.nan,
            "pir_per_min": (pir_tot / min_tot) if min_tot else np.nan,
            "min_avg": df["MinutesFloat"].mean(),
            "reb_avg": reb.mean() if reb is not None else np.nan,
        }

    rows = [
        _window(logs, "combined_2_seasons"),
        _window(logs.tail(5), "last_5"),
        _window(logs.tail(3), "last_3"),
    ]
    return pd.DataFrame(rows)


def player_vs_rival_lines(
    seasons: list[int],
    player_id: str,
    rival: str,
    *,
    quiet: bool = False,
) -> pd.DataFrame:
    """Selected player's boxscore lines only in games against rival."""
    rival = rival.upper()
    player_id = normalize_player_id(player_id)
    frames = []
    for season in seasons:
        schedule = load_schedule(season)
        cache_path = DATA_DIR / f"player_game_logs_{season}.csv"
        if cache_path.exists():
            logs = fetch_played_boxscores(season, cache=True, quiet=True)
        else:
            played = schedule[schedule["played_bool"]]
            rival_games = played[
                (played["homecode"].astype(str).str.upper() == rival)
                | (played["awaycode"].astype(str).str.upper() == rival)
            ]["game"].astype(int).tolist()
            if not quiet:
                print(f"  fetching {len(rival_games)} {rival} games in {season}")
            logs = fetch_boxscores_for_games(season, rival_games, quiet=quiet)
        if logs.empty:
            continue
        logs = attach_opponent(logs)
        hit = logs[
            (logs["Player_ID"].astype(str) == player_id)
            & (logs["Opponent"].astype(str).str.upper() == rival)
        ].copy()
        if not hit.empty:
            hit["Season"] = season
            frames.append(hit)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values("date")


def games_with_positional_defenders(
    seasons: list[int],
    rival: str,
    position_name: str,
    player_vs_rival: pd.DataFrame,
    *,
    min_defender_minutes: float = 12.0,
    quiet: bool = False,
) -> pd.DataFrame:
    """
    Keep only rival games where at least one same-position likely defender
    logged meaningful minutes (proxy for 'he was guarded by that role').
    """
    if player_vs_rival.empty:
        return player_vs_rival

    annotated = []
    for season, chunk in player_vs_rival.groupby("Season"):
        defenders = likely_positional_defenders(int(season), rival, position_name)
        if "likely_defender" in defenders.columns:
            def_ids = set(defenders.loc[defenders["likely_defender"], "Player_ID"].astype(str))
        else:
            def_ids = set()
        if not def_ids:
            # fall back: any same-position rival with minutes
            same = same_position_rivals(int(season), rival, position_name, active_only=False)
            def_ids = set(same["Player_ID"].astype(str))

        # Need full game boxscores to see if defenders played
        cache_path = DATA_DIR / f"player_game_logs_{season}.csv"
        if cache_path.exists():
            season_logs = fetch_played_boxscores(int(season), cache=True, quiet=True)
        else:
            codes = chunk["Gamecode"].astype(int).unique().tolist()
            season_logs = fetch_boxscores_for_games(int(season), codes, quiet=quiet)

        for _, game in chunk.iterrows():
            gc = int(game["Gamecode"])
            game_rows = season_logs[
                (season_logs["Gamecode"].astype(int) == gc)
                & (season_logs["Team"].astype(str).str.upper() == rival.upper())
                & (season_logs["Player_ID"].astype(str).isin(def_ids))
            ]
            played_defs = game_rows[game_rows["MinutesFloat"].fillna(0) >= min_defender_minutes]
            row = game.copy()
            row["defenders_on_floor"] = ", ".join(
                f"{r.Player} ({r.Minutes})" for _, r in played_defs.iterrows()
            ) if not played_defs.empty else ""
            row["had_positional_defender"] = not played_defs.empty
            annotated.append(row)

    out = pd.DataFrame(annotated)
    return out


def annotate_vs_specific_defenders(
    player_vs_rival: pd.DataFrame,
    rival: str,
    defender_ids: set[str],
    *,
    min_defender_minutes: float = 12.0,
    quiet: bool = True,
) -> pd.DataFrame:
    """
    Annotate player's games vs rival with whether specific defender Player_IDs
    (e.g. next opponent's current same-pos likely defenders) were on the floor.
    """
    if player_vs_rival.empty:
        return player_vs_rival
    rival = rival.upper()
    defender_ids = {normalize_player_id(x) for x in defender_ids}
    annotated = []
    for season, chunk in player_vs_rival.groupby("Season"):
        codes = chunk["Gamecode"].astype(int).unique().tolist()
        season_logs = fetch_boxscores_for_games(int(season), codes, quiet=quiet)
        if season_logs.empty:
            for _, game in chunk.iterrows():
                row = game.copy()
                row["defenders_on_floor"] = ""
                row["had_positional_defender"] = False
                annotated.append(row)
            continue
        season_logs = season_logs.copy()
        season_logs["Player_ID"] = season_logs["Player_ID"].map(normalize_player_id)
        for _, game in chunk.iterrows():
            gc = int(game["Gamecode"])
            game_rows = season_logs[
                (season_logs["Gamecode"].astype(int) == gc)
                & (season_logs["Team"].astype(str).str.upper() == rival)
                & (season_logs["Player_ID"].astype(str).isin(defender_ids))
            ]
            played_defs = game_rows[game_rows["MinutesFloat"].fillna(0) >= min_defender_minutes]
            row = game.copy()
            row["defenders_on_floor"] = (
                ", ".join(f"{r.Player} ({r.Minutes})" for _, r in played_defs.iterrows())
                if not played_defs.empty
                else ""
            )
            row["had_positional_defender"] = not played_defs.empty
            annotated.append(row)
    return pd.DataFrame(annotated)


def fetch_boxscores_for_games(
    season: int,
    gamecodes: list[int],
    *,
    quiet: bool = False,
    merge_into_season_cache: bool = True,
) -> pd.DataFrame:
    """Fetch boxscores for specific gamecodes (rate-limited). Reuses season cache when present."""
    if not gamecodes:
        return pd.DataFrame()

    DATA_DIR.mkdir(exist_ok=True)
    cache_path = DATA_DIR / f"player_game_logs_{season}.csv"
    wanted = {int(g) for g in gamecodes}
    cached = pd.DataFrame()
    have: set[int] = set()
    if cache_path.exists():
        cached = _ensure_derived(pd.read_csv(cache_path, parse_dates=["date"]))
        have = set(cached["Gamecode"].astype(int).unique())
        hit = cached[cached["Gamecode"].astype(int).isin(wanted)]
        missing = wanted - have
        if not missing:
            return hit
        gamecodes = sorted(missing)
        if not quiet:
            print(f"  cache partial {season}: need {len(gamecodes)} more games")
    else:
        hit = pd.DataFrame()

    schedule = load_schedule(season)
    schedule = schedule[schedule["game"].isin(gamecodes)]
    box = BoxScoreData("E")
    frames: list[pd.DataFrame] = []
    for _, game in schedule.iterrows():
        gamecode = int(game["game"])
        try:
            df = _boxscore_with_retry(box, season, gamecode)
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"  skip box {season}/{gamecode}: {exc}")
            continue
        df = df.copy()
        df["date"] = game["date_parsed"]
        df["round"] = game["round"]
        df["home_team"] = game["homecode"]
        df["away_team"] = game["awaycode"]
        frames.append(df)
        if not quiet:
            print(f"  box {season}/{gamecode}: {game['hometeam']} vs {game['awayteam']}")

    fetched = _clean_boxscore(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame()
    if merge_into_season_cache and not fetched.empty:
        if not cached.empty:
            merged_cache = pd.concat([cached, fetched], ignore_index=True)
            merged_cache = merged_cache.drop_duplicates(subset=["Gamecode", "Player_ID", "Team"], keep="last")
        else:
            merged_cache = fetched
        merged_cache.to_csv(cache_path, index=False)
        if not quiet:
            print(f"  updated cache {cache_path} ({len(merged_cache)} rows)")

    parts = [p for p in (hit, fetched) if p is not None and not p.empty]
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    return out[out["Gamecode"].astype(int).isin(wanted)]


def player_clubs_in_season(season: int, player_id: str) -> list[str]:
    """Club codes where the player was registered that season."""
    pid = normalize_player_id(player_id)
    url = f"https://api-live.euroleague.net/v2/competitions/E/seasons/E{season}/people/{pid}"
    time.sleep(REQUEST_SLEEP_SEC)
    r = requests.get(url, timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    clubs = []
    for item in r.json().get("data") or []:
        if item.get("typeName") != "Player" and item.get("type") != "J":
            continue
        code = (item.get("club") or {}).get("code")
        if code:
            clubs.append(str(code).upper())
    return sorted(set(clubs))


def load_team_season_logs(season: int, team: str, *, quiet: bool = False) -> pd.DataFrame:
    """Load all played games for one team in a season (cache-first, rate-limited)."""
    team = team.upper()
    schedule = load_schedule(season)
    played = schedule[schedule["played_bool"]]
    games = played[
        (played["homecode"].astype(str).str.upper() == team)
        | (played["awaycode"].astype(str).str.upper() == team)
    ]["game"].astype(int).tolist()
    if not quiet:
        print(f"  team {team} season {season}: {len(games)} played games")
    logs = fetch_boxscores_for_games(season, games, quiet=quiet)
    if logs.empty:
        return logs
    logs = attach_opponent(logs)
    logs = logs[logs["Team"].astype(str).str.upper() == team].copy()
    logs["Season"] = season
    return logs


def h2h_gamecodes(season: int, team_a: str, team_b: str) -> list[int]:
    """Played gamecodes where team_a faced team_b in a season."""
    a, b = team_a.upper(), team_b.upper()
    schedule = load_schedule(season)
    played = schedule[schedule["played_bool"]]
    home = played["homecode"].astype(str).str.upper()
    away = played["awaycode"].astype(str).str.upper()
    mask = ((home == a) & (away == b)) | ((home == b) & (away == a))
    return played.loc[mask, "game"].astype(int).tolist()


def load_h2h_pair_logs(
    seasons: list[int],
    team_a: str,
    team_b: str,
    *,
    quiet: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load only head-to-head boxscores between two teams across seasons.
    Returns (team_a_rows, team_b_rows) — much cheaper than full-season loads.
    """
    a, b = team_a.upper(), team_b.upper()
    frames_a: list[pd.DataFrame] = []
    frames_b: list[pd.DataFrame] = []
    for season in seasons:
        games = h2h_gamecodes(season, a, b)
        if not quiet:
            print(f"  H2H {a} vs {b} season {season}: {len(games)} games {games}")
        if not games:
            continue
        logs = fetch_boxscores_for_games(season, games, quiet=quiet)
        if logs.empty:
            continue
        logs = attach_opponent(logs)
        logs["Season"] = season
        team_col = logs["Team"].astype(str).str.upper()
        part_a = logs[team_col == a]
        part_b = logs[team_col == b]
        if not part_a.empty:
            frames_a.append(part_a)
        if not part_b.empty:
            frames_b.append(part_b)
    our = pd.concat(frames_a, ignore_index=True) if frames_a else pd.DataFrame()
    them = pd.concat(frames_b, ignore_index=True) if frames_b else pd.DataFrame()
    return our, them


def load_player_logs_across_clubs(
    seasons: list[int],
    player_id: str,
    *,
    quiet: bool = False,
) -> pd.DataFrame:
    """Player game logs for given seasons across any EuroLeague club he was on."""
    pid = normalize_player_id(player_id)
    frames = []
    for season in seasons:
        clubs = player_clubs_in_season(season, pid)
        if not clubs:
            if not quiet:
                print(f"  {pid} not registered in {season}")
            continue
        if not quiet:
            print(f"  {pid} clubs in {season}: {clubs}")
        for club in clubs:
            logs = load_team_season_logs(season, club, quiet=quiet)
            if logs.empty:
                continue
            hit = logs[logs["Player_ID"].astype(str) == pid].copy()
            if not hit.empty:
                frames.append(hit)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["Season", "Gamecode", "Player_ID"], keep="last")
    return out.sort_values("date")


def window_metrics(df: pd.DataFrame, label: str) -> dict:
    """Aggregate PTS / REB / 3PT / PIR / PIR-per-min for a game window."""
    if df is None or df.empty:
        return {
            "window": label,
            "games": 0,
            "min_avg": np.nan,
            "pts_avg": np.nan,
            "reb_avg": np.nan,
            "fgm3_avg": np.nan,
            "fga3_avg": np.nan,
            "fg3_pct": np.nan,
            "pir_avg": np.nan,
            "pir_per_min": np.nan,
        }
    use = df[df["MinutesFloat"].fillna(0) > 0].copy()
    if use.empty:
        return window_metrics(pd.DataFrame(), label)
    min_tot = use["MinutesFloat"].sum()
    pir_tot = use["Valuation"].sum()
    fgm = use["FGM3"].sum()
    fga = use["FGA3"].sum()
    reb = pd.to_numeric(use.get("TotalRebounds"), errors="coerce")
    return {
        "window": label,
        "games": int(len(use)),
        "min_avg": float(use["MinutesFloat"].mean()),
        "pts_avg": float(use["Points"].mean()),
        "reb_avg": float(reb.mean()) if reb is not None else np.nan,
        "fgm3_avg": float(use["FGM3"].mean()),
        "fga3_avg": float(use["FGA3"].mean()),
        "fg3_pct": float(100 * fgm / fga) if fga else np.nan,
        "pir_avg": float(use["Valuation"].mean()),
        "pir_per_min": float(pir_tot / min_tot) if min_tot else np.nan,
    }


def player_windows_from_logs(player_logs: pd.DataFrame) -> pd.DataFrame:
    """Season/combined + last-5 + last-3 windows for one player's game log."""
    if player_logs.empty:
        return pd.DataFrame()
    logs = player_logs.sort_values("date")
    logs = logs[logs["MinutesFloat"].fillna(0) > 0]
    rows = [
        window_metrics(logs, "season_or_combined"),
        window_metrics(logs.tail(5), "last_5"),
        window_metrics(logs.tail(3), "last_3"),
    ]
    return pd.DataFrame(rows)


def team_player_form_table(team_logs: pd.DataFrame) -> pd.DataFrame:
    """
    One row per player with current-season averages and L5 / L3
    for min, pts, reb, 3pt, PIR, PIR/min.
    """
    if team_logs.empty:
        return pd.DataFrame()
    logs = team_logs[team_logs["MinutesFloat"].fillna(0) > 0].copy()
    rows = []
    for pid, grp in logs.groupby("Player_ID"):
        grp = grp.sort_values("date")
        season = window_metrics(grp, "season")
        l5 = window_metrics(grp.tail(5), "last_5")
        l3 = window_metrics(grp.tail(3), "last_3")
        last = grp.iloc[-1]
        rows.append(
            {
                "Player_ID": normalize_player_id(pid),
                "Player": last["Player"],
                "Team": last["Team"],
                "gp": season["games"],
                "min_avg": season["min_avg"],
                "pts_avg": season["pts_avg"],
                "reb_avg": season["reb_avg"],
                "fgm3_avg": season["fgm3_avg"],
                "fga3_avg": season["fga3_avg"],
                "fg3_pct": season["fg3_pct"],
                "pir_avg": season["pir_avg"],
                "pir_per_min": season["pir_per_min"],
                "min_L5": l5["min_avg"],
                "pts_L5": l5["pts_avg"],
                "reb_L5": l5["reb_avg"],
                "fgm3_L5": l5["fgm3_avg"],
                "fga3_L5": l5["fga3_avg"],
                "pir_L5": l5["pir_avg"],
                "ppm_L5": l5["pir_per_min"],
                "min_L3": l3["min_avg"],
                "pts_L3": l3["pts_avg"],
                "reb_L3": l3["reb_avg"],
                "fgm3_L3": l3["fgm3_avg"],
                "fga3_L3": l3["fga3_avg"],
                "pir_L3": l3["pir_avg"],
                "ppm_L3": l3["pir_per_min"],
            }
        )
    out = pd.DataFrame(rows)
    return out.sort_values("min_avg", ascending=False)


def attach_opponent(logs: pd.DataFrame) -> pd.DataFrame:
    """Add Opponent team code from home/away columns."""
    out = logs.copy()
    out["Opponent"] = np.where(out["Team"] == out["home_team"], out["away_team"], out["home_team"])
    out["HomeAway"] = np.where(out["Team"] == out["home_team"], "H", "A")
    return out


def resolve_player(logs: pd.DataFrame, query: str) -> pd.DataFrame:
    """Match players by id or case-insensitive name substring."""
    q = query.strip()
    by_id = logs[logs["Player_ID"].astype(str) == normalize_player_id(q)]
    if not by_id.empty:
        return by_id[["Player_ID", "Player", "Team"]].drop_duplicates()
    mask = logs["Player"].astype(str).str.contains(q, case=False, na=False)
    return logs.loc[mask, ["Player_ID", "Player", "Team"]].drop_duplicates()
