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
# 22bet / stake carry classic player O/U (Over+Under); bet365 is mostly N+ milestones.
BOOK_BET365 = "bet365"
BOOK_1XBET = "1xbet"
BOOK_SUPERBET = "superbet.rs"
BOOK_22BET = "22bet"
BOOK_STAKE = "stake"
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

DEFAULT_BOOKS = (BOOK_BET365, BOOK_1XBET, BOOK_SUPERBET, BOOK_22BET, BOOK_STAKE)
# Books that often post real Under on playertotals-points (not synth).
UNDER_CAPABLE_BOOKS = (BOOK_1XBET, BOOK_SUPERBET, BOOK_22BET, BOOK_STAKE)
# UI picker: slug → (label, short note). Defaults = DEFAULT_BOOKS; extras are opt-in.
BOOK_CATALOG: dict[str, dict[str, str]] = {
    BOOK_BET365: {"label": "bet365", "note": "N+ milestones · Over only"},
    BOOK_1XBET: {"label": "1xBet", "note": "O/U half-lines · Under"},
    BOOK_SUPERBET: {"label": "Superbet RS", "note": "Milestones + O/U · Under"},
    BOOK_22BET: {"label": "22bet", "note": "O/U half-lines · Under"},
    BOOK_STAKE: {"label": "Stake", "note": "Milestones + O/U · Under"},
    "admiralbet": {"label": "AdmiralBet", "note": "Local scrape · Over + Under"},
    "1x2online": {"label": "1x2online", "note": "Local scrape · Poeni igraca O/U"},
    "pinnacle": {"label": "Pinnacle", "note": "Often empty on EuroLeague"},
    "unibet": {"label": "Unibet", "note": "Often empty on EuroLeague"},
    "williamhill": {"label": "William Hill", "note": "Often empty on EuroLeague"},
    "coolbet": {"label": "Coolbet", "note": "Often empty on EuroLeague"},
}
FIXTURE_HORIZON_DAYS = 14
MAX_FIXTURES_PER_REFRESH = 12
# RapidAPI /fixtures/odds accepts at most 3 bookmaker slugs per call.
BOOKS_PER_ODDS_REQUEST = 3


def book_label(slug: str) -> str:
    meta = BOOK_CATALOG.get(slug) or {}
    return str(meta.get("label") or slug)


def book_note(slug: str) -> str:
    meta = BOOK_CATALOG.get(slug) or {}
    return str(meta.get("note") or "")


def selectable_book_slugs() -> list[str]:
    """Stable order: defaults first, then catalog extras."""
    out: list[str] = []
    for s in DEFAULT_BOOKS:
        if s not in out:
            out.append(s)
    for s in BOOK_CATALOG:
        if s not in out:
            out.append(s)
    return out

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
# Half-lines that map cleanly to N+ milestones (4.5↔5+, 9.5↔10+, …).
FOCUS_LINES = tuple(float(n) - 0.5 for n in FOCUS_MILESTONES)

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
    BOOK_22BET: DATA_DIR / "oddspapi_22bet.json",
    BOOK_STAKE: DATA_DIR / "oddspapi_stake.json",
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


def _milestone_from_handicap(handicap: object, outcome_side: str | None = None) -> str | None:
    """Map half-line handicap to N+ milestone (4.5→5+). Side ignored — used for both O/U."""
    _ = outcome_side
    try:
        h = float(handicap)
    except (TypeError, ValueError):
        return None
    if abs(h - int(h) - 0.5) < 1e-6:
        return f"{int(h) + 1}+"
    if abs(h - int(h)) < 1e-6:
        return f"{int(h)}+"
    return None


def _line_to_milestone(line: float) -> str | None:
    return _milestone_from_handicap(line)


def _milestone_to_line(milestone: str) -> float | None:
    try:
        n = int(str(milestone).rstrip("+"))
    except ValueError:
        return None
    return float(n) - 0.5


