"""
Multi-book Points milestones (N+) vs AdmiralBet Over/Under (N-0.5).

Primary board: OddsPapi (bet365 / 1xBet / Superbet) when present; else legacy
screenshot `bet365_points_board.json`.

Milestone books usually price Over only. We synthesize an Under by assuming a
fixed overround (default 10%):  1/O + 1/U = 1 + margin.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from euro_admiralbet import load_admiralbet_lines
from euro_oddspapi import BOARD_PATH as ODDSPAPI_BOARD_PATH
from euro_oddspapi import BOOK_1XBET, BOOK_BET365, load_oddspapi_board
from euro_utils import DATA_DIR

BET365_BOARD_PATH = DATA_DIR / "bet365_points_board.json"
COMPARE_PATH = DATA_DIR / "market_value_compare.json"

# Extra OddsPapi books shown beside primary (bet365) overs
SECONDARY_BOOKS = (BOOK_1XBET,)  # Mozzart slug is dynamic — filled from board

# Bet365 milestone -> Admiral half-line
MILESTONE_TO_LINE: dict[str, float] = {
    "5+": 4.5,
    "10+": 9.5,
    "15+": 14.5,
    "20+": 19.5,
}

DEFAULT_B365_MARGIN = 0.10  # 10% overround / "cut"
# |adm_line − bet365 half-line| within this → "close" table (e.g. 15.5 vs 14.5)
DEFAULT_CLOSE_LINE_TOL = 1.0

# Over-gap flags (gap = b365_over - adm_over). Negative = Bet365 shorter / Admiral longer.
MASSIVE_UNDERCUT = -0.20
MILD_UNDERCUT = -0.10
B365_LONGER = 0.10


def flag_over_gap(over_gap: float | None) -> str:
    """Label Over mispricing — not a bet tip, just how far the prices diverge."""
    if over_gap is None:
        return ""
    g = float(over_gap)
    if g <= MASSIVE_UNDERCUT:
        return "MASSIVE undercut"
    if g <= MILD_UNDERCUT:
        return "undercut"
    if g >= B365_LONGER:
        return "B365 longer"
    return "close"


def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("'", "").replace("-", " ").replace(".", " ")
    s = re.sub(r"\b(jr|sr|iii|ii|iv)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def synth_under_from_over(over_odds: float, margin: float = DEFAULT_B365_MARGIN) -> dict[str, float] | None:
    """
    Given one-sided Over odds and assumed two-way overround, back out Under.

    1/O + 1/U = 1 + margin  =>  U = 1 / (1 + margin - 1/O)
    """
    if over_odds is None or over_odds <= 1.0:
        return None
    p_over = 1.0 / float(over_odds)
    residual = (1.0 + float(margin)) - p_over
    if residual <= 0.05:
        # Over already prices more than the whole book — Under undefined / locked
        return {
            "under_odds": None,
            "implied_over_pct": round(100.0 * p_over, 2),
            "implied_under_pct": None,
            "error": "over_implied_exceeds_book",
        }
    u = 1.0 / residual
    return {
        "under_odds": round(u, 3),
        "implied_over_pct": round(100.0 * p_over, 2),
        "implied_under_pct": round(100.0 * residual, 2),
        "error": None,
    }


def _normalize_board_shape(data: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    data = dict(data)
    if path is not None:
        data["path"] = str(path)
    if not data.get("games") and data.get("players"):
        data["games"] = [
            {
                "event": data.get("event") or "Props board",
                "tip_off": data.get("tip_off"),
                "players": data.get("players") or [],
            }
        ]
    return data


def load_bet365_board(path: Path | None = None) -> dict[str, Any]:
    """Legacy screenshot board only."""
    path = path or BET365_BOARD_PATH
    if not path.exists():
        return {"games": [], "players": [], "path": None, "source": None}
    data = json.loads(path.read_text(encoding="utf-8"))
    return _normalize_board_shape(data, path)


def load_props_board(*, prefer_oddspapi: bool = True) -> dict[str, Any]:
    """
    Primary input for Market cut: OddsPapi board when it has players,
    else legacy screenshot bet365_points_board.json.
    """
    if prefer_oddspapi:
        op = load_oddspapi_board()
        n = sum(len(g.get("players") or []) for g in (op.get("games") or []))
        if n > 0:
            return _normalize_board_shape(op, Path(op["path"]) if op.get("path") else ODDSPAPI_BOARD_PATH)
    return load_bet365_board()


def active_board_path(board: dict[str, Any] | None = None) -> Path | None:
    board = board or load_props_board()
    p = board.get("path")
    if p:
        return Path(p)
    if ODDSPAPI_BOARD_PATH.exists():
        return ODDSPAPI_BOARD_PATH
    if BET365_BOARD_PATH.exists():
        return BET365_BOARD_PATH
    return None


def iter_bet365_players(board: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Flatten all games into player dicts with event / tip_off attached."""
    board = board or load_props_board()
    out: list[dict[str, Any]] = []
    for g in board.get("games") or []:
        event = g.get("event") or board.get("event") or ""
        tip = g.get("tip_off") or board.get("tip_off")
        for p in g.get("players") or []:
            row = dict(p)
            row["event"] = event
            row["tip_off"] = tip
            # Ensure primary odds map exists (prefer bet365 from odds_by_book)
            if not row.get("odds") and row.get("odds_by_book"):
                by_book = row["odds_by_book"]
                row["odds"] = dict(by_book.get(BOOK_BET365) or next(iter(by_book.values()), {}) or {})
            out.append(row)
    if not out and board.get("players"):
        for p in board["players"]:
            row = dict(p)
            row.setdefault("event", board.get("event") or "")
            row.setdefault("tip_off", board.get("tip_off"))
            out.append(row)
    return out


