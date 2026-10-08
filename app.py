"""
EuroLeague form explorer — Streamlit UI.

Run:
  .\\.venv\\Scripts\\Activate.ps1
  python -m streamlit run app.py
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime

import pandas as pd
import streamlit as st

from euro_utils import (
    DATA_DIR,
    annotate_vs_specific_defenders,
    filter_player_vs_opponent,
    likely_positional_defenders,
    load_h2h_pair_logs,
    load_player_logs_across_clubs,
    load_schedule,
    load_team_season_logs,
    next_rival,
    normalize_player_id,
    player_clubs_in_season,
    player_position,
    player_windows_from_logs,
    team_player_form_table,
)

st.set_page_config(
    page_title="EuroLeague Form",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

DATA_DIR.mkdir(exist_ok=True)
LOG_PATH = DATA_DIR / "app.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
    force=True,
)
log = logging.getLogger("euro_app")

TEAM_LABELS = {
    "ASV": "ASVEL",
    "BAR": "Barcelona",
    "BAS": "Baskonia",
    "BES": "Besiktas",
    "DUB": "Dubai",
    "HTA": "Hapoel Tel Aviv",
    "IST": "Anadolu Efes",
    "MAD": "Real Madrid",
    "MIL": "Olimpia Milano",
    "MUN": "Bayern Munich",
    "OLY": "Olympiacos",
    "PAM": "Valencia",
    "PAN": "Panathinaikos",
    "PAR": "Partizan",
    "PRS": "Paris",
    "RED": "Crvena Zvezda",
    "TEL": "Maccabi Tel Aviv",
    "ULK": "Fenerbahce",
    "VIR": "Virtus Bologna",
    "ZAL": "Zalgiris",
}

TEAM_FORM_COLS = [
    ("Player", "Player"),
    ("gp", "GP"),
    ("min_avg", "Min"),
    ("pts_avg", "PTS"),
    ("reb_avg", "REB"),
    ("fgm3_avg", "3PM"),
    ("fga3_avg", "3PA"),
    ("fg3_pct", "3P%"),
    ("pir_avg", "PIR"),
    ("pir_per_min", "PIR/min"),
    ("pts_L5", "PTS L5"),
    ("reb_L5", "REB L5"),
    ("fgm3_L5", "3PM L5"),
    ("pir_L5", "PIR L5"),
    ("ppm_L5", "PIR/min L5"),
    ("pts_L3", "PTS L3"),
    ("reb_L3", "REB L3"),
    ("fgm3_L3", "3PM L3"),
    ("pir_L3", "PIR L3"),
    ("ppm_L3", "PIR/min L3"),
]


def debug_enabled() -> bool:
    return bool(st.session_state.get("debug_mode", False))


def ui_log(message: str, level: str = "info") -> None:
    if level == "error":
        log.error(message)
    else:
        log.info(message)
    if not debug_enabled():
        return
    line = f"{datetime.now().strftime('%H:%M:%S')} | {message}"
    st.session_state.setdefault("ui_logs", []).append(line)


def _round(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_float_dtype(out[c]):
            out[c] = out[c].round(2)
    return out


def _team_options(season: int) -> list[str]:
    sched = load_schedule(season)
    codes = sorted(set(sched["homecode"].astype(str)) | set(sched["awaycode"].astype(str)))
    return codes


def _label(code: str) -> str:
    name = TEAM_LABELS.get(code, code)
    return f"{code} - {name}"


@st.cache_data(show_spinner=False)
def cached_team_logs(season: int, team: str) -> pd.DataFrame:
    return load_team_season_logs(season, team, quiet=True)


@st.cache_data(show_spinner=False)
def cached_h2h_pair(seasons: tuple[int, ...], team_a: str, team_b: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    return load_h2h_pair_logs(list(seasons), team_a, team_b, quiet=True)


@st.cache_data(show_spinner=False)
def cached_player_cross(season: int, player_id: str) -> pd.DataFrame:
    pid = normalize_player_id(player_id)
    return load_player_logs_across_clubs([season, season - 1], pid, quiet=True)


def render_team_table(form: pd.DataFrame) -> None:
    cols = [c for c, _ in TEAM_FORM_COLS if c in form.columns]
    view = _round(form[cols]).rename(columns=dict(TEAM_FORM_COLS))
    st.dataframe(view, width="stretch", hide_index=True, height=420)


def render_window_bars(windows: pd.DataFrame, title: str) -> None:
    if windows.empty:
        st.info("No window stats.")
        return
    w = _round(windows.set_index("window"))
    metrics = [c for c in ["pts_avg", "reb_avg", "fgm3_avg", "pir_avg", "pir_per_min", "min_avg"] if c in w.columns]
    labels = {
        "pts_avg": "Points",
        "reb_avg": "Rebounds",
        "fgm3_avg": "3PM",
        "pir_avg": "PIR",
        "pir_per_min": "PIR/min",
        "min_avg": "Minutes",
    }
    st.markdown(f"**{title}**")
    chart_df = w[metrics].rename(columns=labels)
    order = [i for i in ["season_or_combined", "last_5", "last_3"] if i in chart_df.index]
    chart_df = chart_df.loc[order]
    chart_df.index = chart_df.index.map(
        {
            "season_or_combined": "Season / combined",
            "last_5": "Last 5",
            "last_3": "Last 3",
        }
    )
    st.bar_chart(chart_df, height=280)


def render_game_trend(logs: pd.DataFrame) -> None:
    if logs.empty:
        return
    g = logs.sort_values("date").copy()
    g["label"] = g["date"].dt.strftime("%m-%d") + " vs " + g["Opponent"].astype(str)
    plot = g.set_index("label")[["Points", "Valuation", "PIR_per_min"]].rename(
        columns={"Valuation": "PIR", "PIR_per_min": "PIR/min"}
    )
    st.markdown("**Game-by-game trend**")
    st.line_chart(plot, height=300)


def render_logs_panel() -> None:
    if not debug_enabled():
        return
    with st.expander("Debug logs", expanded=True):
        st.caption(f"Also written to `{LOG_PATH}`")
        logs = st.session_state.get("ui_logs") or ["No log lines yet."]
        st.code("\n".join(logs[-80:]), language="text")


def _top_leaders(form: pd.DataFrame, metric: str, n: int = 3, min_minutes: float = 8.0) -> pd.DataFrame:
    """Top-n players by a season metric (rotation filter)."""
    if form.empty or metric not in form.columns:
        return pd.DataFrame()
    use = form[form["min_avg"].fillna(0) >= min_minutes].copy()
    if use.empty:
        use = form.copy()
    cols = ["Player", "gp", "min_avg", metric]
    if metric == "fgm3_avg" and "fga3_avg" in use.columns:
        cols.append("fga3_avg")
    cols = [c for c in cols if c in use.columns]
    return _round(use.sort_values(metric, ascending=False).head(n)[cols])


def _h2h_player_form(h2h_logs: pd.DataFrame) -> pd.DataFrame:
    """Per-player averages limited to H2H games only."""
    if h2h_logs.empty:
        return pd.DataFrame()
    use = h2h_logs[h2h_logs["MinutesFloat"].fillna(0) > 0].copy()
    if use.empty:
        return pd.DataFrame()
    rows = []
    for pid, grp in use.groupby("Player_ID"):
        reb = pd.to_numeric(grp.get("TotalRebounds"), errors="coerce")
        rows.append(
            {
                "Player_ID": normalize_player_id(pid),
                "Player": grp.iloc[-1].get("Player"),
                "gp": int(len(grp)),
                "min_avg": float(grp["MinutesFloat"].mean()),
                "pts_avg": float(grp["Points"].mean()),
                "reb_avg": float(reb.mean()) if reb is not None else float("nan"),
                "fgm3_avg": float(grp["FGM3"].mean()),
                "fga3_avg": float(grp["FGA3"].mean()),
                "pts_tot": float(grp["Points"].sum()),
                "reb_tot": float(reb.sum()) if reb is not None else float("nan"),
                "fgm3_tot": float(grp["FGM3"].sum()),
            }
        )
    return pd.DataFrame(rows)


def _h2h_top_leaders(form: pd.DataFrame, metric: str, n: int = 3) -> pd.DataFrame:
    """Top-n from H2H-only form (no season-minutes filter — sample is already small)."""
    if form.empty or metric not in form.columns:
        return pd.DataFrame()
    cols = ["Player", "gp", "min_avg", metric]
    if metric == "fgm3_avg" and "fga3_avg" in form.columns:
        cols.append("fga3_avg")
    cols = [c for c in cols if c in form.columns]
    return _round(form.sort_values(metric, ascending=False).head(n)[cols])


def _render_category_leaders(
    title: str,
    form: pd.DataFrame,
    *,
    h2h: bool = False,
) -> None:
    st.markdown(f"**{title}**")
    if form.empty:
        st.caption("No player lines.")
        return
    pick = _h2h_top_leaders if h2h else _top_leaders
    st.caption("Points")
    st.dataframe(pick(form, "pts_avg"), width="stretch", hide_index=True)
    st.caption("3-pointers made")
    st.dataframe(pick(form, "fgm3_avg"), width="stretch", hide_index=True)
    st.caption("Rebounds")
    st.dataframe(pick(form, "reb_avg"), width="stretch", hide_index=True)


def render_season_top3(form: pd.DataFrame, season: int, team_code: str) -> None:
    st.markdown(f"**Top 3 this season ({season})**")
    st.caption(f"Season averages for {_label(team_code)} (rotation ≥8 min).")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.caption("Points")
        st.dataframe(_top_leaders(form, "pts_avg"), width="stretch", hide_index=True)
    with c2:
        st.caption("3-pointers made")
        st.dataframe(_top_leaders(form, "fgm3_avg"), width="stretch", hide_index=True)
    with c3:
        st.caption("Rebounds")
        st.dataframe(_top_leaders(form, "reb_avg"), width="stretch", hide_index=True)


def _current_roster_ids(season: int, team: str, form: pd.DataFrame | None = None) -> set[str]:
    """Player_IDs who appear in this team's current-season boxscores."""
    if form is not None and not form.empty and "Player_ID" in form.columns:
        return {normalize_player_id(x) for x in form["Player_ID"] if normalize_player_id(x)}
    logs = cached_team_logs(season, team)
    if logs.empty or "Player_ID" not in logs.columns:
        return set()
    return {normalize_player_id(x) for x in logs["Player_ID"] if normalize_player_id(x)}


