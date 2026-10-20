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

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st

from euro_admiralbet import (
    STAT_LABELS,
    fetch_euroleague_player_totals,
    is_streamlit_cloud,
    load_admiralbet_lines,
    match_player_lines,
    playwright_ready,
)
from euro_market_compare import (
    BOOK_ADMIRAL,
    COMPARE_PATH,
    DEFAULT_B365_MARGIN,
    DEFAULT_CLOSE_LINE_TOL,
    MASSIVE_UNDERCUT,
    available_compare_books,
    board_player_summary,
    build_comparison,
    comparison_cache_is_fresh,
    comparison_table_df,
    decorate_flags,
    load_comparison,
    load_props_board,
    save_comparison,
)
from euro_oddspapi import (
    DEFAULT_BOOKS,
    UNDER_CAPABLE_BOOKS,
    api_ready as oddspapi_ready,
    book_label,
    book_note,
    board_books,
    load_oddspapi_board,
    load_oddspapi_meta,
    odds_by_book_rows,
    refresh_euroleague_props,
    selectable_book_slugs,
)
from euro_utils import (
    DATA_DIR,
    annotate_vs_specific_defenders,
    current_gameday,
    filter_player_vs_opponent,
    likely_positional_defenders,
    load_h2h_pair_logs,
    load_player_logs_across_clubs,
    load_round_game_reports,
    load_schedule,
    load_team_season_logs,
    next_rival,
    normalize_player_id,
    player_clubs_in_season,
    player_position,
    player_windows_from_logs,
    schedule_for_gameday,
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


@st.cache_data(show_spinner=False, ttl=300)
def cached_schedule(season: int) -> pd.DataFrame:
    return load_schedule(season)


@st.cache_data(show_spinner=False, ttl=300)
def cached_round_reports(season: int, gameday: int) -> pd.DataFrame:
    return load_round_game_reports(season, gameday)


def render_round_board(season: int, focus_team: str | None = None) -> None:
    """Current gameday fixtures + latest results at the top of the page."""
    try:
        sched = cached_schedule(int(season))
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not load schedule for round board: {exc}")
        return

    gday = current_gameday(sched)
    if gday is None:
        st.info("No gamedays found in schedule.")
        return

    focus = (focus_team or "").upper() or None
    round_games = schedule_for_gameday(sched, int(gday))
    upcoming = round_games[~round_games["played_bool"]].copy() if not round_games.empty else pd.DataFrame()
    played_now = round_games[round_games["played_bool"]].copy() if not round_games.empty else pd.DataFrame()

    results_gday = int(gday)
    results = pd.DataFrame()
    if not played_now.empty:
        try:
            results = cached_round_reports(int(season), results_gday)
        except Exception:  # noqa: BLE001
            results = pd.DataFrame()
        if results.empty:
            # Fall back to schedule rows without scores
            results = played_now
    else:
        prev = int(gday) - 1
        while prev >= 1:
            prev_games = schedule_for_gameday(sched, prev)
            if not prev_games.empty and prev_games["played_bool"].any():
                results_gday = prev
                try:
                    results = cached_round_reports(int(season), results_gday)
                except Exception:  # noqa: BLE001
                    results = pd.DataFrame()
                if results.empty:
                    results = prev_games[prev_games["played_bool"]].copy()
                break
            prev -= 1

    st.subheader(f"Round {gday}")
    st.caption("Current EuroLeague gameday — upcoming tip-offs and latest final scores.")

    left, right = st.columns(2)
    with left:
        st.markdown(f"**Upcoming · Round {gday}**")
        if upcoming.empty:
            st.caption("No remaining fixtures in this round.")
        else:
            rows = []
            for _, g in upcoming.iterrows():
                home = str(g.get("homecode", "")).upper()
                away = str(g.get("awaycode", "")).upper()
                tip = f"{g.get('date', '')} {g.get('startime', '')}".strip()
                mark = " ★" if focus and focus in {home, away} else ""
                rows.append(
                    {
                        "When": tip,
                        "Home": f"{home} · {_label(home).split(' - ', 1)[-1]}{mark if focus == home else ''}",
                        "Away": f"{away} · {_label(away).split(' - ', 1)[-1]}{mark if focus == away else ''}",
                        "Game": int(g["game"]) if pd.notna(g.get("game")) else "",
                    }
                )
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    with right:
        label = f"**Results · Round {results_gday}**"
        if results_gday != int(gday):
            label += " _(previous)_"
        st.markdown(label)
        if results is None or results.empty:
            st.caption("No results available yet.")
        else:
            rows = []
            for _, g in results.iterrows():
                home = str(g.get("homecode", g.get("local.club.code", ""))).upper()
                away = str(g.get("awaycode", g.get("road.club.code", ""))).upper()
                hs = g.get("home_score", g.get("local.score"))
                as_ = g.get("away_score", g.get("road.score"))
                if pd.isna(hs) or pd.isna(as_):
                    score = "—"
                else:
                    score = f"{int(hs)}–{int(as_)}"
                home_name = g.get("home_short") or TEAM_LABELS.get(home, home)
                away_name = g.get("away_short") or TEAM_LABELS.get(away, away)
                mark_h = " ★" if focus == home else ""
                mark_a = " ★" if focus == away else ""
                rows.append(
                    {
                        "Home": f"{home} · {home_name}{mark_h}",
                        "Score": score,
                        "Away": f"{away} · {away_name}{mark_a}",
                    }
                )
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.divider()


def render_team_table(form: pd.DataFrame) -> None:
    cols = [c for c, _ in TEAM_FORM_COLS if c in form.columns]
    view = _round(form[cols]).rename(columns=dict(TEAM_FORM_COLS))
    st.dataframe(view, width="stretch", hide_index=True, height=420)


FORM_METRICS = (
    ("MinutesFloat", "Minutes", "MIN"),
    ("Points", "Points", "PTS"),
    ("FGM3", "3PM", "3PM"),
    ("Assists", "Assists", "AST"),
    ("TotalRebounds", "Rebounds", "REB"),
)


def render_window_bars(windows: pd.DataFrame, title: str) -> None:
    if windows.empty:
        st.info("No window stats.")
        return
    w = _round(windows.set_index("window"))
    metrics = [c for c in ["pts_avg", "reb_avg", "ast_avg", "fgm3_avg", "pir_avg", "pir_per_min", "min_avg"] if c in w.columns]
    labels = {
        "pts_avg": "Points",
        "reb_avg": "Rebounds",
        "ast_avg": "Assists",
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


def _played_logs(logs: pd.DataFrame) -> pd.DataFrame:
    if logs is None or logs.empty:
        return pd.DataFrame()
    out = logs.copy()
    if "Assists" not in out.columns and "Assistances" in out.columns:
        out["Assists"] = pd.to_numeric(out["Assistances"], errors="coerce")
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
    if "MinutesFloat" in out.columns:
        out = out[out["MinutesFloat"].fillna(0) > 0]
    return out.sort_values("date")


def _baseline_avgs(logs: pd.DataFrame) -> dict[str, float]:
    use = _played_logs(logs)
    if use.empty:
        return {col: float("nan") for col, _, _ in FORM_METRICS}
    out = {}
    for col, _, _ in FORM_METRICS:
        if col in use.columns:
            out[col] = float(pd.to_numeric(use[col], errors="coerce").mean())
        else:
            out[col] = float("nan")
    return out


def _recent_games(logs: pd.DataFrame, n: int) -> pd.DataFrame:
    use = _played_logs(logs).tail(n).copy()
    if use.empty:
        return use
    use["label"] = use["date"].dt.strftime("%m-%d") + "\nvs " + use["Opponent"].astype(str)
    return use


def _fmt_base(v: float) -> str:
    return f"{v:.1f}" if pd.notna(v) else "—"


def _plot_games_vs_baseline(
    games: pd.DataFrame,
    col: str,
    label: str,
    baseline: float,
    baseline_name: str,
    market_line: float | None = None,
) -> None:
    """One chart: each match value + baseline (+ optional AdmiralBet line)."""
    if games.empty or col not in games.columns or pd.isna(baseline):
        st.caption(f"No data for {label}.")
        return

    vals = pd.to_numeric(games[col], errors="coerce").tolist()
    labels = games["label"].tolist()
    finite = [v for v in vals if pd.notna(v)]
    y_pad = (max(finite) * 0.12) if finite else 0.5
    x = np.arange(len(vals))
    ref = float(market_line) if market_line is not None and pd.notna(market_line) else baseline
    colors = []
    for v in vals:
        if pd.isna(v):
            colors.append("#888888")
        elif v > ref:
            colors.append("#2ecc71")
        elif v < ref:
            colors.append("#e74c3c")
        else:
            colors.append("#95a5a6")

    fig, ax = plt.subplots(figsize=(6.8, 3.4), dpi=120)
    ax.set_facecolor("#0e1117")
    fig.patch.set_facecolor("#0e1117")
    bars = ax.bar(x, [0.0 if pd.isna(v) else float(v) for v in vals], color=colors, width=0.65, zorder=2)
    ax.axhline(
        baseline,
        color="#f1c40f",
        linestyle="--",
        linewidth=1.8,
        zorder=3,
        label=f"{baseline_name}: {baseline:.1f}",
    )
    if market_line is not None and pd.notna(market_line):
        ax.axhline(
            float(market_line),
            color="#3498db",
            linestyle=":",
            linewidth=2.0,
            zorder=4,
            label=f"AdmiralBet O/U: {float(market_line):.1f}",
        )
    for bar, v in zip(bars, vals):
        if pd.isna(v):
            continue
        delta = float(v) - ref
        tag = "OVER" if delta > 0 else ("UNDER" if delta < 0 else "FLAT")
        val_txt = f"{v:.1f}" if label == "Minutes" else f"{v:.0f}"
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            float(v) + y_pad * 0.35,
            f"{val_txt}\n{tag}",
            ha="center",
            va="bottom",
            fontsize=8,
            color="#fafafa",
        )
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8, color="#fafafa")
    ax.set_ylabel(label, color="#fafafa")
    title = f"{label} — each game vs {baseline_name}"
    if market_line is not None and pd.notna(market_line):
        title += " · blue = market line"
    ax.set_title(title, color="#fafafa", fontsize=11)
    ax.tick_params(colors="#fafafa")
    refs = [baseline, *finite]
    if market_line is not None and pd.notna(market_line):
        refs.append(float(market_line))
    ymax = max(refs + [1]) + y_pad
    ax.set_ylim(0, ymax)
    for spine in ax.spines.values():
        spine.set_color("#444")
    ax.legend(facecolor="#1a1a1a", edgecolor="#444", labelcolor="#fafafa", fontsize=8)
    ax.grid(axis="y", color="#333", linestyle=":", linewidth=0.6, zorder=1)
    fig.tight_layout()
    st.pyplot(fig, clear_figure=True)
    plt.close(fig)


