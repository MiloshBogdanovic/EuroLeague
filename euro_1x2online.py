"""
1x2online.net player Points O/U scrape (Poeni igraca).

The board only appears after clicking:
  Košarka → Poeni igraca → Selektuj sve

Used by Market cut as book slug `1x2online` (local Playwright only).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from euro_utils import DATA_DIR

LINES_PATH = DATA_DIR / "1x2online_lines.json"
CAPTURE_PATH = DATA_DIR / "1x2online_capture.json"

DEFAULT_URL = "https://www.1x2online.net/prematch/today"
BOOK_SLUG = "1x2online"

# DOM extract after Selektuj sve expands app-event rows.
DOM_EXTRACT_JS = """() => {
  const out = [];
  const events = document.querySelectorAll('app-event.prematch-event, app-event');
  for (const ev of events) {
    const title = ev.querySelector('.event-title');
    const titleAttr = title ? (title.getAttribute('title') || '') : '';
    const items = [...ev.querySelectorAll('.event-title .title-item')].map(
      (s) => (s.textContent || '').trim()
    ).filter(Boolean);
    let player = items[0] || '';
    let team = items[1] || '';
    if ((!player || !team) && titleAttr.includes(' - ')) {
      const parts = titleAttr.split(' - ');
      player = player || (parts[0] || '').trim();
      team = team || (parts.slice(1).join(' - ') || '').trim();
    }
    if (!player) continue;

    const timeEls = [...ev.querySelectorAll('.time-item')].map(
      (s) => (s.textContent || '').trim()
    );
    const tip = timeEls[0] || '';
    const eventId = timeEls.find((t) => /^\\d{2,}$/.test(t)) || null;

    // Broj poena market: selection codes bpkm (under) / bpkv (over), line in .band
    let under = null, over = null, line = null;
    const underEl = ev.querySelector('[data-selectioncode="bpkm"]');
    const overEl = ev.querySelector('[data-selectioncode="bpkv"]');
    const bandEl = ev.querySelector('.band span, .band');
    const parseNum = (el) => {
      if (!el) return null;
      const t = (el.textContent || '').trim().replace(',', '.');
      if (!t || t === '-') return null;
      const n = Number(t);
      return Number.isFinite(n) ? n : null;
    };
    under = parseNum(underEl);
    over = parseNum(overEl);
    line = parseNum(bandEl);

    // Fallback: three consecutive numbers under/line/over in market containers
    if (line == null || (under == null && over == null)) {
      const quotas = [...ev.querySelectorAll('.market-container')].map((mc) =>
        [...mc.querySelectorAll('.selected_quota')].map((q) =>
          (q.textContent || '').trim().replace(',', '.')
        )
      );
      for (const nums of quotas) {
        const vals = nums.map((t) => {
          if (!t || t === '-') return null;
          const n = Number(t);
          return Number.isFinite(n) ? n : null;
        });
        // look for under, line(.5), over
        for (let i = 0; i < vals.length - 2; i++) {
          const a = vals[i], b = vals[i + 1], c = vals[i + 2];
          if (a != null && b != null && c != null && Math.abs(b - Math.floor(b) - 0.5) < 1e-6) {
            under = a; line = b; over = c;
            break;
          }
        }
        if (line != null) break;
      }
    }

    if (line == null) continue;
    out.push({
      player,
      team,
      event_id: eventId,
      tip_text: tip,
      under_odds: under,
      line,
      over_odds: over,
      market: 'Points',
      source: 'dom',
    });
  }
  return out;
}"""


def is_streamlit_cloud() -> bool:
    home = Path.home().as_posix()
    return home.startswith("/home/appuser") or bool(os.environ.get("STREAMLIT_SHARING_MODE"))


def playwright_ready() -> tuple[bool, str]:
    if is_streamlit_cloud():
        return False, "1x2online scrape is local-only (not available on Streamlit Cloud)."
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "Playwright not installed. Locally: pip install playwright && playwright install chromium"
    return True, ""


def save_lines_payload(rows: list[dict[str, Any]], *, source: str = "playwright") -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    clean: list[dict[str, Any]] = []
    for r in rows:
        player = (r.get("player") or "").strip()
        if not player or r.get("line") is None:
            continue
        clean.append(
            {
                "player": player,
                "team": (r.get("team") or "").strip() or None,
                "event_id": r.get("event_id"),
                "line": float(r["line"]),
                "under_odds": float(r["under_odds"]) if r.get("under_odds") is not None else None,
                "over_odds": float(r["over_odds"]) if r.get("over_odds") is not None else None,
                "market": "Points",
                "source": r.get("source") or source,
                "tip_text": r.get("tip_text"),
            }
        )
    if not clean:
        if LINES_PATH.exists():
            raise RuntimeError("1x2online scrape returned 0 rows; left previous file untouched.")
        raise RuntimeError("1x2online scrape returned 0 rows.")

    payload = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "competition": "Poeni igraca",
        "source": source,
        "url": DEFAULT_URL,
        "summary": {"row_count": len(clean), "by_market": {"Points": len(clean)}},
        "lines": clean,
    }
    LINES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    CAPTURE_PATH.write_text(
        json.dumps({**payload, "dom_lines": clean}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return LINES_PATH


def load_1x2online_lines() -> dict[str, Any]:
    for path in (LINES_PATH, CAPTURE_PATH):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = data.get("lines") or data.get("dom_lines") or []
        return {
            "fetched_at": data.get("fetched_at"),
            "source": data.get("source") or "file",
            "summary": data.get("summary")
            or {"row_count": len(rows), "by_market": {"Points": len(rows)}},
            "lines": rows,
            "path": str(path),
        }
    return {
        "fetched_at": None,
        "source": None,
        "summary": {"row_count": 0, "by_market": {}},
        "lines": [],
        "path": None,
    }


async def _click_title(page, pattern: str, *, timeout_ms: int = 8000) -> bool:
    """Click a sidebar/menu span.title matching pattern (case-insensitive)."""
    loc = page.locator("span.title").filter(has_text=re.compile(pattern, re.I))
    try:
        await loc.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        return False
    try:
        await loc.first.click(timeout=3000)
        return True
    except Exception:
        # Parent clickable wrappers (a / label / li)
        handle = await loc.first.element_handle()
        if not handle:
            return False
        clicked = await page.evaluate(
            """(el) => {
              const c = el.closest('a, label, li, button') || el;
              c.click();
              return true;
            }""",
            handle,
        )
        return bool(clicked)


async def fetch_player_points_async(wait_ms: int = 15000) -> dict[str, Any]:
    """
    Playwright: open prematch → Poeni igraca → Selektuj sve → parse Broj poena.
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

    rows: list[dict[str, Any]] = []
    steps: list[str] = []

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
            viewport={"width": 1500, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            await page.goto(DEFAULT_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)
            steps.append("goto_prematch")

            async def _titles() -> list[str]:
                return await page.evaluate(
                    """() => [...document.querySelectorAll('span.title')]
                      .map(s => (s.innerText || s.getAttribute('title') || '').trim())
                      .filter(Boolean)"""
                )

            async def _has_poeni() -> bool:
                titles = await _titles()
                return any(re.search(r"poeni\s*igrac", t, re.I) for t in titles)

            # Poeni igraca often sits under Liste i dodaci — try that, then Košarka.
            if not await _has_poeni():
                if await _click_title(page, r"Liste i dodaci"):
                    steps.append("click_liste_i_dodaci")
                    await page.wait_for_timeout(1200)
            if not await _has_poeni():
                if await _click_title(page, r"^Košarka$|^Kosarka$"):
                    steps.append("click_kosarka")
                    await page.wait_for_timeout(1500)

            # Prefer JS click by exact title text (Angular wrappers)
            clicked_poeni = await page.evaluate(
                """() => {
                  const el = [...document.querySelectorAll('span.title')].find(s =>
                    /poeni\\s*igrac/i.test((s.innerText || s.getAttribute('title') || ''))
                  );
                  if (!el) return false;
                  (el.closest('a, label, li, button') || el).click();
                  return true;
                }"""
            )
            if clicked_poeni:
                steps.append("click_poeni_igraca")
                await page.wait_for_timeout(1500)
            elif await _click_title(page, r"Poeni\s*igrac"):
                steps.append("click_poeni_igraca_locator")
                await page.wait_for_timeout(1500)
            else:
                titles = await _titles()
                await browser.close()
                return {
                    "saved_to": None,
                    "error": "Could not find 'Poeni igraca' in the left menu.",
                    "steps": steps,
                    "menu_titles": titles[:40],
                    "summary": {"row_count": 0, "by_market": {}},
                    "lines": [],
                }

            # Critical: table only appears after Select all
            selektuj = page.locator('span.title[title="Selektuj sve"]').or_(
                page.locator("span.title").filter(has_text=re.compile(r"^Selektuj sve$", re.I))
            )
            try:
                await selektuj.first.wait_for(state="visible", timeout=10000)
                handle = await selektuj.first.element_handle()
                if handle:
                    await page.evaluate(
                        """(el) => {
                          const c = el.closest('a, label, li, button') || el;
                          c.click();
                        }""",
                        handle,
                    )
                else:
                    await selektuj.first.click()
                steps.append("click_selektuj_sve")
            except Exception as exc:  # noqa: BLE001
                await browser.close()
                return {
                    "saved_to": None,
                    "error": f"Could not click 'Selektuj sve': {exc}",
                    "steps": steps,
                    "summary": {"row_count": 0, "by_market": {}},
                    "lines": [],
                }

            # Wait for Broj poena / app-event rows
            deadline_ms = max(wait_ms, 10000)
            await page.wait_for_timeout(1500)
            try:
                await page.wait_for_function(
                    """() => {
                      const n = document.querySelectorAll('app-event').length;
                      const body = document.body ? document.body.innerText : '';
                      return n >= 3 && /Broj poena/i.test(body);
                    }""",
                    timeout=deadline_ms,
                )
                steps.append("board_ready")
            except Exception:
                steps.append("board_wait_timeout")

            # Scroll to load lazy rows
            for _ in range(5):
                await page.mouse.wheel(0, 1800)
                await page.wait_for_timeout(400)

            rows = await page.evaluate(DOM_EXTRACT_JS)
            steps.append(f"extracted_{len(rows)}")
        finally:
            await browser.close()

    if not rows:
        return {
            "saved_to": None,
            "error": (
                "1x2online scrape returned 0 player rows after Selektuj sve. "
                f"steps={steps}"
            ),
            "steps": steps,
            "summary": {"row_count": 0, "by_market": {}},
            "lines": [],
        }

    try:
        path = save_lines_payload(rows, source="playwright")
    except RuntimeError as exc:
        return {
            "saved_to": None,
            "error": str(exc),
            "steps": steps,
            "summary": {"row_count": 0, "by_market": {}},
            "lines": [],
        }

    return {
        "saved_to": str(path),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "steps": steps,
        "summary": {"row_count": len(rows), "by_market": {"Points": len(rows)}},
        "lines": rows,
    }


def fetch_player_points(wait_ms: int = 15000) -> dict[str, Any]:
    """Sync wrapper for Streamlit / CLI."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("Event loop running; use fetch_player_points_async")
    return asyncio.run(fetch_player_points_async(wait_ms=wait_ms))


if __name__ == "__main__":
    result = fetch_player_points()
    print(json.dumps({k: result.get(k) for k in ("ok", "error", "saved_to", "fetched_at", "summary", "steps")}, indent=2))
    if result.get("lines"):
        print("sample:", result["lines"][:3])