def _filter_h2h_to_roster(h2h_form: pd.DataFrame, roster_ids: set[str]) -> pd.DataFrame:
    if h2h_form.empty or not roster_ids:
        return pd.DataFrame()
    ids = h2h_form["Player_ID"].map(normalize_player_id)
    return h2h_form[ids.isin(roster_ids)].copy()


def render_h2h_block(season: int, team_code: str, our_form: pd.DataFrame) -> None:
    st.divider()
    st.subheader("H2H vs next opponent")

    try:
        sched = load_schedule(season)
        rival, next_row = next_rival(sched, team_code)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not resolve next opponent: {exc}")
        ui_log(f"H2H next_rival failed: {exc}", "error")
        return

    if not rival:
        st.info("No upcoming unplayed game found for this team.")
        return

    if next_row is not None:
        st.info(
            f"Next game: {next_row.get('date')} · "
            f"{next_row.get('hometeam')} vs {next_row.get('awayteam')} "
            f"(opponent `{rival}`)"
        )

    prev = season - 1
    with st.spinner(f"Loading H2H vs {_label(rival)} ({prev}/{season})..."):
        try:
            our_roster = _current_roster_ids(season, team_code, our_form)
            opp_roster = _current_roster_ids(season, rival)
            our_h2h_logs, opp_h2h_logs = cached_h2h_pair((prev, season), team_code, rival)
            our_h2h_form = _filter_h2h_to_roster(_h2h_player_form(our_h2h_logs), our_roster)
            opp_h2h_form = _filter_h2h_to_roster(_h2h_player_form(opp_h2h_logs), opp_roster)
            n_games = (
                our_h2h_logs["Gamecode"].nunique()
                if not our_h2h_logs.empty
                else 0
            )
            ui_log(
                f"H2H pair {team_code} vs {rival}: games={n_games} "
                f"roster_kept us={len(our_h2h_form)} them={len(opp_h2h_form)}"
            )
        except Exception as exc:  # noqa: BLE001
            ui_log(f"H2H pair load failed: {exc}\n{traceback.format_exc()}", "error")
            st.warning(f"H2H history unavailable: {exc}")
            return

    if our_h2h_form.empty and opp_h2h_form.empty:
        st.caption(
            f"No H2H lines for current {season} roster players "
            f"between {team_code} and {rival} in {prev}/{season}."
        )
        return

    st.markdown(f"**Top 3 vs each other ({prev} + {season} meetings)**")
    st.caption("Averages in games between these teams — current-season roster only.")
    left, right = st.columns(2)
    with left:
        _render_category_leaders(_label(team_code), our_h2h_form, h2h=True)
    with right:
        _render_category_leaders(_label(rival), opp_h2h_form, h2h=True)