def _outcome_side(oname: str | None, *, bookmaker_outcome_id: object = None) -> str | None:
    blob = f"{oname or ''} {bookmaker_outcome_id or ''}".strip().lower()
    if not blob:
        return None
    if re.search(r"\bunder\b", blob):
        return "under"
    if re.search(r"\bover\b", blob):
        return "over"
    # Milestone ladders (5+, 10+) are Over-only
    if re.search(r"\b\d{1,2}\s*\+", blob):
        return "over"
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
    Default books: bet365 + 1xbet + superbet.rs + 22bet + stake.

    Mozzart/MaxBet are listed but without player props. 22bet/stake (and often
    1xbet/superbet) post classic playertotals O/U with real Unders; bet365 is
    mostly N+ milestone Overs. Odds requests are chunked (max 3 books/call).
    """
    if refresh_catalog or not BOOKS_CACHE_PATH.exists():
        try:
            fetch_bookmakers(player_props_only=False)
            time.sleep(REQUEST_SLEEP_SEC)
        except Exception:  # noqa: BLE001
            pass
    return list(DEFAULT_BOOKS)


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
    # Classic O/U player totals: market handicap → N+ (both Over and Under)
    mt = _norm(str((meta or {}).get("marketType") or ""))
    if meta and "playertotals" in mt.replace(" ", ""):
        return _milestone_from_handicap(meta.get("handicap"))
    return _milestone_from_text(oname, row.get("bookmakerOutcomeId"))


def _classify_rapid_prop_row(
    row: dict[str, Any],
    markets_meta: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """
    Parse one RapidAPI odds row into {stat, side, line, milestone, market_family}.
    Returns None if not a Points player prop we care about.
    """
    mid_s = str(row.get("marketId") or "")
    meta = markets_meta.get(mid_s)
    mt = _norm(str((meta or {}).get("marketType") or ""))
    stat = _stat_for_market(meta, mid_s)
    if stat != "Points":
        if "point" not in mt:
            return None
        stat = "Points"
    oname = _outcome_name_from_meta(meta, row.get("outcomeId"))
    side = _outcome_side(oname, bookmaker_outcome_id=row.get("bookmakerOutcomeId"))
    family = "unknown"
    line: float | None = None
    milestone: str | None = None

    if "playertotals" in mt.replace(" ", ""):
        family = "totals"
        try:
            line = float(meta.get("handicap")) if meta and meta.get("handicap") is not None else None
        except (TypeError, ValueError):
            line = None
        if side is None:
            side = _outcome_side(oname)
        milestone = _milestone_from_handicap(line) if line is not None else None
    else:
        # players-points milestone ladder (5+, 10+, …) — Over only
        family = "milestone"
        milestone = _milestone_for_rapid_row(row, markets_meta)
        if milestone:
            line = _milestone_to_line(milestone)
            side = side or "over"

    if side not in ("over", "under"):
        return None
    if family == "milestone" and side == "under":
        return None
    if line is None and milestone is None:
        return None
    return {
        "stat": stat,
        "side": side,
        "line": line,
        "milestone": milestone,
        "market_family": family,
        "market_id": mid_s,
        "market_type": (meta or {}).get("marketType"),
    }


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
    """
    GET /fixtures/odds — RapidAPI nested odds-by-book shape.

    API allows at most 3 bookmakers per call; we chunk and merge when needed.
    """
    slugs = [s for s in book_slugs if s]
    if not slugs:
        return {"odds": {}}
    chunks = [
        slugs[i : i + BOOKS_PER_ODDS_REQUEST]
        for i in range(0, len(slugs), BOOKS_PER_ODDS_REQUEST)
    ]
    merged: dict[str, Any] = {"odds": {}, "fixtureId": fixture_id}
    for i, chunk in enumerate(chunks):
        payload = _get(
            "/fixtures/odds",
            {
                "fixtureId": fixture_id,
                "bookmakers": ",".join(chunk),
            },
        )
        if not isinstance(payload, dict):
            continue
        if i == 0:
            for k, v in payload.items():
                if k != "odds":
                    merged[k] = v
        odds = payload.get("odds") or {}
        if isinstance(odds, dict):
            merged.setdefault("odds", {}).update(odds)
        if i + 1 < len(chunks):
            time.sleep(REQUEST_SLEEP_SEC)
    return merged


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


def _pair_side_quotes(side_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Collapse Over/Under quotes for the same (book, player, line) into one row.
    """
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in side_rows:
        line = r.get("line")
        try:
            line_f = float(line) if line is not None else None
        except (TypeError, ValueError):
            line_f = None
        key = (r.get("book"), r.get("player_id"), r.get("player"), line_f, r.get("milestone"))
        b = buckets.get(key)
        if b is None:
            b = {
                "book": r.get("book"),
                "player": r.get("player"),
                "player_id": r.get("player_id"),
                "team": r.get("team"),
                "team_hint": r.get("team_hint"),
                "market": r.get("market") or "Points",
                "milestone": r.get("milestone"),
                "line": line_f,
                "over_odds": None,
                "under_odds": None,
                "fixture_id": r.get("fixture_id"),
                "event": r.get("event"),
                "tip_off": r.get("tip_off"),
                "home": r.get("home"),
                "away": r.get("away"),
                "market_id": r.get("market_id"),
                "market_family": r.get("market_family"),
                "main_line": bool(r.get("main_line")),
                "has_real_under": False,
            }
            buckets[key] = b
        side = r.get("side")
        price = r.get("price")
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        if price_f is None:
            continue
        if side == "over":
            prev = b.get("over_odds")
            if prev is None or price_f < float(prev):
                b["over_odds"] = round(price_f, 3)
            b["main_line"] = b["main_line"] or bool(r.get("main_line"))
            if r.get("market_id"):
                b["market_id"] = r.get("market_id")
            if r.get("market_family"):
                b["market_family"] = r.get("market_family")
        elif side == "under":
            prev = b.get("under_odds")
            if prev is None or price_f < float(prev):
                b["under_odds"] = round(price_f, 3)
            b["has_real_under"] = True
            b["main_line"] = b["main_line"] or bool(r.get("main_line"))
    return [b for b in buckets.values() if b.get("over_odds") is not None or b.get("under_odds") is not None]


