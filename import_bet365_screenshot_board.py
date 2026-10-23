"""
Build data/bet365_points_board.json from hand-transcribed bet365 Player Points
screenshots (local / OddsPapi-quota workaround).

Usage:
  python import_bet365_screenshot_board.py
  python import_bet365_screenshot_board.py --prefer   # also set .env flag

Market cut uses this board when it is fresher than OddsPapi, or when
EURO_PREFER_BET365_BOARD=1 is set in .env.

N+ milestone = Over (N - 0.5) vs Admiral half-lines.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BOARD_PATH = ROOT / "data" / "bet365_points_board.json"
ENV_PATH = ROOT / ".env"
ENV_FLAG = "EURO_PREFER_BET365_BOARD"


def _player(name: str, team_hint: str, odds: dict[str, float]) -> dict[str, Any]:
    clean = {k: float(v) for k, v in odds.items() if v is not None}
    return {
        "player": name,
        "team_hint": team_hint,
        "odds": clean,
        "odds_by_book": {"bet365": dict(clean)},
    }


# --- 2026-10-23 screenshots (Zalgiris vs ASVEL) ---
ZALGIRIS_ASVEL: list[dict[str, Any]] = [
    _player("Carsen Edwards", "Zalgiris", {"10+": 1.035, "15+": 1.22, "20+": 1.83, "25+": 3.35, "30+": 7.25}),
    _player("Patty Mills", "ASVEL", {"5+": 1.028, "10+": 1.32, "15+": 2.40, "20+": 5.75, "25+": 13.0}),
    _player("Jonas Valanciunas", "Zalgiris", {"5+": 1.02, "10+": 1.28, "15+": 2.30, "20+": 5.75, "25+": 13.0}),
    _player("Sterling Brown", "Zalgiris", {"5+": 1.045, "10+": 1.43, "15+": 2.82, "20+": 7.25, "25+": 17.0}),
    _player("Hugo Besson", "ASVEL", {"5+": 1.045, "10+": 1.43, "15+": 2.90, "20+": 7.50, "25+": 17.0}),
    _player("Azuolas Tubelis", "Zalgiris", {"5+": 1.045, "10+": 1.47, "15+": 3.05, "20+": 8.25, "25+": 18.0}),
    _player("Jae Crowder", "ASVEL", {"5+": 1.09, "10+": 1.80, "15+": 4.50, "20+": 12.0, "25+": 26.0}),
    _player("Marius Grigonis", "Zalgiris", {"5+": 1.12, "10+": 1.86, "15+": 4.75, "20+": 12.0, "25+": 26.0}),
    _player("Tremont Waters", "ASVEL", {"5+": 1.16, "10+": 2.10, "15+": 5.50, "20+": 14.0, "25+": 29.0}),
    _player("Dustin Sleva", "Zalgiris", {"5+": 1.32, "10+": 3.20, "15+": 9.75, "20+": 21.0, "25+": 36.0}),
    _player("Both Tut Gach", "ASVEL", {"5+": 1.33, "10+": 3.40, "15+": 11.0, "20+": 26.0, "25+": 36.0}),
    _player("Joel Bolomboy", "ASVEL", {"5+": 1.45, "10+": 4.20, "15+": 14.0, "20+": 36.0}),
]

# --- 2026-10-23 screenshots (Olympiacos vs Fenerbahce) — merged from two scrolls ---
OLY_FENER: list[dict[str, Any]] = [
    _player(
        "Aleksandar Vezenkov",
        "Olympiacos",
        {"10+": 1.02, "15+": 1.16, "20+": 1.66, "25+": 2.95, "30+": 6.50, "35+": 13.0},
    ),
    _player(
        "Talen Horton-Tucker",
        "Fenerbahce",
        {"5+": 1.025, "10+": 1.31, "15+": 2.40, "20+": 6.00, "25+": 14.0, "30+": 29.0},
    ),
    _player(
        "Codi Miller-McIntyre",
        "Olympiacos",
        {"10+": 1.32, "15+": 2.40, "20+": 6.00, "25+": 14.0, "30+": 29.0},
    ),
    _player(
        "Wade Baldwin",
        "Fenerbahce",
        {"10+": 1.34, "15+": 2.55, "20+": 6.75, "25+": 16.0, "30+": 36.0},
    ),
    _player(
        "Shavon Shields",
        "Fenerbahce",
        {"5+": 1.076, "10+": 1.64, "15+": 3.75, "20+": 10.0, "25+": 20.0},
    ),
    _player(
        "Nikola Milutinov",
        "Olympiacos",
        {"5+": 1.083, "10+": 1.74, "15+": 4.20, "20+": 11.5, "25+": 26.0},
    ),
    _player(
        "Mbaye Ndiaye",
        "Olympiacos",
        {"5+": 1.083, "10+": 1.74, "15+": 4.20, "20+": 11.5, "25+": 26.0},
    ),
    _player(
        "Will Clyburn",
        "Fenerbahce",
        {"5+": 1.12, "10+": 1.86, "15+": 4.75, "20+": 13.0, "25+": 26.0},
    ),
    _player(
        "Braxton Key",
        "Fenerbahce",
        {"5+": 1.11, "10+": 1.86, "15+": 5.25, "20+": 14.0, "25+": 29.0},
    ),
    _player(
        "Tyson Ward",
        "Olympiacos",
        {"5+": 1.14, "10+": 2.10, "15+": 6.00, "20+": 16.0, "25+": 36.0},
    ),
    _player("Trent Forrest", "Fenerbahce", {"5+": 1.55, "10+": 5.00, "15+": 17.0, "20+": 36.0}),
    _player("Tyrique Jones", "Olympiacos", {"5+": 1.45, "10+": 4.50, "15+": 14.0, "20+": 36.0}),
    _player("Nicolo Melli", "Fenerbahce", {"5+": 1.45, "10+": 4.00, "15+": 12.0, "20+": 36.0}),
    _player(
        "Kostas Papanikolaou",
        "Olympiacos",
        {"5+": 1.50, "10+": 4.50, "15+": 14.0, "20+": 36.0},
    ),
]

# --- 2026-10-23 screenshot (Paris vs Valencia) — only 5 / 10 columns visible ---
PARIS_VALENCIA: list[dict[str, Any]] = [
    _player("Tyson Etienne", "Paris", {"10+": 1.045}),
    _player("T.J. Shorts", "Valencia", {"10+": 1.09}),
    _player("Nikola Mirotic", "Valencia", {"10+": 1.13}),
    _player("Armoni Brooks", "Valencia", {"5+": 1.02, "10+": 1.25}),
    _player("Kameron Taylor", "Valencia", {"5+": 1.015, "10+": 1.23}),
    _player("Jared Rhoden", "Paris", {"5+": 1.028, "10+": 1.34}),
    _player("Sebastian Herrera", "Paris", {"5+": 1.09, "10+": 1.66}),
    _player("Nate Reuvers", "Valencia", {"5+": 1.14, "10+": 2.10}),
    _player("Josep Puerto", "Valencia", {"5+": 1.30, "10+": 3.00}),
    _player("Mouhamed Faye", "Paris", {"5+": 1.37, "10+": 3.65}),
    _player("Allan Dokossi", "Paris", {"5+": 1.66, "10+": 5.75}),
    _player("Alize Johnson", "Paris", {"5+": 1.41, "10+": 4.00}),
]


def build_board() -> dict[str, Any]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "source": "bet365 screenshots",
        "fetched_at": now,
        "note": (
            "Hand-transcribed Player Points N+ overs. "
            "N+ maps to Admiral Over (N-0.5). Locked/blank cells omitted. "
            "2026-10-23: Zalgiris–ASVEL, Olympiacos–Fener, Paris–Valencia."
        ),
        "games": [
            {
                "event": "Zalgiris Kaunas vs ASVEL Lyon-Villeurbanne",
                "tip_off": "2026-10-23T18:00",
                "players": ZALGIRIS_ASVEL,
            },
            {
                "event": "Olympiacos vs Fenerbahce",
                "tip_off": "2026-10-23T19:45",
                "players": OLY_FENER,
            },
            {
                "event": "Paris Basketball vs Valencia Basket",
                "tip_off": "2026-10-23T19:00",
                "players": PARIS_VALENCIA,
            },
        ],
    }


def write_board(path: Path | None = None) -> dict[str, Any]:
    path = path or BOARD_PATH
    payload = build_board()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    n = sum(len(g["players"]) for g in payload["games"])
    print(f"Wrote {path} — {len(payload['games'])} games, {n} players, fetched_at={payload['fetched_at']}")
    return payload


def ensure_prefer_env() -> None:
    """Append EURO_PREFER_BET365_BOARD=1 to local .env if missing (gitignored)."""
    line = f"{ENV_FLAG}=1"
    if ENV_PATH.exists():
        text = ENV_PATH.read_text(encoding="utf-8")
        if ENV_FLAG in text:
            print(f".env already mentions {ENV_FLAG}")
            return
        ENV_PATH.write_text(text.rstrip() + "\n" + line + "\n", encoding="utf-8")
    else:
        ENV_PATH.write_text(line + "\n", encoding="utf-8")
    print(f"Set {line} in {ENV_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefer",
        action="store_true",
        help=f"Write {ENV_FLAG}=1 into local .env so Market cut uses this board",
    )
    args = parser.parse_args()
    write_board()
    if args.prefer:
        ensure_prefer_env()
        os.environ[ENV_FLAG] = "1"


if __name__ == "__main__":
    main()
