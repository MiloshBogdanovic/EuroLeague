"""
Bet365 backup via RapidAPI (odds-papi Bet365 API).

Uses the same RapidAPI key as OddsPapi (`ODDSPAPI_API_KEY` / `RAPIDAPI_KEY`)
against host `bet36528.p.rapidapi.com` when OddsPapi quota is exhausted.

Writes `data/bet365_points_board.json` (Market cut prefers it when fresher).

CLI:
  python euro_bet365_rapid.py --refresh
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from euro_utils import DATA_DIR

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

BASE_URL_DEFAULT = "https://bet36528.p.rapidapi.com"
HOST_DEFAULT = "bet36528.p.rapidapi.com"
SPORT_BASKETBALL = 11
TOURNAMENT_EUROLEAGUE = 138  # slug euroleague

BOARD_PATH = DATA_DIR / "bet365_points_board.json"
MARKETS_CACHE_PATH = DATA_DIR / "_bet365_rapid_markets.json"
META_PATH = DATA_DIR / "bet365_rapid_meta.json"

BOOK_BET365 = "bet365"


def api_key() -> str | None:
    key = (
        os.environ.get("ODDSPAPI_API_KEY")
        or os.environ.get("RAPIDAPI_KEY")
        or os.environ.get("X_RAPIDAPI_KEY")
        or os.environ.get("BET365_RAPIDAPI_KEY")
        or ""
    ).strip().strip("'\"")
    return key or None


def api_ready() -> tuple[bool, str]:
    if not api_key():
        return False, "Set ODDSPAPI_API_KEY in .env (shared RapidAPI key)."
    return True, ""


def _base_url() -> str:
    return (
        os.environ.get("BET365_RAPIDAPI_BASE_URL") or BASE_URL_DEFAULT
    ).strip().rstrip("/")


def _headers() -> dict[str, str]:
    key = api_key() or ""
    host = (os.environ.get("BET365_RAPIDAPI_HOST") or HOST_DEFAULT).strip()
    return {
        "Accept": "application/json",
        "x-rapidapi-key": key,
        "x-rapidapi-host": host,
    }


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    if not api_key():
        raise RuntimeError("ODDSPAPI_API_KEY / RAPIDAPI_KEY not set")
    if not path.startswith("/"):
        path = "/" + path
    url = f"{_base_url()}{path}"
    r = requests.get(url, params=params or {}, headers=_headers(), timeout=90)
    if r.status_code == 429:
        time.sleep(2.0)
        r = requests.get(url, params=params or {}, headers=_headers(), timeout=90)
    if not r.ok:
        raise requests.HTTPError(
            f"{r.status_code} for {r.url.split('?')[0]}: {(r.text or '')[:300]}",
            response=r,
        )
    return r.json()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("'", "").replace("-", " ").replace(".", " ")
    return re.sub(r"\s+", " ", s).strip()


def _display_name(raw: str) -> str:
    """'Miller-McIntyre, Codi' → 'Codi Miller-McIntyre'."""
    s = str(raw or "").strip()
    if "," in s:
        last, first = s.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return s


def _milestone_to_line(ms: str) -> float | None:
    try:
        n = int(str(ms).rstrip("+"))
    except ValueError:
        return None
    return float(n) - 0.5


def load_markets_catalog(*, force: bool = False) -> dict[str, dict[str, Any]]:
    """marketId(str) → market meta (cached)."""
    if not force and MARKETS_CACHE_PATH.exists():
        try:
            data = json.loads(MARKETS_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("by_id"):
                return {str(k): v for k, v in data["by_id"].items()}
        except (OSError, json.JSONDecodeError):
            pass
    rows = _get("/markets", {"sportId": SPORT_BASKETBALL})
    by_id = {str(m["marketId"]): m for m in rows if isinstance(m, dict) and m.get("marketId") is not None}
    try:
        MARKETS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MARKETS_CACHE_PATH.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sportId": SPORT_BASKETBALL,
                    "count": len(by_id),
                    "by_id": by_id,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    return by_id


def fetch_euroleague_fixtures(*, has_odds_only: bool = True) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"tournamentId": TOURNAMENT_EUROLEAGUE}
    if has_odds_only:
        params["hasOdds"] = "true"
    data = _get("/fixtures", params)
    if not isinstance(data, list):
        return []
    return [fx for fx in data if isinstance(fx, dict)]


def upcoming_with_odds(
    fixtures: list[dict[str, Any]] | None = None,
    *,
    within_days: float = 3.0,
) -> list[dict[str, Any]]:
    fixtures = fixtures if fixtures is not None else fetch_euroleague_fixtures(has_odds_only=True)
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=within_days)
    out = []
    for fx in fixtures:
        if not fx.get("hasOdds"):
            continue
        st = fx.get("startTime") or ""
        try:
            tip = datetime.fromisoformat(str(st).replace("Z", "+00:00"))
        except ValueError:
            continue
        if tip < now - timedelta(hours=3) or tip > end:
            continue
        out.append(fx)
    out.sort(key=lambda x: str(x.get("startTime") or ""))
    return out


def fetch_fixture_odds(fixture_id: str) -> dict[str, Any]:
    return _get("/odds", {"fixtureId": fixture_id})


def _outcome_name_map(meta: dict[str, Any]) -> dict[str, str]:
    return {
        str(o["outcomeId"]): str(o.get("outcomeName") or "")
        for o in (meta.get("outcomes") or [])
        if isinstance(o, dict) and o.get("outcomeId") is not None
    }


def parse_players_points_rows(
    odds_payload: dict[str, Any],
    markets: dict[str, dict[str, Any]],
    *,
    event: str,
    tip_off: str | None,
    fixture_id: str,
) -> list[dict[str, Any]]:
    """Extract bet365 Player Points N+ overs from /odds payload."""
    bm = ((odds_payload.get("bookmakerOdds") or {}).get("bet365") or {}).get("markets") or {}
    rows: list[dict[str, Any]] = []
    for mid, market in bm.items():
        meta = markets.get(str(mid))
        if not meta:
            continue
        if meta.get("marketType") != "players-points":
            continue
        # Skip 3PM / combined markets by name
        mname = str(meta.get("marketName") or "").lower()
        if "3 point" in mname or "3pt" in mname:
            continue
        if "assist" in mname or "rebound" in mname:
            continue
        onames = _outcome_name_map(meta)
        for oid, oc in (market.get("outcomes") or {}).items():
            ms = onames.get(str(oid)) or ""
            if not re.fullmatch(r"\d+\+", str(ms).strip()):
                continue
            for _pid, pl in (oc.get("players") or {}).items():
                if not isinstance(pl, dict):
                    continue
                price = pl.get("price")
                if price is None:
                    continue
                # Prefer active quotes; still keep priced lines if book left market flagged inactive
                if pl.get("active") is False:
                    continue
                raw_name = pl.get("playerName") or ""
                if not raw_name:
                    continue
                name = _display_name(raw_name)
                line = _milestone_to_line(ms)
                rows.append(
                    {
                        "book": BOOK_BET365,
                        "fixture_id": fixture_id,
                        "event": event,
                        "tip_off": tip_off,
                        "player": name,
                        "player_raw": raw_name,
                        "team_hint": None,
                        "market": "Points",
                        "milestone": ms,
                        "line": line,
                        "over_odds": float(price),
                        "under_odds": None,
                        "has_real_under": False,
                    }
                )
    return rows


def _rows_to_board(rows: list[dict[str, Any]], *, fetched_at: str) -> dict[str, Any]:
    games_map: dict[str, dict[str, Any]] = {}
    for r in rows:
        fid = str(r.get("fixture_id") or r.get("event") or "")
        g = games_map.setdefault(
            fid,
            {
                "event": r.get("event") or fid,
                "tip_off": r.get("tip_off"),
                "fixture_id": r.get("fixture_id"),
                "players_map": {},
            },
        )
        key = _norm(str(r["player"]))
        p = g["players_map"].setdefault(
            key,
            {
                "player": r["player"],
                "team_hint": r.get("team_hint"),
                "odds": {},
                "odds_by_book": {BOOK_BET365: {}},
            },
        )
        ms = r.get("milestone")
        over = r.get("over_odds")
        if ms and over is not None:
            p["odds"][str(ms)] = round(float(over), 3)
            p["odds_by_book"][BOOK_BET365][str(ms)] = round(float(over), 3)

    games = []
    for g in games_map.values():
        players = []
        for p in g["players_map"].values():
            players.append(
                {
                    "player": p["player"],
                    "team_hint": p.get("team_hint"),
                    "odds": p["odds"],
                    "odds_by_book": p["odds_by_book"],
                }
            )
        players.sort(key=lambda x: _norm(x["player"]))
        games.append(
            {
                "event": g["event"],
                "tip_off": g.get("tip_off"),
                "fixture_id": g.get("fixture_id"),
                "players": players,
            }
        )
    games.sort(key=lambda x: str(x.get("tip_off") or ""))
    n_players = sum(len(g["players"]) for g in games)
    return {
        "source": "bet365 rapidapi",
        "fetched_at": fetched_at,
        "note": (
            "Player Points N+ overs from RapidAPI Bet365 API "
            f"({HOST_DEFAULT}). N+ ↔ Over (N-0.5)."
        ),
        "summary": {"games": len(games), "players": n_players, "quotes": len(rows)},
        "games": games,
    }


def refresh_euroleague_bet365_points(
    *,
    within_days: float = 3.0,
    force_markets: bool = False,
) -> dict[str, Any]:
    """
    Pull upcoming EuroLeague fixtures with odds and write bet365_points_board.json.
    """
    ready, reason = api_ready()
    if not ready:
        return {"error": reason, "saved_to": None, "summary": {}}

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets = load_markets_catalog(force=force_markets)
    fixtures = upcoming_with_odds(within_days=within_days)
    all_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for fx in fixtures:
        fid = str(fx.get("fixtureId") or "")
        if not fid:
            continue
        home = fx.get("participant1Name") or fx.get("participant1ShortName") or "?"
        away = fx.get("participant2Name") or fx.get("participant2ShortName") or "?"
        event = f"{home} vs {away}"
        tip = fx.get("startTime")
        try:
            odds = fetch_fixture_odds(fid)
            rows = parse_players_points_rows(
                odds,
                markets,
                event=event,
                tip_off=tip,
                fixture_id=fid,
            )
            all_rows.extend(rows)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fid}: {exc}")

    board = _rows_to_board(all_rows, fetched_at=fetched_at)
    BOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOARD_PATH.write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    meta = {
        "fetched_at": fetched_at,
        "source": "bet365 rapidapi",
        "host": HOST_DEFAULT,
        "fixtures_considered": len(fixtures),
        "summary": board.get("summary"),
        "errors": errors,
        "board_path": str(BOARD_PATH),
    }
    try:
        META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass

    out = {
        "saved_to": str(BOARD_PATH),
        "fetched_at": fetched_at,
        "summary": board.get("summary"),
        "fixtures": [
            {
                "fixtureId": fx.get("fixtureId"),
                "event": f"{fx.get('participant1Name')} vs {fx.get('participant2Name')}",
                "startTime": fx.get("startTime"),
            }
            for fx in fixtures
        ],
        "errors": errors,
    }
    if not all_rows and not fixtures:
        out["error"] = "No upcoming EuroLeague fixtures with Bet365 odds in the window."
    elif not all_rows:
        out["error"] = "Fixtures found but no Player Points milestone quotes parsed."
    return out


def load_bet365_rapid_meta() -> dict[str, Any]:
    if not META_PATH.exists():
        return {}
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Fetch and write board")
    parser.add_argument("--days", type=float, default=3.0, help="Upcoming window (days)")
    parser.add_argument("--force-markets", action="store_true", help="Refresh markets cache")
    args = parser.parse_args()
    result = refresh_euroleague_bet365_points(
        within_days=args.days,
        force_markets=args.force_markets,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