def _baseline_metric_row(title: str, games_n: int, baseline: dict[str, float]) -> None:
    st.markdown(f"**{title}** · {games_n} games")
    cols = st.columns(len(FORM_METRICS))
    for widget, (col, _label, short) in zip(cols, FORM_METRICS):
        widget.metric(short, _fmt_base(baseline.get(col)))


def _metric_game_table(
    games: pd.DataFrame,
    col: str,
    label: str,
    short: str,
    baseline: float,
) -> pd.DataFrame:
    rows = []
    for _, g in games.iterrows():
        val = float(pd.to_numeric(g.get(col), errors="coerce")) if col in g.index else float("nan")
        if pd.notna(val) and pd.notna(baseline):
            delta = val - baseline
            vs = f"{delta:+.1f} {'OVER' if delta > 0 else ('UNDER' if delta < 0 else 'FLAT')}"
        else:
            vs = "—"
        rows.append(
            {
                "Game": str(g["label"]).replace("\n", " "),
                label: val,
                f"{short} vs BL": vs,
            }
        )
    return _round(pd.DataFrame(rows))


def render_player_form_vs_baselines(
    season: int,
    this_season_logs: pd.DataFrame,
    combined_logs: pd.DataFrame,
    player_name: str | None = None,
    team_hint: str | None = None,
) -> None:
    """Highlight two baselines, AdmiralBet O/U lines, then form charts."""
    cur = _played_logs(this_season_logs)
    comb = _played_logs(combined_logs)
    if cur.empty and comb.empty:
        st.warning("No played games for baselines.")
        return

    bl_cur = _baseline_avgs(cur)
    bl_comb = _baseline_avgs(comb)

    market_by_stat: dict[str, dict] = {}
    if player_name:
        market_by_stat = match_player_lines(player_name, team_hint=team_hint)
        ui_log(
            f"AdmiralBet match for {player_name}: "
            + (
                ", ".join(f"{STAT_LABELS.get(k, k)}={v.get('line')}" for k, v in market_by_stat.items())
                if market_by_stat
                else "no lines"
            )
        )

    st.markdown("### Baselines")
    st.caption(
        "Two reference averages: **this season**, and **this + last season** "
        "(MIN / PTS / 3PM / AST / REB). Pick one baseline; each chart has its own form window."
    )
    left, right = st.columns(2)
    with left:
        _baseline_metric_row(f"This season ({season})", len(cur), bl_cur)
    with right:
        _baseline_metric_row(f"This + last season ({season - 1}/{season})", len(comb), bl_comb)

    st.markdown("### AdmiralBet lines")
    odds_payload = load_admiralbet_lines()
    fetched = odds_payload.get("fetched_at") or "never"
    if not market_by_stat:
        st.caption(
            f"No matching O/U lines for **{player_name or 'this player'}** "
            f"(last odds file: {fetched}). Refresh from the sidebar after MCP/local scrape."
        )
    else:
        st.caption(
            f"Matched from `data/admiralbet_lines.json` · fetched {fetched}. "
            "Blue dotted line on charts = market O/U; bar colors use the market line when present."
        )
        mcols = st.columns(max(len(market_by_stat), 1))
        for widget, (stat, row) in zip(mcols, market_by_stat.items()):
            short = STAT_LABELS.get(stat, stat)
            line = row.get("line")
            u, o = row.get("under_odds"), row.get("over_odds")
            odds = ""
            if u is not None and o is not None:
                odds = f"U {u} / O {o}"
            widget.metric(short, _fmt_base(float(line) if line is not None else float("nan")), odds or None)

    st.markdown("### Current form vs baseline")
    baseline_choice = st.radio(
        "Baseline",
        options=["this_season", "combined"],
        format_func=lambda x: (
            f"This season ({season})"
            if x == "this_season"
            else f"This + last season ({season - 1}/{season})"
        ),
        horizontal=True,
        key="form_baseline_choice",
    )

    if baseline_choice == "this_season":
        baseline = bl_cur
        baseline_name = f"Season {season}"
    else:
        baseline = bl_comb
        baseline_name = f"{season - 1}/{season}"
    form_source = comb if not comb.empty else cur

    st.caption(
        f"Comparing to **{baseline_name}** baseline. "
        "Green = over · Red = under · Yellow dashed = baseline · Blue dotted = AdmiralBet. "
        "Each graph picks last 3 / 5 / 7 games (default 3)."
    )

    for col, label, short in FORM_METRICS:
        st.markdown(f"#### {label}")
        n_pick = st.radio(
            f"Form window — {label}",
            options=[3, 5, 7],
            index=0,
            format_func=lambda x: f"Last {x} games",
            horizontal=True,
            key=f"form_n_{short}",
        )
        games = _recent_games(form_source, int(n_pick))
        if games.empty:
            st.caption(f"Not enough games for {label}.")
            continue
        mrow = market_by_stat.get(col)
        mline = float(mrow["line"]) if mrow and mrow.get("line") is not None else None
        bl_val = baseline.get(col, float("nan"))
        _plot_games_vs_baseline(games, col, label, bl_val, baseline_name, market_line=mline)
        table = _metric_game_table(games, col, label, short, bl_val)
        if mline is not None and not table.empty and label in table.columns:
            deltas = []
            for v in table[label]:
                try:
                    fv = float(v)
                    d = fv - mline
                    deltas.append(f"{d:+.1f} {'OVER' if d > 0 else ('UNDER' if d < 0 else 'FLAT')}")
                except (TypeError, ValueError):
                    deltas.append("—")
            table[f"{short} vs MKT"] = deltas
        st.dataframe(table, width="stretch", hide_index=True)


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