def _keep_prop_row(row: dict[str, Any]) -> bool:
    """Keep focus milestones/lines, any real O/U under, or main-line totals."""
    ms = row.get("milestone")
    if ms:
        try:
            if int(str(ms).rstrip("+")) in FOCUS_MILESTONES:
                return True
        except ValueError:
            pass
    line = row.get("line")
    try:
        line_f = float(line) if line is not None else None
    except (TypeError, ValueError):
        line_f = None
    if line_f is not None and any(abs(line_f - fl) < 1e-6 for fl in FOCUS_LINES):
        return True
    if row.get("has_real_under") and row.get("under_odds") is not None:
        return True
    if row.get("market_family") == "totals" and row.get("main_line"):
        return True
    return False


def extract_rows_from_fixture(
    fixture: dict[str, Any],
    *,
    book_slugs: list[str],
    markets_meta: dict[str, dict[str, Any]],
    player_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Flatten player-prop Over / Under / milestone prices.

    Supports RapidAPI nested `odds[book][oddsId]` and legacy v4 `bookmakerOdds`.
    Classic playertotals-points yield real Under; milestone ladders are Over-only.
    """
    home, away = _fixture_participants(fixture)
    event = f"{home} vs {away}".strip(" vs")
    tip = _fixture_start_iso(fixture)
    fid = fixture.get("fixtureId")
    player_names = player_names or {}
    side_rows: list[dict[str, Any]] = []

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
                parsed = _classify_rapid_prop_row(row, markets_meta)
                if not parsed:
                    continue
                price = row.get("price")
                try:
                    price_f = float(price)
                except (TypeError, ValueError):
                    continue
                pname = player_names.get(str(pid)) or row.get("playerName")
                side_rows.append(
                    {
                        "book": book,
                        "player": pname or f"player:{pid}",
                        "player_id": pid,
                        "team": None,
                        "team_hint": None,
                        "market": "Points",
                        "milestone": parsed.get("milestone"),
                        "line": parsed.get("line"),
                        "side": parsed.get("side"),
                        "price": price_f,
                        "fixture_id": fid,
                        "event": event,
                        "tip_off": tip,
                        "home": home,
                        "away": away,
                        "market_id": parsed.get("market_id"),
                        "market_family": parsed.get("market_family"),
                        "main_line": bool(row.get("mainLine")),
                    }
                )
        paired = _pair_side_quotes(side_rows)
        return [r for r in paired if _keep_prop_row(r)]

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
            mt = _norm(str((meta or {}).get("marketType") or ""))
            family = "totals" if "playertotals" in mt.replace(" ", "") else "milestone"
            for oid, pname, price, price_obj in _iter_player_prices(mblock):
                side_label = _outcome_side_label(meta, oid, price_obj)
                side = _outcome_side(side_label, bookmaker_outcome_id=price_obj.get("bookmakerOutcomeId"))
                milestone = _milestone_from_text(side_label, price_obj.get("bookmakerOutcomeId"))
                line = None
                if milestone is None and handicap is not None:
                    milestone = _milestone_from_handicap(handicap)
                    try:
                        line = float(handicap)
                    except (TypeError, ValueError):
                        line = None
                    family = "totals"
                if milestone and line is None:
                    line = _milestone_to_line(milestone)
                if side is None:
                    continue
                if family == "milestone" and side == "under":
                    continue
                if milestone is None and line is None:
                    continue
                side_rows.append(
                    {
                        "book": book,
                        "player": pname or f"player:{price_obj.get('bookmakerOutcomeId') or oid}",
                        "player_id": None,
                        "team": None,
                        "team_hint": None,
                        "market": "Points",
                        "milestone": milestone,
                        "line": line,
                        "side": side,
                        "price": price,
                        "fixture_id": fid,
                        "event": event,
                        "tip_off": tip,
                        "home": home,
                        "away": away,
                        "market_id": mid_s,
                        "market_family": family,
                        "main_line": bool(price_obj.get("mainLine")),
                    }
                )
    paired = _pair_side_quotes(side_rows)
    return [r for r in paired if _keep_prop_row(r)]


def _line_key(line: float | None) -> str | None:
    if line is None:
        return None
    try:
        f = float(line)
    except (TypeError, ValueError):
        return None
    if abs(f - int(f)) < 1e-6:
        return str(int(f))
    return f"{f:g}"


def _rows_to_board(rows: list[dict[str, Any]], *, points_only: bool = True) -> dict[str, Any]:
    """
    Build unified multi-book board + Bet365-primary legacy games[] shape.

    Per player:
      odds_by_book[book][milestone] = over float   (Market cut compatible)
      under_by_book[book][milestone] = under float  (when real Under exists)
      ou_by_book[book][line] = {over, under, milestone, has_real_under}
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
        ms = str(r["milestone"]) if r.get("milestone") else None
        line = r.get("line")
        if line is None and ms:
            line = _milestone_to_line(ms)
        if ms is None and line is not None:
            ms = _line_to_milestone(float(line))
        p = g["players_map"].setdefault(
            _norm(pname),
            {
                "player": pname,
                "team_hint": r.get("team_hint"),
                "odds_by_book": {},
                "under_by_book": {},
                "ou_by_book": {},
            },
        )
        if r.get("team_hint") and not p.get("team_hint"):
            p["team_hint"] = r["team_hint"]

        over = r.get("over_odds")
        under = r.get("under_odds")
        if ms and over is not None:
            book_odds = p["odds_by_book"].setdefault(book, {})
            prev = book_odds.get(ms)
            if prev is None or float(over) < float(prev):
                book_odds[ms] = round(float(over), 3)
        if ms and under is not None:
            book_under = p["under_by_book"].setdefault(book, {})
            prev_u = book_under.get(ms)
            if prev_u is None or float(under) < float(prev_u):
                book_under[ms] = round(float(under), 3)

        lk = _line_key(float(line) if line is not None else None)
        if lk is not None:
            ou_book = p["ou_by_book"].setdefault(book, {})
            cell = ou_book.get(lk) or {
                "over": None,
                "under": None,
                "milestone": ms,
                "has_real_under": False,
            }
            if over is not None and (cell["over"] is None or float(over) < float(cell["over"])):
                cell["over"] = round(float(over), 3)
            if under is not None and (cell["under"] is None or float(under) < float(cell["under"])):
                cell["under"] = round(float(under), 3)
                cell["has_real_under"] = True
            if ms and not cell.get("milestone"):
                cell["milestone"] = ms
            if r.get("has_real_under"):
                cell["has_real_under"] = True
            ou_book[lk] = cell

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
                    "under_by_book": p.get("under_by_book") or {},
                    "ou_by_book": p.get("ou_by_book") or {},
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
                under = (p.get("under_by_book") or {}).get(book) or {}
                ou = (p.get("ou_by_book") or {}).get(book) or {}
                if not odds and not under and not ou:
                    continue
                players.append(
                    {
                        "player": p.get("player"),
                        "team_hint": p.get("team_hint"),
                        "odds": odds,
                        "under": under,
                        "ou": ou,
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
      2) GET /fixtures/odds per near-term fixture (chunked, ≤3 books/call)
      3) GET /players for name resolution

    Default books: bet365, 1xbet, superbet.rs, 22bet, stake.
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
    n_with_under = sum(1 for r in all_rows if r.get("under_odds") is not None)
    books_with_under = sorted(
        {str(r.get("book")) for r in all_rows if r.get("under_odds") is not None and r.get("book")}
    )
    unified["note"] = (
        "RapidAPI /fixtures + /fixtures/odds. "
        f"Books: {', '.join(slugs)}. "
        "bet365 ≈ N+ milestones (Over); "
        "1xbet / superbet.rs / 22bet / stake often post playertotals Over+Under. "
        "(Mozzart/MaxBet have no player props on OddsPapi)."
    )
    unified["under_capable_books"] = [b for b in slugs if b in UNDER_CAPABLE_BOOKS]
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
        "rows_with_under": n_with_under,
        "books_with_under": books_with_under,
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
        "under_capable_books": unified.get("under_capable_books"),
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


def _milestone_sort_key(ms: str) -> int:
    try:
        return int(str(ms).rstrip("+"))
    except ValueError:
        return 999


def board_books(board: dict[str, Any] | None = None) -> list[str]:
    """Stable book order: preferred defaults first, then any extras on the board."""
    board = board or load_oddspapi_board()
    found: set[str] = set()
    for b in board.get("books") or []:
        if b:
            found.add(str(b))
    for g in board.get("games") or []:
        for p in g.get("players") or []:
            for b in (p.get("odds_by_book") or {}):
                found.add(str(b))
    ordered: list[str] = []
    for b in DEFAULT_BOOKS:
        if b in found:
            ordered.append(b)
            found.discard(b)
    ordered.extend(sorted(found))
    return ordered


def odds_by_book_rows(board: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """
    Flatten board into one row per player × line with Over/Under per book.

    Columns: event, tip_off, player, team_hint, line, milestone,
             <book>_over, <book>_under, ..., best_over_book, best_under_book,
             has_real_under
    Also keeps legacy <book> = over for callers that still expect it.
    """
    board = board or load_oddspapi_board()
    books = board_books(board)
    rows: list[dict[str, Any]] = []
    for g in board.get("games") or []:
        event = g.get("event") or ""
        tip = g.get("tip_off")
        for p in g.get("players") or []:
            by_book = p.get("odds_by_book") or {}
            under_by = p.get("under_by_book") or {}
            ou_by = p.get("ou_by_book") or {}

            # Union of line keys from ou_by_book + milestones mapped to lines
            line_meta: dict[str, dict[str, Any]] = {}
            for book, ou_map in ou_by.items():
                if not isinstance(ou_map, dict):
                    continue
                for lk, cell in ou_map.items():
                    if not isinstance(cell, dict):
                        continue
                    meta = line_meta.setdefault(
                        str(lk),
                        {"line": float(lk), "milestone": cell.get("milestone"), "has_real_under": False},
                    )
                    if cell.get("milestone") and not meta.get("milestone"):
                        meta["milestone"] = cell.get("milestone")
                    if cell.get("has_real_under") or cell.get("under") is not None:
                        meta["has_real_under"] = True

            milestones: set[str] = set()
            for b_odds in by_book.values():
                if isinstance(b_odds, dict):
                    milestones.update(str(k) for k in b_odds)
            for b_under in under_by.values():
                if isinstance(b_under, dict):
                    milestones.update(str(k) for k in b_under)
            if not milestones and p.get("odds"):
                milestones.update(str(k) for k in (p.get("odds") or {}))
            for ms in milestones:
                line = _milestone_to_line(ms)
                if line is None:
                    continue
                lk = _line_key(line)
                if not lk:
                    continue
                meta = line_meta.setdefault(
                    lk, {"line": line, "milestone": ms, "has_real_under": False}
                )
                if not meta.get("milestone"):
                    meta["milestone"] = ms

            for lk, meta in sorted(line_meta.items(), key=lambda kv: float(kv[0])):
                ms = meta.get("milestone")
                row: dict[str, Any] = {
                    "event": event,
                    "tip_off": tip,
                    "player": p.get("player"),
                    "team_hint": p.get("team_hint"),
                    "line": meta.get("line"),
                    "milestone": ms,
                    "has_real_under": bool(meta.get("has_real_under")),
                }
                overs: list[tuple[str, float]] = []
                unders: list[tuple[str, float]] = []
                for book in books:
                    cell = (ou_by.get(book) or {}).get(lk) if isinstance(ou_by.get(book), dict) else None
                    over = cell.get("over") if isinstance(cell, dict) else None
                    under = cell.get("under") if isinstance(cell, dict) else None
                    if over is None and ms:
                        raw = (by_book.get(book) or {}).get(ms)
                        if raw is None and book == BOOK_BET365 and (p.get("odds") or {}).get(ms) is not None:
                            raw = (p.get("odds") or {}).get(ms)
                        over = raw
                    if under is None and ms:
                        under = (under_by.get(book) or {}).get(ms)
                    over_f = round(float(over), 3) if over is not None else None
                    under_f = round(float(under), 3) if under is not None else None
                    row[f"{book}_over"] = over_f
                    row[f"{book}_under"] = under_f
                    row[book] = over_f  # legacy alias
                    if over_f is not None:
                        overs.append((book, over_f))
                    if under_f is not None:
                        unders.append((book, under_f))
                        row["has_real_under"] = True
                if overs:
                    best_book, best_over = min(overs, key=lambda t: t[1])
                    row["best_over_book"] = best_book
                    row["best_over"] = best_over
                    row["best_book"] = best_book  # legacy
                else:
                    row["best_over_book"] = None
                    row["best_over"] = None
                    row["best_book"] = None
                if unders:
                    # Longer under decimal = shorter under probability; pick longest under? 
                    # For value hunting, higher under price is better for betting Under.
                    # For "shortest price" parity with over, use min.
                    best_u_book, best_under = min(unders, key=lambda t: t[1])
                    row["best_under_book"] = best_u_book
                    row["best_under"] = best_under
                else:
                    row["best_under_book"] = None
                    row["best_under"] = None
                if any(row.get(f"{b}_over") is not None or row.get(f"{b}_under") is not None for b in books):
                    rows.append(row)
    rows.sort(
        key=lambda r: (
            str(r.get("tip_off") or ""),
            str(r.get("event") or ""),
            _norm(str(r.get("player") or "")),
            float(r.get("line") or 999),
        )
    )
    return rows


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
