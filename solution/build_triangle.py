#!/usr/bin/env python3
"""Rebuild the cumulative development triangle from the incremental claim movements.

Implements the reserving committee's final aggregation decision (#RSV-4180 in
/app/incident/reserving_governance_log.md), which supersedes the #RSV-4002 draft
and revises the #RSV-4020 interim: a movement lands in the cell of the accident
period it belongs to at the development lag implied by its booking period, the
paid figure nets signed recoveries, case reserve movements move the incurred
figure only, cumulative cells carry the prior lag forward when a lag has no
movement, and the result is written to /app/data/development_triangle.json.
"""

from __future__ import annotations

import json
from pathlib import Path

MOVEMENTS_PATH = Path("/app/data/claim_movements.json")
CALENDAR_PATH = Path("/app/data/period_calendar.json")
TRIANGLE_PATH = Path("/app/data/development_triangle.json")

PAID_TYPES = ("payment", "alae_expense", "salvage_recovery", "subrogation_recovery")
INCURRED_ONLY_TYPES = ("case_reserve_change",)


def coerce_int(value: object) -> int:
    # report_spec.json states the conversion as int(str(value).strip()), so a
    # boolean goes through str() like anything else: "True" is not a number and
    # falls to the zero the contract names, rather than to the 1 it is worth in
    # Python arithmetic.
    if isinstance(value, int) and not isinstance(value, bool):
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


def canon_name(value: object) -> str:
    text = str(value).strip().lower()
    return text if text else "unknown"


def deduplicate(movements: list[dict]) -> list[dict]:
    """#RSV-4180: one row per movement_id, the highest posted_seq, then latest filed."""
    best: dict[str, tuple[int, int]] = {}
    for index, movement in enumerate(movements):
        movement_id = str(movement.get("movement_id", "")).strip()
        key = (coerce_int(movement.get("posted_seq", 0)), index)
        if movement_id not in best or key > best[movement_id]:
            best[movement_id] = key
    keep = {index for _, index in best.values()}
    return [m for i, m in enumerate(movements) if i in keep]


def claim_accident_periods(posted: list[dict], position: dict[str, int]) -> dict[str, str]:
    """#RSV-4180: a blank accident period is taken from the claim's other movements."""
    known: dict[str, str] = {}
    for movement in posted:
        accident = str(movement.get("accident_period", "")).strip()
        if accident not in position:
            continue
        claim = str(movement.get("claim_ref", "")).strip()
        if claim not in known or position[accident] < position[known[claim]]:
            known[claim] = accident
    return known


def build(movements: list[dict], calendar: dict) -> dict:
    periods: list[str] = list(calendar["periods"])
    position = {name: i for i, name in enumerate(periods)}
    lines = sorted({canon_name(line) for line in calendar["lines"]})
    horizon = position[calendar["valuation_period"]] + 1

    paid: dict[tuple[str, str, int], int] = {}
    incurred: dict[tuple[str, str, int], int] = {}
    counts: dict[tuple[str, str, int], int] = {}

    posted = [
        movement
        for movement in deduplicate(movements)
        if canon_name(movement.get("status", "posted")) == "posted"
    ]
    claim_accident = claim_accident_periods(posted, position)

    for movement in posted:
        line = canon_name(movement.get("line", ""))
        accident = str(movement.get("accident_period", "")).strip()
        booking = str(movement.get("booking_period", "")).strip()
        if accident not in position:
            accident = claim_accident.get(str(movement.get("claim_ref", "")).strip(), "")
        if line not in lines or accident not in position or booking not in position:
            continue
        lag = position[booking] - position[accident]
        if lag < 0 or position[accident] + lag >= horizon:
            continue
        kind = canon_name(movement.get("movement_type", ""))
        amount = coerce_int(movement.get("amount_cents", 0))
        cell = (line, accident, lag)
        if kind in PAID_TYPES:
            paid[cell] = paid.get(cell, 0) + amount
            incurred[cell] = incurred.get(cell, 0) + amount
        elif kind in INCURRED_ONLY_TYPES:
            incurred[cell] = incurred.get(cell, 0) + amount
        else:
            continue
        counts[cell] = counts.get(cell, 0) + 1

    triangle: dict[str, dict[str, list[dict]]] = {}
    for line in lines:
        per_line: dict[str, list[dict]] = {}
        for accident in periods[:horizon]:
            rows = []
            cumulative_paid = 0
            cumulative_incurred = 0
            for lag in range(horizon - position[accident]):
                cell = (line, accident, lag)
                cumulative_paid += paid.get(cell, 0)
                cumulative_incurred += incurred.get(cell, 0)
                rows.append(
                    {
                        "dev_lag": lag,
                        "cumulative_paid_cents": cumulative_paid,
                        "cumulative_incurred_cents": cumulative_incurred,
                        "movement_count": counts.get(cell, 0),
                    }
                )
            per_line[accident] = rows
        triangle[line] = {name: per_line[name] for name in sorted(per_line)}
    return {line: triangle[line] for line in sorted(triangle)}


def main() -> None:
    movements = json.loads(MOVEMENTS_PATH.read_text(encoding="utf-8"))
    calendar = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    triangle = build(movements, calendar)
    TRIANGLE_PATH.write_text(json.dumps(triangle, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