def render_market_cut_page() -> None:
    """Primary book vs other books (incl. Admiral) on same / close Points lines."""
    st.title("Market cut")
    st.caption(
        "Pick a **primary** bookmaker, then compare its Points Over/Under to every "
        "other selected book on the **same line** or within your **close-line ±** "
        "tolerance. AdmiralBet scrape is available as `admiralbet`."
    )
    with st.expander("How gaps work", expanded=False):
        st.markdown(
            f"""
**Over gap** = primary Over − other Over  
(negative = primary is *shorter* / undercutting the other book)

**Under gap** = other Under − primary Under  
(primary Under = real price when present, else synth from the margin slider)

**Close line ±** = max |other_line − primary_line| still treated as a pair  
(e.g. primary 14.5 vs other 15.5 when ± ≥ 1.0)

🔴 MASSIVE when over gap ≤ {MASSIVE_UNDERCUT:.2f}.
"""
        )

    board = load_props_board()
    games = board.get("games") or []
    players = board_player_summary(board)
    src_label = board.get("source") or "screenshot"
    book_opts = available_compare_books(board, include_admiral=True)
    if not book_opts:
        book_opts = list(DEFAULT_BOOKS)

    default_primary = "bet365" if "bet365" in book_opts else book_opts[0]
    c1, c2, c3 = st.columns([1.2, 1.8, 1.2])
    with c1:
        primary = st.selectbox(
            "Primary book",
            options=book_opts,
            index=book_opts.index(default_primary) if default_primary in book_opts else 0,
            format_func=lambda s: (
                f"{book_label(s)} (scrape)" if s == BOOK_ADMIRAL else f"{book_label(s)} ({s})"
            ),
            key="market_cut_primary",
            help="Anchor book — every other selected book is compared to this one.",
        )
    with c2:
        other_opts = [b for b in book_opts if b != primary]
        default_others = [b for b in other_opts if b != primary]
        compare_books = st.multiselect(
            "Compare against",
            options=other_opts,
            default=default_others,
            format_func=lambda s: (
                f"{book_label(s)} (scrape)" if s == BOOK_ADMIRAL else f"{book_label(s)} ({s})"
            ),
            key="market_cut_compare_books",
            help="OddsPapi books + optional AdmiralBet scrape.",
        )
    with c3:
        close_tol = st.slider(
            "Close line ± points",
            min_value=0.0,
            max_value=3.0,
            value=float(st.session_state.get("close_line_tol", DEFAULT_CLOSE_LINE_TOL)),
            step=0.5,
            key="close_line_tol",
            help="0 = exact lines only for the close table; exact table always requires equal lines.",
        )

    c4, c5, c6 = st.columns([1, 1, 1.5])
    with c4:
        margin_pct = st.slider(
            "Synth Under margin %",
            min_value=5,
            max_value=15,
            value=int(st.session_state.get("b365_margin_pct", DEFAULT_B365_MARGIN * 100)),
            step=1,
            key="b365_margin_pct",
            help="Only used when the primary book has Over but no real Under.",
        )
    with c5:
        rebuild = st.button("Rebuild comparison", type="primary", use_container_width=True)
    with c6:
        st.caption(
            f"Source: **{src_label}** · **{len(games)}** games · "
            f"**{len(players)}** players · `{COMPARE_PATH.name}`"
        )

    if not compare_books:
        st.warning("Select at least one bookmaker to compare against.")
        return

    margin = margin_pct / 100.0
    include_admiral = BOOK_ADMIRAL in compare_books or primary == BOOK_ADMIRAL
    cached = None if rebuild else load_comparison()
    need_rebuild = rebuild or not comparison_cache_is_fresh(
        cached,
        margin=margin,
        close_tol=close_tol,
        primary_book=primary,
        compare_books=compare_books,
    )
    cmp_payload = None
    if need_rebuild:
        try:
            cmp_payload = save_comparison(
                primary_book=primary,
                compare_books=compare_books,
                margin=margin,
                close_tol=close_tol,
                include_admiral=include_admiral,
            )
            if cmp_payload.get("save_error"):
                st.caption(
                    f"Compare built in-memory (could not write cache: {cmp_payload['save_error']})"
                )
        except Exception as exc:  # noqa: BLE001
            ui_log(f"Market compare failed: {exc}\n{traceback.format_exc()}", "error")
            st.error(f"Compare failed: {exc}")
            try:
                cmp_payload = build_comparison(
                    primary_book=primary,
                    compare_books=compare_books,
                    margin=margin,
                    close_tol=close_tol,
                    include_admiral=include_admiral,
                )
            except Exception:  # noqa: BLE001
                cmp_payload = None
    else:
        cmp_payload = cached

    event_opts = ["All games"] + [g.get("event") or f"Game {i+1}" for i, g in enumerate(games)]
    f1, f2 = st.columns([2, 1.2])
    with f1:
        pick = st.selectbox("Filter by game", options=event_opts, key="market_cut_game")
    with f2:
        other_filter = st.selectbox(
            "Filter by other book",
            options=["All books"] + compare_books,
            format_func=lambda s: book_label(s) if s != "All books" else s,
            key="market_cut_other_filter",
        )

    with st.expander("Input players (props board)", expanded=False):
        if not players:
            op_meta = load_oddspapi_meta()
            if op_meta.get("fetched_at") and (op_meta.get("summary") or {}).get("fixtures_fetched"):
                st.info(op_meta.get("empty_props_note") or "Fixtures loaded, props not out yet.")
            else:
                st.warning("No players on the OddsPapi board — refresh from the sidebar.")
        else:
            show = players if pick == "All games" else [p for p in players if p.get("event") == pick]
            st.dataframe(pd.DataFrame(show), width="stretch", hide_index=True)

    if not cmp_payload:
        return

    summ = cmp_payload.get("summary") or {}
    primary_label = cmp_payload.get("primary_book") or primary
    over_col = f"over_gap ({primary_label}-other)"
    under_col = "under_gap (otherU-primaryU)"

    st.caption(
        f"**{book_label(primary_label)}** vs "
        f"{', '.join(book_label(b) for b in (cmp_payload.get('compare_books') or compare_books))} · "
        f"exact **{summ.get('exact_half_line_matches', 0)}** · "
        f"close **{summ.get('close_line_matches', 0)}** · "
        f"far **{summ.get('far_nearest', 0)}** · "
        f"OddsPapi `{cmp_payload.get('oddspapi_fetched_at') or '—'}` · "
        f"Admiral `{cmp_payload.get('admiral_fetched_at') or '—'}`"
    )

    if (
        int(summ.get("exact_half_line_matches") or 0) == 0
        and int(summ.get("close_line_matches") or 0) == 0
    ):
        st.warning(
            "No same/close line overlaps. Try a wider close-line ±, another primary book, "
            "or refresh OddsPapi / Admiral from the sidebar."
        )

    def _filter_view(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        out = df
        if pick != "All games" and "event" in out.columns:
            out = out[out["event"] == pick]
        if other_filter != "All books" and "other_book" in out.columns:
            out = out[out["other_book"] == other_filter]
        return out.reset_index(drop=True)

    def _gap_metrics(df: pd.DataFrame, title: str) -> None:
        if df.empty:
            return
        st.caption(title)
        m1, m2, m3, m4 = st.columns(4)
        if over_col in df.columns and df[over_col].notna().any():
            i_over = df[over_col].idxmin()
            r_over = df.loc[i_over]
            m1.metric(
                "Biggest Over undercut",
                f"{r_over[over_col]:+.3f}",
                delta=f"{r_over.get('player', '')} vs {r_over.get('other_book', '')}",
                delta_color="off",
            )
            p_over_col = f"{primary_label}_over"
            m2.metric(
                "Primary O vs other O",
                f"{r_over.get(p_over_col, '—')} vs {r_over.get('other_over', '—')}",
            )
        if under_col in df.columns and df[under_col].notna().any():
            i_under = df[under_col].abs().idxmax()
            r_under = df.loc[i_under]
            m3.metric(
                "Largest |Under gap|",
                f"{r_under[under_col]:+.3f}",
                delta=f"{r_under.get('player', '')} vs {r_under.get('other_book', '')}",
                delta_color="off",
            )
            m4.metric(
                "Other U vs primary U",
                f"{r_under.get('other_under', '—')} vs "
                f"{r_under.get(f'{primary_label}_under_used', r_under.get(f'{primary_label}_under', '—'))}",
            )

    other_arg = None if other_filter == "All books" else other_filter

    st.subheader("1) Exact same line")
    st.markdown(
        f"**Over gap** = {book_label(primary_label)} Over − other Over "
        f"(negative = primary shorter). Exact rows: "
        f"**{summ.get('exact_half_line_matches', 0)}**."
    )
    exact_df = _filter_view(comparison_table_df(cmp_payload, tier="exact", other_book=other_arg))
    if exact_df.empty:
        st.info("No exact line overlaps for this filter.")
    else:
        _gap_metrics(exact_df, "Exact-match gap highlights")
        st.caption("🔴 MASSIVE undercut · 🟠 undercut · 🟢 primary longer")
        st.dataframe(decorate_flags(exact_df), width="stretch", hide_index=True)

    st.subheader(f"2) Close lines (|other − primary| ≤ {close_tol:g})")
    st.caption("Same gap math when the other book’s line is only near the primary line.")
    close_df = _filter_view(comparison_table_df(cmp_payload, tier="close", other_book=other_arg))
    if close_df.empty:
        st.info(f"No close-line pairs within ±{close_tol:g} for this filter.")
    else:
        st.caption(f"Close rows: **{summ.get('close_line_matches', 0)}** (all games) · 🟡 close line")
        _gap_metrics(close_df, "Close-line gap highlights")
        st.dataframe(decorate_flags(close_df), width="stretch", hide_index=True)

    with st.expander(
        f"Far nearest only (no gaps) — {summ.get('far_nearest', 0)} rows",
        expanded=False,
    ):
        far_df = _filter_view(comparison_table_df(cmp_payload, tier="far", other_book=other_arg))
        if far_df.empty:
            st.caption("None.")
        else:
            st.caption("Nearest other line is farther than the close-line ± — gaps left empty.")
            st.dataframe(far_df, width="stretch", hide_index=True)

    st.caption(f"Saved → `{COMPARE_PATH.as_posix()}`")


def _sidebar_oddspapi_refresh(*, button_key: str, show_book_picker: bool = False) -> list[str]:
    """
    Shared OddsPapi status + refresh control.
    Returns the selected book slugs (for fetch / display).
    """
    op_meta = load_oddspapi_meta()
    st.caption(
        f"OddsPapi games={op_meta.get('summary', {}).get('games', 0)} · "
        f"players={op_meta.get('summary', {}).get('players', 0)} · "
        f"fetched={op_meta.get('fetched_at') or '—'} · "
        f"books={', '.join(op_meta.get('books') or []) or '—'}"
    )
    if op_meta.get("empty_props_note") and not (op_meta.get("summary") or {}).get("props_ready"):
        st.info(op_meta["empty_props_note"])

    catalog = selectable_book_slugs()
    cached = [b for b in (op_meta.get("books") or []) if b in catalog]
    default_pick = cached or list(DEFAULT_BOOKS)
    if show_book_picker:
        st.markdown("**Bookmakers to fetch**")
        picked = st.multiselect(
            "Books",
            options=catalog,
            default=[b for b in default_pick if b in catalog],
            format_func=lambda s: f"{book_label(s)} — {book_note(s)}" if book_note(s) else book_label(s),
            key=f"{button_key}_books",
            help=(
                "bet365 ≈ milestones only. "
                "1xBet / Superbet / 22bet / Stake often have real Under. "
                "API allows 3 books per request — more books = more quota."
            ),
        )
        if len(picked) < 2:
            st.warning("Pick at least 2 bookmakers.")
        # Compact reminder of why some look "empty"
        with st.expander("Why only bet365 / Superbet?", expanded=False):
            st.markdown(
                """
- **bet365** & **Superbet** post dense **5+/10+/15+/20+** ladders.
- **1xBet / 22bet / Stake** mostly post **other half-lines** (5.5, 8.5, 12.5…) with **Under**.
- On API Fetch: clear the milestone filter or tick **Only rows with Under** to see them.
"""
            )
    else:
        picked = default_pick if len(default_pick) >= 2 else list(DEFAULT_BOOKS)

    op_ok, op_reason = oddspapi_ready()
    if not op_ok:
        st.info(op_reason)
    elif is_streamlit_cloud():
        st.info(
            "OddsPapi refresh is local-only (quota). "
            "Push `data/oddspapi_*.json` after a local refresh."
        )
    else:
        refresh_op = st.button(
            "Refresh OddsPapi props",
            key=button_key,
            disabled=len(picked) < 2,
            help="Fetches selected books for near-term EuroLeague fixtures (chunked ≤3 books/call).",
        )
        if refresh_op:
            with st.spinner(f"Fetching props for {', '.join(picked)}…"):
                try:
                    result = refresh_euroleague_props(book_slugs=picked)
                    if result.get("error") or not result.get("ok"):
                        st.warning(result.get("error") or "OddsPapi refresh failed")
                        ui_log(f"OddsPapi refresh: {result}", "error")
                    else:
                        ui_log(f"OddsPapi refresh: {result.get('summary')}")
                        n_pl = int((result.get("summary") or {}).get("players") or 0)
                        n_u = int((result.get("summary") or {}).get("rows_with_under") or 0)
                        if n_pl == 0 and result.get("empty_props_note"):
                            st.info(result["empty_props_note"])
                        else:
                            st.success(
                                f"Saved {n_pl} players · {n_u} with Under · "
                                f"books={', '.join(result.get('books') or [])}"
                            )
                        st.rerun()
                except Exception as exc:  # noqa: BLE001
                    ui_log(
                        f"OddsPapi refresh failed: {exc}\n{traceback.format_exc()}",
                        "error",
                    )
                    st.error(f"OddsPapi refresh failed: {exc}")
    return picked


def render_api_fetch_page(*, selected_fetch_books: list[str] | None = None) -> None:
    """Raw OddsPapi Over/Under ladders side-by-side by bookmaker."""
    st.title("API Fetch")
    st.caption(
        "EuroLeague player Points from OddsPapi (RapidAPI). "
        "Pick bookmakers in the sidebar, then refresh. "
        "bet365 ≈ N+ Over only · 1xBet / Superbet / 22bet / Stake often have **Under**."
    )

    board = load_oddspapi_board()
    all_books = board_books(board) or list(DEFAULT_BOOKS)
    games = board.get("games") or []
    rows = odds_by_book_rows(board)
    meta = load_oddspapi_meta()
    summ = meta.get("summary") or {}
    n_under = int(summ.get("rows_with_under") or sum(1 for r in rows if r.get("has_real_under")))
    under_books = summ.get("books_with_under") or [
        b for b in all_books if b in UNDER_CAPABLE_BOOKS
    ]

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Games", int(summ.get("games") or len(games)))
    m2.metric("Players", int(summ.get("players") or 0))
    m3.metric("Prop rows", len(rows))
    m4.metric("With Under", n_under)
    m5.metric("Books on board", len(all_books))
    st.caption(
        f"Fetched `{meta.get('fetched_at') or 'never'}` · "
        f"board={', '.join(all_books) or '—'} · "
        f"under books={', '.join(under_books) or '—'} · "
        f"`{(meta.get('path') or 'data/oddspapi_points_board.json')}`"
    )
    if meta.get("note"):
        st.caption(meta["note"])

    # Always-visible coverage so thin books aren't "invisible"
    if rows and all_books:
        cov_all = []
        for book in all_books:
            n_o = sum(1 for r in rows if r.get(f"{book}_over") is not None)
            n_u = sum(1 for r in rows if r.get(f"{book}_under") is not None)
            cov_all.append(
                {
                    "book": book_label(book),
                    "slug": book,
                    "overs": n_o,
                    "unders": n_u,
                    "note": book_note(book),
                }
            )
        st.dataframe(pd.DataFrame(cov_all), width="stretch", hide_index=True, height=220)

    if not rows:
        if meta.get("empty_props_note") and (summ.get("fixtures_fetched") or games):
            st.info(meta["empty_props_note"])
        else:
            st.warning(
                "No OddsPapi props cached yet. "
                "Pick books in the sidebar and hit **Refresh OddsPapi props**."
            )
        return

    show_default = [b for b in (selected_fetch_books or all_books) if b in all_books] or all_books
    show_books = st.multiselect(
        "Show bookmakers (columns / tabs)",
        options=all_books,
        default=show_default,
        format_func=lambda s: f"{book_label(s)} ({s})",
        key="api_fetch_show_books",
        help="Does not re-fetch — only changes which columns you see.",
    )
    if not show_books:
        st.warning("Select at least one bookmaker to display.")
        return

    event_opts = ["All games"] + [g.get("event") or f"Game {i+1}" for i, g in enumerate(games)]
    milestones = sorted(
        {str(r.get("milestone")) for r in rows if r.get("milestone")},
        key=lambda x: int(x.rstrip("+")) if str(x).rstrip("+").isdigit() else 999,
    )
    f1, f2, f3, f4 = st.columns([1.6, 1.1, 1.1, 1.2])
    with f1:
        pick = st.selectbox("Filter by game", options=event_opts, key="api_fetch_game")
    with f2:
        ms_pick = st.multiselect(
            "Milestones (empty = all lines)",
            options=milestones,
            default=[],
            key="api_fetch_milestones_v2",
            help=(
                "Leave empty to see all lines (needed for 1xBet/22bet/Stake). "
                "5+/10+/15+/20+ alone mostly shows bet365 + Superbet."
            ),
        )
    with f3:
        only_under = st.checkbox(
            "Only rows with Under",
            value=False,
            key="api_fetch_only_under",
            help="Show lines where at least one book posted a real Under (not synth).",
        )
    with f4:
        view = st.radio(
            "View",
            options=["Compare books", "Per book"],
            horizontal=True,
            key="api_fetch_view",
        )

    filtered = rows
    if pick != "All games":
        filtered = [r for r in filtered if r.get("event") == pick]
    if only_under:
        filtered = [r for r in filtered if r.get("has_real_under")]
    elif ms_pick:
        ms_set = set(ms_pick)
        filtered = [r for r in filtered if r.get("milestone") in ms_set]

    # Drop rows with no price in any selected display book
    filtered = [
        r
        for r in filtered
        if any(
            r.get(f"{b}_over") is not None or r.get(f"{b}_under") is not None for b in show_books
        )
    ]

    if not filtered:
        st.info(
            "No rows for this filter. Tip: clear milestones or enable "
            "**Only rows with Under** to surface 1xBet / 22bet / Stake."
        )
        return

    if view == "Compare books":
        st.subheader("Over / Under by book")
        st.caption(
            "Shorter Over = shorter price. "
            "`best_over_book` / `best_under_book` = shortest decimal on that side. "
            "Empty Under = book only has N+ milestones (typical for bet365)."
        )
        df = pd.DataFrame(filtered)
        show_cols = ["event", "player", "line", "milestone"]
        for book in show_books:
            show_cols.extend([f"{book}_over", f"{book}_under"])
        show_cols.extend(
            ["best_over_book", "best_over", "best_under_book", "best_under", "has_real_under"]
        )
        show_cols = [c for c in show_cols if c in df.columns]
        st.dataframe(df[show_cols], width="stretch", hide_index=True, height=520)
    else:
        st.subheader("Per bookmaker")
        tabs = st.tabs([book_label(b) for b in show_books])
        for tab, book in zip(tabs, show_books):
            with tab:
                book_rows = []
                for r in filtered:
                    over = r.get(f"{book}_over")
                    under = r.get(f"{book}_under")
                    if over is None and under is None:
                        continue
                    book_rows.append(
                        {
                            "event": r.get("event"),
                            "player": r.get("player"),
                            "line": r.get("line"),
                            "milestone": r.get("milestone"),
                            "over": over,
                            "under": under,
                            "★O": "★" if r.get("best_over_book") == book else "",
                            "★U": "★" if r.get("best_under_book") == book else "",
                        }
                    )
                if not book_rows:
                    st.info(
                        f"No {book_label(book)} prices for this filter. "
                        "Try clearing milestones or Only rows with Under."
                    )
                else:
                    n_u = sum(1 for x in book_rows if x.get("under") is not None)
                    st.caption(
                        f"{len(book_rows)} lines · {n_u} with Under · "
                        "★ = shortest vs other books on that side"
                    )
                    st.dataframe(
                        pd.DataFrame(book_rows),
                        width="stretch",
                        hide_index=True,
                        height=480,
                    )

    with st.expander("Line coverage (this filter)", expanded=False):
        view_lines = sorted({float(r["line"]) for r in filtered if r.get("line") is not None})
        st.caption(f"Distinct lines in view: {', '.join(f'{x:g}' for x in view_lines) or '—'}")
        cov = []
        for book in show_books:
            n_o = sum(1 for r in filtered if r.get(f"{book}_over") is not None)
            n_u = sum(1 for r in filtered if r.get(f"{book}_under") is not None)
            cov.append({"book": book_label(book), "slug": book, "overs": n_o, "unders": n_u})
        st.dataframe(pd.DataFrame(cov), width="stretch", hide_index=True)


def main() -> None:
    with st.sidebar:
        page = st.radio(
            "Page",
            options=["Form", "Market cut", "API Fetch"],
            index=0,
            key="app_page",
        )
        st.divider()

    if page == "Market cut":
        with st.sidebar:
            st.caption("OddsPapi milestones → Admiral half-lines + synth Under.")
            odds_meta = load_admiralbet_lines()
            st.caption(
                f"Admiral rows={odds_meta.get('summary', {}).get('row_count', 0)} · "
                f"fetched={odds_meta.get('fetched_at') or '—'}"
            )
            _sidebar_oddspapi_refresh(
                button_key="market_cut_refresh_oddspapi",
                show_book_picker=True,
            )
            board = load_props_board()
            n_games = len(board.get("games") or [])
            n_players = len(board_player_summary(board))
            st.caption(
                f"Active board ({board.get('source') or '—'}): "
                f"{n_games} games · {n_players} players"
            )
        render_market_cut_page()
        render_logs_panel()
        return

    if page == "API Fetch":
        with st.sidebar:
            st.caption(
                "Choose which bookmakers to pull, then refresh. "
                "Thin O/U books need empty milestone filter or Only Under."
            )
            picked_books = _sidebar_oddspapi_refresh(
                button_key="api_fetch_refresh_oddspapi",
                show_book_picker=True,
            )
        render_api_fetch_page(selected_fetch_books=picked_books)
        render_logs_panel()
        return

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
        st.divider()
        st.subheader("AdmiralBet odds")
        odds_meta = load_admiralbet_lines()
        st.caption(
            f"rows={odds_meta.get('summary', {}).get('row_count', 0)} · "
            f"fetched={odds_meta.get('fetched_at') or '—'}"
        )
        scrape_ok, scrape_reason = playwright_ready()
        if is_streamlit_cloud() or not scrape_ok:
            st.info(
                "Cloud uses the committed `data/admiralbet_lines.json` snapshot. "
                "Refresh odds locally (MCP or sidebar), then push that file to update the deploy."
            )
            if scrape_reason and not is_streamlit_cloud():
                st.caption(scrape_reason)
        else:
            refresh_odds = st.button(
                "Refresh AdmiralBet lines",
                help="Local Playwright scrape of Evroliga player O/U (PTS/AST/REB/3PM).",
            )
            if refresh_odds:
                with st.spinner("Scraping AdmiralBet Evroliga player totals…"):
                    try:
                        result = fetch_euroleague_player_totals(wait_ms=12000)
                        if result.get("error"):
                            st.warning(result["error"])
                            ui_log(f"AdmiralBet refresh: {result.get('error')}", "error")
                        else:
                            ui_log(f"AdmiralBet refresh: {result.get('summary')}")
                            st.success(f"Saved {result.get('summary', {}).get('row_count', 0)} lines.")
                    except Exception as exc:  # noqa: BLE001
                        ui_log(f"AdmiralBet refresh failed: {exc}\n{traceback.format_exc()}", "error")
                        st.error(f"Refresh failed: {exc}")

        st.subheader("OddsPapi props")
        op_meta = load_oddspapi_meta()
        st.caption(
            f"players={op_meta.get('summary', {}).get('players', 0)} · "
            f"fetched={op_meta.get('fetched_at') or '—'} · "
            f"books={', '.join(op_meta.get('books') or []) or '—'}"
        )
        if op_meta.get("empty_props_note") and not (op_meta.get("summary") or {}).get("props_ready"):
            st.caption(op_meta["empty_props_note"])
        op_ok, op_reason = oddspapi_ready()
        if is_streamlit_cloud():
            st.caption("Refresh OddsPapi locally, then commit `data/oddspapi_*.json`.")
        elif not op_ok:
            st.caption(op_reason)
        else:
            refresh_op = st.button(
                "Refresh OddsPapi props",
                key="form_refresh_oddspapi",
                help="bet365 + 1xBet + superbet.rs EuroLeague player milestones (near-term fixtures).",
            )
            if refresh_op:
                with st.spinner("Fetching EuroLeague props from OddsPapi…"):
                    try:
                        result = refresh_euroleague_props()
                        if result.get("error") or not result.get("ok"):
                            st.warning(result.get("error") or "OddsPapi refresh failed")
                            ui_log(f"OddsPapi refresh: {result}", "error")
                        else:
                            ui_log(f"OddsPapi refresh: {result.get('summary')}")
                            n_pl = int((result.get("summary") or {}).get("players") or 0)
                            if n_pl == 0 and result.get("empty_props_note"):
                                st.info(result["empty_props_note"])
                            else:
                                st.success(f"Saved {n_pl} players.")
                    except Exception as exc:  # noqa: BLE001
                        ui_log(
                            f"OddsPapi refresh failed: {exc}\n{traceback.format_exc()}",
                            "error",
                        )
                        st.error(f"OddsPapi refresh failed: {exc}")
        if clear:
            st.cache_data.clear()
            st.session_state.pop("team_form", None)
            st.session_state.pop("player_cross", None)
            st.session_state.pop("player_id", None)
            st.session_state.pop("matchup", None)
            st.session_state["ui_logs"] = []
            ui_log("Streamlit cache + session player/team state cleared")
            st.success("Cache cleared.")

    focus_team = None
    if st.session_state.get("loaded_key"):
        focus_team = st.session_state["loaded_key"][1]
    elif team:
        focus_team = team
    render_round_board(int(season), focus_team=focus_team)

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

    rot = sorted_form[sorted_form["min_avg"] > 10].head(12).copy()
    if not rot.empty and "pts_L3" in rot.columns:
        st.markdown("**Season PTS vs last-3 PTS (rotation, >10 min)**")
        cmp = rot.set_index("Player")[["pts_avg", "pts_L3"]].rename(
            columns={"pts_avg": "Season PTS", "pts_L3": "Last 3 PTS"}
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

    this_season = cross[cross["Season"].astype(int) == int(season_i)].copy()
    team_hint = None
    if not this_season.empty and "Team" in this_season.columns:
        team_hint = str(this_season["Team"].iloc[-1])
    render_player_form_vs_baselines(
        season_i,
        this_season,
        cross,
        player_name=player_name,
        team_hint=TEAM_LABELS.get(team_hint or "", team_hint),
    )

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
            "Assists",
            "TotalRebounds",
            "FGM3",
            "FGA3",
            "Valuation",
            "PIR_per_min",
        ]
        if c in cross.columns or c == "Assists"
    ]
    show = cross.copy()
    if "Assists" not in show.columns and "Assistances" in show.columns:
        show["Assists"] = show["Assistances"]
    recent_cols = [c for c in recent_cols if c in show.columns]
    st.markdown("**Recent games (raw)**")
    st.dataframe(
        _round(show.sort_values("date", ascending=False)[recent_cols].head(20)),
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
