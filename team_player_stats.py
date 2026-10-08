"""
Team / player form stats with minimal API traffic.

1) Team (current season):
   each player -> season avg + last-5 + last-3
   (min, pts, reb, 3PM/3PA, PIR, PIR/min)

2) Optional --player:
   same windows but across THIS + LAST season,
   regardless of which EuroLeague club the player was on.

Uses on-disk cache and rate-limited boxscore pulls (avoids 429 spam).
"""

from __future__ import annotations

import argparse

import pandas as pd

from euro_utils import (
    DATA_DIR,
    load_player_logs_across_clubs,
    load_team_season_logs,
    player_windows_from_logs,
    resolve_player,
    team_player_form_table,
)

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_colwidth", 28)


def _round_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_float_dtype(out[c]):
            out[c] = out[c].round(2)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Team + player form stats (cache-friendly)")
    parser.add_argument("--season", type=int, default=2026, help="Current season start year")
    parser.add_argument("--team", type=str, required=True, help="Team code, e.g. PAR, PRS, OLY")
    parser.add_argument(
        "--player",
        type=str,
        default=None,
        help="Optional player name/id: expand to this+last season across all clubs",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    season = args.season
    team = args.team.upper()
    prev = season - 1

    print(f"\n=== {team} current season ({season}) player form ===")
    print("Columns: season averages + last 5 + last 3 (if enough games)")
    team_logs = load_team_season_logs(season, team, quiet=False)
    if team_logs.empty:
        print("No team logs available.")
        return

    team_form = team_player_form_table(team_logs)
    show_cols = [
        "Player",
        "gp",
        "min_avg",
        "pts_avg",
        "reb_avg",
        "fgm3_avg",
        "fga3_avg",
        "fg3_pct",
        "pir_avg",
        "pir_per_min",
        "pts_L5",
        "reb_L5",
        "fgm3_L5",
        "fga3_L5",
        "pir_L5",
        "ppm_L5",
        "pts_L3",
        "reb_L3",
        "fgm3_L3",
        "fga3_L3",
        "pir_L3",
        "ppm_L3",
    ]
    show_cols = [c for c in show_cols if c in team_form.columns]
    print(_round_frame(team_form[show_cols]).to_string(index=False))
    out_team = DATA_DIR / f"team_form_{season}_{team}.csv"
    team_form.to_csv(out_team, index=False)
    print(f"\nSaved {out_team}")

    if not args.player:
        return

    matches = resolve_player(team_logs, args.player)
    if matches.empty:
        print(f"\nNo player match for '{args.player}' on {team} this season.")
        return
    if len(matches) > 1:
        print("\nMultiple players matched - tighten --player:")
        print(matches.to_string(index=False))
        return

    player_id = str(matches.iloc[0]["Player_ID"])
    player_name = matches.iloc[0]["Player"]
    print(f"\n=== Selected player: {player_name} ({player_id}) ===")
    print(f"This + last season ({season}, {prev}), any EuroLeague club")

    cross = load_player_logs_across_clubs([season, prev], player_id, quiet=False)
    if cross.empty:
        print("No cross-season game logs found.")
        return

    # Per-season breakdown + combined
    print("\n--- By season (all clubs) ---")
    for s, chunk in cross.groupby("Season"):
        clubs = sorted(chunk["Team"].astype(str).unique())
        print(f"\nSeason {s} clubs={clubs}")
        print(_round_frame(player_windows_from_logs(chunk)).to_string(index=False))

    print("\n--- Combined this + last season ---")
    combined = player_windows_from_logs(cross)
    print(_round_frame(combined).to_string(index=False))

    recent = cross.tail(10)
    recent_cols = [
        c
        for c in [
            "Season",
            "date",
            "Team",
            "Opponent",
            "Minutes",
            "Points",
            "TotalRebounds",
            "FGM3",
            "FGA3",
            "Valuation",
            "PIR_per_min",
        ]
        if c in recent.columns
    ]
    print("\n--- Last 10 games (any club) ---")
    print(recent[recent_cols].to_string(index=False))

    safe = "".join(ch if ch.isalnum() else "_" for ch in player_name)
    cross.to_csv(DATA_DIR / f"player_cross_{safe}_{prev}_{season}.csv", index=False)
    combined.to_csv(DATA_DIR / f"player_windows_{safe}_{prev}_{season}.csv", index=False)
    print(f"\nSaved data/player_cross_{safe}_{prev}_{season}.csv")


if __name__ == "__main__":
    main()
