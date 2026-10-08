"""
Explore EuroLeague player form, minutes, and rotation signals.

Uses euroleague-api (Georgios Giasemidis) — same data family as the shot-chart article.
Useful building blocks for an underperformance / form model:
  - game-level minutes + Valuation (PIR)
  - starter vs bench (rotation)
  - rolling form vs season baseline
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from euroleague_api.boxscore_data import BoxScoreData
from euroleague_api.player_stats import PlayerStats
from euroleague_api.schedule import Schedule

DATA_DIR = Path(__file__).resolve().parent / "data"


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


def fetch_played_boxscores(season: int, max_games: int | None = None) -> pd.DataFrame:
    """Pull player boxscores for all played games in a season."""
    schedule = load_schedule(season)
    played = schedule[schedule["played_bool"]].sort_values("date_parsed")
    if max_games is not None:
        played = played.head(max_games)

    box = BoxScoreData("E")
    frames: list[pd.DataFrame] = []
    for _, game in played.iterrows():
        gamecode = int(game["game"])
        try:
            df = box.get_players_boxscore_stats(season, gamecode)
        except Exception as exc:  # noqa: BLE001 - API can 404 mid-season
            print(f"  skip {season}/{gamecode}: {exc}")
            continue
        df = df.copy()
        df["date"] = game["date_parsed"]
        df["round"] = game["round"]
        df["home_team"] = game["homecode"]
        df["away_team"] = game["awaycode"]
        frames.append(df)
        print(f"  fetched {season} game {gamecode} ({game['hometeam']} vs {game['awayteam']})")

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    # Drop team totals / empty rows
    out = out[out["Player"].notna() & (out["Player"].str.lower() != "total") & (out["Player"].str.lower() != "team")]
    out["MinutesFloat"] = out["Minutes"].map(parse_minutes)
    out["IsStarter"] = out["IsStarter"].fillna(0).astype(int)
    return out


def season_averages(season: int) -> pd.DataFrame:
    ps = PlayerStats("E")
    trad = ps.get_player_stats_single_season("traditional", season, statistic_mode="PerGame")
    return trad[
        [
            "player.code",
            "player.name",
            "player.team.code",
            "gamesPlayed",
            "minutesPlayed",
            "pointsScored",
            "assists",
            "totalRebounds",
            "pir",
        ]
    ].rename(
        columns={
            "player.code": "Player_ID",
            "player.name": "Player",
            "player.team.code": "Team",
            "gamesPlayed": "gp_avg",
            "minutesPlayed": "min_avg",
            "pointsScored": "pts_avg",
            "assists": "ast_avg",
            "totalRebounds": "reb_avg",
            "pir": "pir_avg",
        }
    )


def build_form_table(game_logs: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    """Rolling minutes / Valuation vs recent games — core features for form models."""
    logs = game_logs.sort_values(["Player_ID", "date"]).copy()
    g = logs.groupby("Player_ID", group_keys=False)

    logs["pir_roll"] = g["Valuation"].apply(lambda s: s.shift(1).rolling(window, min_periods=2).mean())
    logs["min_roll"] = g["MinutesFloat"].apply(lambda s: s.shift(1).rolling(window, min_periods=2).mean())
    logs["start_rate_roll"] = g["IsStarter"].apply(lambda s: s.shift(1).rolling(window, min_periods=2).mean())
    logs["pir_season_to_date"] = g["Valuation"].apply(lambda s: s.shift(1).expanding(min_periods=2).mean())
    logs["min_season_to_date"] = g["MinutesFloat"].apply(lambda s: s.shift(1).expanding(min_periods=2).mean())

    # Simple underperformance labels for prototyping (not production targets)
    logs["pir_vs_roll"] = logs["Valuation"] - logs["pir_roll"]
    logs["min_vs_roll"] = logs["MinutesFloat"] - logs["min_roll"]
    logs["underperformed"] = (
        (logs["pir_vs_roll"] < -4) & (logs["MinutesFloat"] >= 12) & logs["pir_roll"].notna()
    ).astype(int)
    return logs


def latest_form_snapshot(form: pd.DataFrame, n_players: int = 25) -> pd.DataFrame:
    """Latest game per player with form context — who looks cold / minutes dropping."""
    latest = form.sort_values("date").groupby("Player_ID", as_index=False).tail(1)
    latest = latest[latest["MinutesFloat"].fillna(0) >= 10].copy()
    latest["form_gap"] = latest["pir_roll"] - latest["pir_season_to_date"]
    latest["minutes_trend"] = latest["min_roll"] - latest["min_season_to_date"]
    return latest.sort_values("form_gap").head(n_players)[
        [
            "date",
            "Team",
            "Player",
            "MinutesFloat",
            "Valuation",
            "IsStarter",
            "pir_roll",
            "pir_season_to_date",
            "form_gap",
            "min_roll",
            "minutes_trend",
            "start_rate_roll",
        ]
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="EuroLeague player form explorer")
    parser.add_argument("--season", type=int, default=2025, help="Season start year, e.g. 2025")
    parser.add_argument("--max-games", type=int, default=None, help="Limit games fetched (smoke test)")
    parser.add_argument("--window", type=int, default=5, help="Rolling form window in games")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)

    print(f"\n=== Season averages ({args.season}) ===")
    averages = season_averages(args.season)
    print(averages.sort_values("pir_avg", ascending=False).head(12).to_string(index=False))
    averages.to_csv(DATA_DIR / f"player_averages_{args.season}.csv", index=False)

    print(f"\n=== Fetching played boxscores ({args.season}) ===")
    logs = fetch_played_boxscores(args.season, max_games=args.max_games)
    if logs.empty:
        print("No boxscore rows returned.")
        return

    logs.to_csv(DATA_DIR / f"player_game_logs_{args.season}.csv", index=False)
    print(f"Saved {len(logs)} player-game rows")

    form = build_form_table(logs, window=args.window)
    form.to_csv(DATA_DIR / f"player_form_{args.season}.csv", index=False)

    print(f"\n=== Cold form / soft minutes (last game, {args.window}-game window) ===")
    snap = latest_form_snapshot(form)
    print(snap.to_string(index=False))
    snap.to_csv(DATA_DIR / f"form_snapshot_{args.season}.csv", index=False)

    # Quick rotation view for one recent team
    recent_team = form.sort_values("date").iloc[-1]["Team"]
    recent_date = form["date"].max()
    rotation = form[(form["Team"] == recent_team) & (form["date"] == recent_date)].sort_values(
        "MinutesFloat", ascending=False
    )
    print(f"\n=== Sample rotation: {recent_team} on {recent_date.date()} ===")
    print(
        rotation[["Player", "IsStarter", "Minutes", "Points", "Valuation", "Plusminus"]].to_string(
            index=False
        )
    )


if __name__ == "__main__":
    main()
