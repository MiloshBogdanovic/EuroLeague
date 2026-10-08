"""
Team-under-coach analysis + player form / matchup explorer.

Entities are (team, coach): if a club changes coach mid-season, that stretch is
treated as a different entity while still being filterable by team code.

Reports:
  1) Roster efficiency: PIR, minutes, PIR/min
  2) Recent form: points + 3PT over last 3 / last 5 vs season avg and last-season avg
  3) Selected player form: combine this + last season → overall / L5 / L3
     (PTS, 3PT, PIR/min). Vs a rival: YOUR lines in those games (not opponent stats),
     preferring games where same-position likely defenders were on the floor.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from euro_utils import (
    DATA_DIR,
    attach_opponent,
    fetch_game_metadata,
    fetch_played_boxscores,
    games_with_positional_defenders,
    likely_positional_defenders,
    load_logs_for_seasons,
    load_schedule,
    player_game_log,
    player_position,
    player_vs_rival_lines,
    resolve_player,
    season_player_averages,
    summarize_player_windows,
)

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_colwidth", 28)


def build_coach_map(metadata: pd.DataFrame) -> pd.DataFrame:
    """Long table: one row per team-game with its coach."""
    rows = []
    for _, g in metadata.iterrows():
        rows.append(
            {
                "Season": g["Season"],
                "Gamecode": int(g["Gamecode"]),
                "Team": g["CodeTeamA"],
                "Coach": str(g["CoachA"]).strip(),
                "IsHome": True,
            }
        )
        rows.append(
            {
                "Season": g["Season"],
                "Gamecode": int(g["Gamecode"]),
                "Team": g["CodeTeamB"],
                "Coach": str(g["CoachB"]).strip(),
                "IsHome": False,
            }
        )
    return pd.DataFrame(rows)


def coach_eras(coach_map: pd.DataFrame, logs: pd.DataFrame) -> pd.DataFrame:
    """Attach coach + entity id to each player-game row; detect era stretches."""
    merged = logs.merge(coach_map[["Gamecode", "Team", "Coach"]], on=["Gamecode", "Team"], how="left")
    merged["Coach"] = merged["Coach"].fillna("UNKNOWN")
    merged["entity"] = merged["Team"].astype(str) + " | " + merged["Coach"].astype(str)

    # Era index within team: increments when coach changes chronologically
    team_games = (
        merged[["Team", "Gamecode", "date", "Coach", "entity"]]
        .drop_duplicates(["Team", "Gamecode"])
        .sort_values(["Team", "date", "Gamecode"])
    )
    team_games["coach_changed"] = team_games.groupby("Team")["Coach"].transform(
        lambda s: s.ne(s.shift(1)).fillna(True)
    )
    team_games["era_id"] = team_games.groupby("Team")["coach_changed"].cumsum().astype(int)
    merged = merged.merge(
        team_games[["Team", "Gamecode", "era_id"]],
        on=["Team", "Gamecode"],
        how="left",
    )
    return merged


def list_entities(merged: pd.DataFrame) -> pd.DataFrame:
    summary = (
        merged.groupby(["entity", "Team", "Coach", "era_id"], as_index=False)
        .agg(
            games=("Gamecode", "nunique"),
            first_game=("date", "min"),
            last_game=("date", "max"),
            players=("Player_ID", "nunique"),
        )
        .sort_values(["Team", "first_game"])
    )
    return summary


def roster_efficiency(merged: pd.DataFrame, entity: str | None, team: str | None) -> pd.DataFrame:
    subset = merged.copy()
    if entity:
        subset = subset[subset["entity"] == entity]
    elif team:
        subset = subset[subset["Team"].str.upper() == team.upper()]

    subset = subset[subset["MinutesFloat"].fillna(0) > 0]
    agg = (
        subset.groupby(["Player_ID", "Player", "Team", "Coach", "entity"], as_index=False)
        .agg(
            gp=("Gamecode", "nunique"),
            min_tot=("MinutesFloat", "sum"),
            min_avg=("MinutesFloat", "mean"),
            pts_avg=("Points", "mean"),
            pir_avg=("Valuation", "mean"),
            fgm3_avg=("FGM3", "mean"),
            fga3_avg=("FGA3", "mean"),
            start_rate=("IsStarter", "mean"),
        )
    )
    agg["pir_per_min"] = agg["pir_avg"] / agg["min_avg"].replace(0, np.nan)
    agg["fg3_pct"] = np.where(agg["fga3_avg"] > 0, 100 * agg["fgm3_avg"] / agg["fga3_avg"], np.nan)
    return agg.sort_values("pir_per_min", ascending=False)


def rolling_form_vs_baselines(
    merged: pd.DataFrame,
    season: int,
    entity: str | None,
    team: str | None,
    last_season_avgs: pd.DataFrame | None,
) -> pd.DataFrame:
    """Last-3 / last-5 PTS + 3PT vs current-season and last-season averages."""
    subset = merged.copy()
    if entity:
        subset = subset[subset["entity"] == entity]
    elif team:
        subset = subset[subset["Team"].str.upper() == team.upper()]

    subset = subset.sort_values(["Player_ID", "date"])
    rows = []
    for pid, grp in subset.groupby("Player_ID"):
        grp = grp.sort_values("date")
        if grp.empty:
            continue
        last = grp.iloc[-1]
        last3 = grp.tail(3)
        last5 = grp.tail(5)
        season_slice = grp

        def _avg(df: pd.DataFrame, col: str) -> float:
            return float(df[col].mean()) if len(df) else np.nan

        row = {
            "Player_ID": pid,
            "Player": last["Player"],
            "Team": last["Team"],
            "Coach": last["Coach"],
            "entity": last["entity"],
            "gp": len(grp),
            "min_L3": _avg(last3, "MinutesFloat"),
            "pts_L3": _avg(last3, "Points"),
            "fgm3_L3": _avg(last3, "FGM3"),
            "fga3_L3": _avg(last3, "FGA3"),
            "pir_L3": _avg(last3, "Valuation"),
            "ppm_L3": _avg(last3, "PIR_per_min"),
            "min_L5": _avg(last5, "MinutesFloat"),
            "pts_L5": _avg(last5, "Points"),
            "fgm3_L5": _avg(last5, "FGM3"),
            "fga3_L5": _avg(last5, "FGA3"),
            "pir_L5": _avg(last5, "Valuation"),
            "ppm_L5": _avg(last5, "PIR_per_min"),
            "pts_season": _avg(season_slice, "Points"),
            "fgm3_season": _avg(season_slice, "FGM3"),
            "fga3_season": _avg(season_slice, "FGA3"),
            "pir_season": _avg(season_slice, "Valuation"),
            "ppm_season": _avg(season_slice, "PIR_per_min"),
            "min_season": _avg(season_slice, "MinutesFloat"),
        }
        row["pts_L3_vs_season"] = row["pts_L3"] - row["pts_season"]
        row["pts_L5_vs_season"] = row["pts_L5"] - row["pts_season"]
        row["fgm3_L3_vs_season"] = row["fgm3_L3"] - row["fgm3_season"]
        row["fgm3_L5_vs_season"] = row["fgm3_L5"] - row["fgm3_season"]
        row["pir_L3_vs_season"] = row["pir_L3"] - row["pir_season"]
        row["pir_L5_vs_season"] = row["pir_L5"] - row["pir_season"]

        if last_season_avgs is not None and not last_season_avgs.empty:
            prev = last_season_avgs[last_season_avgs["Player_ID"].astype(str) == str(pid)]
            if not prev.empty:
                p = prev.iloc[0]
                row["pts_last_season"] = p["pts_avg"]
                row["fgm3_last_season"] = p["fgm3_avg"]
                row["fga3_last_season"] = p["fga3_avg"]
                row["pir_last_season"] = p["pir_avg"]
                row["ppm_last_season"] = p["pir_per_min_avg"]
                row["pts_L3_vs_last"] = row["pts_L3"] - p["pts_avg"]
                row["pts_L5_vs_last"] = row["pts_L5"] - p["pts_avg"]
                row["fgm3_L3_vs_last"] = row["fgm3_L3"] - p["fgm3_avg"]
                row["fgm3_L5_vs_last"] = row["fgm3_L5"] - p["fgm3_avg"]
                row["pir_L3_vs_last"] = row["pir_L3"] - p["pir_avg"]
                row["pir_L5_vs_last"] = row["pir_L5"] - p["pir_avg"]
            else:
                for c in [
                    "pts_last_season",
                    "fgm3_last_season",
                    "fga3_last_season",
                    "pir_last_season",
                    "ppm_last_season",
                    "pts_L3_vs_last",
                    "pts_L5_vs_last",
                    "fgm3_L3_vs_last",
                    "fgm3_L5_vs_last",
                    "pir_L3_vs_last",
                    "pir_L5_vs_last",
                ]:
                    row[c] = np.nan
        rows.append(row)

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("pir_L5_vs_season")


def next_rival(schedule: pd.DataFrame, team: str) -> tuple[str | None, pd.Series | None]:
    """Return next unplayed opponent code for team, plus the schedule row."""
    team = team.upper()
    upcoming = schedule[
        (~schedule["played_bool"])
        & ((schedule["homecode"] == team) | (schedule["awaycode"] == team))
    ].sort_values("date_parsed")
    if upcoming.empty:
        return None, None
    row = upcoming.iloc[0]
    rival = row["awaycode"] if row["homecode"] == team else row["homecode"]
    return str(rival), row


def player_vs_rival(
    seasons: list[int],
    player_id: str,
    rival: str,
    *,
    quiet: bool = False,
) -> pd.DataFrame:
    """Historical boxscore lines for a player against a rival team code."""
    rival = rival.upper()
    player_id = str(player_id)
    frames = []
    for season in seasons:
        schedule = load_schedule(season)
        cache_path = DATA_DIR / f"player_game_logs_{season}.csv"
        if cache_path.exists():
            if not quiet:
                print(f"  using cache for rival history: {season}")
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
        ]
        if not hit.empty:
            hit = hit.copy()
            hit["Season"] = season
            frames.append(hit)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True).sort_values("date")
    return out


def summarize_matchup(history: pd.DataFrame) -> pd.DataFrame:
    if history.empty:
        return history
    summary = {
        "games": len(history),
        "min_avg": history["MinutesFloat"].mean(),
        "pts_avg": history["Points"].mean(),
        "fgm3_avg": history["FGM3"].mean(),
        "fga3_avg": history["FGA3"].mean(),
        "fg3_pct": 100 * history["FGM3"].sum() / history["FGA3"].sum()
        if history["FGA3"].sum()
        else np.nan,
        "pir_avg": history["Valuation"].mean(),
        "pir_per_min": history["Valuation"].sum() / history["MinutesFloat"].sum()
        if history["MinutesFloat"].sum()
        else np.nan,
        "start_rate": history["IsStarter"].mean(),
    }
    return pd.DataFrame([summary])


def pick_entity(entities: pd.DataFrame, team: str, coach: str | None) -> str | None:
    team_ents = entities[entities["Team"].str.upper() == team.upper()]
    if team_ents.empty:
        return None
    if coach:
        hit = team_ents[team_ents["Coach"].str.contains(coach, case=False, na=False)]
        if hit.empty:
            return None
        # Prefer most recent era if multiple partial matches
        return hit.sort_values("last_game").iloc[-1]["entity"]
    # Default: most recent coach era for the team
    return team_ents.sort_values("last_game").iloc[-1]["entity"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Team-under-coach + player matchup analysis")
    parser.add_argument("--season", type=int, default=2026, help="Current season start year")
    parser.add_argument("--team", type=str, required=True, help="Team code, e.g. OLY, MAD, PAN")
    parser.add_argument("--coach", type=str, default=None, help="Optional coach name filter")
    parser.add_argument("--list-entities", action="store_true", help="List team|coach entities and exit")
    parser.add_argument("--player", type=str, default=None, help="Player name substring or Player_ID")
    parser.add_argument("--vs", type=str, default=None, help="Rival team code for matchup history")
    parser.add_argument(
        "--next-rival",
        action="store_true",
        help="Use the team's next unplayed opponent as rival",
    )
    parser.add_argument(
        "--history-seasons",
        type=str,
        default=None,
        help="Comma seasons for player form + rival games, default: current,last",
    )
    parser.add_argument("--max-games", type=int, default=None, help="Limit games (smoke test)")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    team = args.team.upper()
    season = args.season
    prev_season = season - 1

    print(f"\n=== Loading {season} boxscores + coaches for {team} ===")
    logs = fetch_played_boxscores(season, max_games=args.max_games, cache=True)
    if logs.empty:
        print("No boxscore data.")
        return
    logs = attach_opponent(logs)

    meta = fetch_game_metadata(season, cache=True)
    coach_map = build_coach_map(meta)
    merged = coach_eras(coach_map, logs)
    entities = list_entities(merged)

    print("\n=== Team | Coach entities (this season) ===")
    team_entities = entities[entities["Team"].str.upper() == team]
    print(team_entities.to_string(index=False) if not team_entities.empty else f"No games for {team}")
    entities.to_csv(DATA_DIR / f"coach_entities_{season}.csv", index=False)

    if args.list_entities:
        print("\n=== All entities ===")
        print(entities.to_string(index=False))
        return

    entity = pick_entity(entities, team, args.coach)
    if entity is None:
        print(f"Could not resolve entity for team={team} coach={args.coach}")
        return
    print(f"\nUsing entity: {entity}")

    print("\n=== Roster efficiency (PIR / min) ===")
    eff = roster_efficiency(merged, entity=entity, team=None)
    show_eff = eff[
        [
            "Player",
            "gp",
            "min_avg",
            "pts_avg",
            "pir_avg",
            "pir_per_min",
            "fgm3_avg",
            "fga3_avg",
            "fg3_pct",
            "start_rate",
        ]
    ]
    print(show_eff.to_string(index=False))
    eff.to_csv(DATA_DIR / f"efficiency_{season}_{team}.csv", index=False)

    print(f"\n=== Loading last-season ({prev_season}) averages ===")
    try:
        last_avgs = season_player_averages(prev_season)
    except Exception as exc:  # noqa: BLE001
        print(f"  last-season averages unavailable: {exc}")
        last_avgs = None

    print("\n=== Form: L3 / L5 PTS + 3PT vs season & last-season avg ===")
    form = rolling_form_vs_baselines(merged, season, entity=entity, team=None, last_season_avgs=last_avgs)
    form_cols = [
        "Player",
        "gp",
        "pts_L3",
        "pts_L5",
        "pts_season",
        "pts_L3_vs_season",
        "pts_L5_vs_season",
        "pts_last_season",
        "pts_L3_vs_last",
        "pts_L5_vs_last",
        "fgm3_L3",
        "fgm3_L5",
        "fgm3_season",
        "fgm3_L3_vs_season",
        "fgm3_L5_vs_season",
        "fgm3_last_season",
        "fgm3_L3_vs_last",
        "fgm3_L5_vs_last",
        "pir_L3",
        "pir_L5",
        "pir_season",
        "ppm_L5",
        "ppm_season",
    ]
    form_cols = [c for c in form_cols if c in form.columns]
    # Focus on rotation pieces
    form_view = form[form["min_season"].fillna(0) >= 8][form_cols]
    print(form_view.to_string(index=False))
    form.to_csv(DATA_DIR / f"form_vs_baselines_{season}_{team}.csv", index=False)

    # --- Player vs rival ---
    rival = args.vs.upper() if args.vs else None
    schedule = load_schedule(season)
    next_row = None
    if args.next_rival or (args.player and not rival):
        auto_rival, next_row = next_rival(schedule, team)
        if args.next_rival:
            rival = auto_rival
            if next_row is not None:
                print(
                    f"\nNext rival for {team}: {rival} on {next_row['date']} "
                    f"({next_row['hometeam']} vs {next_row['awayteam']})"
                )

    if args.player:
        matches = resolve_player(merged[merged["Team"].str.upper() == team], args.player)
        if matches.empty:
            matches = resolve_player(merged, args.player)
        if matches.empty:
            print(f"\nNo player match for '{args.player}'")
            return
        if len(matches) > 1:
            print("\nMultiple players matched - pick one with a tighter --player value:")
            print(matches.to_string(index=False))
            return

        player_id = str(matches.iloc[0]["Player_ID"])
        player_name = matches.iloc[0]["Player"]
        print(f"\nSelected player: {player_name} ({player_id})")

        if args.history_seasons:
            form_seasons = [int(x) for x in args.history_seasons.split(",")]
        else:
            form_seasons = [season, season - 1]

        print(f"\n=== Player form (seasons {form_seasons} combined) ===")
        print("Windows: combined average, last 5 games, last 3 games -> PTS / 3PT / PIR per minute")
        all_logs = load_logs_for_seasons(form_seasons, team=team, quiet=False)
        plog = player_game_log(all_logs, player_id)
        if plog.empty:
            print("No game logs found for this player across those seasons.")
        else:
            windows = summarize_player_windows(plog)
            print(windows.round(2).to_string(index=False))
            recent = plog.tail(8)[
                [c for c in ["Season", "date", "Opponent", "Minutes", "Points", "FGM3", "FGA3", "Valuation", "PIR_per_min", "TotalRebounds"] if c in plog.columns]
            ]
            print("\nRecent games (up to 8):")
            print(recent.to_string(index=False))
            safe_name = "".join(ch if ch.isalnum() else "_" for ch in player_name)
            windows.to_csv(DATA_DIR / f"form_windows_{safe_name}.csv", index=False)
            plog.to_csv(DATA_DIR / f"player_logs_{safe_name}_{form_seasons[0]}_{form_seasons[-1]}.csv", index=False)

        if not rival:
            auto_rival, next_row = next_rival(schedule, team)
            rival = auto_rival
            if next_row is not None:
                print(
                    f"\nAuto next rival: {rival} on {next_row['date']} "
                    f"({next_row['hometeam']} vs {next_row['awayteam']})"
                )

        if not rival:
            print("No rival resolved (pass --vs CODE or --next-rival).")
            return

        focal = player_position(season, team, player_id)
        if focal is None:
            focal = player_position(season - 1, team, player_id)
        if focal is None:
            print(f"Could not resolve position for {player_name} on {team}.")
            return

        print(
            f"\n=== Likely same-position defenders on {rival} "
            f"({focal['positionName']}) - heuristic, not official ==="
        )
        print(
            "EuroLeague has no defender flag. We rank same-position players by "
            "start rate + minutes + steals/blocks/dreb per minute (who is most "
            "likely to guard him). Showing names only - not their scoring."
        )
        for s in form_seasons:
            defs = likely_positional_defenders(s, rival, focal["positionName"])
            if defs.empty:
                print(f"  {s}: no same-position defenders found")
                continue
            show = defs.copy()
            for c in ["min_avg", "start_rate", "stl_avg", "blk_avg", "dreb_avg", "defender_score"]:
                if c in show.columns:
                    show[c] = pd.to_numeric(show[c], errors="coerce").round(2)
            flagged = show[show["likely_defender"]] if "likely_defender" in show.columns else show.head(3)
            print(f"\n  Season {s} likely defenders:")
            cols = [c for c in ["Player", "min_avg", "start_rate", "stl_avg", "blk_avg", "dreb_avg", "defender_score"] if c in flagged.columns]
            print(flagged[cols].to_string(index=False))

        print(
            f"\n=== YOUR performance vs {rival} "
            f"(games in {form_seasons}) - selected player lines only ==="
        )
        # Prefer already-loaded player log (avoids re-fetching whole rival schedule)
        vs_lines = pd.DataFrame()
        if not plog.empty and "Opponent" in plog.columns:
            vs_lines = plog[plog["Opponent"].astype(str).str.upper() == rival.upper()].copy()
        if vs_lines.empty:
            vs_lines = player_vs_rival_lines(form_seasons, player_id, rival, quiet=False)
        if vs_lines.empty:
            print(
                f"No games found for {player_name} against {rival} in {form_seasons}. "
                "Form windows above are still valid; matchup history will fill in after they play."
            )
            return

        annotated = games_with_positional_defenders(
            form_seasons, rival, focal["positionName"], vs_lines, quiet=False
        )
        cols = [
            c
            for c in [
                "Season",
                "date",
                "HomeAway",
                "Minutes",
                "Points",
                "FGM3",
                "FGA3",
                "Valuation",
                "PIR_per_min",
                "TotalRebounds",
                "had_positional_defender",
                "defenders_on_floor",
            ]
            if c in annotated.columns
        ]
        print(annotated[cols].to_string(index=False))

        print("\n--- Averages: all games vs rival ---")
        print(summarize_player_windows(vs_lines).round(2).to_string(index=False))

        guarded = (
            annotated[annotated["had_positional_defender"].astype(bool)]
            if "had_positional_defender" in annotated.columns
            else annotated
        )
        if not guarded.empty and len(guarded) < len(annotated):
            print("\n--- Averages: only games with same-pos likely defender on floor ---")
            print(summarize_player_windows(guarded).round(2).to_string(index=False))
        elif not guarded.empty:
            print("\n(All listed games had a same-position likely defender available.)")

        safe_name = "".join(ch if ch.isalnum() else "_" for ch in player_name)
        annotated.to_csv(DATA_DIR / f"my_lines_vs_{rival}_{safe_name}.csv", index=False)
        print(f"\nSaved data/my_lines_vs_{rival}_{safe_name}.csv")


if __name__ == "__main__":
    main()
