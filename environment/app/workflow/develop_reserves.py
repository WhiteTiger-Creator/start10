#!/usr/bin/env python3
"""Claims reserving engine (INCIDENT SNAPSHOT -- DO NOT SHIP).

This is the reserving engine as it stood when the year-end valuation was pulled.
Several stages still evaluate the winter draft proposals and the spring interim
decisions that the reserving committee later reversed, so the ultimates and the
carried reserves it produces are wrong. Restore it to the committee's final
decisions recorded in /app/incident/reserving_governance_log.md.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# Fixed absolute operational-input paths. --input selects the triangle only; the
# period calendar and the reserving policy never become relative to it.
DEFAULT_INPUT = "/app/data/development_triangle.json"
DEFAULT_OUTPUT_DIR = "/app/output"
CALENDAR_PATH = "/app/data/period_calendar.json"
RESERVING_POLICY_PATH = "/app/data/reserving_policy.json"

SCHEMA_VERSION = "reserve-triangle-v1"
TIER_ORDER = ["escalate", "review", "watch"]
BP = 10000

# Draft / interim constants (pre-reversal).
FLAT_TAIL_BP = 10500        # #RSV-4012 draft: one flat tail for every line
ADMITTED_LINES = ("liability", "motor", "property")
LINE_CAP = 3

# Baseline reserving policy (#RSV-4150). Any field the policy file omits keeps
# these values; the policy file may override per default and per line.
POLICY_BASELINE = {
    "admission_min_cents": 4000000,
    "escalate_reserve_min_cents": 30000000,
    "escalate_ibnr_min_cents": 8000000,
    "escalate_cdf_min_bp": 17000,
    "review_reserve_min_cents": 12000000,
    "review_ibnr_min_cents": 3000000,
    "review_unreported_min_bp": 3000,
    "expected_loss_ratio_bp": 6200,
    "max_credibility_bp": 9500,
    "min_cell_movements": 2,
}


def floor_div(numer: int, denom: int) -> int:
    return numer // denom


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
        except ValueError:
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
def column_totals(cells: dict, periods: list[str], lag: int):
    """#RSV-4004 draft: all-year volume-weighted column totals, every cell counted."""
    current_total = 0
    following_total = 0
    for accident in periods:
        current = cells.get((accident, lag))
        following = cells.get((accident, lag + 1))
        if current is None or following is None:
            continue
        current_total += current["cumulative_paid_cents"]
        following_total += following["cumulative_paid_cents"]
    return current_total, following_total


def select_factor_bp(totals: tuple[int, int]) -> int:
    current_total, following_total = totals
    if current_total <= 0:
        return BP
    return floor_div(following_total * BP, current_total)


def tail_factor_bp(last_selected_bp: int) -> int:
    # #RSV-4012 draft: a single flat tail regardless of the observed development.
    return FLAT_TAIL_BP


def cumulative_factor_bp(factors_bp: list[int], from_lag: int, tail_bp: int) -> int:
    cdf = BP
    for lag in range(from_lag, len(factors_bp)):
        cdf = floor_div(cdf * factors_bp[lag], BP)
    return floor_div(cdf * tail_bp, BP)


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

    # #RSV-4016 interim: the case-incurred figure carries the projection.
    ultimate_cl = floor_div(latest_incurred * cdf_bp, BP)
    a_priori = floor_div(earned_premium_cents * policy["expected_loss_ratio_bp"], BP)
    unreported = BP - floor_div(BP * BP, cdf_bp)
    ultimate_bf = latest_incurred + floor_div(a_priori * unreported, BP)
    # #RSV-4020 draft: no credibility blend, the chain ladder stands alone.
    credibility = BP
    ultimate = floor_div(credibility * ultimate_cl + (BP - credibility) * ultimate_bf, BP)
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
        "ibnr_cents": ultimate - latest_paid,
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
)
QUEUE_FIELDS = ("cohort_id", "line", *DEVELOPMENT_FIELDS, "tier")


def develop_line(line: str, per_line: dict, calendar: dict, policy: dict) -> list[dict]:
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
        select_factor_bp(column_totals(cells, periods, lag))
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
    return rows


def run(input_path: str, output_dir: str) -> None:
    triangle = json.loads(Path(input_path).read_text(encoding="utf-8"))
    calendar = json.loads(Path(CALENDAR_PATH).read_text(encoding="utf-8"))
    policy_data = json.loads(Path(RESERVING_POLICY_PATH).read_text(encoding="utf-8"))

    all_rows: list[dict] = []
    for line in sorted(triangle):
        policy = resolve_policy(line, policy_data)
        all_rows.extend(develop_line(line, triangle[line], calendar, policy))

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
