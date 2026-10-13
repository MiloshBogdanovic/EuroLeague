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

LINES_PATH = DATA_DIR / "admiralbet_lines.json"
CAPTURE_PATH = DATA_DIR / "admiralbet_capture.json"

# AdmiralBet market label -> boxscore column used in the app
MARKET_TABS: list[tuple[str, str]] = [
    ("Ukupno Poena", "Points"),
    ("Ukupno Asistencija", "Assists"),
    ("Ukupno Skokova", "TotalRebounds"),
    ("Ukupno Pogodjenih Trojki", "FGM3"),
    ("Ukupno Pogođenih Trojki", "FGM3"),
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

DOM_EXTRACT_JS = """(marketKey) => {
  const text = document.body ? document.body.innerText : '';
  const lines = text.split(/\\n/).map(s => s.trim()).filter(Boolean);
  const start = lines.findIndex(l => l === 'EVROLIGA' || l === 'Igrači ukupno');
  const slice = start >= 0 ? lines.slice(start) : lines;
  const skip = new Set([
    'Evroliga','EVROLIGA','Igrači ukupno','IGRAČI UKUPNO','Košarka igrači',
    '+','-','manje','vise','Ukupno Poena','Ukupno Asistencija','Ukupno Skokova',
    'Ukupno Pogodjenih Trojki','Ukupno Pogođenih Trojki','Select statistic',
    'OBIČAN','Uplata','BRZI TIKET'
  ]);
  const nameRe = /^[A-Za-zČĆŠŽĐčćšžđ][A-Za-zČĆŠŽĐčćšžđ.'\\-]*(?:\\s+[A-Za-zČĆŠŽĐčćšžđ.'\\-]+){1,3}$/;
  const out = [];
  for (let i = 0; i < slice.length - 4; i++) {
    const name = slice[i];
    const team = slice[i + 1];
    const date = slice[i + 2] || '';
    if (!nameRe.test(name)) continue;
    if (skip.has(name) || skip.has(team)) continue;
    if (!/\\d{1,2}\\.\\s*\\d{1,2}\\./.test(date) && !/\\d{4,}/.test(date)) continue;
    const nums = [];
    for (let j = i + 3; j < Math.min(i + 10, slice.length); j++) {
      const tok = slice[j];
      if (tok === 'Evroliga' || tok === '+') break;
      if (tok === '-') { nums.push(null); continue; }
      if (/^\\d+(?:\\.\\d+)?$/.test(tok)) nums.push(Number(tok));
      else break;
    }
    if (nums.filter(n => n != null).length < 2) continue;
    let under = null, line = null, over = null;
    if (nums.length >= 3 && nums[1] === nums[2]) {
      under = nums[0]; line = nums[1]; over = nums[3] != null ? nums[3] : nums[0];
    } else if (nums.length >= 3) {
      under = nums[0]; line = nums[1]; over = nums[2];
    } else {
      line = nums.find(n => n != null && String(n).includes('.'));
    }
    if (line == null) continue;
    const idm = String(date).match(/(\\d{1,2}):(\\d{2})(\\d{4,})$/);
    out.push({
      player: name,
      team,
      event_id: idm ? idm[3] : null,
      under_odds: under,
      line,
      over_odds: over,
      market: marketKey || 'Points',
      source: 'dom'
    });
  }
  const seen = new Set();
  const dedup = [];
  for (const r of out) {
    const k = r.player + '|' + r.team;
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
        # Map Serbian labels if still present
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

    payload = {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "competition": "Evroliga",
        "source": source,
        "summary": {"row_count": len(clean), "by_market": by_market},
        "lines": clean,
    }
    LINES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Keep capture file in sync for MCP / debug
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
        # last-token / contains fallback
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
        # keep first (or prefer one with odds)
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


async def _click_market(target, label: str) -> bool:
    try:
        loc = target.get_by_text(label, exact=True)
        if await loc.count() > 0:
            await loc.first.click(timeout=2500)
            return True
    except Exception:
        pass
    try:
        loc = target.get_by_text(label, exact=False)
        if await loc.count() > 0:
            await loc.first.click(timeout=2500)
            return True
    except Exception:
        pass
    return False


async def fetch_euroleague_player_totals_async(
    wait_ms: int = 12000,
    *,
    markets: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Playwright scrape of Evroliga player O/U tabs; writes admiralbet_lines.json."""
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
    all_rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

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
        await page.goto(OUTER_URL, wait_until="domcontentloaded", timeout=60000)
        await _accept_cookies(page)
        await page.wait_for_timeout(1500)
        target = page.frame(name="sportIframe") or page
        if target is not page:
            try:
                await target.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await target.evaluate(
                """() => {
                  const h = '#/betting/prematch?language=sr'
                    + '&sport=' + encodeURIComponent('Košarka_igrači')
                    + '&region=' + encodeURIComponent('Igrači_ukupno')
                    + '&competition=Evroliga'
                    + '&competitionId=123-1416-25158';
                  location.hash = h;
                }"""
            )
            await page.wait_for_timeout(2500)
        else:
            await page.goto(DEFAULT_SPA_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)

        # Warm-up: wait for any prop list
        deadline = time.time() + max(wait_ms, 8000) / 1000
        while time.time() < deadline:
            try:
                snip = await target.inner_text("body")
            except Exception:
                snip = ""
            if "Ukupno Poena" in snip or "Shields" in snip or re.search(r"\b\d+\.5\b", snip):
                break
            await page.wait_for_timeout(400)

        for label, key in tabs:
            if key in seen_keys and label != "Ukupno Pogođenih Trojki":
                # allow alt spelling only if FGM3 not collected yet
                if key in seen_keys:
                    continue
            clicked = await _click_market(target, label)
            await page.wait_for_timeout(1600 if clicked else 400)
            for _ in range(6):
                try:
                    await target.mouse.wheel(0, 2600)
                except Exception:
                    await page.mouse.wheel(0, 2600)
                await page.wait_for_timeout(250)
            rows = await target.evaluate(DOM_EXTRACT_JS, key)
            if len(rows) >= 5:
                seen_keys.add(key)
                all_rows.extend(rows)

        await browser.close()

    by_market: dict[str, int] = {}
    for r in all_rows:
        m = str(r.get("market"))
        by_market[m] = by_market.get(m, 0) + 1
    if not all_rows:
        return {
            "saved_to": None,
            "error": "Scrape returned 0 rows (SPA/hash may need a headed browser). Previous file kept if present.",
            "summary": {"row_count": 0, "by_market": {}},
            "lines": [],
        }
    path = save_lines_payload(all_rows, source="playwright")
    return {
        "saved_to": str(path),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
        # Nested loop (e.g. MCP) — caller should use async variant
        raise RuntimeError("Event loop running; use fetch_euroleague_player_totals_async")
    return asyncio.run(fetch_euroleague_player_totals_async(wait_ms=wait_ms))
