#!/usr/bin/env python3
"""Claims reserving engine: chain-ladder development over a loss triangle.

Projects ultimate losses and carried reserves from the cumulative development
triangle rebuilt by the aggregation step. Every selection rule here is the
reserving committee's own convention and is reconstructed from
/app/incident/reserving_governance_log.md, the operational data, and
/app/docs/report_spec.json (output contract only).

All money is integer minor units and every ratio is an exact rational scaled to
basis points, so the projection is bit-for-bit reproducible; the rounding
direction of each stage is fixed independently by its governing decision.
"""

from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path

# Fixed absolute operational-input paths. --input selects the triangle only; the
# period calendar and the reserving policy never become relative to it.
DEFAULT_INPUT = "/app/data/development_triangle.json"
DEFAULT_OUTPUT_DIR = "/app/output"
CALENDAR_PATH = "/app/data/period_calendar.json"
RESERVING_POLICY_PATH = "/app/data/reserving_policy.json"
MOVEMENTS_PATH = "/app/data/claim_movements.json"

SCHEMA_VERSION = "reserve-triangle-v1"
TIER_ORDER = ["escalate", "review", "watch"]
BP = 10000

# --- Governance constants (final decisions; see log entries in comments) ---
SELECTION_WINDOW = 3        # #RSV-4104: latest 3 surviving individual ratios
TRIM_THRESHOLD = 5          # #RSV-4104: drop high+low once 5 ratios are available
TAIL_SHARE_DIV = 2          # #RSV-4112: half of the final observed development
ADMITTED_LINES = ("liability", "motor", "property")   # #RSV-4140
LINE_CAP = 18                # #RSV-4146: at most 18 queue rows per line, after ordering

# Baseline reserving policy (#RSV-4150). Any field the policy file omits keeps
# these values; the policy file may override per default and per line.
POLICY_BASELINE = {
    "admission_min_cents": 4500000,
    "escalate_reserve_min_cents": 2800000000,
    "escalate_ibnr_min_cents": 8000000,
    "escalate_cdf_min_bp": 120000,
    "review_reserve_min_cents": 180000000,
    "review_ibnr_min_cents": 1,
    "review_unreported_min_bp": 2500,
    "expected_loss_ratio_bp": 6200,
    "max_credibility_bp": 9500,
    "min_cell_movements": 2,
}


def floor_div(numer: int, denom: int) -> int:
    return numer // denom