def main() -> None:
    st.title("EuroLeague player form")
    st.caption(
        "Team form, player this+last season, then vs next opponent and vs that "
        "opponent's same-position likely defenders (your lines only)."
    )

    with st.sidebar:
        st.header("Controls")
        season = st.number_input("Season start year", min_value=2018, max_value=2030, value=2026, step=1)
        try:
            codes = _team_options(int(season))
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not load schedule: {exc}")
            ui_log(f"schedule error: {exc}", "error")
            render_logs_panel()
            return

        default_idx = codes.index("PAR") if "PAR" in codes else 0
        team = st.selectbox(
            "Team",
            options=codes,
            index=default_idx,
            format_func=_label,
        )
        load = st.button("Load team", type="primary", use_container_width=True)
        clear = st.button("Clear cache", use_container_width=True)
        st.toggle("Debug mode", value=False, key="debug_mode")
        if clear:
            st.cache_data.clear()
            st.session_state.pop("team_form", None)
            st.session_state.pop("player_cross", None)
            st.session_state.pop("player_id", None)
            st.session_state.pop("matchup", None)
            st.session_state["ui_logs"] = []
            ui_log("Streamlit cache + session player/team state cleared")
            st.success("Cache cleared.")

    if load or (
        "team_form" in st.session_state and st.session_state.get("loaded_key") == (int(season), team)
    ):
        key = (int(season), team)
        if load or st.session_state.get("loaded_key") != key:
            with st.spinner(f"Loading {team} {season} (cache-first)..."):
                try:
                    ui_log(f"Loading team logs season={season} team={team}")
                    logs = cached_team_logs(int(season), team)
                    ui_log(
                        f"Team logs rows={len(logs)} games={logs['Gamecode'].nunique() if not logs.empty else 0}"
                    )
                except Exception as exc:  # noqa: BLE001
                    ui_log(f"Team load failed: {exc}\n{traceback.format_exc()}", "error")
                    st.error(f"Failed to load team logs: {exc}")
                    render_logs_panel()
                    return
            if logs.empty:
                st.warning("No played games / boxscores for this team yet.")
                ui_log("Team logs empty")
                render_logs_panel()
                return
            form = team_player_form_table(logs)
            form["Player_ID"] = form["Player_ID"].map(normalize_player_id)
            st.session_state["team_logs"] = logs
            st.session_state["team_form"] = form
            st.session_state["loaded_key"] = key
            # reset player detail when team changes
            st.session_state.pop("player_cross", None)
            st.session_state.pop("player_id", None)
            st.session_state.pop("matchup", None)
            form.to_csv(DATA_DIR / f"team_form_{season}_{team}.csv", index=False)
            ui_log(f"Team form players={len(form)}")

    if "team_form" not in st.session_state:
        st.info("Pick a team and click **Load team**.")
        render_logs_panel()
        return

    form: pd.DataFrame = st.session_state["team_form"]
    logs: pd.DataFrame = st.session_state["team_logs"]
    season_i, team_code = st.session_state["loaded_key"]

    st.subheader(f"{_label(team_code)} - {season_i} season form")
    st.markdown(
        "Season columns are full-season averages. **L5 / L3** are the same metrics over the "
        "last 5 / last 3 games (with only 2 games played, L5 ≈ L3 ≈ season)."
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Players", len(form))
    c2.metric("Games in sample", int(logs["Gamecode"].nunique()))
    top = form.sort_values("pir_per_min", ascending=False).iloc[0]
    c3.metric("Best PIR/min", f"{top['pir_per_min']:.2f}", top["Player"])
    c4.metric("Best PTS", f"{form['pts_avg'].max():.1f}", form.loc[form["pts_avg"].idxmax(), "Player"])

    metric = st.selectbox(
        "Sort / chart metric",
        options=["pir_per_min", "pts_avg", "reb_avg", "fgm3_avg", "min_avg", "pir_avg"],
        format_func=lambda x: {
            "pir_per_min": "PIR / min",
            "pts_avg": "Points",
            "reb_avg": "Rebounds",
            "fgm3_avg": "3PM",
            "min_avg": "Minutes",
            "pir_avg": "PIR",
        }[x],
    )
    sorted_form = form.sort_values(metric, ascending=False)
    render_team_table(sorted_form)
    render_season_top3(form, season_i, team_code)

    rot = sorted_form[sorted_form["min_avg"] >= 10].head(12).copy()
    if not rot.empty and "pts_L5" in rot.columns:
        st.markdown("**Season PTS vs last-5 PTS (rotation, >=10 min)**")
        cmp = rot.set_index("Player")[["pts_avg", "pts_L5"]].rename(
            columns={"pts_avg": "Season PTS", "pts_L5": "Last 5 PTS"}
        )
        st.bar_chart(cmp, height=320)

    render_h2h_block(season_i, team_code, form)

    st.divider()
    st.subheader("Player detail - this + last season (any club)")

    players = sorted(form["Player"].astype(str).unique())
    player_name = st.selectbox("Player", options=players)
    row = form[form["Player"] == player_name].iloc[0]
    raw_id = row["Player_ID"]
    player_id = normalize_player_id(raw_id)
    st.caption(f"Player_ID raw=`{raw_id!r}` normalized=`{player_id}`")

    if st.button("Load player history", type="primary"):
        ui_log(f"Load player history clicked name={player_name} id={player_id} season={season_i}")
        with st.spinner(f"Loading {player_name} for {season_i} and {season_i - 1} across clubs..."):
            try:
                clubs_now = player_clubs_in_season(season_i, player_id)
                clubs_prev = player_clubs_in_season(season_i - 1, player_id)
                ui_log(f"Clubs {season_i}: {clubs_now}")
                ui_log(f"Clubs {season_i - 1}: {clubs_prev}")
                # Drop bad cached empties for this key by clearing then refetching
                cached_player_cross.clear()
                cross = cached_player_cross(season_i, player_id)
                ui_log(
                    f"Player cross rows={len(cross)} "
                    f"seasons={sorted(cross['Season'].unique().tolist()) if not cross.empty else []}"
                )
                st.session_state["player_cross"] = cross
                st.session_state["player_id"] = player_id
                st.session_state["player_name"] = player_name
                st.session_state.pop("matchup", None)
                if cross.empty:
                    st.warning(
                        "No game logs returned. Enable **Debug mode** in the sidebar "
                        "if you need details (bad Player_ID / registration lookup is usual)."
                    )
                else:
                    st.success(f"Loaded {len(cross)} games for {player_name}.")
            except Exception as exc:  # noqa: BLE001
                ui_log(f"Player load failed: {exc}\n{traceback.format_exc()}", "error")
                st.error(f"Failed to load player history: {exc}")
                render_logs_panel()
                return

    render_logs_panel()

    if st.session_state.get("player_id") != player_id:
        st.caption("Click **Load player history** to pull this + last season (uses cache when available).")
        return

    cross: pd.DataFrame = st.session_state.get("player_cross", pd.DataFrame())
    if cross is None or cross.empty:
        st.warning("No game logs found for this player across those seasons.")
        return

    clubs = sorted(cross["Team"].astype(str).unique())
    st.write(f"**{player_name}** · clubs in sample: {', '.join(clubs)} · {len(cross)} games")

    m1, m2, m3, m4 = st.columns(4)
    combined = player_windows_from_logs(cross)
    comb_row = combined[combined["window"] == "season_or_combined"].iloc[0]
    l3_row = combined[combined["window"] == "last_3"].iloc[0]
    m1.metric("Combined PTS", f"{comb_row['pts_avg']:.1f}")
    m2.metric("Combined PIR/min", f"{comb_row['pir_per_min']:.2f}")
    m3.metric("Last 3 PTS", f"{l3_row['pts_avg']:.1f}")
    m4.metric("Last 3 PIR/min", f"{l3_row['pir_per_min']:.2f}")

    left, right = st.columns(2)
    with left:
        render_window_bars(combined, "Combined this + last season")
        st.dataframe(_round(combined), width="stretch", hide_index=True)
    with right:
        for s, chunk in cross.groupby("Season"):
            st.markdown(f"Season **{s}** clubs={sorted(chunk['Team'].astype(str).unique())}")
            st.dataframe(
                _round(player_windows_from_logs(chunk)),
                width="stretch",
                hide_index=True,
            )

    render_game_trend(cross.tail(25))

    recent_cols = [
        c
        for c in [
            "Season",
            "date",
            "Team",
            "Opponent",
            "HomeAway",
            "Minutes",
            "Points",
            "TotalRebounds",
            "FGM3",
            "FGA3",
            "Valuation",
            "PIR_per_min",
        ]
        if c in cross.columns
    ]
    st.markdown("**Recent games**")
    st.dataframe(
        _round(cross.sort_values("date", ascending=False)[recent_cols].head(20)),
        width="stretch",
        hide_index=True,
    )

    # --- Vs next / selected opponent ---
    st.divider()
    st.subheader("Vs opponent (your lines only)")
    st.markdown(
        "Uses this + last season games already loaded. Opponent defaults to the **next "
        "unplayed** schedule game; you can override. Defender matchup uses same-position "
        "likely defenders on that opponent (heuristic: minutes / starts / steals-blocks-dreb)."
    )

    try:
        sched = load_schedule(season_i)
        auto_rival, next_row = next_rival(sched, team_code)
    except Exception as exc:  # noqa: BLE001
        auto_rival, next_row = None, None
        ui_log(f"next_rival failed: {exc}", "error")

    try:
        opp_options = _team_options(season_i)
    except Exception:
        opp_options = sorted(TEAM_LABELS.keys())

    default_opp = auto_rival if auto_rival in opp_options else (opp_options[0] if opp_options else team_code)
    if next_row is not None and auto_rival:
        st.info(
            f"Next game: {next_row.get('date')} · "
            f"{next_row.get('hometeam')} vs {next_row.get('awayteam')} "
            f"(rival `{auto_rival}`)"
        )

    rival = st.selectbox(
        "Opponent",
        options=opp_options,
        index=opp_options.index(default_opp) if default_opp in opp_options else 0,
        format_func=_label,
        key="opponent_select",
    )

    if st.button("Analyze vs opponent", type="primary"):
        ui_log(f"Analyze vs opponent player={player_name} rival={rival}")
        with st.spinner(f"Building matchup vs {rival}..."):
            try:
                vs_team = filter_player_vs_opponent(cross, rival)
                ui_log(f"vs {rival} games from player history: {len(vs_team)}")

                focal = player_position(season_i, team_code, player_id)
                if focal is None:
                    focal = player_position(season_i - 1, team_code, player_id)
                pos_name = (focal or {}).get("positionName") or "Guard"
                ui_log(f"player position={pos_name}")

                defs = likely_positional_defenders(season_i, rival, pos_name)
                if defs.empty:
                    defs = likely_positional_defenders(season_i - 1, rival, pos_name)
                def_ids = set()
                if not defs.empty and "likely_defender" in defs.columns:
                    def_ids = set(
                        defs.loc[defs["likely_defender"], "Player_ID"].map(normalize_player_id)
                    )
                ui_log(f"likely defenders ({rival} {pos_name}): {sorted(def_ids)}")

                annotated = pd.DataFrame()
                if not vs_team.empty and def_ids:
                    annotated = annotate_vs_specific_defenders(
                        vs_team, rival, def_ids, quiet=True
                    )
                elif not vs_team.empty:
                    annotated = vs_team.copy()
                    annotated["had_positional_defender"] = False
                    annotated["defenders_on_floor"] = ""

                st.session_state["matchup"] = {
                    "rival": rival,
                    "pos_name": pos_name,
                    "vs_team": vs_team,
                    "defs": defs,
                    "annotated": annotated,
                }
                st.success(f"Matchup ready vs {_label(rival)}")
            except Exception as exc:  # noqa: BLE001
                ui_log(f"matchup failed: {exc}\n{traceback.format_exc()}", "error")
                st.error(f"Matchup failed: {exc}")

    matchup = st.session_state.get("matchup")
    if not matchup or matchup.get("rival") != rival:
        st.caption("Click **Analyze vs opponent** after choosing the rival.")
        render_logs_panel()
        return

    vs_team: pd.DataFrame = matchup["vs_team"]
    defs: pd.DataFrame = matchup["defs"]
    annotated: pd.DataFrame = matchup["annotated"]
    pos_name = matchup["pos_name"]

    st.markdown(f"### Your lines vs {_label(rival)}")
    if vs_team.empty:
        st.warning(
            f"No games for {player_name} against {rival} in the loaded this+last season sample."
        )
    else:
        vs_windows = player_windows_from_logs(vs_team)
        v1, v2, v3, v4 = st.columns(4)
        comb = vs_windows[vs_windows["window"] == "season_or_combined"].iloc[0]
        v1.metric("Games vs opp", int(comb["games"]))
        v2.metric("PTS vs opp", f"{comb['pts_avg']:.1f}")
        v3.metric("PIR/min vs opp", f"{comb['pir_per_min']:.2f}")
        v4.metric("3PM vs opp", f"{comb['fgm3_avg']:.1f}")
        left_m, right_m = st.columns(2)
        with left_m:
            render_window_bars(vs_windows, f"Windows vs {rival}")
            st.dataframe(_round(vs_windows), width="stretch", hide_index=True)
        with right_m:
            show = vs_team.sort_values("date", ascending=False)
            show_cols = [
                c
                for c in [
                    "Season",
                    "date",
                    "Team",
                    "HomeAway",
                    "Minutes",
                    "Points",
                    "TotalRebounds",
                    "FGM3",
                    "FGA3",
                    "Valuation",
                    "PIR_per_min",
                ]
                if c in show.columns
            ]
            st.markdown("**Game log vs opponent**")
            st.dataframe(_round(show[show_cols]), width="stretch", hide_index=True)

    st.markdown(f"### Same-position likely defenders on {_label(rival)} ({pos_name})")
    st.caption(
        "Not official defense ratings — ranked by start rate, minutes, steals/blocks/dreb. "
        "Names only; below is how **you** played when those players were on the floor."
    )
    if defs is None or defs.empty:
        st.info("Could not resolve likely defenders for this opponent/position.")
    else:
        dshow = defs.copy()
        for c in ["min_avg", "start_rate", "stl_avg", "blk_avg", "dreb_avg", "defender_score"]:
            if c in dshow.columns:
                dshow[c] = pd.to_numeric(dshow[c], errors="coerce").round(2)
        flagged = dshow[dshow["likely_defender"]] if "likely_defender" in dshow.columns else dshow.head(3)
        st.dataframe(
            flagged[
                [
                    c
                    for c in [
                        "Player",
                        "min_avg",
                        "start_rate",
                        "stl_avg",
                        "blk_avg",
                        "dreb_avg",
                        "defender_score",
                        "likely_defender",
                    ]
                    if c in flagged.columns
                ]
            ],
            width="stretch",
            hide_index=True,
        )

    st.markdown("### Your lines when those defenders were on the floor")
    if annotated is None or annotated.empty:
        st.info("No annotated defender games (need vs-opponent history first).")
    elif "had_positional_defender" not in annotated.columns:
        st.info("No defender annotation available.")
    else:
        guarded = annotated[annotated["had_positional_defender"].astype(bool)]
        g1, g2 = st.columns(2)
        with g1:
            st.markdown(f"**All games vs {rival}** ({len(annotated)})")
            st.dataframe(
                _round(player_windows_from_logs(annotated)),
                width="stretch",
                hide_index=True,
            )
        with g2:
            st.markdown(f"**With likely defenders on floor** ({len(guarded)})")
            if guarded.empty:
                st.warning(
                    "None of the current likely defenders appeared with 12+ min "
                    "in your historical games vs this team (roster turnover or thin sample)."
                )
            else:
                st.dataframe(
                    _round(player_windows_from_logs(guarded)),
                    width="stretch",
                    hide_index=True,
                )

        ann_cols = [
            c
            for c in [
                "Season",
                "date",
                "Minutes",
                "Points",
                "FGM3",
                "Valuation",
                "PIR_per_min",
                "had_positional_defender",
                "defenders_on_floor",
            ]
            if c in annotated.columns
        ]
        st.markdown("**Annotated game log**")
        st.dataframe(
            _round(annotated.sort_values("date", ascending=False)[ann_cols]),
            width="stretch",
            hide_index=True,
        )

    render_logs_panel()


if __name__ == "__main__":
    main()
