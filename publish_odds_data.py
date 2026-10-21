"""
Publish local odds snapshots for Streamlit Cloud.

Local machine only:
  1) Refresh OddsPapi / AdmiralBet / 1x2online → writes data/*.json
  2) Run this script to stage the Cloud-readable snapshots
  3) Commit + push (Cloud never calls RapidAPI or Playwright)

Usage:
  python publish_odds_data.py              # status + stage snapshots
  python publish_odds_data.py --refresh    # refresh all sources then stage
  python publish_odds_data.py --status     # show file ages only (no git)

Cloud reads:
  data/oddspapi_points_board.json
  data/admiralbet_lines.json
  data/1x2online_lines.json
  data/bet365_points_board.json   (optional legacy fallback)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# Files Streamlit Cloud must be able to read from git (no live API/scrape).
CLOUD_SNAPSHOTS = (
    DATA / "oddspapi_points_board.json",
    DATA / "admiralbet_lines.json",
    DATA / "1x2online_lines.json",
    DATA / "bet365_points_board.json",
)


def _fetched_at(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("fetched_at") or "—")
    except (OSError, json.JSONDecodeError):
        return "unreadable"


def _mtime(path: Path) -> str:
    if not path.exists():
        return "—"
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def _rows(path: Path) -> str:
    if not path.exists():
        return "—"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "?"
    if "lines" in data:
        return str(len(data.get("lines") or []))
    games = data.get("games") or []
    n = sum(len(g.get("players") or []) for g in games)
    return str(n)


def print_status() -> None:
    print("Cloud odds snapshots (commit these after local refresh):\n")
    print(f"{'file':40} {'rows':>6}  {'fetched_at':22}  mtime")
    print("-" * 90)
    for path in CLOUD_SNAPSHOTS:
        rel = path.relative_to(ROOT).as_posix()
        print(f"{rel:40} {_rows(path):>6}  {_fetched_at(path):22}  {_mtime(path)}")
    print()
    print("Local-only (gitignored — do not need to push):")
    for name in (
        "oddspapi_props.json",
        "oddspapi_bet365.json",
        "1x2online_capture.json",
        "admiralbet_capture.json",
        "market_value_compare.json",
    ):
        p = DATA / name
        mark = "ok" if p.exists() else "—"
        print(f"  [{mark}] data/{name}")


def run_refresh() -> int:
    """Refresh OddsPapi + scrapes locally. Returns process exit code."""
    print("Refreshing OddsPapi props…")
    r1 = subprocess.run([sys.executable, "euro_oddspapi.py", "--refresh"], cwd=ROOT)
    if r1.returncode != 0:
        print("OddsPapi refresh failed (continuing with other sources).")

    print("Refreshing AdmiralBet scrape…")
    r2 = subprocess.run(
        [
            sys.executable,
            "-c",
            "from euro_admiralbet import fetch_euroleague_player_totals; "
            "import json; print(json.dumps(fetch_euroleague_player_totals(), default=str)[:500])",
        ],
        cwd=ROOT,
    )
    if r2.returncode != 0:
        print("AdmiralBet refresh failed (continuing).")

    print("Refreshing 1x2online scrape (Selektuj sve)…")
    r3 = subprocess.run([sys.executable, "euro_1x2online.py"], cwd=ROOT)
    if r3.returncode != 0:
        print("1x2online refresh failed.")

    return 0 if (r1.returncode == 0 or (DATA / "oddspapi_points_board.json").exists()) else 1


def stage_snapshots() -> list[str]:
    """git add the cloud snapshot files that exist. Returns staged relative paths."""
    staged: list[str] = []
    for path in CLOUD_SNAPSHOTS:
        if not path.exists():
            print(f"  skip (missing): {path.relative_to(ROOT).as_posix()}")
            continue
        rel = path.relative_to(ROOT).as_posix()
        subprocess.run(["git", "add", "-f", rel], cwd=ROOT, check=False)
        staged.append(rel)
        print(f"  staged: {rel}")
    return staged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Locally refresh OddsPapi + AdmiralBet + 1x2online before staging",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Only print snapshot status (no git)",
    )
    parser.add_argument(
        "--stage",
        action="store_true",
        help="git add cloud snapshots (default if neither --status nor --refresh-only)",
    )
    args = parser.parse_args()

    print_status()
    if args.status:
        return 0

    if args.refresh:
        run_refresh()
        print()
        print_status()

    do_stage = args.stage or args.refresh or not args.status
    if do_stage and not args.status:
        print("\nStaging cloud snapshots for commit…")
        staged = stage_snapshots()
        if not staged:
            print("Nothing to stage.")
            return 1
        print(
            "\nNext (when ready):\n"
            "  git status\n"
            "  git commit -m \"Update odds snapshots for Streamlit Cloud\"\n"
            "  git push\n"
            "\nCloud will redeploy and read these files — no RapidAPI key / Playwright on the server."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
