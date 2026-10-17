"""
OddsPapi EuroLeague player props (bet365, 1xBet, Superbet RS).

RapidAPI paths: GET /fixtures + GET /fixtures/odds (near-term fixtures only).
Mozzart/MaxBet are skipped — no player props on this feed. Refresh is manual.
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

# --- constants -------------------------------------------------------------

SPORT_ID_BASKETBALL = 11
TOURNAMENT_ID_EUROLEAGUE = 138

# Preferred slugs — Mozzart/MaxBet have no player props on OddsPapi; use Superbet.
BOOK_BET365 = "bet365"
BOOK_1XBET = "1xbet"
BOOK_SUPERBET = "superbet.rs"
BOOK_MOZZART_CANDIDATES = (
    "mozzartbet",
    "maxbet.rs",
    "maxbet.ro",
    "mozzart",
    "mozzart-bet",
    "maxbet",
    "max-bet",
    "mozzart.rs",
)

DEFAULT_BOOKS = (BOOK_BET365, BOOK_1XBET, BOOK_SUPERBET)
FIXTURE_HORIZON_DAYS = 14
MAX_FIXTURES_PER_REFRESH = 12

# Shown when fixtures load but player ladders are still empty (common days before tip).
EMPTY_PROPS_NOTE = (
    "Fixtures loaded, but EuroLeague player props (5+/10+/…) are not on the OddsPapi "
    "board yet — books often open ladders closer to tip-off / gameday. "
    "Re-run refresh nearer to the games to continue testing "
    "(python euro_oddspapi.py --refresh)."
)

# Documented player-prop market families (verified / overridden via /markets).
DEFAULT_PROP_MARKET_HINTS: dict[str, str] = {
    "112254": "Points",
    "112305": "TotalRebounds",
    "112389": "Assists",
}

FOCUS_MILESTONES = (5, 10, 15, 20, 25, 30, 35)

# Direct OddsPapi uses /v4; RapidAPI listing is host-root (see .env.example).
BASE_URL_DIRECT = "https://api.oddspapi.io/v4"
BASE_URL_RAPIDAPI = "https://odds-api1.p.rapidapi.com"
RAPIDAPI_HOST_DEFAULT = "odds-api1.p.rapidapi.com"
BASE_URL_DEFAULT = BASE_URL_RAPIDAPI
REQUEST_SLEEP_SEC = 0.55

LINES_DIR = DATA_DIR
UNIFIED_PATH = DATA_DIR / "oddspapi_props.json"
BOARD_PATH = DATA_DIR / "oddspapi_points_board.json"  # Bet365-shaped for compare
MARKETS_CACHE_PATH = DATA_DIR / "oddspapi_markets_basketball.json"
BOOKS_CACHE_PATH = DATA_DIR / "oddspapi_bookmakers.json"

PER_BOOK_PATHS = {
    BOOK_BET365: DATA_DIR / "oddspapi_bet365.json",
    BOOK_1XBET: DATA_DIR / "oddspapi_1xbet.json",
    BOOK_SUPERBET: DATA_DIR / "oddspapi_superbet_rs.json",
}


def _book_path(slug: str) -> Path:
    if slug in PER_BOOK_PATHS:
        return PER_BOOK_PATHS[slug]
    return DATA_DIR / f"oddspapi_{slug.replace('.', '_')}.json"


# --- auth / HTTP -----------------------------------------------------------


def api_key() -> str | None:
    key = (
        os.environ.get("ODDSPAPI_API_KEY")
        or os.environ.get("RAPIDAPI_KEY")
        or os.environ.get("X_RAPIDAPI_KEY")
        or ""
    ).strip()
    # Strip accidental quotes from .env (showed up as %27 in failed URLs)
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        key = key[1:-1].strip()
    key = key.strip("'\"")
    return key or None


def api_ready() -> tuple[bool, str]:
    if not api_key():
        return False, "Set ODDSPAPI_API_KEY in .env (see .env.example)."
    return True, ""


def _uses_rapidapi() -> bool:
    base = (os.environ.get("ODDSPAPI_BASE_URL") or BASE_URL_DEFAULT).lower()
    host = (os.environ.get("ODDSPAPI_RAPIDAPI_HOST") or "").lower()
    return "rapidapi.com" in base or bool(host) or "msh" in (api_key() or "")


def _base_url() -> str:
    explicit = (os.environ.get("ODDSPAPI_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if _uses_rapidapi():
        return BASE_URL_RAPIDAPI
    return BASE_URL_DIRECT


def _headers() -> dict[str, str]:
    key = api_key() or ""
    headers = {"Accept": "application/json"}
    if _uses_rapidapi():
        host = (os.environ.get("ODDSPAPI_RAPIDAPI_HOST") or "").strip() or RAPIDAPI_HOST_DEFAULT
        headers["x-rapidapi-key"] = key
        headers["x-rapidapi-host"] = host
    return headers


def _normalize_path(path: str) -> str:
    """RapidAPI host is root; direct OddsPapi expects /v4/..."""
    if not path.startswith("/"):
        path = "/" + path
    base = _base_url()
    if base.rstrip("/").endswith("/v4"):
        return path
    # RapidAPI: try /v4/... first (OddsPapi catalog); callers pass "/bookmakers"
    if path.startswith("/v4/"):
        return path
    return path


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    key = api_key()
    if not key:
        raise RuntimeError("ODDSPAPI_API_KEY not set")
    path = _normalize_path(path)
    url = f"{_base_url()}{path}"
    q = dict(params or {})
    # Direct OddsPapi auth is query apiKey; RapidAPI uses headers only.
    if not _uses_rapidapi():
        q.setdefault("apiKey", key)
    r = requests.get(url, params=q, headers=_headers(), timeout=60)
    # Some RapidAPI deployments mount under /v4 — retry once on 404
    if r.status_code == 404 and _uses_rapidapi() and not path.startswith("/v4/"):
        alt = f"{_base_url()}/v4{path}"
        r = requests.get(alt, params=q, headers=_headers(), timeout=60)
        url = alt
    if r.status_code == 429:
        time.sleep(2.0)
        r = requests.get(url, params=q, headers=_headers(), timeout=60)
    if not r.ok:
        # Include a short body snippet for debugging (no full key in message)
        snippet = (r.text or "")[:300]
        raise requests.HTTPError(
            f"{r.status_code} for {r.url.split('?')[0]}: {snippet}",
            response=r,
        )
    return r.json()


# --- name / milestone helpers ---------------------------------------------


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("'", "").replace("-", " ").replace(".", " ")
    return re.sub(r"\s+", " ", s).strip()


def _milestone_from_text(*parts: object) -> str | None:
    """Parse '10+', 'Over 10.5', '9.5/over', handicap 10 -> '10+' (Over only)."""
    blob = " ".join(str(p) for p in parts if p is not None).strip().lower()
    if not blob:
        return None
    if re.search(r"\bunder\b", blob) and not re.search(r"\bover\b", blob) and "+" not in blob:
        return None
    m = re.search(r"\b(\d{1,2})\s*\+", blob)
    if m:
        return f"{int(m.group(1))}+"
    m = re.search(r"(?:over|under)?\s*(\d{1,2}(?:\.5)?)", blob)
    if m:
        val = float(m.group(1))
        # Classic O/U half-line N.5 ↔ milestone (N+1)+
        if abs(val - int(val) - 0.5) < 1e-6:
            return f"{int(val) + 1}+"
        if abs(val - int(val)) < 1e-6 and "over" in blob:
            return f"{int(val)}+"
    return None


def _milestone_from_handicap(handicap: object, outcome_side: str | None) -> str | None:
    try:
        h = float(handicap)
    except (TypeError, ValueError):
        return None
    side = (outcome_side or "").lower()
    if "under" in side:
        return None  # we only keep Over / milestone overs for the board
    if abs(h - int(h) - 0.5) < 1e-6:
        return f"{int(h) + 1}+"
    if abs(h - int(h)) < 1e-6:
        return f"{int(h)}+"
    return None


def _is_points_market(meta: dict[str, Any] | None, market_id: str) -> bool:
    if market_id in DEFAULT_PROP_MARKET_HINTS:
        return DEFAULT_PROP_MARKET_HINTS[market_id] == "Points"
    if not meta:
        return False
    mt = _norm(str(meta.get("marketType") or meta.get("marketName") or ""))
    if "rebound" in mt or "assist" in mt or "three" in mt or "3pt" in mt:
        return False
    if "point" in mt or "score" in mt or mt.endswith("pts"):
        return True
    # playerProp + totals-like name
    if meta.get("playerProp") and ("player" in mt or "prop" in mt):
        return "point" in mt or "pts" in mt
    return False


def _stat_for_market(meta: dict[str, Any] | None, market_id: str) -> str | None:
    if market_id in DEFAULT_PROP_MARKET_HINTS:
        return DEFAULT_PROP_MARKET_HINTS[market_id]
    if not meta:
        return None
    mt = _norm(str(meta.get("marketType") or meta.get("marketName") or ""))
    if "rebound" in mt:
        return "TotalRebounds"
    if "assist" in mt:
        return "Assists"
    if "three" in mt or "3pt" in mt or "3-pt" in mt:
        return "FGM3"
    if "point" in mt or "score" in mt:
        return "Points"
    return None


# --- catalog ---------------------------------------------------------------


def fetch_bookmakers(*, player_props_only: bool = False) -> list[dict[str, Any]]:
    """Fetch bookmaker catalog. Default = full list (Mozzart/MaxBet are not playerProps-flagged)."""
    params: dict[str, Any] = {}
    if player_props_only:
        params["playerProps"] = "true"
    data = _get("/bookmakers", params)
    if isinstance(data, dict):
        data = data.get("bookmakers") or data.get("data") or []
    if not isinstance(data, list):
        return []
    try:
        BOOKS_CACHE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


def resolve_mozzart_slug(bookmakers: list[dict[str, Any]] | None = None) -> str | None:
    books = bookmakers if bookmakers is not None else []
    if not books and BOOKS_CACHE_PATH.exists():
        try:
            books = json.loads(BOOKS_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            books = []
    if not books:
        try:
            books = fetch_bookmakers(player_props_only=False)
        except Exception:  # noqa: BLE001
            books = []

    by_slug = {_norm(str(b.get("slug") or "")): str(b.get("slug") or "") for b in books}
    for cand in BOOK_MOZZART_CANDIDATES:
        if _norm(cand) in by_slug:
            return by_slug[_norm(cand)]

    # Fuzzy: name contains mozzart or maxbet (prefer RS)
    fuzzy: list[tuple[int, str]] = []
    for b in books:
        slug = str(b.get("slug") or "")
        name = _norm(str(b.get("bookmakerName") or ""))
        sn = _norm(slug)
        score = 0
        if "mozzart" in name or "mozzart" in sn:
            score = 20
        elif "maxbet" in name.replace(" ", "") or "max bet" in name or "maxbet" in sn:
            score = 10
        if score:
            if sn.endswith(" rs") or sn.endswith(".rs") or ".rs" in sn:
                score += 5
            fuzzy.append((score, slug))
    if fuzzy:
        fuzzy.sort(key=lambda x: -x[0])
        return fuzzy[0][1]
    return None


def target_book_slugs(*, refresh_catalog: bool = False) -> list[str]:
    """
    Return up to 3 slugs: bet365, 1xbet, superbet.rs.

    Mozzart/MaxBet are listed by OddsPapi but without player props — Superbet RS
    is the regional third book that actually carries props on this feed.
    """
    if refresh_catalog or not BOOKS_CACHE_PATH.exists():
        try:
            fetch_bookmakers(player_props_only=False)
            time.sleep(REQUEST_SLEEP_SEC)
        except Exception:  # noqa: BLE001
            pass
    return [BOOK_BET365, BOOK_1XBET, BOOK_SUPERBET]


def fetch_markets_basketball(*, force: bool = False) -> dict[str, dict[str, Any]]:
    """Return marketId -> meta. Cached to save quota."""
    if not force and MARKETS_CACHE_PATH.exists():
        try:
            raw = json.loads(MARKETS_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("by_id"):
                return {str(k): v for k, v in raw["by_id"].items()}
        except (OSError, json.JSONDecodeError):
            pass

    data = _get("/markets", {"sportId": SPORT_ID_BASKETBALL})
    if isinstance(data, dict):
        items = data.get("markets") or data.get("data") or []
    else:
        items = data if isinstance(data, list) else []

    by_id: dict[str, dict[str, Any]] = {}
    for m in items:
        mid = str(m.get("marketId") or m.get("id") or "")
        if not mid:
            continue
        by_id[mid] = m
        # Also seed DEFAULT hints from name when matching known families
    payload = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sportId": SPORT_ID_BASKETBALL,
        "count": len(by_id),
        "by_id": by_id,
    }
    try:
        MARKETS_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return by_id


# --- extract props from odds payload --------------------------------------


def _iter_player_prices(
    market_block: dict[str, Any],
) -> list[tuple[str, str | None, float, dict[str, Any]]]:
    """
    Yield (outcome_id, player_name, price, price_obj) for non-zero player keys
    and any row that carries playerName.
    """
    out: list[tuple[str, str | None, float, dict[str, Any]]] = []
    outcomes = market_block.get("outcomes") or {}
    for oid, outcome in outcomes.items():
        players = (outcome or {}).get("players") or {}
        for pid, price_obj in players.items():
            if not price_obj or not price_obj.get("active", True):
                continue
            price = price_obj.get("price")
            if price is None:
                continue
            try:
                price_f = float(price)
            except (TypeError, ValueError):
                continue
            pname = price_obj.get("playerName")
            if pname is None and str(pid) in ("0", ""):
                continue
            if pname is None and str(pid) not in ("0", ""):
                pname = None  # id-only; keep if we can still attach milestone
            out.append((str(oid), pname, price_f, price_obj))
    return out


def _outcome_side_label(meta: dict[str, Any] | None, outcome_id: str, price_obj: dict[str, Any]) -> str:
    parts = [
        price_obj.get("bookmakerOutcomeId"),
        outcome_id,
    ]
    if meta:
        outcomes = meta.get("outcomes") or {}
        if isinstance(outcomes, dict) and outcome_id in outcomes:
            om = outcomes[outcome_id]
            if isinstance(om, dict):
                parts.extend([om.get("outcomeName"), om.get("name"), om.get("shortName")])
            else:
                parts.append(om)
        parts.extend([meta.get("marketName"), meta.get("marketType"), meta.get("handicap")])
    return " ".join(str(p) for p in parts if p is not None)


def _fixture_participants(fixture: dict[str, Any]) -> tuple[str, str]:
    parts = fixture.get("participants") or {}
    if isinstance(parts, dict) and parts:
        home = parts.get("participant1Name") or parts.get("participant1ShortName") or ""
        away = parts.get("participant2Name") or parts.get("participant2ShortName") or ""
        return str(home), str(away)
    home = fixture.get("participant1Name") or fixture.get("participant1ShortName") or ""
    away = fixture.get("participant2Name") or fixture.get("participant2ShortName") or ""
    return str(home), str(away)


def _fixture_start_iso(fixture: dict[str, Any]) -> str | None:
    tip = fixture.get("startTime") or fixture.get("trueStartTime")
    if tip is None:
        return None
    if isinstance(tip, (int, float)):
        # RapidAPI often returns unix seconds
        ts = float(tip)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(tip)


def _outcome_name_from_meta(meta: dict[str, Any] | None, outcome_id: object) -> str | None:
    if not meta:
        return None
    oid = int(outcome_id) if str(outcome_id).isdigit() else outcome_id
    for oc in meta.get("outcomes") or []:
        if isinstance(oc, dict) and oc.get("outcomeId") == oid:
            return str(oc.get("outcomeName") or "") or None
    return None


def _milestone_for_rapid_row(
    row: dict[str, Any],
    markets_meta: dict[str, dict[str, Any]],
) -> str | None:
    mid = str(row.get("marketId") or "")
    oid = row.get("outcomeId")
    meta = markets_meta.get(mid)
    oname = _outcome_name_from_meta(meta, oid)
    if oname:
        ms = _milestone_from_text(oname)
        if ms:
            return ms
    # Classic O/U player totals: market handicap + Over outcome
    mt = _norm(str((meta or {}).get("marketType") or ""))
    if meta and "playertotals" in mt.replace(" ", ""):
        side = (oname or "").lower()
        if "under" in side:
            return None
        return _milestone_from_handicap(meta.get("handicap"), side or "over")
    return _milestone_from_text(oname, row.get("bookmakerOutcomeId"))


def fetch_euroleague_fixtures() -> list[dict[str, Any]]:
    """GET /fixtures?tournamentId=138 (RapidAPI)."""
    data = _get("/fixtures", {"tournamentId": TOURNAMENT_ID_EUROLEAGUE})
    if isinstance(data, list):
        return [f for f in data if isinstance(f, dict)]
    if isinstance(data, dict):
        return [f for f in (data.get("fixtures") or data.get("data") or []) if isinstance(f, dict)]
    return []


def select_fixtures_for_refresh(
    fixtures: list[dict[str, Any]],
    *,
    horizon_days: int = FIXTURE_HORIZON_DAYS,
    limit: int = MAX_FIXTURES_PER_REFRESH,
) -> list[dict[str, Any]]:
    """Prefer near-term pre-game fixtures to stay inside free-tier quota."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=max(1, horizon_days))
    scored: list[tuple[datetime, dict[str, Any]]] = []
    for f in fixtures:
        st = f.get("status") or {}
        sid = st.get("statusId") if isinstance(st, dict) else f.get("statusId")
        if sid not in (0, 1, None):
            # Keep unknown/None; drop finished/live if clearly marked
            if isinstance(st, dict) and (st.get("live") or sid in (2, 3)):
                continue
        tip_iso = _fixture_start_iso(f)
        if not tip_iso:
            continue
        try:
            tip_dt = datetime.fromisoformat(tip_iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        if tip_dt < now - timedelta(hours=2):
            continue
        if tip_dt > horizon:
            continue
        scored.append((tip_dt, f))
    scored.sort(key=lambda x: x[0])
    return [f for _, f in scored[: max(1, limit)]]


def fetch_fixture_odds(fixture_id: str, book_slugs: list[str]) -> dict[str, Any]:
    """GET /fixtures/odds — RapidAPI nested odds-by-book shape."""
    return _get(
        "/fixtures/odds",
        {
            "fixtureId": fixture_id,
            "bookmakers": ",".join(book_slugs[:3]),
        },
    )


def resolve_player_names(player_ids: list[int | str]) -> dict[str, str]:
    """Batch GET /players?playerIds=…"""
    ids = []
    for p in player_ids:
        try:
            ids.append(str(int(p)))
        except (TypeError, ValueError):
            continue
    ids = sorted(set(ids))
    out: dict[str, str] = {}
    # Keep batches small
    for i in range(0, len(ids), 40):
        chunk = ids[i : i + 40]
        data = _get("/players", {"playerIds": ",".join(chunk)})
        rows = data if isinstance(data, list) else (data.get("players") or data.get("data") or [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("playerId") or "")
            name = row.get("playerName") or row.get("name")
            if pid and name:
                # "White, Derrick" → "Derrick White"
                name_s = str(name)
                if "," in name_s:
                    last, first = [x.strip() for x in name_s.split(",", 1)]
                    name_s = f"{first} {last}".strip()
                out[pid] = name_s
        if i + 40 < len(ids):
            time.sleep(REQUEST_SLEEP_SEC)
    return out


def extract_rows_from_fixture(
    fixture: dict[str, Any],
    *,
    book_slugs: list[str],
    markets_meta: dict[str, dict[str, Any]],
    player_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Flatten player-prop Over/milestone prices.

    Supports RapidAPI nested `odds[book][oddsId]` and legacy v4 `bookmakerOdds`.
    """
    rows: list[dict[str, Any]] = []
    home, away = _fixture_participants(fixture)
    event = f"{home} vs {away}".strip(" vs")
    tip = _fixture_start_iso(fixture)
    fid = fixture.get("fixtureId")
    player_names = player_names or {}

    # RapidAPI: odds nested by book
    rapid_odds = fixture.get("odds")
    if isinstance(rapid_odds, dict) and rapid_odds:
        wanted = set(book_slugs)
        for book, block in rapid_odds.items():
            if book not in wanted or not isinstance(block, dict):
                continue
            for _oid_key, row in block.items():
                if not isinstance(row, dict) or not row.get("active", True):
                    continue
                pid = int(row.get("playerId") or 0)
                if pid == 0:
                    continue
                mid_s = str(row.get("marketId") or "")
                meta = markets_meta.get(mid_s)
                stat = _stat_for_market(meta, mid_s)
                if stat != "Points":
                    # Still accept players-points / playertotals-points by type
                    mt = _norm(str((meta or {}).get("marketType") or ""))
                    if "point" not in mt:
                        continue
                    stat = "Points"
                milestone = _milestone_for_rapid_row(row, markets_meta)
                if not milestone:
                    continue
                try:
                    n = int(milestone.rstrip("+"))
                except ValueError:
                    continue
                if n not in FOCUS_MILESTONES:
                    continue
                price = row.get("price")
                try:
                    price_f = float(price)
                except (TypeError, ValueError):
                    continue
                pname = player_names.get(str(pid)) or row.get("playerName")
                rows.append(
                    {
                        "book": book,
                        "player": pname or f"player:{pid}",
                        "player_id": pid,
                        "team": None,
                        "team_hint": None,
                        "market": "Points",
                        "milestone": milestone,
                        "line": float(n) - 0.5,
                        "over_odds": price_f,
                        "under_odds": None,
                        "fixture_id": fid,
                        "event": event,
                        "tip_off": tip,
                        "home": home,
                        "away": away,
                        "market_id": mid_s,
                        "outcome_id": row.get("outcomeId"),
                        "main_line": bool(row.get("mainLine")),
                    }
                )
        return rows

    # Legacy v4 bookmakerOdds shape
    bookmaker_odds = fixture.get("bookmakerOdds") or {}
    for book in book_slugs:
        feed = bookmaker_odds.get(book)
        if not feed:
            continue
        markets = feed.get("markets") or {}
        for mid, mblock in markets.items():
            mid_s = str(mid)
            meta = markets_meta.get(mid_s)
            stat = _stat_for_market(meta, mid_s) or (
                DEFAULT_PROP_MARKET_HINTS.get(mid_s)
            )
            if stat != "Points":
                continue
            handicap = (meta or {}).get("handicap")
            for oid, pname, price, price_obj in _iter_player_prices(mblock):
                side = _outcome_side_label(meta, oid, price_obj)
                if "under" in side.lower() and "+" not in side:
                    continue
                milestone = _milestone_from_text(side, price_obj.get("bookmakerOutcomeId"))
                if milestone is None and handicap is not None:
                    milestone = _milestone_from_handicap(handicap, side)
                if milestone is None:
                    continue
                try:
                    n = int(milestone.rstrip("+"))
                except ValueError:
                    continue
                if n not in FOCUS_MILESTONES:
                    continue
                rows.append(
                    {
                        "book": book,
                        "player": pname or f"player:{price_obj.get('bookmakerOutcomeId') or oid}",
                        "team": None,
                        "team_hint": None,
                        "market": "Points",
                        "milestone": milestone,
                        "line": float(n) - 0.5,
                        "over_odds": price,
                        "under_odds": None,
                        "fixture_id": fid,
                        "event": event,
                        "tip_off": tip,
                        "home": home,
                        "away": away,
                        "market_id": mid_s,
                        "outcome_id": oid,
                        "main_line": bool(price_obj.get("mainLine")),
                    }
                )
    return rows


def _rows_to_board(rows: list[dict[str, Any]], *, points_only: bool = True) -> dict[str, Any]:
    """
    Build unified multi-book board + Bet365-primary legacy games[] shape.
    """
    use = [r for r in rows if (not points_only or r.get("market") == "Points")]
    # Drop nameless placeholders
    use = [r for r in use if r.get("player") and not str(r["player"]).startswith("player:")]

    games_map: dict[str, dict[str, Any]] = {}
    for r in use:
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
        pname = str(r["player"])
        book = str(r["book"])
        ms = str(r["milestone"])
        p = g["players_map"].setdefault(
            _norm(pname),
            {"player": pname, "team_hint": r.get("team_hint"), "odds_by_book": {}},
        )
        if r.get("team_hint") and not p.get("team_hint"):
            p["team_hint"] = r["team_hint"]
        book_odds = p["odds_by_book"].setdefault(book, {})
        # Prefer shorter (more likely main) price if duplicate
        prev = book_odds.get(ms)
        if prev is None or float(r["over_odds"]) < float(prev):
            book_odds[ms] = round(float(r["over_odds"]), 3)

    games: list[dict[str, Any]] = []
    for g in games_map.values():
        players = []
        for p in g["players_map"].values():
            # Legacy single odds map: prefer bet365, else first book
            odds = dict(p["odds_by_book"].get(BOOK_BET365) or {})
            if not odds:
                for b_odds in p["odds_by_book"].values():
                    odds = dict(b_odds)
                    break
            players.append(
                {
                    "player": p["player"],
                    "team_hint": p.get("team_hint"),
                    "odds": odds,
                    "odds_by_book": p["odds_by_book"],
                }
            )
        players.sort(key=lambda x: _norm(x["player"]))
        games.append(
            {
                "event": g["event"],
                "tip_off": g["tip_off"],
                "fixture_id": g.get("fixture_id"),
                "players": players,
            }
        )
    games.sort(key=lambda x: str(x.get("tip_off") or ""))

    return {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "oddspapi",
        "competition": "EuroLeague",
        "tournamentId": TOURNAMENT_ID_EUROLEAGUE,
        "games": games,
    }


def _split_per_book(unified: dict[str, Any], book_slugs: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for book in book_slugs:
        games = []
        for g in unified.get("games") or []:
            players = []
            for p in g.get("players") or []:
                odds = (p.get("odds_by_book") or {}).get(book) or {}
                if not odds:
                    continue
                players.append(
                    {
                        "player": p.get("player"),
                        "team_hint": p.get("team_hint"),
                        "odds": odds,
                    }
                )
            if players:
                games.append(
                    {
                        "event": g.get("event"),
                        "tip_off": g.get("tip_off"),
                        "fixture_id": g.get("fixture_id"),
                        "players": players,
                    }
                )
        out[book] = {
            "fetched_at": unified.get("fetched_at"),
            "source": "oddspapi",
            "book": book,
            "competition": "EuroLeague",
            "games": games,
            "summary": {
                "games": len(games),
                "players": sum(len(g["players"]) for g in games),
            },
        }
    return out


# --- public refresh / load -------------------------------------------------


def refresh_euroleague_props(
    *,
    book_slugs: list[str] | None = None,
    refresh_markets: bool = False,
    refresh_catalog: bool = False,
    horizon_days: int = FIXTURE_HORIZON_DAYS,
    max_fixtures: int = MAX_FIXTURES_PER_REFRESH,
) -> dict[str, Any]:
    """
    Pull EuroLeague player props via RapidAPI:
      1) GET /fixtures?tournamentId=138
      2) GET /fixtures/odds per near-term fixture (bet365,1xbet,superbet.rs)
      3) GET /players for name resolution

    Mozzart/MaxBet are skipped (no player props on this feed).
    """
    ok, reason = api_ready()
    if not ok:
        return {"error": reason, "ok": False}

    slugs = book_slugs or target_book_slugs(refresh_catalog=refresh_catalog)
    if len(slugs) < 2:
        return {"error": f"Need at least 2 book slugs, got {slugs}", "ok": False}

    markets_meta = fetch_markets_basketball(force=refresh_markets)
    time.sleep(REQUEST_SLEEP_SEC)

    all_fixtures = fetch_euroleague_fixtures()
    time.sleep(REQUEST_SLEEP_SEC)
    selected = select_fixtures_for_refresh(
        all_fixtures, horizon_days=horizon_days, limit=max_fixtures
    )
    if not selected and all_fixtures:
        # Fall back to soonest pre-game fixtures even outside horizon
        selected = select_fixtures_for_refresh(
            all_fixtures, horizon_days=21, limit=max_fixtures
        )

    merged_fixtures: list[dict[str, Any]] = []
    for i, fx in enumerate(selected):
        fid = str(fx.get("fixtureId") or "")
        if not fid:
            continue
        try:
            odds_payload = fetch_fixture_odds(fid, slugs)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"fixtures/odds failed for {fid}: {exc}", "books": slugs}
        # Merge odds onto fixture metadata
        if isinstance(odds_payload, dict):
            merged = dict(fx)
            merged["odds"] = odds_payload.get("odds") or {}
            # Prefer richer participant block from odds payload when present
            if odds_payload.get("participants"):
                merged["participants"] = odds_payload["participants"]
            merged_fixtures.append(merged)
        if i + 1 < len(selected):
            time.sleep(REQUEST_SLEEP_SEC)

    # Collect player ids then resolve names (1 extra request batch)
    player_ids: set[int] = set()
    for fx in merged_fixtures:
        odds = fx.get("odds") or {}
        for block in odds.values():
            if not isinstance(block, dict):
                continue
            for row in block.values():
                if isinstance(row, dict) and int(row.get("playerId") or 0) != 0:
                    player_ids.add(int(row["playerId"]))

    player_names: dict[str, str] = {}
    if player_ids:
        time.sleep(REQUEST_SLEEP_SEC)
        try:
            player_names = resolve_player_names(sorted(player_ids))
        except Exception as exc:  # noqa: BLE001
            # Non-fatal — board can still use player:ID placeholders filtered out later
            player_names = {}
            name_error = str(exc)
        else:
            name_error = None
    else:
        name_error = None

    all_rows: list[dict[str, Any]] = []
    for fx in merged_fixtures:
        all_rows.extend(
            extract_rows_from_fixture(
                fx,
                book_slugs=slugs,
                markets_meta=markets_meta,
                player_names=player_names,
            )
        )

    unified = _rows_to_board(all_rows, points_only=True)
    unified["books"] = slugs
    unified["raw_row_count"] = len(all_rows)
    unified["fixture_count"] = len(merged_fixtures)
    unified["fixtures_available"] = len(all_fixtures)
    n_players = sum(len(g.get("players") or []) for g in (unified.get("games") or []))
    unified["note"] = (
        "RapidAPI /fixtures + /fixtures/odds. "
        "Books: bet365, 1xbet, superbet.rs "
        "(Mozzart/MaxBet have no player props on OddsPapi)."
    )
    if merged_fixtures and n_players == 0:
        unified["empty_props_note"] = EMPTY_PROPS_NOTE
        unified["note"] = f"{unified['note']} {EMPTY_PROPS_NOTE}"
    if name_error:
        unified["player_name_error"] = name_error
    unified["summary"] = {
        "games": len(unified.get("games") or []),
        "players": n_players,
        "books": slugs,
        "raw_prop_rows": len(all_rows),
        "fixtures_fetched": len(merged_fixtures),
        "fixtures_available": len(all_fixtures),
        "points_players": n_players,
        "props_ready": n_players > 0,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UNIFIED_PATH.write_text(json.dumps(unified, indent=2, ensure_ascii=False), encoding="utf-8")

    board = {
        "fetched_at": unified["fetched_at"],
        "source": "oddspapi",
        "books": slugs,
        "note": unified["note"],
        "empty_props_note": unified.get("empty_props_note"),
        "games": unified["games"],
        "summary": unified["summary"],
    }
    BOARD_PATH.write_text(json.dumps(board, indent=2, ensure_ascii=False), encoding="utf-8")

    per_book = _split_per_book(unified, slugs)
    for book, payload in per_book.items():
        _book_path(book).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "ok": True,
        "books": slugs,
        "summary": unified["summary"],
        "note": unified["note"],
        "empty_props_note": unified.get("empty_props_note"),
        "paths": {
            "unified": str(UNIFIED_PATH),
            "board": str(BOARD_PATH),
            **{b: str(_book_path(b)) for b in slugs},
        },
        "fetched_at": unified["fetched_at"],
    }


def load_oddspapi_board() -> dict[str, Any]:
    if BOARD_PATH.exists():
        try:
            data = json.loads(BOARD_PATH.read_text(encoding="utf-8"))
            data["path"] = str(BOARD_PATH)
            return data
        except (OSError, json.JSONDecodeError):
            pass
    if UNIFIED_PATH.exists():
        try:
            data = json.loads(UNIFIED_PATH.read_text(encoding="utf-8"))
            data["path"] = str(UNIFIED_PATH)
            return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"games": [], "players": [], "path": None, "source": None}


def load_oddspapi_meta() -> dict[str, Any]:
    board = load_oddspapi_board()
    games = board.get("games") or []
    n_players = sum(len(g.get("players") or []) for g in games)
    empty_note = board.get("empty_props_note")
    if not empty_note and board.get("source") == "oddspapi" and n_players == 0:
        empty_note = EMPTY_PROPS_NOTE
    return {
        "fetched_at": board.get("fetched_at"),
        "source": board.get("source"),
        "books": board.get("books") or [],
        "note": board.get("note"),
        "empty_props_note": empty_note,
        "summary": {
            "games": len(games),
            "players": n_players,
            "row_count": n_players,
            "props_ready": n_players > 0,
            **(board.get("summary") or {}),
        },
        "path": board.get("path"),
    }


def validate_catalog() -> dict[str, Any]:
    """
    Resolve Mozzart/MaxBet slug and confirm player-prop market IDs exist.
    Costs 1–2 billable requests (bookmakers + markets).
    """
    ok, reason = api_ready()
    if not ok:
        return {"ok": False, "error": reason}

    books = fetch_bookmakers(player_props_only=False)
    time.sleep(REQUEST_SLEEP_SEC)
    moz = resolve_mozzart_slug(books)
    slugs = target_book_slugs(refresh_catalog=False)

    present = {str(b.get("slug") or "") for b in books}
    missing_core = [s for s in slugs if s not in present]
    moz_meta = next((b for b in books if str(b.get("slug")) == moz), None) if moz else None
    super_meta = next((b for b in books if str(b.get("slug")) == BOOK_SUPERBET), None)

    markets = fetch_markets_basketball(force=True)
    hinted = {}
    for mid, stat in DEFAULT_PROP_MARKET_HINTS.items():
        meta = markets.get(mid)
        hinted[mid] = {
            "stat": stat,
            "found": meta is not None,
            "marketType": (meta or {}).get("marketType"),
            "marketName": (meta or {}).get("marketName") or (meta or {}).get("name"),
            "playerProp": (meta or {}).get("playerProp"),
            "handicap": (meta or {}).get("handicap"),
        }

    # Also surface other player point markets by name
    pointish = []
    for mid, meta in markets.items():
        mt = _norm(str(meta.get("marketType") or meta.get("marketName") or ""))
        if meta.get("playerProp") and "point" in mt:
            pointish.append(
                {
                    "marketId": mid,
                    "marketType": meta.get("marketType"),
                    "handicap": meta.get("handicap"),
                }
            )
            if len(pointish) >= 25:
                break

    return {
        "ok": True,
        "book_count": len(books),
        "bet365_present": BOOK_BET365 in present,
        "1xbet_present": BOOK_1XBET in present,
        "mozzart_slug": moz,
        "mozzart_player_props_flag": (moz_meta or {}).get("playerProps") if moz_meta else None,
        "superbet_slug": BOOK_SUPERBET,
        "superbet_player_props_flag": (super_meta or {}).get("playerProps") if super_meta else None,
        "mozzart_note": (
            "Mozzart/MaxBet have playerProps=false on OddsPapi — using superbet.rs as 3rd book."
        ),
        "target_slugs": slugs[:3],
        "missing_core": missing_core,
        "hinted_markets": hinted,
        "sample_player_point_markets": pointish,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="OddsPapi EuroLeague props helper")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Resolve Mozzart slug + confirm market IDs (no props pull)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Pull and cache EuroLeague props (default if no flags)",
    )
    args = parser.parse_args()
    do_validate = args.validate
    do_refresh = args.refresh or not args.validate

    ready, msg = api_ready()
    if not ready:
        print(msg)
        raise SystemExit(1)

    if do_validate:
        print("Validating catalog…")
        report = validate_catalog()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if not report.get("ok"):
            raise SystemExit(1)
        if not do_refresh:
            raise SystemExit(0)

    print("Resolving books…")
    slugs = target_book_slugs(refresh_catalog=do_validate)
    print("books:", slugs)
    print("Refreshing EuroLeague props (/fixtures + /fixtures/odds)…")
    result = refresh_euroleague_props(
        book_slugs=slugs,
        refresh_catalog=False,
        refresh_markets=not MARKETS_CACHE_PATH.exists(),
    )
    print(
        json.dumps(
            {
                k: result.get(k)
                for k in ("ok", "error", "books", "summary", "fetched_at", "empty_props_note", "note")
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    if result.get("empty_props_note"):
        print("\nNOTE:", result["empty_props_note"])
