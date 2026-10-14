"""
AdmiralBet EuroLeague player prop lines (PTS / AST / REB / 3PM).

Used by the Streamlit app (read + optional refresh) and by tools/admiralbet_mcp.
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

from euro_utils import DATA_DIR


def is_streamlit_cloud() -> bool:
    """Streamlit Community Cloud runs as appuser under /home/appuser."""
    home = Path.home().as_posix()
    return home.startswith("/home/appuser") or bool(os.environ.get("STREAMLIT_SHARING_MODE"))


def playwright_ready() -> tuple[bool, str]:
    """Return (ok, reason) for local scrape availability."""
    if is_streamlit_cloud():
        return False, "AdmiralBet scrape is local-only (not available on Streamlit Cloud)."
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "Playwright not installed. Locally: pip install playwright && playwright install chromium"
    return True, ""


def offer_date_window(now: datetime | None = None) -> tuple[str, str]:
    """AdmiralBet offer API window: from *today* (UTC) through +5 years."""
    now = now or datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%dT%H:%M:%S.000")
    date_to = (now + timedelta(days=365 * 5)).strftime("%Y-%m-%dT%H:%M:%S.000")
    return date_from, date_to


LINES_PATH = DATA_DIR / "admiralbet_lines.json"
CAPTURE_PATH = DATA_DIR / "admiralbet_capture.json"

# AdmiralBet market label -> boxscore column used in the app
# UI copy may be title-case or lowercase depending on layout.
MARKET_TABS: list[tuple[str, str]] = [
    ("Ukupno Poena", "Points"),
    ("Ukupno poena", "Points"),
    ("Ukupno Asistencija", "Assists"),
    ("Ukupno asistencija", "Assists"),
    ("Ukupno Skokova", "TotalRebounds"),
    ("Ukupno skokova", "TotalRebounds"),
    ("Ukupno Pogodjenih Trojki", "FGM3"),
    ("Ukupno Pogođenih Trojki", "FGM3"),
    ("Ukupno pogodjenih trojki", "FGM3"),
    ("Ukupno pogođenih trojki", "FGM3"),
]

STAT_LABELS = {
    "Points": "PTS",
    "Assists": "AST",
    "TotalRebounds": "REB",
    "FGM3": "3PM",
}

DEFAULT_SPA_URL = (
    "https://admiralbet.rs/sport-site/sr/"
    "#/betting/prematch"
    "?language=sr"
    "&sport=Ko%C5%A1arka_igra%C4%8Di"
    "&region=Igra%C4%8Di_ukupno"
    "&competition=Evroliga"
    "&competitionId=123-1416-25158"
)

OUTER_URL = (
    "https://admiralbet.rs/sport-prematch"
    "?sport=Ko%C5%A1arka_igra%C4%8Di"
    "&region=Igra%C4%8Di_ukupno"
    "&competition=Evroliga"
    "&competitionId=123-1416-25158"
)

# Time-filter chips on the prematch board (Serbian UI).
TIME_FILTERS_TODAY_FIRST = ("DANAS", "SVE", "12h", "6h", "3h")


def today_date_chips(now: datetime | None = None) -> list[str]:
    """AdmiralBet day chips look like '14.10.' — build variants for today."""
    now = now or datetime.now()
    d, m = now.day, now.month
    return [
        f"{d:02d}.{m:02d}.",
        f"{d}.{m}.",
        f"{d:02d}.{m}.",
        f"{d}.{m:02d}.",
    ]

# marketKey may be a single stat, or "ALL" to emit PTS/AST/REB column triples.
DOM_EXTRACT_JS = """(marketKey) => {
  const text = document.body ? document.body.innerText : '';
  const lines = text.split(/\\n/).map(s => s.trim()).filter(Boolean);
  const nameRe = /^[A-Za-zČĆŠŽĐčćšžđ][A-Za-zČĆŠŽĐčćšžđ.'\\-]*(?:\\s+[A-Za-zČĆŠŽĐčćšžđ.'\\-]+){0,3}$/;
  const tipRe = /^(?:pon|uto|sre|čet|cet|pet|sub|ned|ponedeljak|utorak|sreda|cetvrtak|četvrtak|petak|subota|nedelja)\\s*,?\\s*\\d{1,2}\\.\\s*\\d{1,2}\\.(?:\\s*\\d{4}\\.?)?\\s*\\d{1,2}:\\d{2}/i;
  const legacyDateRe = /(?:pon|uto|sre|čet|cet|pet|sub|ned)?\\s*,?\\s*\\d{1,2}\\.\\s*\\d{1,2}\\./i;
  const skip = new Set([
    'Evroliga','EVROLIGA','Igrači ukupno','IGRAČI UKUPNO','Košarka igrači',
    '+','-','manje','vise','Ukupno Poena','Ukupno poena','Ukupno Asistencija',
    'Ukupno asistencija','Ukupno Skokova','Ukupno skokova','Select statistic',
    'OBIČAN','Uplata','BRZI TIKET','DANAS','SVE','3h','6h','12h','Manje','Vise'
  ]);
  // Multi-column board order on Igrači ukupno
  const colMarkets = ['Points', 'Assists', 'TotalRebounds'];
  const wantAll = !marketKey || marketKey === 'ALL' || marketKey === 'Points';
  const out = [];

  function pushTriple(name, team, eventId, tip, under, line, over, market) {
    if (line == null) return;
    out.push({
      player: name,
      team,
      event_id: eventId,
      under_odds: under,
      line,
      over_odds: over,
      market,
      source: 'dom',
      tip_text: tip
    });
  }

  function parseTriple(nums, offset) {
    const a = nums[offset], b = nums[offset + 1], c = nums[offset + 2];
    if (a == null && b == null && c == null) return null;
    // duplicated line value: under, line, line, over
    if (b != null && c != null && b === c && nums[offset + 3] != null) {
      return { under: a, line: b, over: nums[offset + 3], width: 4 };
    }
    if (b != null) return { under: a, line: b, over: c, width: 3 };
    return null;
  }

  // Layout A: tip → event_id → player → team → up to 3 market triples (PTS/AST/REB)
  for (let i = 0; i < lines.length - 5; i++) {
    const tip = lines[i];
    if (!tipRe.test(tip)) continue;
    let j = i + 1;
    let eventId = null;
    if (/^\\d{4,}$/.test(lines[j] || '')) { eventId = lines[j]; j += 1; }
    const name = lines[j];
    const team = lines[j + 1];
    if (!nameRe.test(name || '') || skip.has(name) || skip.has(team)) continue;
    const toks = [];
    for (let k = j + 2; k < Math.min(j + 16, lines.length); k++) {
      const tok = lines[k];
      if (tipRe.test(tok) || tok === '+' || /^\\d{4,}$/.test(tok)) break;
      if (tok === 'manje' || tok === 'vise' || tok === 'Manje' || tok === 'Vise') continue;
      if (tok === '-') { toks.push(null); continue; }
      if (/^\\d+(?:\\.\\d+)?$/.test(tok)) toks.push(Number(tok));
      else break;
    }
    if (wantAll) {
      let offset = 0;
      for (const market of colMarkets) {
        const t = parseTriple(toks, offset);
        if (!t) { offset += 3; continue; }
        pushTriple(name, team, eventId, tip, t.under, t.line, t.over, market);
        offset += t.width;
      }
    } else {
      const t = parseTriple(toks, 0);
      if (t) pushTriple(name, team, eventId, tip, t.under, t.line, t.over, marketKey);
    }
  }

  // Layout B (legacy single-market list)
  if (out.length < 3) {
    for (let i = 0; i < lines.length - 4; i++) {
      const name = lines[i];
      const team = lines[i + 1];
      const date = lines[i + 2] || '';
      if (!nameRe.test(name) || skip.has(name) || skip.has(team)) continue;
      if (!legacyDateRe.test(date) && !/\\d{4,}/.test(date)) continue;
      if (tipRe.test(name)) continue;
      const nums = [];
      for (let j = i + 3; j < Math.min(i + 10, lines.length); j++) {
        const tok = lines[j];
        if (tok === 'Evroliga' || tok === 'EVROLIGA' || tok === '+') break;
        if (tok === '-') { nums.push(null); continue; }
        if (/^\\d+(?:\\.\\d+)?$/.test(tok)) nums.push(Number(tok));
        else break;
      }
      const t = parseTriple(nums, 0);
      if (!t) continue;
      const idm = String(date).match(/(\\d{1,2}):(\\d{2})(\\d{4,})$/);
      pushTriple(name, team, idm ? idm[3] : null, date, t.under, t.line, t.over, marketKey || 'Points');
    }
  }

  const seen = new Set();
  const dedup = [];
  for (const r of out) {
    const k = r.player + '|' + r.team + '|' + r.market;
    if (seen.has(k)) continue;
    seen.add(k);
    dedup.push(r);
  }
  return dedup;
}"""


def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("'", "").replace("-", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def save_lines_payload(rows: list[dict[str, Any]], *, source: str = "fetch") -> Path:
    """Normalize and write data/admiralbet_lines.json (+ capture mirror).

    Refuses to overwrite an existing non-empty file with an empty scrape.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    by_market: dict[str, int] = {}
    clean: list[dict[str, Any]] = []
    for r in rows:
        market = str(r.get("market") or "Points")
        for label, key in MARKET_TABS:
            if market == label:
                market = key
                break
        if market.startswith("Ukupno Poena"):
            market = "Points"
        row = {
            "player": r.get("player"),
            "team": r.get("team"),
            "event_id": r.get("event_id"),
            "line": r.get("line"),
            "under_odds": r.get("under_odds"),
            "over_odds": r.get("over_odds"),
            "market": market,
            "source": r.get("source") or source,
            "tip_text": r.get("tip_text"),
        }
        if not row["player"] or row["line"] is None:
            continue
        clean.append(row)
        by_market[market] = by_market.get(market, 0) + 1

    if not clean:
        if LINES_PATH.exists() or CAPTURE_PATH.exists():
            raise RuntimeError(
                "Scrape returned 0 player lines; left previous admiralbet_lines.json untouched."
            )
        raise RuntimeError("Scrape returned 0 player lines and no previous file exists.")

    date_from, date_to = offer_date_window()
    payload = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "offer_date_from": date_from,
        "offer_date_to": date_to,
        "competition": "Evroliga",
        "source": source,
        "summary": {"row_count": len(clean), "by_market": by_market},
        "lines": clean,
    }
    LINES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    CAPTURE_PATH.write_text(
        json.dumps(
            {
                **payload,
                "dom_lines": clean,
                "url": DEFAULT_SPA_URL,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return LINES_PATH


def load_admiralbet_lines() -> dict[str, Any]:
    """Load last saved lines file (or empty payload)."""
    for path in (LINES_PATH, CAPTURE_PATH):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = data.get("lines") or data.get("dom_lines") or []
        if rows:
            return {
                "fetched_at": data.get("fetched_at"),
                "summary": data.get("summary") or {"row_count": len(rows)},
                "lines": rows,
                "path": str(path),
            }
    return {"fetched_at": None, "summary": {"row_count": 0}, "lines": [], "path": None}


def match_player_lines(
    player_name: str,
    lines: list[dict[str, Any]] | None = None,
    *,
    team_hint: str | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Return {stat_col: row} for the best-matching player.
    Prefers exact normalized name; optional team_hint breaks ties.
    """
    if lines is None:
        lines = load_admiralbet_lines().get("lines") or []
    target = _norm_name(player_name)
    if not target or not lines:
        return {}

    candidates = [r for r in lines if _norm_name(r.get("player", "")) == target]
    if not candidates:
        parts = target.split()
        last = parts[-1] if parts else target
        candidates = [
            r
            for r in lines
            if last in _norm_name(r.get("player", ""))
            or _norm_name(r.get("player", "")) in target
        ]
    if not candidates:
        return {}

    if team_hint:
        th = _norm_name(team_hint)
        tighter = [r for r in candidates if th and th in _norm_name(r.get("team", ""))]
        if tighter:
            candidates = tighter

    by_stat: dict[str, dict[str, Any]] = {}
    for r in candidates:
        market = str(r.get("market") or "")
        for label, key in MARKET_TABS:
            if market == label:
                market = key
                break
        if market not in STAT_LABELS:
            continue
        prev = by_stat.get(market)
        if prev is None or (prev.get("line") is None and r.get("line") is not None):
            by_stat[market] = r
    return by_stat


async def _accept_cookies(page) -> None:
    for label in ("Prihvati sve", "Accept all", "Prihvati", "Accept"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if await btn.count() > 0:
                await btn.first.click(timeout=2000)
                await page.wait_for_timeout(400)
                return
        except Exception:
            continue


async def _click_label(target, label: str, *, exact: bool = True) -> bool:
    try:
        loc = target.get_by_text(label, exact=exact)
        if await loc.count() > 0:
            await loc.first.click(timeout=2500)
            return True
    except Exception:
        pass
    if exact:
        return await _click_label(target, label, exact=False)
    return False


async def _click_market(target, label: str) -> bool:
    return await _click_label(target, label, exact=True)


async def _apply_time_filter(target, label: str) -> bool:
    """Click DANAS / SVE / Nh chips so the board uses today's window (not a stale filter)."""
    return await _click_label(target, label, exact=True)


async def _scroll_board(target, page, n: int = 8) -> None:
    for _ in range(n):
        try:
            await target.mouse.wheel(0, 2800)
        except Exception:
            await page.mouse.wheel(0, 2800)
        await page.wait_for_timeout(280)


async def _scrape_markets(target, page, tabs: list[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """Parse the multi-column PTS/AST/REB board (and optionally a 3PM tab)."""
    _ = tabs
    await _scroll_board(target, page, n=12)
    # One pass reads PTS + AST + REB columns from the Igrači ukupno grid.
    all_rows: list[dict[str, Any]] = await target.evaluate(DOM_EXTRACT_JS, "ALL")

    # 3PM is often a separate tab when present.
    for label in ("Ukupno Pogodjenih Trojki", "Ukupno pogodjenih trojki", "Ukupno Pogođenih Trojki"):
        if await _click_market(target, label):
            await page.wait_for_timeout(1600)
            await _scroll_board(target, page, n=8)
            threes = await target.evaluate(DOM_EXTRACT_JS, "FGM3")
            pts = {
                (r.get("player"), r.get("team")): r.get("line")
                for r in all_rows
                if r.get("market") == "Points"
            }
            same_as_pts = sum(
                1
                for r in threes
                if pts.get((r.get("player"), r.get("team"))) == r.get("line")
            )
            # If almost every "3PM" line equals PTS, the tab did not switch — skip.
            if threes and same_as_pts < max(3, int(0.8 * len(threes))):
                all_rows.extend(threes)
            break
    return all_rows


def _tip_is_today(tip_text: str, now: datetime | None = None) -> bool:
    """True if tip_text contains today's day/month (Serbian board format)."""
    now = now or datetime.now()
    text = str(tip_text or "")
    patterns = (
        rf"\b{now.day}\.\s*{now.month}\.",
        rf"\b{now.day:02d}\.\s*{now.month:02d}\.",
        rf"\b{now.day}\.\s*{now.month:02d}\.",
        rf"\b{now.day:02d}\.\s*{now.month}\.",
    )
    return any(re.search(p, text) for p in patterns)


async def _open_evroliga_player_board(target, page) -> str | None:
    """
    Stay on the hash-routed Evroliga player board.
    Prefer SVE (DANAS alone can kick the SPA back to general sports).
    Then try today's day chip (e.g. 14.10.) if present.
    """
    used = None
    if await _apply_time_filter(target, "SVE"):
        used = "SVE"
        await page.wait_for_timeout(1200)

    for chip in today_date_chips():
        if await _apply_time_filter(target, chip):
            used = chip
            await page.wait_for_timeout(1000)
            break

    for label in ("Ukupno Poena", "Ukupno poena"):
        if await _click_market(target, label):
            await page.wait_for_timeout(1000)
            break
    return used


async def _goto_player_props(page):
    """Open Evroliga Igrači ukupno — direct SPA hash is most reliable in headless."""
    await page.goto(DEFAULT_SPA_URL, wait_until="domcontentloaded", timeout=60000)
    await _accept_cookies(page)
    await page.wait_for_timeout(4000)
    target = page.frame(name="sportIframe") or page
    used = await _open_evroliga_player_board(target, page)
    return target, used


async def fetch_euroleague_player_totals_async(
    wait_ms: int = 12000,
    *,
    markets: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Playwright scrape of Evroliga player O/U tabs; writes admiralbet_lines.json.

    Uses *today's* offer window and clicks DANAS first (then SVE if empty).
    """
    ok, reason = playwright_ready()
    if not ok:
        return {
            "saved_to": None,
            "error": reason,
            "summary": {"row_count": 0, "by_market": {}},
            "lines": [],
        }

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return {
            "saved_to": None,
            "error": f"Playwright import failed: {exc}",
            "summary": {"row_count": 0, "by_market": {}},
            "lines": [],
        }

    tabs = markets or MARKET_TABS
    date_from, date_to = offer_date_window()
    all_rows: list[dict[str, Any]] = []
    used_filter = None

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            return {
                "saved_to": None,
                "error": (
                    f"Playwright browser missing ({exc}). "
                    "Locally run: python -m playwright install chromium"
                ),
                "summary": {"row_count": 0, "by_market": {}},
                "lines": [],
            }
        context = await browser.new_context(
            locale="sr-Latn-RS",
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Soft-patch fetch dates if the SPA builds offer URLs with dateFrom/dateTo.
        await page.add_init_script(
            f"""() => {{
              const dateFrom = {json.dumps(date_from)};
              const dateTo = {json.dumps(date_to)};
              const orig = window.fetch.bind(window);
              window.fetch = (input, init) => {{
                try {{
                  let url = typeof input === 'string' ? input : (input && input.url) || '';
                  if (url.includes('dateFrom=') || url.includes('getWebEventsSelections')) {{
                    url = url
                      .replace(/dateFrom=[^&]+/g, 'dateFrom=' + encodeURIComponent(dateFrom))
                      .replace(/dateTo=[^&]+/g, 'dateTo=' + encodeURIComponent(dateTo));
                    if (typeof input === 'string') input = url;
                    else if (input && typeof Request !== 'undefined' && input instanceof Request) {{
                      input = new Request(url, input);
                    }}
                  }}
                }} catch (e) {{}}
                return orig(input, init);
              }};
            }}"""
        )

        target, used_filter = await _goto_player_props(page)

        # Wait until player odds rows appear (tip→id→player layout or legacy).
        deadline = time.time() + max(wait_ms, 10000) / 1000
        while time.time() < deadline:
            try:
                snip = await target.inner_text("body")
            except Exception:
                snip = ""
            if "Ukupno poena" in snip.lower() or re.search(r"\b\d+\.5\b", snip):
                break
            await page.wait_for_timeout(400)

        await _scroll_board(target, page, n=4)
        probe = await target.evaluate(DOM_EXTRACT_JS, "Points")
        if len(probe) < 3:
            # One more widen attempt
            await _apply_time_filter(target, "SVE")
            await page.wait_for_timeout(1200)
            used_filter = used_filter or "SVE"
            await _scroll_board(target, page, n=4)
            probe = await target.evaluate(DOM_EXTRACT_JS, "Points")

        if len(probe) >= 3:
            all_rows = await _scrape_markets(target, page, tabs)
            # Prefer rows tipping today when the board also includes later days.
            today_rows = [r for r in all_rows if _tip_is_today(str(r.get("tip_text") or ""))]
            if len(today_rows) >= 3:
                all_rows = today_rows
                used_filter = (used_filter or "") + "+today"

        await browser.close()

    by_market: dict[str, int] = {}
    for r in all_rows:
        m = str(r.get("market"))
        by_market[m] = by_market.get(m, 0) + 1
    if not all_rows:
        return {
            "saved_to": None,
            "error": (
                "Scrape returned 0 rows for today's window "
                f"(dateFrom={date_from}). Previous file kept if present."
            ),
            "offer_date_from": date_from,
            "offer_date_to": date_to,
            "time_filter": used_filter,
            "summary": {"row_count": 0, "by_market": {}},
            "lines": [],
        }
    path = save_lines_payload(all_rows, source="playwright")
    return {
        "saved_to": str(path),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "offer_date_from": date_from,
        "offer_date_to": date_to,
        "time_filter": used_filter,
        "summary": {"row_count": len(all_rows), "by_market": by_market},
        "lines": all_rows,
    }


def fetch_euroleague_player_totals(wait_ms: int = 12000) -> dict[str, Any]:
    """Sync wrapper for Streamlit button / scripts."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("Event loop running; use fetch_euroleague_player_totals_async")
    return asyncio.run(fetch_euroleague_player_totals_async(wait_ms=wait_ms))
