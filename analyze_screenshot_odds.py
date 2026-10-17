"""Compare screenshot 'X+' overs vs AdmiralBet O/U Points pricing.

Deprecated as the main path — prefer OddsPapi refresh in the Streamlit app
(`euro_oddspapi.refresh_euroleague_props`) + Market cut page. Kept for
one-off debugging of pasted boards.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "admiralbet_capture.json"

# Screenshot provider: X+ = Over (X - 0.5). No unders shown.
SCREENSHOT = {
    "Carlik Jones": {15: 1.55, 20: 3.00, 25: 7.25, 30: 16.00, 35: 29.00},
    "Talen Horton-Tucker": {10: 1.31, 15: 2.40, 20: 6.00, 25: 14.00, 30: 26.00},
    "Wade Baldwin": {10: 1.34, 15: 2.55, 20: 6.75, 25: 16.00, 30: 29.00},
    "Braxton Key": {10: 1.43, 15: 2.95, 20: 7.75, 25: 18.00, 30: 36.00},
    "Tonye Jekiri": {5: 1.071, 10: 1.64, 15: 3.90, 20: 10.50, 25: 21.00},
    "Shavon Shields": {5: 1.10, 10: 1.83, 15: 4.50, 20: 12.00, 25: 26.00},
    "Will Clyburn": {5: 1.18, 10: 2.30, 15: 6.25, 20: 16.00, 25: 36.00},
    "Luca Vildoza": {5: 1.22, 10: 2.55, 15: 7.50, 20: 19.00, 25: 36.00},
    "Kevarrius Hayes": {5: 1.27, 10: 2.95, 15: 9.75, 20: 23.00, 25: 36.00},
    "Nicolo Melli": {5: 1.34, 10: 3.30, 15: 10.00, 20: 23.00, 25: 36.00},
    "Alessandro Pajola": {5: 1.52, 10: 4.50, 15: 14.00, 20: 36.00},
}

# Focus lines user asked for: 5+/10+/15+/20+ -> 4.5/9.5/14.5/19.5
FOCUS_PLUS = (5, 10, 15, 20)


def norm(s: str) -> str:
    return s.lower().replace("-", " ").replace(".", "").strip()


def implied(odds: float) -> float:
    return 1.0 / odds


def vig(over: float, under: float) -> float:
    return implied(over) + implied(under)


def fair_probs(over: float, under: float) -> tuple[float, float]:
    io, iu = implied(over), implied(under)
    t = io + iu
    return io / t, iu / t


def impute_under(over_odds: float, target_vig: float) -> float:
    """Solve 1/O + 1/U = target_vig  =>  U = 1 / (target_vig - 1/O)."""
    io = implied(over_odds)
    rem = target_vig - io
    if rem <= 0:
        return float("nan")
    return 1.0 / rem


def main() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    pts = [r for r in data["lines"] if r.get("market") == "Points" and r.get("over_odds") and r.get("under_odds")]

    # Index AdmiralBet by normalized player name
    by_player: dict[str, list[dict]] = {}
    for r in pts:
        by_player.setdefault(norm(r["player"]), []).append(r)

    # Global AdmiralBet vig stats for Points
    vigs = [vig(r["over_odds"], r["under_odds"]) for r in pts]
    avg_vig = sum(vigs) / len(vigs)
    med_vig = sorted(vigs)[len(vigs) // 2]
    print("=" * 72)
    print("ADMIRALBET POINTS — book cut (over+under implied)")
    print(f"  n={len(vigs)}  avg_raw={avg_vig*100:.2f}%  med={med_vig*100:.2f}%  "
          f"min={min(vigs)*100:.2f}%  max={max(vigs)*100:.2f}%")
    print(f"  typical cut (above 100%) ~= {(avg_vig-1)*100:.2f}%")
    print("=" * 72)

    # Match screenshot players
    print("\nPLAYER MATCH + ADMIRALBET AVAILABLE LINES")
    matched: dict[str, list[dict]] = {}
    for name in SCREENSHOT:
        key = norm(name)
        rows = by_player.get(key)
        if not rows:
            # fuzzy: all tokens present
            rows = []
            for k, v in by_player.items():
                if all(tok in k for tok in key.split()):
                    rows = v
                    break
        if rows:
            matched[name] = sorted(rows, key=lambda x: x["line"])
            lines = ", ".join(f"{r['line']}(O{r['over_odds']}/U{r['under_odds']})" for r in matched[name])
            print(f"  OK  {name:22} team={matched[name][0].get('team')}  lines: {lines}")
        else:
            print(f"  --  {name:22} NOT IN ADMIRALBET CAPTURE")

    # Compare focus overs
    print("\n" + "=" * 72)
    print("COMPARISON: screenshot OVER vs AdmiralBet (same line when available)")
    print("  X+  <=>  Over (X-0.5)")
    print("=" * 72)

    findings = []

    for name, plus_map in SCREENSHOT.items():
        rows = {r["line"]: r for r in matched.get(name, [])}
        print(f"\n--- {name} ---")
        player_vigs = [vig(r["over_odds"], r["under_odds"]) for r in matched.get(name, [])]
        player_avg_vig = sum(player_vigs) / len(player_vigs) if player_vigs else avg_vig

        for plus in FOCUS_PLUS:
            if plus not in plus_map:
                continue
            line = plus - 0.5
            ss_over = plus_map[plus]
            ss_ip = implied(ss_over)
            admiral = rows.get(line)

            if admiral:
                ao, au = admiral["over_odds"], admiral["under_odds"]
                a_vig = vig(ao, au)
                fair_o, fair_u = fair_probs(ao, au)
                # Impute SS under using Admiral's vig on this line
                ss_under = impute_under(ss_over, a_vig)
                # Also using player avg vig
                ss_under_avg = impute_under(ss_over, player_avg_vig)

                edge_over = (implied(ao) - ss_ip)  # positive => SS has better (lower) over price
                print(
                    f"  {plus:>2}+ / {line}: SS O={ss_over:.3f} ({ss_ip*100:5.1f}%) | "
                    f"ADM O={ao:.2f} ({implied(ao)*100:5.1f}%) U={au:.2f} ({implied(au)*100:5.1f}%) "
                    f"raw={a_vig*100:.1f}% fairO={fair_o*100:.1f}% | "
                    f"dO_ip={edge_over*100:+.1f}pp | "
                    f"imputed SS U~={ss_under:.2f} (line vig) / {ss_under_avg:.2f} (player avg vig)"
                )
                findings.append({
                    "player": name,
                    "plus": plus,
                    "line": line,
                    "ss_over": ss_over,
                    "adm_over": ao,
                    "adm_under": au,
                    "adm_vig": a_vig,
                    "ss_ip": ss_ip,
                    "adm_ip": implied(ao),
                    "edge_pp": edge_over * 100,
                    "imputed_ss_under": ss_under,
                    "fair_over": fair_o,
                })
            else:
                # No matching Admiral line — impute under with book avg vig
                ss_under = impute_under(ss_over, player_avg_vig)
                nearest = None
                if rows:
                    nearest = min(rows.keys(), key=lambda L: abs(L - line))
                print(
                    f"  {plus:>2}+ / {line}: SS O={ss_over:.3f} ({ss_ip*100:5.1f}%) | "
                    f"ADM: no {line} line"
                    + (f" (nearest {nearest}: O{rows[nearest]['over_odds']}/U{rows[nearest]['under_odds']})" if nearest is not None else "")
                    + f" | imputed SS U~={ss_under:.2f} using vig={player_avg_vig*100:.1f}%"
                )
                findings.append({
                    "player": name,
                    "plus": plus,
                    "line": line,
                    "ss_over": ss_over,
                    "adm_over": None,
                    "adm_under": None,
                    "adm_vig": player_avg_vig,
                    "ss_ip": ss_ip,
                    "adm_ip": None,
                    "edge_pp": None,
                    "imputed_ss_under": ss_under,
                    "fair_over": None,
                })

        # Also show non-focus screenshot ladders vs any admiral main line
        extra = [p for p in sorted(plus_map) if p not in FOCUS_PLUS]
        if extra:
            print(f"  (also on SS ladder: {', '.join(f'{p}+ @{plus_map[p]}' for p in extra)})")

    # Summary edges where both books have same line
    print("\n" + "=" * 72)
    print("EDGE SUMMARY (same line only) — positive d = screenshot OVER cheaper than Admiral")
    print("=" * 72)
    same = [f for f in findings if f["edge_pp"] is not None]
    if not same:
        print("  No exact overlapping lines between screenshot focus and AdmiralBet.")
    else:
        same_sorted = sorted(same, key=lambda x: -abs(x["edge_pp"]))
        for f in same_sorted:
            cheaper = "SS better OVER" if f["edge_pp"] > 0 else "ADM better OVER"
            print(
                f"  {f['player']:22} {f['plus']}+ ({f['line']}): "
                f"SS {f['ss_over']:.3f} vs ADM {f['adm_over']:.2f}  "
                f"dip={f['edge_pp']:+.1f}pp  ({cheaper})  "
                f"imputed U={f['imputed_ss_under']:.2f}"
            )

    # Pattern: Admiral main lines vs screenshot ladder at nearby thresholds
    print("\n" + "=" * 72)
    print("MAIN-LINE CROSSWALK (Admiral primary line vs nearest SS ladder rung)")
    print("=" * 72)
    for name in SCREENSHOT:
        if name not in matched:
            continue
        # pick "main" as closest to even money among admiral points lines
        main = min(
            matched[name],
            key=lambda r: abs(vig(r["over_odds"], r["under_odds"]) / 2 - implied(r["over_odds"])),
        )
        plus_map = SCREENSHOT[name]
        # nearest X+ where line = X-0.5
        ss_lines = [(p - 0.5, p, o) for p, o in plus_map.items()]
        nearest_line, nearest_plus, nearest_o = min(ss_lines, key=lambda t: abs(t[0] - main["line"]))
        ao, au = main["over_odds"], main["under_odds"]
        print(
            f"  {name:22} ADM main {main['line']} O{ao}/U{au} "
            f"(fairO={fair_probs(ao,au)[0]*100:.0f}%) | "
            f"nearest SS {nearest_plus}+ / {nearest_line} @ {nearest_o} "
            f"(ip={implied(nearest_o)*100:.0f}%)"
        )


if __name__ == "__main__":
    main()