def ceil_div(numer: int, denom: int) -> int:
    """Integer ceil; ceil(x/n) == -(-x // n)."""
    return -(-numer // denom)


def half_up_div(numer: int, denom: int) -> int:
    """Integer round-half-up; ties resolve upward (toward positive infinity)."""
    return (2 * numer + denom) // (2 * denom)


def canon_name(value: object) -> str:
    text = str(value).strip().lower()
    return text if text else "unknown"


def coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except (ValueError, OverflowError):
            # The contract's fallback is 0 whenever both conversions fail, and the
            # second one fails two ways: "abc" raises ValueError, while "1e999" and
            # "inf" parse as floats and raise OverflowError on int(). Catching only
            # ValueError turned a documented fallback into a crash.
            return 0


# --------------------------------------------------------------------------
# Policy resolution (#RSV-4150, #RSV-4152)
# --------------------------------------------------------------------------
def resolve_policy(line: str, policy_data: dict) -> dict:
    resolved = dict(POLICY_BASELINE)
    for field, value in policy_data.get("default", {}).items():
        if field in resolved:
            resolved[field] = coerce_int(value)
    override = policy_data.get("line_overrides", {}).get(line)
    if isinstance(override, dict):
        for field, value in override.items():
            if field in resolved:
                resolved[field] = coerce_int(value)
    return resolved


# --------------------------------------------------------------------------
# Stage 1: individual age-to-age ratios (#RSV-4102, #RSV-4106)
# --------------------------------------------------------------------------
def individual_ratios(cells: dict, periods: list[str], lag: int, min_cell_movements: int):
    """Exact per-accident-period ratios out of `lag`, oldest accident period first."""
    ratios = []
    for order, accident in enumerate(periods):
        current = cells.get((accident, lag))
        following = cells.get((accident, lag + 1))
        if current is None or following is None:
            continue
        if current["cumulative_paid_cents"] <= 0:
            continue
        if current["movement_count"] < min_cell_movements:
            continue
        ratios.append(
            (order, Fraction(following["cumulative_paid_cents"], current["cumulative_paid_cents"]))
        )
    return ratios


def select_factor_bp(ratios: list[tuple[int, Fraction]]) -> int:
    """#RSV-4104: trim the extremes once 5 are available, keep the latest 3, average."""
    if not ratios:
        return BP
    surviving = list(ratios)
    if len(surviving) >= TRIM_THRESHOLD:
        highest = max(surviving, key=lambda item: (item[1], -item[0]))
        surviving.remove(highest)
        lowest = min(surviving, key=lambda item: (item[1], item[0]))
        surviving.remove(lowest)
    surviving.sort(key=lambda item: item[0])
    kept = surviving[-SELECTION_WINDOW:]
    total = sum((ratio for _, ratio in kept), Fraction(0))
    mean = total / len(kept)
    return half_up_div(mean.numerator * BP, mean.denominator)


def tail_factor_bp(last_selected_bp: int) -> int:
    """#RSV-4112: carry half of the final observed development beyond the triangle."""
    tail = BP + floor_div(last_selected_bp - BP, TAIL_SHARE_DIV)
    return max(tail, BP)


def cumulative_factor_bp(factors_bp: list[int], from_lag: int, tail_bp: int) -> int:
    """#RSV-4114: chain the remaining selected factors, then the tail, rounding up
    at every step, in increasing development order."""
    cdf = BP
    for lag in range(from_lag, len(factors_bp)):
        cdf = ceil_div(cdf * factors_bp[lag], BP)
    return ceil_div(cdf * tail_bp, BP)


# --------------------------------------------------------------------------
# Stage 2: per accident period projection (#RSV-4116 .. #RSV-4132)
# --------------------------------------------------------------------------
def project_period(
    line: str,
    accident: str,
    latest: dict,
    latest_lag: int,
    cdf_bp: int,
    tail_bp: int,
    selected_bp: int,
    earned_premium_cents: int,
    policy: dict,
) -> dict:
    latest_paid = latest["cumulative_paid_cents"]
    latest_incurred = latest["cumulative_incurred_cents"]

    ultimate_cl = floor_div(latest_paid * cdf_bp, BP)
    a_priori = floor_div(earned_premium_cents * policy["expected_loss_ratio_bp"], BP)
    unreported = BP - floor_div(BP * BP, cdf_bp)
    unreported = min(max(unreported, 0), BP)
    ultimate_bf = latest_paid + ceil_div(a_priori * unreported, BP)
    if ultimate_cl <= 0:
        credibility = 0
    else:
        credibility = min(floor_div(latest_paid * BP, ultimate_cl), policy["max_credibility_bp"])
        credibility = max(credibility, 0)
    ultimate = half_up_div(credibility * ultimate_cl + (BP - credibility) * ultimate_bf, BP)
    return {
        "line": line,
        "accident_period": accident,
        "latest_dev_lag": latest_lag,
        "latest_cumulative_paid_cents": latest_paid,
        "latest_cumulative_incurred_cents": latest_incurred,
        "movement_count": latest["movement_count"],
        "selected_factor_bp": selected_bp,
        "tail_factor_bp": tail_bp,
        "cdf_bp": cdf_bp,
        "earned_premium_cents": earned_premium_cents,
        "a_priori_loss_cents": a_priori,
        "pct_unreported_bp": unreported,
        "ultimate_chain_ladder_cents": ultimate_cl,
        "ultimate_bornhuetter_ferguson_cents": ultimate_bf,
        "credibility_bp": credibility,
        "ultimate_blended_cents": ultimate,
        "reserve_cents": ultimate - latest_paid,
        "ibnr_cents": max(ultimate - latest_incurred, 0),
    }


def assign_tier(row: dict, policy: dict) -> str:
    if (
        row["reserve_cents"] >= policy["escalate_reserve_min_cents"]
        or row["ibnr_cents"] >= policy["escalate_ibnr_min_cents"]
        or row["cdf_bp"] >= policy["escalate_cdf_min_bp"]
    ):
        return "escalate"
    if (
        row["reserve_cents"] >= policy["review_reserve_min_cents"]
        or row["ibnr_cents"] >= policy["review_ibnr_min_cents"]
        or row["pct_unreported_bp"] >= policy["review_unreported_min_bp"]
    ):
        return "review"
    return "watch"


DEVELOPMENT_FIELDS = (
    "accident_period",
    "latest_dev_lag",
    "latest_cumulative_paid_cents",
    "latest_cumulative_incurred_cents",
    "movement_count",
    "selected_factor_bp",
    "tail_factor_bp",
    "cdf_bp",
    "earned_premium_cents",
    "a_priori_loss_cents",
    "pct_unreported_bp",
    "ultimate_chain_ladder_cents",
    "ultimate_bornhuetter_ferguson_cents",
    "credibility_bp",
    "ultimate_blended_cents",
    "reserve_cents",
    "ibnr_cents",
    "claim_count",
    "largest_claim_share_bp",
)
QUEUE_FIELDS = ("cohort_id", "line", *DEVELOPMENT_FIELDS, "tier")


def claim_attribution(movements: list[dict], periods: list[str]) -> dict[tuple, dict]:
    """#RSV-4182: per cell, how many distinct claims contribute and what share the
    largest of them holds.

    Every movement is bucketed once into its cell and claim, so the whole book
    costs a single pass. Re-scanning the movement file for each cell is the cell
    count times the movement count and cannot meet the runtime budget.
    """
    position = {name: i for i, name in enumerate(periods)}
    per_cell: dict[tuple, dict[str, int]] = {}
    for row in movements:
        if canon_name(row.get("status", "posted")) == "void":
            continue
        line = canon_name(row.get("line", ""))
        accident = str(row.get("accident_period", "")).strip()
        booking = str(row.get("booking_period", "")).strip()
        if accident not in position or booking not in position:
            continue
        lag = position[booking] - position[accident]
        if lag < 0:
            continue
        claim = str(row.get("claim_ref", "")).strip()
        bucket = per_cell.setdefault((line, accident, lag), {})
        bucket[claim] = bucket.get(claim, 0) + abs(coerce_int(row.get("amount_cents", 0)))
    out: dict[tuple, dict] = {}
    for key, claims in per_cell.items():
        total = sum(claims.values())
        largest = max(claims.values()) if claims else 0
        out[key] = {
            "claim_count": len(claims),
            "largest_claim_share_bp": (largest * BP // total) if total else 0,
        }
    return out


def develop_line(line: str, per_line: dict, calendar: dict, policy: dict,
                 attribution: dict[tuple, dict] | None = None) -> list[dict]:
    periods: list[str] = list(calendar["periods"])
    position = {name: i for i, name in enumerate(periods)}
    horizon = position[calendar["valuation_period"]] + 1
    premiums = calendar.get("earned_premium_cents", {}).get(line, {})

    cells: dict[tuple[str, int], dict] = {}
    for accident, rows in per_line.items():
        for row in rows:
            cells[(accident, coerce_int(row["dev_lag"]))] = {
                "cumulative_paid_cents": coerce_int(row["cumulative_paid_cents"]),
                "cumulative_incurred_cents": coerce_int(row["cumulative_incurred_cents"]),
                "movement_count": coerce_int(row["movement_count"]),
            }

    factors_bp = [
        select_factor_bp(individual_ratios(cells, periods, lag, policy["min_cell_movements"]))
        for lag in range(horizon - 1)
    ]
    tail_bp = tail_factor_bp(factors_bp[-1] if factors_bp else BP)

    rows = []
    for accident in sorted(per_line):
        lags = sorted(coerce_int(row["dev_lag"]) for row in per_line[accident])
        if not lags:
            continue
        latest_lag = lags[-1]
        selected_bp = factors_bp[latest_lag] if latest_lag < len(factors_bp) else tail_bp
        cell_key = (line, accident, latest_lag)
        credit = (attribution or {}).get(cell_key, {"claim_count": 0, "largest_claim_share_bp": 0})
        rows.append(
            project_period(
                line,
                accident,
                cells[(accident, latest_lag)],
                latest_lag,
                cumulative_factor_bp(factors_bp, latest_lag, tail_bp),
                tail_bp,
                selected_bp,
                coerce_int(premiums.get(accident, 0)),
                policy,
            )
        )
        rows[-1]["claim_count"] = credit["claim_count"]
        rows[-1]["largest_claim_share_bp"] = credit["largest_claim_share_bp"]
    return rows


def run(input_path: str, output_dir: str) -> None:
    triangle = json.loads(Path(input_path).read_text(encoding="utf-8"))
    calendar = json.loads(Path(CALENDAR_PATH).read_text(encoding="utf-8"))
    policy_data = json.loads(Path(RESERVING_POLICY_PATH).read_text(encoding="utf-8"))
    movements = json.loads(Path(MOVEMENTS_PATH).read_text(encoding="utf-8"))
    attribution = claim_attribution(movements, list(calendar["periods"]))

    all_rows: list[dict] = []
    for line in sorted(triangle):
        policy = resolve_policy(line, policy_data)
        all_rows.extend(develop_line(line, triangle[line], calendar, policy, attribution))

    queue_rows: list[dict] = []
    for row in all_rows:
        policy = resolve_policy(row["line"], policy_data)
        if row["line"] not in ADMITTED_LINES:
            continue
        if row["reserve_cents"] < policy["admission_min_cents"]:
            continue
        entry = dict(row)
        entry["tier"] = assign_tier(row, policy)
        entry["cohort_id"] = f"{row['line']}:{row['accident_period']}@{row['latest_dev_lag']}"
        queue_rows.append(entry)

    tier_rank = {name: len(TIER_ORDER) - i for i, name in enumerate(TIER_ORDER)}
    queue_rows.sort(
        key=lambda row: (
            -tier_rank[row["tier"]],
            -row["reserve_cents"],
            -row["ibnr_cents"],
            -row["ultimate_blended_cents"],
            -row["cdf_bp"],
            -row["latest_cumulative_paid_cents"],
            row["line"],
            row["accident_period"],
        )
    )
    seen: dict[str, int] = {}
    capped: list[dict] = []
    for row in queue_rows:
        taken = seen.get(row["line"], 0)
        if taken < LINE_CAP:
            capped.append(row)
            seen[row["line"]] = taken + 1
    queue_rows = capped

    tier_counts = {tier: 0 for tier in TIER_ORDER}
    for row in queue_rows:
        tier_counts[row["tier"]] += 1

    def queue_max(field: str) -> int:
        return max((row[field] for row in queue_rows), default=0)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "valuation_period": str(calendar["valuation_period"]),
        "line_count": len({row["line"] for row in all_rows}),
        "cohort_count": len(all_rows),
        "triangle_cell_count": sum(
            len(rows) for per_line in triangle.values() for rows in per_line.values()
        ),
        "triangle_movement_count": sum(
            coerce_int(cell["movement_count"])
            for per_line in triangle.values()
            for rows in per_line.values()
            for cell in rows
        ),
        "tier_counts": tier_counts,
        "total_latest_paid_cents": sum(r["latest_cumulative_paid_cents"] for r in all_rows),
        "total_latest_incurred_cents": sum(r["latest_cumulative_incurred_cents"] for r in all_rows),
        "total_ultimate_cents": sum(r["ultimate_blended_cents"] for r in all_rows),
        "total_reserve_cents": sum(r["reserve_cents"] for r in all_rows),
        "total_ibnr_cents": sum(r["ibnr_cents"] for r in all_rows),
        "largest_cdf_bp": max((r["cdf_bp"] for r in all_rows), default=0),
        "queued_cohort_count": len(queue_rows),
        "max_reserve_cents": queue_max("reserve_cents"),
        "max_ibnr_cents": queue_max("ibnr_cents"),
        "max_largest_claim_share_bp": max((r["largest_claim_share_bp"] for r in all_rows), default=0),
        "max_credibility_bp": queue_max("credibility_bp"),
    }

    by_line: dict[str, list[dict]] = {}
    for row in all_rows:
        by_line.setdefault(row["line"], []).append(row)
    development = {
        line: [
            {field: row[field] for field in DEVELOPMENT_FIELDS}
            for row in sorted(by_line[line], key=lambda r: r["accident_period"])
        ]
        for line in sorted(by_line)
    }
    out_queue = [{field: row[field] for field in QUEUE_FIELDS} for row in queue_rows]

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (out / "line_development.json").write_text(
        json.dumps(development, indent=2) + "\n", encoding="utf-8"
    )
    with (out / "reserve_queue.jsonl").open("w", encoding="utf-8") as handle:
        for row in out_queue:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Claims reserving development engine")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    run(args.input, args.output_dir)


if __name__ == "__main__":
    main()