def _secondary_book_slugs(board: dict[str, Any]) -> list[str]:
    """Non-bet365 books present on the board (e.g. 1xbet, mozzart)."""
    declared = [str(b) for b in (board.get("books") or []) if b and str(b) != BOOK_BET365]
    found: set[str] = set(declared)
    for p in iter_bet365_players(board):
        for b in (p.get("odds_by_book") or {}):
            if b != BOOK_BET365:
                found.add(str(b))
    # Stable order: 1xbet first, then others alpha
    ordered = []
    if BOOK_1XBET in found:
        ordered.append(BOOK_1XBET)
        found.discard(BOOK_1XBET)
    ordered.extend(sorted(found))
    return ordered


def board_player_summary(board: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One row per input player (for the Market cut player list)."""
    board = board or load_props_board()
    rows = []
    for p in iter_bet365_players(board):
        odds = p.get("odds") or {}
        by_book = p.get("odds_by_book") or {}
        book_bits = []
        for b, b_odds in sorted(by_book.items()):
            if b_odds:
                book_bits.append(b)
        rows.append(
            {
                "event": p.get("event"),
                "tip_off": p.get("tip_off"),
                "player": p.get("player"),
                "team_hint": p.get("team_hint"),
                "books": ", ".join(book_bits) if book_bits else (board.get("source") or "bet365"),
                "milestones": ", ".join(
                    f"{k}@{v}" for k, v in sorted(odds.items(), key=lambda kv: int(kv[0].rstrip("+")))
                ),
                "n_milestones": len(odds),
            }
        )
    return rows


def _player_tokens(name: str) -> set[str]:
    return {t for t in _norm_name(name).split() if len(t) > 1}


def _name_match_score(
    player: str,
    adm_player: str,
    adm_team: str | None = None,
    team_hint: str | None = None,
) -> int:
    """
    Score name similarity. Last-name-only hits require team_hint agreement
    (avoids Anthony Brown ↔ Sterling Brown).
    """
    target = _norm_name(player)
    pname = _norm_name(adm_player)
    if not target or not pname:
        return 0
    target_toks = _player_tokens(player)
    pt = _player_tokens(adm_player)
    last = target.split()[-1] if target else ""
    hint = _norm_name(team_hint or "")
    team = _norm_name(adm_team or "")
    team_ok = (not hint) or (hint in team)

    if pname == target:
        return 100 + (10 if team_ok else 0)
    # substring only if multi-token (Duane Washington Jr ↔ Duane Washington)
    if len(target_toks) >= 2 and (target in pname or pname in target):
        return 80 + (10 if team_ok else 0)
    if last and last in pname.split():
        overlap = len(target_toks & pt)
        # same last name alone is too weak across the league
        if overlap >= 2:
            return 60 + 10 * overlap + (10 if team_ok else 0)
        if overlap == 1 and team_ok and hint:
            return 45
        return 0
    return 0


def _best_adm_row(
    player: str,
    line: float,
    adm_points: list[dict[str, Any]],
    team_hint: str | None = None,
) -> dict[str, Any] | None:
    """Exact line match + fuzzy name (allows Jr. / shortened first names)."""
    scored: list[tuple[int, dict[str, Any]]] = []
    for r in adm_points:
        try:
            if abs(float(r.get("line")) - float(line)) > 1e-6:
                continue
        except (TypeError, ValueError):
            continue
        score = _name_match_score(player, r.get("player", ""), r.get("team"), team_hint)
        if score > 0:
            scored.append((score, r))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _nearest_adm_points(
    player: str,
    adm_points: list[dict[str, Any]],
    team_hint: str | None = None,
) -> dict[str, Any] | None:
    """Any Points line for this player (for context when no exact half-line)."""
    scored: list[tuple[int, dict[str, Any]]] = []
    for r in adm_points:
        score = _name_match_score(player, r.get("player", ""), r.get("team"), team_hint)
        if score > 0:
            scored.append((score, r))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def build_comparison(
    *,
    bet365_board: dict[str, Any] | None = None,
    admiral_payload: dict[str, Any] | None = None,
    margin: float = DEFAULT_B365_MARGIN,
    close_tol: float = DEFAULT_CLOSE_LINE_TOL,
) -> dict[str, Any]:
    board = bet365_board or load_props_board()
    adm = admiral_payload or load_admiralbet_lines()
    adm_lines = [
        r
        for r in (adm.get("lines") or [])
        if str(r.get("market") or "") in ("Points", "Ukupno Poena", "Ukupno poena")
    ]
    secondary = _secondary_book_slugs(board)

    rows: list[dict[str, Any]] = []
    games = board.get("games") or []
    events = [g.get("event") for g in games if g.get("event")]

    for p in iter_bet365_players(board):
        name = p.get("player") or ""
        hint = p.get("team_hint")
        event = p.get("event") or ""
        tip_off = p.get("tip_off")
        odds_map = p.get("odds") or {}
        by_book = p.get("odds_by_book") or {}
        nearest = _nearest_adm_points(name, adm_lines, hint)

        for milestone, line in MILESTONE_TO_LINE.items():
            b365 = odds_map.get(milestone)
            if b365 is None and by_book:
                b365 = (by_book.get(BOOK_BET365) or {}).get(milestone)
            if b365 is None:
                # Still emit row if another book has this milestone (multi-book only)
                if not any((by_book.get(b) or {}).get(milestone) is not None for b in secondary):
                    continue
                b365 = None
            else:
                b365 = float(b365)

            synth = synth_under_from_over(b365, margin) if b365 is not None else {}
            adm_row = _best_adm_row(name, line, adm_lines, hint)
            exact = adm_row is not None
            src = adm_row if exact else nearest

            adm_over = adm_under = adm_line = None
            adm_team = adm_player = None
            if src is not None:
                adm_team = src.get("team")
                adm_player = src.get("player")
                try:
                    adm_line = float(src["line"])
                except (TypeError, ValueError, KeyError):
                    adm_line = None
                if src.get("over_odds") is not None:
                    adm_over = float(src["over_odds"])
                if src.get("under_odds") is not None:
                    adm_under = float(src["under_odds"])

            line_diff = None
            if adm_line is not None:
                line_diff = round(adm_line - float(line), 1)

            abs_diff = abs(line_diff) if line_diff is not None else None
            if exact:
                match_tier = "exact"
            elif abs_diff is not None and abs_diff <= float(close_tol):
                match_tier = "close"
            elif src is not None:
                match_tier = "far"
            else:
                match_tier = "none"

            over_gap = None
            under_gap = None
            if match_tier in ("exact", "close") and adm_over is not None and b365 is not None:
                over_gap = round(b365 - adm_over, 3)
            synth_u = synth.get("under_odds") if synth else None
            if match_tier in ("exact", "close") and adm_under is not None and synth_u is not None:
                under_gap = round(adm_under - float(synth_u), 3)

            if match_tier == "exact":
                flag = flag_over_gap(over_gap)
            elif match_tier == "close":
                base = flag_over_gap(over_gap)
                flag = f"close line · {base}" if base else "close line"
            else:
                flag = ""

            # Best Over among all books that have this milestone
            book_overs: dict[str, float] = {}
            if b365 is not None:
                book_overs[BOOK_BET365] = b365
            for b in secondary:
                v = (by_book.get(b) or {}).get(milestone)
                if v is not None:
                    book_overs[b] = float(v)
            best_book = None
            best_over = None
            if book_overs:
                best_book = max(book_overs, key=lambda k: book_overs[k])
                best_over = book_overs[best_book]

            row: dict[str, Any] = {
                "event": event,
                "tip_off": tip_off,
                "player": name,
                "team_hint": hint,
                "milestone": milestone,
                "line": line,
                "b365_over": b365,
                "b365_implied_over_pct": synth.get("implied_over_pct") if synth else None,
                "b365_synth_under": synth.get("under_odds") if synth else None,
                "b365_implied_under_pct": synth.get("implied_under_pct") if synth else None,
                "b365_margin": margin,
                "exact_match": exact,
                "match_tier": match_tier,
                "adm_team": adm_team,
                "adm_player": adm_player,
                "adm_line": adm_line,
                "line_diff": line_diff,
                "adm_over": adm_over,
                "adm_under": adm_under,
                "over_gap": over_gap,
                "under_gap": under_gap,
                "flag": flag,
                "nearest_only": match_tier in ("close", "far"),
                "best_book": best_book,
                "best_over": best_over,
            }
            for b in secondary:
                col = f"{b.replace('.', '_').replace('-', '_')}_over"
                v = (by_book.get(b) or {}).get(milestone)
                row[col] = float(v) if v is not None else None
                if adm_over is not None and v is not None and match_tier in ("exact", "close"):
                    row[f"{col}_gap_vs_adm"] = round(float(v) - adm_over, 3)
            rows.append(row)

    exact_rows = [r for r in rows if r.get("match_tier") == "exact"]
    close_rows = [r for r in rows if r.get("match_tier") == "close"]
    far_rows = [r for r in rows if r.get("match_tier") == "far"]

    by_over = sorted(
        [r for r in exact_rows if r["over_gap"] is not None],
        key=lambda r: abs(r["over_gap"]),
        reverse=True,
    )
    by_under = sorted(
        [r for r in exact_rows if r["under_gap"] is not None],
        key=lambda r: abs(r["under_gap"]),
        reverse=True,
    )

    players_in = board_player_summary(board)
    board_path = active_board_path(board)
    board_mtime = None
    try:
        if board_path and board_path.exists():
            board_mtime = board_path.stat().st_mtime
    except OSError:
        board_mtime = None
    payload = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "board_source": board.get("source") or "bet365 screenshots",
        "board_books": board.get("books") or [BOOK_BET365],
        "secondary_books": secondary,
        "bet365_events": events,
        "bet365_event": ", ".join(events) if events else board.get("event"),
        "bet365_tip_off": board.get("tip_off"),
        "bet365_source": board.get("source"),
        "bet365_board_mtime": board_mtime,
        "board_path": str(board_path) if board_path else None,
        "admiral_fetched_at": adm.get("fetched_at"),
        "oddspapi_fetched_at": board.get("fetched_at") if board.get("source") == "oddspapi" else None,
        "b365_margin_assumed": margin,
        "close_line_tol": close_tol,
        "method": {
            "exact": "Milestone N+ ↔ Admiral Over (N-0.5)",
            "close": f"|adm_line − (N-0.5)| ≤ {close_tol} (e.g. 15.5 vs 14.5)",
            "synth_under": "1/O + 1/U = 1 + margin (primary book Over)",
            "over_gap": "primary_over (bet365) - adm_over",
            "under_gap": "adm_under - synth_under",
            "books": "OddsPapi bet365 + 1xbet + superbet.rs vs Admiral scrape",
        },
        "summary": {
            "games": len(games) or (1 if board.get("players") else 0),
            "input_players": len(players_in),
            "bet365_milestone_rows": len(rows),
            "exact_half_line_matches": len(exact_rows),
            "close_line_matches": len(close_rows),
            "far_nearest": len(far_rows),
            "books": board.get("books") or secondary,
        },
        "input_players": players_in,
        "biggest_over_gaps": by_over[:15],
        "biggest_under_gaps": by_under[:15],
        "exact_matches": exact_rows,
        "close_matches": close_rows,
        "far_nearest": far_rows,
        "all_rows": rows,
    }
    return payload


def save_comparison(
    path: Path | None = None,
    *,
    margin: float = DEFAULT_B365_MARGIN,
    close_tol: float = DEFAULT_CLOSE_LINE_TOL,
) -> dict[str, Any]:
    """Build from live JSON sources. Best-effort write (Cloud FS may be read-only)."""
    path = path or COMPARE_PATH
    payload = build_comparison(margin=margin, close_tol=close_tol)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        payload["saved_path"] = str(path)
    except OSError as exc:
        payload["saved_path"] = None
        payload["save_error"] = str(exc)
    return payload


def load_comparison(path: Path | None = None) -> dict[str, Any] | None:
    path = path or COMPARE_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def comparison_cache_is_fresh(
    payload: dict[str, Any] | None,
    *,
    margin: float,
    close_tol: float,
) -> bool:
    """True only if cache matches current slider + source file timestamps."""
    if not payload:
        return False
    if abs(float(payload.get("b365_margin_assumed") or 0) - margin) > 1e-6:
        return False
    if abs(float(payload.get("close_line_tol") or 0) - close_tol) > 1e-6:
        return False
    adm = load_admiralbet_lines()
    if (payload.get("admiral_fetched_at") or None) != (adm.get("fetched_at") or None):
        return False
    board = load_props_board()
    if (payload.get("oddspapi_fetched_at") or None) != (
        board.get("fetched_at") if board.get("source") == "oddspapi" else None
    ):
        # Screenshot board has no oddspapi_fetched_at — only fail when sources disagree
        if board.get("source") == "oddspapi" or payload.get("oddspapi_fetched_at"):
            return False
    board_path = active_board_path(board)
    if board_path and board_path.exists():
        cached_mtime = payload.get("bet365_board_mtime")
        try:
            mtime = board_path.stat().st_mtime
        except OSError:
            mtime = None
        if cached_mtime is None or mtime is None or abs(float(cached_mtime) - float(mtime)) > 1e-3:
            return False
    summ = payload.get("summary") or {}
    if int(summ.get("exact_half_line_matches") or 0) == 0 and int(
        summ.get("close_line_matches") or 0
    ) == 0 and int(summ.get("input_players") or 0) > 0:
        return False
    return True


def comparison_table_df(
    payload: dict[str, Any] | None = None,
    *,
    exact_only: bool = True,
    tier: str | None = None,
):
    """
    Return a pandas DataFrame suitable for st.dataframe.

    tier: "exact" | "close" | "far" | "all"
    exact_only=True is legacy alias for tier="exact".
    """
    import pandas as pd

    payload = payload or load_comparison() or build_comparison()
    if tier is None:
        tier = "exact" if exact_only else "all"
    key = {
        "exact": "exact_matches",
        "close": "close_matches",
        "far": "far_nearest",
        "all": "all_rows",
    }.get(tier, "all_rows")
    rows = payload.get(key) or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    rename = {
        "over_gap": "over_gap (B365−Adm)",
        "under_gap": "under_gap (AdmU−synthU)",
        "b365_synth_under": "b365_synth_under (from slider)",
        "best_over": "best_over (any book)",
        "1xbet_over": "1xbet_over",
    }
    # Dynamic secondary book columns (mozzart / maxbet / …)
    extra_over_cols = [
        c
        for c in df.columns
        if c.endswith("_over") and c not in ("b365_over", "adm_over", "best_over")
    ]
    cols = [
        "flag",
        "match_tier",
        "event",
        "player",
        "team_hint",
        "milestone",
        "line",
        "adm_line",
        "line_diff",
        "b365_over",
        *extra_over_cols,
        "best_book",
        "best_over",
        "adm_over",
        "over_gap",
        "b365_synth_under",
        "adm_under",
        "under_gap",
        "b365_margin",
        "adm_team",
        "exact_match",
    ]
    cols = [c for c in cols if c in df.columns]
    out = df[cols].copy()
    numeric_cols = [
        "b365_over",
        "adm_over",
        "over_gap",
        "b365_synth_under",
        "adm_under",
        "under_gap",
        "line",
        "adm_line",
        "line_diff",
        "b365_margin",
        "best_over",
        *extra_over_cols,
    ]
    for c in numeric_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").round(3)
    out = out.rename(columns=rename)
    gap_col = "over_gap (B365−Adm)"
    if gap_col in out.columns:
        out["_gap_sort"] = out[gap_col].fillna(0.0)
        if "exact_match" in out.columns:
            out = out.sort_values(by=["exact_match", "_gap_sort"], ascending=[False, True])
        else:
            out = out.sort_values(by="_gap_sort", ascending=True)
        out = out.drop(columns=["_gap_sort"])
    return out.reset_index(drop=True)


def decorate_flags(df):
    """
    Make flags obvious without pandas Styler backgrounds (those wash out on
    Streamlit dark theme). Returns a plain DataFrame.
    """
    if df is None or df.empty or "flag" not in df.columns:
        return df
    out = df.copy()

    def _label(v: object) -> str:
        s = str(v or "")
        if not s:
            return ""
        if "MASSIVE" in s:
            return f"🔴 {s}"
        if s == "undercut" or s.endswith("· undercut"):
            return f"🟠 {s}"
        if "B365 longer" in s:
            return f"🟢 {s}"
        if s.startswith("close line"):
            return f"🟡 {s}"
        if s == "close":
            return "close"
        return s

    out["flag"] = out["flag"].map(_label)
    return out


def style_gap_table(df):
    """Compat wrapper — prefer decorate_flags (no Styler)."""
    return decorate_flags(df)


if __name__ == "__main__":
    payload = save_comparison()
    print(f"Wrote {COMPARE_PATH}")
    print("summary", payload["summary"])
    print("\nExact matches (ranked |over_gap|):")
    for r in payload["biggest_over_gaps"]:
        print(
            f"  {r['over_gap']:+.3f}  {r['player']:22} {r['milestone']:4} "
            f"B365 O={r['b365_over']}  Adm O={r['adm_over']} U={r['adm_under']}  "
            f"synthU={r['b365_synth_under']}  under_gap={r['under_gap']}"
        )
    print("\nNearest-only (no exact half-line) sample:")
    for r in payload["all_rows"]:
        if r.get("nearest_only") and r["milestone"] in ("10+", "15+"):
            print(
                f"  {r['player']:22} {r['milestone']} B365={r['b365_over']}  "
                f"Adm nearest {r['adm_line']} O={r['adm_over']} U={r['adm_under']}  "
                f"synthU={r['b365_synth_under']}"
            )
