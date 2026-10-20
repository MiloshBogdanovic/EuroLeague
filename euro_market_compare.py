"""
Market cut: pick a primary bookmaker and compare Over/Under vs other books
(and optional AdmiralBet scrape) on the same line or within a close-line tolerance.

Primary board: OddsPapi multi-book props. Legacy screenshot board is fallback.
When a book only posts Over (N+ milestones), Under can be synthesized with an
assumed overround for gap display — real Unders are preferred when present.
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
from euro_oddspapi import BOOK_1XBET, BOOK_BET365, board_books, load_oddspapi_board
from euro_utils import DATA_DIR

BET365_BOARD_PATH = DATA_DIR / "bet365_points_board.json"
COMPARE_PATH = DATA_DIR / "market_value_compare.json"

BOOK_ADMIRAL = "admiralbet"

DEFAULT_B365_MARGIN = 0.10  # synth Under overround when primary has Over only
DEFAULT_CLOSE_LINE_TOL = 1.0

# Over-gap flags (gap = primary_over - other_over). Negative = primary shorter.
MASSIVE_UNDERCUT = -0.20
MILD_UNDERCUT = -0.10
PRIMARY_LONGER = 0.10


def flag_over_gap(over_gap: float | None) -> str:
    """Label Over mispricing — not a bet tip, just how far the prices diverge."""
    if over_gap is None:
        return ""
    g = float(over_gap)
    if g <= MASSIVE_UNDERCUT:
        return "MASSIVE undercut"
    if g <= MILD_UNDERCUT:
        return "undercut"
    if g >= PRIMARY_LONGER:
        return "primary longer"
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
    """OddsPapi board when it has players, else legacy screenshot board."""
    if prefer_oddspapi:
        op = load_oddspapi_board()
        n = sum(len(g.get("players") or []) for g in (op.get("games") or []))
        if n > 0:
            return _normalize_board_shape(
                op, Path(op["path"]) if op.get("path") else ODDSPAPI_BOARD_PATH
            )
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


def iter_board_players(board: dict[str, Any] | None = None) -> list[dict[str, Any]]:
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
            if not row.get("odds") and row.get("odds_by_book"):
                by_book = row["odds_by_book"]
                row["odds"] = dict(
                    by_book.get(BOOK_BET365) or next(iter(by_book.values()), {}) or {}
                )
            out.append(row)
    if not out and board.get("players"):
        for p in board["players"]:
            row = dict(p)
            row.setdefault("event", board.get("event") or "")
            row.setdefault("tip_off", board.get("tip_off"))
            out.append(row)
    return out


# Back-compat alias
iter_bet365_players = iter_board_players


def _milestone_to_line(milestone: str) -> float | None:
    try:
        n = int(str(milestone).rstrip("+"))
    except ValueError:
        return None
    return float(n) - 0.5


def _line_to_milestone(line: float) -> str | None:
    try:
        f = float(line)
    except (TypeError, ValueError):
        return None
    if abs(f - int(f) - 0.5) < 1e-6:
        return f"{int(f) + 1}+"
    if abs(f - int(f)) < 1e-6:
        return f"{int(f)}+"
    return None


def _line_key(line: float) -> str:
    f = float(line)
    if abs(f - int(f)) < 1e-6:
        return str(int(f))
    return f"{f:g}"


def board_player_summary(board: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One row per input player (for the Market cut player list)."""
    board = board or load_props_board()
    rows = []
    for p in iter_board_players(board):
        odds = p.get("odds") or {}
        by_book = p.get("odds_by_book") or {}
        book_bits = [b for b, b_odds in sorted(by_book.items()) if b_odds]
        rows.append(
            {
                "event": p.get("event"),
                "tip_off": p.get("tip_off"),
                "player": p.get("player"),
                "team_hint": p.get("team_hint"),
                "books": ", ".join(book_bits) if book_bits else (board.get("source") or "—"),
                "milestones": ", ".join(
                    f"{k}@{v}"
                    for k, v in sorted(odds.items(), key=lambda kv: int(kv[0].rstrip("+")))
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
    if len(target_toks) >= 2 and (target in pname or pname in target):
        return 80 + (10 if team_ok else 0)
    if last and last in pname.split():
        overlap = len(target_toks & pt)
        if overlap >= 2:
            return 60 + 10 * overlap + (10 if team_ok else 0)
        if overlap == 1 and team_ok and hint:
            return 45
        return 0
    return 0


def available_compare_books(
    board: dict[str, Any] | None = None,
    *,
    include_admiral: bool = True,
) -> list[str]:
    """Books present on the props board (+ admiralbet when scrape has Points)."""
    board = board or load_props_board()
    books = board_books(board) if board.get("source") == "oddspapi" else []
    if not books:
        # Legacy / mixed
        found: set[str] = set()
        for b in board.get("books") or []:
            found.add(str(b))
        for p in iter_board_players(board):
            found.update(str(b) for b in (p.get("odds_by_book") or {}))
            found.update(str(b) for b in (p.get("ou_by_book") or {}))
        if not found and board.get("source"):
            found.add(BOOK_BET365)
        books = sorted(found)
    if include_admiral:
        adm = load_admiralbet_lines()
        has_pts = any(
            str(r.get("market") or "") in ("Points", "Ukupno Poena", "Ukupno poena")
            for r in (adm.get("lines") or [])
        )
        if has_pts and BOOK_ADMIRAL not in books:
            books = list(books) + [BOOK_ADMIRAL]
    return books


def _quotes_for_book_on_player(player: dict[str, Any], book: str) -> list[dict[str, Any]]:
    """Line-level Over/Under quotes for one book on one board player."""
    by_line: dict[str, dict[str, Any]] = {}

    ou = (player.get("ou_by_book") or {}).get(book) or {}
    for lk, cell in ou.items():
        if not isinstance(cell, dict):
            continue
        try:
            line = float(lk)
        except (TypeError, ValueError):
            continue
        key = _line_key(line)
        by_line[key] = {
            "line": line,
            "milestone": cell.get("milestone") or _line_to_milestone(line),
            "over": float(cell["over"]) if cell.get("over") is not None else None,
            "under": float(cell["under"]) if cell.get("under") is not None else None,
            "has_real_under": bool(cell.get("has_real_under") or cell.get("under") is not None),
        }

    odds = (player.get("odds_by_book") or {}).get(book) or {}
    unders = (player.get("under_by_book") or {}).get(book) or {}
    # Legacy single map
    if not odds and book == BOOK_BET365 and player.get("odds"):
        odds = player.get("odds") or {}
    for ms, over in odds.items():
        line = _milestone_to_line(str(ms))
        if line is None:
            continue
        key = _line_key(line)
        cell = by_line.get(key) or {
            "line": line,
            "milestone": str(ms),
            "over": None,
            "under": None,
            "has_real_under": False,
        }
        if over is not None and cell["over"] is None:
            cell["over"] = float(over)
        if cell.get("milestone") is None:
            cell["milestone"] = str(ms)
        u = unders.get(ms)
        if u is not None:
            cell["under"] = float(u)
            cell["has_real_under"] = True
        by_line[key] = cell

    for ms, u in unders.items():
        line = _milestone_to_line(str(ms))
        if line is None:
            continue
        key = _line_key(line)
        cell = by_line.get(key) or {
            "line": line,
            "milestone": str(ms),
            "over": None,
            "under": None,
            "has_real_under": False,
        }
        if u is not None:
            cell["under"] = float(u)
            cell["has_real_under"] = True
        by_line[key] = cell

    return sorted(by_line.values(), key=lambda q: float(q["line"]))


def _admiral_points_index() -> list[dict[str, Any]]:
    adm = load_admiralbet_lines()
    out = []
    for r in adm.get("lines") or []:
        if str(r.get("market") or "") not in ("Points", "Ukupno Poena", "Ukupno poena"):
            continue
        try:
            line = float(r["line"])
        except (TypeError, ValueError, KeyError):
            continue
        out.append(
            {
                "book": BOOK_ADMIRAL,
                "player": r.get("player"),
                "team": r.get("team"),
                "line": line,
                "milestone": _line_to_milestone(line),
                "over": float(r["over_odds"]) if r.get("over_odds") is not None else None,
                "under": float(r["under_odds"]) if r.get("under_odds") is not None else None,
                "has_real_under": r.get("under_odds") is not None,
                "fetched_at": adm.get("fetched_at"),
            }
        )
    return out


def _best_admiral_quote(
    player: str,
    line: float,
    adm_index: list[dict[str, Any]],
    team_hint: str | None,
    *,
    close_tol: float,
) -> tuple[dict[str, Any] | None, str]:
    """
    Best Admiral quote for player near `line`.
    Returns (quote, tier) where tier is exact|close|far|none.
    """
    scored: list[tuple[int, float, dict[str, Any]]] = []
    for r in adm_index:
        score = _name_match_score(player, r.get("player") or "", r.get("team"), team_hint)
        if score <= 0:
            continue
        try:
            diff = abs(float(r["line"]) - float(line))
        except (TypeError, ValueError, KeyError):
            continue
        scored.append((score, diff, r))
    if not scored:
        return None, "none"
    # Prefer higher name score, then closer line
    scored.sort(key=lambda t: (-t[0], t[1]))
    best_score, best_diff, best = scored[0]
    _ = best_score
    if best_diff < 1e-6:
        return best, "exact"
    if best_diff <= float(close_tol) + 1e-9:
        return best, "close"
    return best, "far"


def _nearest_other_quote(
    primary_line: float,
    other_quotes: list[dict[str, Any]],
    *,
    close_tol: float,
) -> tuple[dict[str, Any] | None, str]:
    if not other_quotes:
        return None, "none"
    ranked = sorted(
        other_quotes,
        key=lambda q: abs(float(q["line"]) - float(primary_line)),
    )
    best = ranked[0]
    diff = abs(float(best["line"]) - float(primary_line))
    if diff < 1e-6:
        return best, "exact"
    if diff <= float(close_tol) + 1e-9:
        return best, "close"
    return best, "far"


def build_comparison(
    *,
    board: dict[str, Any] | None = None,
    primary_book: str | None = None,
    compare_books: list[str] | None = None,
    close_tol: float = DEFAULT_CLOSE_LINE_TOL,
    margin: float = DEFAULT_B365_MARGIN,
    include_admiral: bool = True,
    # Legacy aliases
    bet365_board: dict[str, Any] | None = None,
    admiral_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Compare primary_book vs other books on same / close Points lines.

    Emits one row per (player × primary line × other book) with Over/Under gaps.
    """
    _ = admiral_payload  # optional override unused — always load live admiral index
    board = board or bet365_board or load_props_board()
    books_on_board = available_compare_books(board, include_admiral=False)
    all_books = available_compare_books(board, include_admiral=include_admiral)

    primary = primary_book or (
        BOOK_BET365 if BOOK_BET365 in all_books else (all_books[0] if all_books else BOOK_BET365)
    )
    others = [
        b
        for b in (compare_books if compare_books is not None else all_books)
        if b and b != primary
    ]
    if not others:
        others = [b for b in all_books if b != primary]

    need_adm = primary == BOOK_ADMIRAL or BOOK_ADMIRAL in others
    adm_index = _admiral_points_index() if need_adm else []
    adm_meta = load_admiralbet_lines() if include_admiral or need_adm else {}

    rows: list[dict[str, Any]] = []
    games = board.get("games") or []
    events = [g.get("event") for g in games if g.get("event")]

    def _emit_pair(
        *,
        name: str,
        hint: str | None,
        event: str,
        tip_off: Any,
        pq: dict[str, Any],
        other: str,
        oq: dict[str, Any],
        tier: str,
    ) -> None:
        p_line = float(pq["line"])
        p_over = pq.get("over")
        p_under = pq.get("under")
        p_real_u = bool(pq.get("has_real_under") and p_under is not None)
        synth = (
            synth_under_from_over(p_over, margin)
            if p_over is not None and not p_real_u
            else {}
        )
        synth_u = synth.get("under_odds") if synth else None
        primary_under_for_gap = p_under if p_real_u else synth_u
        o_line = float(oq["line"])
        o_over = oq.get("over")
        o_under = oq.get("under")
        o_real_u = bool(oq.get("has_real_under") and o_under is not None)
        line_diff = round(o_line - p_line, 1)

        over_gap = under_gap = None
        if tier in ("exact", "close") and p_over is not None and o_over is not None:
            over_gap = round(float(p_over) - float(o_over), 3)
        if (
            tier in ("exact", "close")
            and primary_under_for_gap is not None
            and o_under is not None
        ):
            under_gap = round(float(o_under) - float(primary_under_for_gap), 3)

        if tier == "exact":
            flag = flag_over_gap(over_gap)
        elif tier == "close":
            base = flag_over_gap(over_gap)
            flag = f"close line · {base}" if base else "close line"
        else:
            flag = ""

        rows.append(
            {
                "event": event,
                "tip_off": tip_off,
                "player": name,
                "team_hint": hint,
                "primary_book": primary,
                "other_book": other,
                "milestone": pq.get("milestone") or oq.get("milestone"),
                "primary_line": p_line,
                "other_line": o_line,
                "line_diff": line_diff,
                "match_tier": tier,
                "exact_match": tier == "exact",
                "primary_over": p_over,
                "primary_under": p_under,
                "primary_under_real": p_real_u,
                "primary_synth_under": synth_u,
                "primary_under_used": primary_under_for_gap,
                "other_over": o_over,
                "other_under": o_under,
                "other_under_real": o_real_u,
                "over_gap": over_gap,
                "under_gap": under_gap,
                "flag": flag,
                "margin": margin,
                "adm_team": (pq.get("team") if primary == BOOK_ADMIRAL else oq.get("team"))
                if BOOK_ADMIRAL in (primary, other)
                else None,
                "adm_player": (pq.get("player") if primary == BOOK_ADMIRAL else oq.get("player"))
                if BOOK_ADMIRAL in (primary, other)
                else None,
                "line": p_line,
                "b365_over": p_over if primary == BOOK_BET365 else None,
                "adm_over": o_over if other == BOOK_ADMIRAL else (
                    p_over if primary == BOOK_ADMIRAL else None
                ),
                "adm_under": o_under if other == BOOK_ADMIRAL else (
                    p_under if primary == BOOK_ADMIRAL else None
                ),
                "adm_line": o_line if other == BOOK_ADMIRAL else (
                    p_line if primary == BOOK_ADMIRAL else None
                ),
            }
        )

    if primary == BOOK_ADMIRAL:
        # Anchor on Admiral scrape; match each line to OddsPapi books via name.
        board_players = iter_board_players(board)
        for aq in adm_index:
            name = aq.get("player") or ""
            hint = aq.get("team")
            # Attach event from best name-matched board player when possible
            event = ""
            tip_off = None
            matched_p = None
            best_score = 0
            for p in board_players:
                score = _name_match_score(
                    name, p.get("player") or "", p.get("team_hint"), hint
                )
                if score > best_score:
                    best_score = score
                    matched_p = p
            if matched_p is not None and best_score > 0:
                event = matched_p.get("event") or ""
                tip_off = matched_p.get("tip_off")
            pq = {
                "line": aq["line"],
                "milestone": aq.get("milestone"),
                "over": aq.get("over"),
                "under": aq.get("under"),
                "has_real_under": aq.get("has_real_under"),
                "team": aq.get("team"),
                "player": aq.get("player"),
            }
            for other in others:
                if other == BOOK_ADMIRAL:
                    continue
                if matched_p is None:
                    continue
                oq, tier = _nearest_other_quote(
                    float(pq["line"]),
                    _quotes_for_book_on_player(matched_p, other),
                    close_tol=close_tol,
                )
                if oq is None or tier == "none":
                    continue
                _emit_pair(
                    name=name,
                    hint=hint,
                    event=event,
                    tip_off=tip_off,
                    pq=pq,
                    other=other,
                    oq=oq,
                    tier=tier,
                )
    else:
        for p in iter_board_players(board):
            name = p.get("player") or ""
            hint = p.get("team_hint")
            event = p.get("event") or ""
            tip_off = p.get("tip_off")
            primary_quotes = _quotes_for_book_on_player(p, primary)
            if not primary_quotes:
                continue

            other_quote_maps: dict[str, list[dict[str, Any]]] = {}
            for b in others:
                if b == BOOK_ADMIRAL:
                    continue
                other_quote_maps[b] = _quotes_for_book_on_player(p, b)

            for pq in primary_quotes:
                for other in others:
                    if other == BOOK_ADMIRAL:
                        oq, tier = _best_admiral_quote(
                            name, float(pq["line"]), adm_index, hint, close_tol=close_tol
                        )
                    else:
                        oq, tier = _nearest_other_quote(
                            float(pq["line"]),
                            other_quote_maps.get(other) or [],
                            close_tol=close_tol,
                        )
                    if oq is None or tier == "none":
                        continue
                    _emit_pair(
                        name=name,
                        hint=hint,
                        event=event,
                        tip_off=tip_off,
                        pq=pq,
                        other=other,
                        oq=oq,
                        tier=tier,
                    )

    exact_rows = [r for r in rows if r.get("match_tier") == "exact"]
    close_rows = [r for r in rows if r.get("match_tier") == "close"]
    far_rows = [r for r in rows if r.get("match_tier") == "far"]

    by_over = sorted(
        [r for r in exact_rows if r["over_gap"] is not None],
        key=lambda r: r["over_gap"],
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
        "board_source": board.get("source") or "screenshot",
        "board_books": books_on_board,
        "primary_book": primary,
        "compare_books": others,
        "include_admiral": include_admiral,
        "bet365_events": events,
        "bet365_board_mtime": board_mtime,
        "board_path": str(board_path) if board_path else None,
        "admiral_fetched_at": adm_meta.get("fetched_at"),
        "oddspapi_fetched_at": board.get("fetched_at") if board.get("source") == "oddspapi" else None,
        "b365_margin_assumed": margin,
        "close_line_tol": close_tol,
        "method": {
            "exact": "Same Points line on primary vs other book",
            "close": f"|other_line − primary_line| ≤ {close_tol}",
            "over_gap": "primary_over − other_over (negative = primary shorter)",
            "under_gap": "other_under − primary_under (real Under, else synth from margin)",
            "synth_under": "1/O + 1/U = 1 + margin when primary has no real Under",
        },
        "summary": {
            "games": len(games) or (1 if board.get("players") else 0),
            "input_players": len(players_in),
            "compare_rows": len(rows),
            "exact_half_line_matches": len(exact_rows),
            "close_line_matches": len(close_rows),
            "far_nearest": len(far_rows),
            "books": all_books,
            "primary_book": primary,
            "compare_books": others,
        },
        "input_players": players_in,
        "biggest_over_gaps": by_over[:20],
        "biggest_under_gaps": by_under[:20],
        "exact_matches": exact_rows,
        "close_matches": close_rows,
        "far_nearest": far_rows,
        "all_rows": rows,
    }
    return payload


def save_comparison(
    path: Path | None = None,
    *,
    primary_book: str | None = None,
    compare_books: list[str] | None = None,
    margin: float = DEFAULT_B365_MARGIN,
    close_tol: float = DEFAULT_CLOSE_LINE_TOL,
    include_admiral: bool = True,
) -> dict[str, Any]:
    path = path or COMPARE_PATH
    payload = build_comparison(
        primary_book=primary_book,
        compare_books=compare_books,
        margin=margin,
        close_tol=close_tol,
        include_admiral=include_admiral,
    )
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
    primary_book: str | None = None,
    compare_books: list[str] | None = None,
) -> bool:
    if not payload:
        return False
    if abs(float(payload.get("b365_margin_assumed") or 0) - margin) > 1e-6:
        return False
    if abs(float(payload.get("close_line_tol") or 0) - close_tol) > 1e-6:
        return False
    if primary_book and (payload.get("primary_book") or "") != primary_book:
        return False
    if compare_books is not None:
        cached = list(payload.get("compare_books") or [])
        if cached != list(compare_books):
            return False
    adm = load_admiralbet_lines()
    if (payload.get("admiral_fetched_at") or None) != (adm.get("fetched_at") or None):
        return False
    board = load_props_board()
    if (payload.get("oddspapi_fetched_at") or None) != (
        board.get("fetched_at") if board.get("source") == "oddspapi" else None
    ):
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
    if (
        int(summ.get("exact_half_line_matches") or 0) == 0
        and int(summ.get("close_line_matches") or 0) == 0
        and int(summ.get("input_players") or 0) > 0
    ):
        return False
    return True


def comparison_table_df(
    payload: dict[str, Any] | None = None,
    *,
    exact_only: bool = True,
    tier: str | None = None,
    other_book: str | None = None,
):
    """Return a pandas DataFrame for st.dataframe (exact/close/far/all)."""
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
    rows = list(payload.get(key) or [])
    if other_book:
        rows = [r for r in rows if r.get("other_book") == other_book]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    primary = payload.get("primary_book") or "primary"
    rename = {
        "primary_over": f"{primary}_over",
        "primary_under": f"{primary}_under",
        "primary_synth_under": f"{primary}_synth_under",
        "primary_under_used": f"{primary}_under_used",
        "over_gap": f"over_gap ({primary}-other)",
        "under_gap": "under_gap (otherU-primaryU)",
        "primary_line": "line",
    }
    cols = [
        "flag",
        "match_tier",
        "event",
        "player",
        "team_hint",
        "milestone",
        "primary_line",
        "other_book",
        "other_line",
        "line_diff",
        "primary_over",
        "other_over",
        "over_gap",
        "primary_under",
        "primary_synth_under",
        "primary_under_used",
        "other_under",
        "under_gap",
        "primary_under_real",
        "other_under_real",
        "adm_team",
    ]
    cols = [c for c in cols if c in df.columns]
    out = df[cols].copy()
    for c in out.columns:
        if c in (
            "primary_line",
            "other_line",
            "line_diff",
            "primary_over",
            "other_over",
            "over_gap",
            "primary_under",
            "primary_synth_under",
            "primary_under_used",
            "other_under",
            "under_gap",
        ):
            out[c] = pd.to_numeric(out[c], errors="coerce").round(3)
    out = out.rename(columns=rename)
    gap_col = f"over_gap ({primary}-other)"
    if gap_col in out.columns:
        out["_gap_sort"] = out[gap_col].fillna(0.0)
        out = out.sort_values(by=["_gap_sort"], ascending=True).drop(columns=["_gap_sort"])
    return out.reset_index(drop=True)


def decorate_flags(df):
    """Emoji flags for Streamlit (no Styler backgrounds)."""
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
        if "primary longer" in s or "B365 longer" in s:
            return f"🟢 {s}"
        if s.startswith("close line"):
            return f"🟡 {s}"
        if s == "close":
            return "close"
        return s

    out["flag"] = out["flag"].map(_label)
    return out


def style_gap_table(df):
    return decorate_flags(df)


if __name__ == "__main__":
    payload = save_comparison()
    print(f"Wrote {COMPARE_PATH}")
    print("summary", payload["summary"])
    print("primary", payload.get("primary_book"), "vs", payload.get("compare_books"))
    print("\nBiggest primary-shorter Overs (exact):")
    for r in payload["biggest_over_gaps"][:12]:
        print(
            f"  {r['over_gap']:+.3f}  {r['player']:22} L{r['primary_line']} "
            f"{r['primary_book']} O={r['primary_over']}  "
            f"{r['other_book']} L{r['other_line']} O={r['other_over']} U={r['other_under']}"
        )
