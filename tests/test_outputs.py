"""Verifier tests for the claims-reserving development task."""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

import pytest

WORKFLOW_PATH = Path("/app/workflow/develop_reserves.py")
ORIGINAL_WORKFLOW_PATH = Path("/app/workflow/.develop_reserves.original")
DEFAULT_INPUT = Path("/app/data/development_triangle.json")
TRIANGLE_PATH = DEFAULT_INPUT   # the rebuilt triangle the engine reads
MOVEMENTS_PATH = Path("/app/data/claim_movements.json")
CALENDAR_PATH = Path("/app/data/period_calendar.json")
# The shipped partial triangle is overwritten in place by the rebuild, so the
# verifier keeps its own copy to prove the engine depends on that step.
SHIPPED_TRIANGLE_REFERENCE_PATH = Path("/tests/fixtures/shipped_triangle.json")
POLICY_PATH = Path("/app/data/reserving_policy.json")
SPEC_PATH = Path("/app/docs/report_spec.json")
# The contract is golden metadata: the verifier reads it from its own image,
# never from the agent-writable copy under /app.
GOLDEN_CONTRACT_PATH = Path("/tests/fixtures/contract_golden.json")
LOG_PATH = Path("/app/incident/reserving_governance_log.md")
EXPECTED_FIXTURE = Path("/tests/fixtures/expected_report.json")
ALT_INPUT = Path("/tests/fixtures/alt_triangle.json")

TIER_ORDER = ["escalate", "review", "watch"]
TIER_RANK = {name: len(TIER_ORDER) - idx for idx, name in enumerate(TIER_ORDER)}
ADMITTED_LINES = {"liability", "motor", "property"}
BP = 10000

FIXTURE = json.loads(EXPECTED_FIXTURE.read_text())
SPEC = json.loads(GOLDEN_CONTRACT_PATH.read_text())

POLICY_FIELDS = (
    "admission_min_cents", "escalate_reserve_min_cents", "escalate_ibnr_min_cents",
    "escalate_cdf_min_bp", "review_reserve_min_cents", "review_ibnr_min_cents",
    "review_unreported_min_bp", "expected_loss_ratio_bp", "max_credibility_bp",
    "min_cell_movements",
)
# Policy baseline exactly as fixed by the final #RSV-4150 decision. Any field the
# shipped policy omits falls back to these, so they must match the governance log
# rather than the pre-repair engine's constants.
BASELINE = {
    "admission_min_cents": 4500000, "escalate_reserve_min_cents": 2800000000,
    "escalate_ibnr_min_cents": 8000000, "escalate_cdf_min_bp": 120000,
    "review_reserve_min_cents": 180000000, "review_ibnr_min_cents": 1,
    "review_unreported_min_bp": 2500, "expected_loss_ratio_bp": 6200,
    "max_credibility_bp": 9500, "min_cell_movements": 2,
}

CELL_KEYS = set(SPEC["triangle_source"]["required_fields"])
DEVELOPMENT_KEYS = set(SPEC["line_development_json"]["required_fields"])
QUEUE_KEYS = set(SPEC["reserve_queue"]["required_fields"])
SUMMARY_KEYS = set(SPEC["summary_json"]["required_fields"])


def _variant_triangles() -> dict:
    """Perturbations of the rebuilt triangle, not reimplementations of it: the
    latest diagonal zeroed, incurred replaced by paid, and every cell shifted one
    lag earlier. Each must develop to a different answer, proving the engine
    reads the triangle's content rather than its shape."""
    base = _load_json(TRIANGLE_PATH)

    def zeroed(triangle):
        out = {}
        for line, cells in triangle.items():
            rows = {}
            for accident, entries in cells.items():
                keep = sorted(entries, key=lambda r: int(r["dev_lag"]))[:-1]
                rows[accident] = keep or entries
            out[line] = rows
        return out

    def paid_as_incurred(triangle):
        out = {}
        for line, cells in triangle.items():
            out[line] = {
                accident: [dict(r, cumulative_incurred_cents=r["cumulative_paid_cents"])
                           for r in entries]
                for accident, entries in cells.items()
            }
        return out

    def shifted(triangle):
        out = {}
        for line, cells in triangle.items():
            out[line] = {
                accident: [dict(r, dev_lag=max(int(r["dev_lag"]) - 1, 0)) for r in entries]
                for accident, entries in cells.items()
            }
        return out

    return {
        "latest_diagonal_dropped": zeroed(base),
        "incurred_replaced_by_paid": paid_as_incurred(base),
        "lags_shifted": shifted(base),
    }


def _digest(value: object) -> str:
    """Content digest of a whole artifact; the graded movement file and triangle
    are far too large to embed in a fixture, so equality is asserted over their
    digests."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# Documented wall-clock budget for one full run on the graded book.
# instruction.md and report_spec.json state the same number. The reference
# buckets every movement into its cell and claim in a single pass; re-scanning
# the movement file per cell is the cell count times the movement count.
RUNTIME_BUDGET_SEC = 120.0
# Wall-clock of each graded run, keyed by the input it was given, so the budget
# stated in instruction.md and report_spec.json is actually enforced below.
_ELAPSED: dict[str, float] = {}


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


# --- verifier execution isolation -------------------------------------------------
# The submitted /app/workflow/develop_reserves.py is untrusted once the separate verifier runs
# it. We execute it under an unprivileged UID (65534 / nobody) via setpriv, so it cannot write
# the reward path, read the held-out fixtures under /tests, or interfere with the verifier.
# Inputs are staged into a candidate-writable work area; the calendar and the reserving policy
# keep their fixed paths under /app.
_CWORK = Path("/candidate-work")
_run_ctr = itertools.count()
_SETPRIV = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--no-new-privs"]

# The submitted program gets a minimal explicit environment rather than inheriting the
# verifier's (PATH/PYTHONPATH/CI variables and any other grader context).
_CANDIDATE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/candidate-work", "LANG": "C.UTF-8"}
_CANDIDATE_TIMEOUT = 300


def _candidate_dir() -> Path:
    directory = _CWORK / f"run-{next(_run_ctr)}"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o777)
    return directory


def _run_agent(argv, cwd: Path):
    """Run the submitted program under the unprivileged candidate UID with a scrubbed env."""
    return subprocess.run(
        _SETPRIV + argv, check=True, capture_output=True, text=True, cwd=str(cwd),
        env=dict(_CANDIDATE_ENV), timeout=_CANDIDATE_TIMEOUT,
    )


def _run_pipeline(script_path: Path = WORKFLOW_PATH, input_path: Path = DEFAULT_INPUT):
    work = _candidate_dir()
    out_dir = work / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o777)
    staged_input = work / "triangle.json"
    shutil.copy(str(input_path), str(staged_input))
    os.chmod(staged_input, 0o644)
    started = time.monotonic()
    result = _run_agent(
        [sys.executable, str(script_path), "--input", str(staged_input), "--output-dir", str(out_dir)],
        cwd=work,
    )
    _ELAPSED[str(input_path)] = time.monotonic() - started
    assert result.returncode == 0
    summary = _load_json(out_dir / "summary.json")
    development = _load_json(out_dir / "line_development.json")
    queue = _load_jsonl(out_dir / "reserve_queue.jsonl")
    return out_dir, summary, development, queue


def _run_on_triangle(tmp_path: Path, label: str, triangle: dict):
    staged = tmp_path / f"{label}.json"
    _write_json(staged, triangle)
    return _run_pipeline(input_path=staged)


@pytest.fixture(scope="session")
def primary_outputs():
    return _run_pipeline()


# --------------------------------------------------------------------------
# Step 1: the cumulative development triangle must be rebuilt in place
# --------------------------------------------------------------------------
def test_aggregation_sources_are_intact():
    """Verifies that aggregation sources are intact."""
    assert _digest(_load_json(MOVEMENTS_PATH)) == FIXTURE["movements_digest"]
    assert _digest(_load_json(CALENDAR_PATH)) == FIXTURE["calendar_digest"]


def test_development_triangle_rebuilt():
    """/app/data/development_triangle.json shipped with one column; it must hold the rebuild."""
    rebuilt = _load_json(DEFAULT_INPUT)
    assert isinstance(rebuilt, dict)
    assert _digest(rebuilt) == FIXTURE["expected_triangle_digest"]


def test_triangle_cells_carry_only_the_declared_fields():
    """Verifies that triangle cells carry only the declared fields."""
    triangle = _load_json(DEFAULT_INPUT)
    assert list(triangle) == sorted(triangle)
    for per_line in triangle.values():
        assert list(per_line) == sorted(per_line)
        for rows in per_line.values():
            assert [row["dev_lag"] for row in rows] == sorted(row["dev_lag"] for row in rows)
            for row in rows:
                assert set(row) == CELL_KEYS


def test_empty_development_lags_carry_the_previous_cumulative_forward():
    """Verifies that empty development lags carry the previous cumulative forward."""
    triangle = _load_json(DEFAULT_INPUT)
    carried = 0
    for per_line in triangle.values():
        for rows in per_line.values():
            for previous, row in itertools.pairwise(rows):
                if row["movement_count"] == 0:
                    assert row["cumulative_paid_cents"] == previous["cumulative_paid_cents"]
                    assert row["cumulative_incurred_cents"] == previous["cumulative_incurred_cents"]
                    carried += 1
    assert carried, "the extract must exercise a development lag with no movement of its own"


def test_shipped_and_wrongly_aggregated_triangles_differ_from_the_rebuild():
    """The aggregation is real work: the shipped column and each wrong netting differ."""
    expected = FIXTURE["expected_triangle_digest"]
    assert _digest(_load_json(SHIPPED_TRIANGLE_REFERENCE_PATH)) != expected
    for label, triangle in _variant_triangles().items():
        assert _digest(triangle) != expected, label


def test_engine_output_depends_on_the_rebuilt_triangle(tmp_path: Path):
    """Even a correctly repaired engine emits wrong artifacts on a wrongly built triangle."""
    variants = dict(_variant_triangles())
    variants["shipped_empty"] = _load_json(SHIPPED_TRIANGLE_REFERENCE_PATH)
    for label, triangle in variants.items():
        _, summary, development, queue = _run_on_triangle(tmp_path, label, triangle)
        assert summary != FIXTURE["primary"]["summary"], label
        assert _digest(development) != FIXTURE["primary"]["development_digest"], label
        assert _digest(queue) != FIXTURE["primary"]["queue_digest"], label


# --------------------------------------------------------------------------
# Step 2: the engine output contract
# --------------------------------------------------------------------------
def test_cli_exists():
    """Verifies that cli exists."""
    assert WORKFLOW_PATH.exists()


def test_output_dir_contains_exactly_three_files(primary_outputs):
    """Verifies that output dir contains exactly three files."""
    out_dir, _, _, _ = primary_outputs
    names = sorted(path.name for path in out_dir.iterdir() if path.is_file())
    assert names == ["line_development.json", "reserve_queue.jsonl", "summary.json"]


def test_primary_summary_matches_fixture(primary_outputs):
    """Verifies that primary summary matches fixture."""
    _, summary, _, _ = primary_outputs
    assert summary == FIXTURE["primary"]["summary"]


def test_primary_development_matches_fixture(primary_outputs):
    """Verifies that primary development matches fixture."""
    _, _, development, _ = primary_outputs
    assert _digest(development) == FIXTURE["primary"]["development_digest"]


def test_primary_queue_matches_fixture(primary_outputs):
    """Verifies that primary queue matches fixture."""
    _, _, _, queue = primary_outputs
    assert _digest(queue) == FIXTURE["primary"]["queue_digest"]


def test_summary_schema(primary_outputs):
    """Verifies that summary schema."""
    _, summary, _, _ = primary_outputs
    assert set(summary) == SUMMARY_KEYS
    assert summary["schema_version"] == "reserve-triangle-v1"
    assert list(summary["tier_counts"]) == TIER_ORDER
    assert summary["valuation_period"] == _load_json(CALENDAR_PATH)["valuation_period"]


def test_development_schema_and_sorting(primary_outputs):
    """Verifies that development schema and sorting."""
    _, _, development, _ = primary_outputs
    assert list(development) == sorted(development)
    for rows in development.values():
        periods = [row["accident_period"] for row in rows]
        assert periods == sorted(periods)
        for row in rows:
            assert set(row) == DEVELOPMENT_KEYS
            assert row["tail_factor_bp"] >= BP
            assert 0 <= row["pct_unreported_bp"] <= BP
            assert 0 <= row["credibility_bp"] <= BASELINE["max_credibility_bp"]
            assert row["ibnr_cents"] >= 0
            assert row["reserve_cents"] == (
                row["ultimate_blended_cents"] - row["latest_cumulative_paid_cents"]
            )


def test_queue_required_fields(primary_outputs):
    """Verifies that queue required fields."""
    _, _, _, queue = primary_outputs
    for row in queue:
        assert set(row) == QUEUE_KEYS
        assert row["tier"] in TIER_RANK
        assert row["line"] in ADMITTED_LINES
        assert row["cohort_id"] == f"{row['line']}:{row['accident_period']}@{row['latest_dev_lag']}"


def test_queue_sorted(primary_outputs):
    """Verifies that queue sorted."""
    _, _, _, queue = primary_outputs
    assert queue == sorted(
        queue,
        key=lambda row: (
            -TIER_RANK[row["tier"]],
            -row["reserve_cents"],
            -row["ibnr_cents"],
            -row["ultimate_blended_cents"],
            -row["cdf_bp"],
            -row["latest_cumulative_paid_cents"],
            row["line"],
            row["accident_period"],
        ),
    )


def test_reserve_queue_jsonl_compact(primary_outputs):
    """Verifies that reserve queue jsonl compact."""
    out_dir, _, _, _ = primary_outputs
    for line in (out_dir / "reserve_queue.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        assert ": " not in line
        assert json.dumps(json.loads(line), separators=(",", ":")) == line


def test_summary_math_consistency(primary_outputs):
    """Verifies that summary math consistency."""
    _, summary, development, queue = primary_outputs
    rows = [row for line_rows in development.values() for row in line_rows]
    assert summary["cohort_count"] == len(rows)
    assert summary["line_count"] == len(development)
    assert summary["total_latest_paid_cents"] == sum(r["latest_cumulative_paid_cents"] for r in rows)
    assert summary["total_latest_incurred_cents"] == sum(
        r["latest_cumulative_incurred_cents"] for r in rows
    )
    assert summary["total_ultimate_cents"] == sum(r["ultimate_blended_cents"] for r in rows)
    assert summary["total_reserve_cents"] == sum(r["reserve_cents"] for r in rows)
    assert summary["total_ibnr_cents"] == sum(r["ibnr_cents"] for r in rows)
    assert summary["largest_cdf_bp"] == max((r["cdf_bp"] for r in rows), default=0)
    assert summary["queued_cohort_count"] == len(queue)
    for field in ("reserve_cents", "ibnr_cents", "credibility_bp"):
        assert summary["max_" + field] == max((row[field] for row in queue), default=0)


def test_summary_counts_track_the_rebuilt_triangle(primary_outputs):
    """Verifies that summary counts track the rebuilt triangle."""
    _, summary, _, _ = primary_outputs
    triangle = _load_json(DEFAULT_INPUT)
    cells = [cell for per_line in triangle.values() for rows in per_line.values() for cell in rows]
    assert summary["triangle_cell_count"] == len(cells)
    assert summary["triangle_movement_count"] == sum(cell["movement_count"] for cell in cells)


def test_tier_counts_enumerate_all_three(primary_outputs):
    """Verifies that tier counts enumerate all three."""
    _, summary, _, queue = primary_outputs
    counts = {tier: 0 for tier in TIER_ORDER}
    for row in queue:
        counts[row["tier"]] += 1
    assert summary["tier_counts"] == counts
    assert set(summary["tier_counts"]) == set(TIER_ORDER)
    assert all(counts[tier] > 0 for tier in TIER_ORDER), "the valuation must exercise every tier"


# --------------------------------------------------------------------------
# Original / broken snapshot
# --------------------------------------------------------------------------
def test_original_snapshot_preserved():
    """Verifies that original snapshot preserved."""
    assert ORIGINAL_WORKFLOW_PATH.exists()
    digest = hashlib.sha256(ORIGINAL_WORKFLOW_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE["broken_engine_sha256"]


def test_broken_snapshot_is_wrong():
    """Verifies that broken snapshot is wrong."""
    _, summary, development, queue = _run_pipeline(script_path=ORIGINAL_WORKFLOW_PATH)
    assert summary != FIXTURE["primary"]["summary"]
    assert _digest(development) != FIXTURE["primary"]["development_digest"]
    assert _digest(queue) != FIXTURE["primary"]["queue_digest"]


# --------------------------------------------------------------------------
# Generalization / idempotency / CLI
# --------------------------------------------------------------------------
def test_graded_run_meets_documented_runtime_budget(primary_outputs):
    """The graded run finishes inside the 120-second budget instruction.md and the
    output contract both state. Bucketing the claim counts and largest-claim shares
    once keeps the run well inside it; rescanning the book per cell does not."""
    elapsed = _ELAPSED[str(DEFAULT_INPUT)]
    assert elapsed <= RUNTIME_BUDGET_SEC, (
        f"graded run took {elapsed:.1f}s, over the {RUNTIME_BUDGET_SEC}s budget"
    )


def test_runtime_budget_is_stated_in_the_contract():
    """The budget the previous test enforces is the one the output contract publishes,
    so the verifier and the contract cannot drift apart."""
    assert int(SPEC["runtime_budget_seconds"]) == int(RUNTIME_BUDGET_SEC)


def test_pipeline_rerun_idempotent():
    """Verifies that pipeline rerun idempotent."""
    _, summary_a, development_a, queue_a = _run_pipeline()
    _, summary_b, development_b, queue_b = _run_pipeline()
    assert (summary_a, development_a, queue_a) == (summary_b, development_b, queue_b)


def test_pipeline_supports_alternate_triangle():
    """Verifies that pipeline supports alternate triangle."""
    _, summary, development, queue = _run_pipeline(input_path=ALT_INPUT)
    assert summary == FIXTURE["alternate"]["summary"]
    assert _digest(development) == FIXTURE["alternate"]["development_digest"]
    assert _digest(queue) == FIXTURE["alternate"]["queue_digest"]


def test_cli_defaults_work_and_match_explicit_run():
    """Verifies that cli defaults work and match explicit run."""
    _, explicit_summary, _, _ = _run_pipeline()
    # The no-argument run writes to the default /app/output; clear any root-owned artifacts from
    # solve.sh and make the dir candidate-writable so the unprivileged program can populate it.
    default_out = Path("/app/output")
    shutil.rmtree(default_out, ignore_errors=True)
    default_out.mkdir(parents=True, exist_ok=True)
    os.chmod(default_out, 0o777)
    _run_agent([sys.executable, str(WORKFLOW_PATH)], cwd=_candidate_dir())
    assert _load_json(default_out / "summary.json") == explicit_summary


def test_submitted_program_runs_unprivileged_and_cannot_write_reward():
    """The isolation itself works: code run the way the verifier runs the agent is unprivileged
    (uid 65534) and cannot write the reward path."""
    os.makedirs("/logs/verifier", exist_ok=True)
    reward = Path("/logs/verifier/reward.txt")
    if not reward.exists():
        reward.write_text("0")
    os.chmod("/logs/verifier", 0o755)
    os.chmod(reward, 0o644)
    probe = _candidate_dir() / "probe.py"
    probe.write_text(
        "import os\n"
        "print(os.getuid())\n"
        "open('/logs/verifier/reward.txt', 'w').write('1')\n",
        encoding="utf-8",
    )
    os.chmod(probe, 0o644)
    result = subprocess.run(
        _SETPRIV + [sys.executable, str(probe)],
        capture_output=True, text=True, cwd=str(_CWORK), check=False,
    )
    assert result.stdout.strip().splitlines()[0] == "65534", "submitted program must run as uid 65534"
    assert result.returncode != 0 and "Permission denied" in result.stderr, (
        "unprivileged submitted program must not be able to write the reward path"
    )


# --------------------------------------------------------------------------
# Source-path influence
# --------------------------------------------------------------------------
def test_calendar_source_path_affects_output():
    """Verifies that calendar source path affects output."""
    original = CALENDAR_PATH.read_text(encoding="utf-8")
    try:
        _, summary_a, development_a, queue_a = _run_pipeline()
        data = json.loads(original)
        data["earned_premium_cents"] = {line: {} for line in data["earned_premium_cents"]}
        _write_json(CALENDAR_PATH, data)
        _, summary_b, development_b, queue_b = _run_pipeline()
        assert any(
            row["a_priori_loss_cents"] > 0 for rows in development_a.values() for row in rows
        )
        assert all(
            row["a_priori_loss_cents"] == 0 for rows in development_b.values() for row in rows
        )
        assert summary_a != summary_b
        assert development_a != development_b
        assert queue_a != queue_b
    finally:
        CALENDAR_PATH.write_text(original, encoding="utf-8")


def test_policy_source_path_affects_output():
    """Verifies that policy source path affects output."""
    original = POLICY_PATH.read_text(encoding="utf-8")
    try:
        data = json.loads(original)
        data["default"]["admission_min_cents"] = 99000000000
        _write_json(POLICY_PATH, data)
        _, summary, _, queue = _run_pipeline()
        assert summary != FIXTURE["primary"]["summary"]
        assert len(queue) < FIXTURE["primary"]["queue_count"]
    finally:
        POLICY_PATH.write_text(original, encoding="utf-8")


# --------------------------------------------------------------------------
# Policy resolution
# --------------------------------------------------------------------------
def _resolve(line: str, data: dict) -> dict:
    resolved = dict(BASELINE)
    resolved.update({k: int(v) for k, v in data.get("default", {}).items() if k in BASELINE})
    override = data.get("line_overrides", {}).get(line)
    if isinstance(override, dict):
        resolved.update({k: int(v) for k, v in override.items() if k in BASELINE})
    return resolved


def test_sparse_override_inherits_remaining_fields():
    """Verifies that sparse override inherits remaining fields."""
    data = _load_json(POLICY_PATH)
    overrides = data.get("line_overrides", {})
    sparse = [line for line, fields in overrides.items() if len(fields) == 1]
    assert sparse, "the shipped policy must exercise a single-field override"
    default_resolved = _resolve("__absent__", data)
    for line in sparse:
        resolved = _resolve(line, data)
        named = next(iter(overrides[line]))
        assert resolved[named] == int(overrides[line][named])
        for field in POLICY_FIELDS:
            if field != named:
                assert resolved[field] == default_resolved[field]


def test_policy_default_may_omit_fields_and_falls_back_to_baseline():
    """Verifies that policy default may omit fields and falls back to baseline."""
    data = _load_json(POLICY_PATH)
    omitted = [field for field in POLICY_FIELDS if field not in data.get("default", {})]
    assert omitted, "the shipped policy must omit at least one field to exercise fallback"
    resolved = _resolve("__absent__", data)
    for field in omitted:
        assert resolved[field] == BASELINE[field]


def test_admission_follows_resolved_policy(primary_outputs):
    """Verifies that admission follows resolved policy."""
    _, _, development, queue = primary_outputs
    data = _load_json(POLICY_PATH)
    queued = {(row["line"], row["accident_period"]) for row in queue}
    for line, rows in development.items():
        for row in rows:
            admissible = (
                line in ADMITTED_LINES
                and row["reserve_cents"] >= _resolve(line, data)["admission_min_cents"]
            )
            if (line, row["accident_period"]) in queued:
                assert admissible, (line, row["accident_period"])


def test_tier_rules_follow_resolved_policy(primary_outputs):
    """Verifies that tier rules follow resolved policy."""
    _, _, _, queue = primary_outputs
    data = _load_json(POLICY_PATH)
    for row in queue:
        policy = _resolve(row["line"], data)
        if (
            row["reserve_cents"] >= policy["escalate_reserve_min_cents"]
            or row["ibnr_cents"] >= policy["escalate_ibnr_min_cents"]
            or row["cdf_bp"] >= policy["escalate_cdf_min_bp"]
        ):
            assert row["tier"] == "escalate"
        elif (
            row["reserve_cents"] >= policy["review_reserve_min_cents"]
            or row["ibnr_cents"] >= policy["review_ibnr_min_cents"]
            or row["pct_unreported_bp"] >= policy["review_unreported_min_bp"]
        ):
            assert row["tier"] == "review"
        else:
            assert row["tier"] == "watch"


# --------------------------------------------------------------------------
# Capacity cap
# --------------------------------------------------------------------------
def test_line_capacity_cap_applied_after_ordering(primary_outputs):
    """Verifies that line capacity cap applied after ordering."""
    _, _, development, queue = primary_outputs
    data = _load_json(POLICY_PATH)
    per_line: dict[str, int] = {}
    for row in queue:
        per_line[row["line"]] = per_line.get(row["line"], 0) + 1
    assert per_line
    # #RSV-4146 caps each line at eighteen queue rows, applied after the ordering
    # rather than before it, so the cap keeps the strongest rows of a busy line.
    assert max(per_line.values()) <= 18, f"line exceeded the cap: {per_line}"
    assert max(per_line.values()) == 18, "the cap must actually bind on some line"
    admissible = sum(
        1
        for line, rows in development.items()
        for row in rows
        if line in ADMITTED_LINES
        and row["reserve_cents"] >= _resolve(line, data)["admission_min_cents"]
    )
    assert admissible > len(queue), "the valuation must admit more cohorts than the cap allows"
    seen_order = [row["line"] for row in queue]
    for line in per_line:
        positions = [index for index, name in enumerate(seen_order) if name == line]
        assert positions == sorted(positions)


# --------------------------------------------------------------------------
# The selection convention deviates from the textbook chain ladder
# --------------------------------------------------------------------------
def _staged_triangle(pairs, counts=None):
    """One line, six accident periods with two lags each plus a green cohort at lag 0."""
    periods = _load_json(CALENDAR_PATH)["periods"]
    counts = counts or {}
    triangle: dict[str, dict] = {"motor": {}}
    for index, (first, second) in enumerate(pairs):
        period = periods[index]
        triangle["motor"][period] = [
            {
                "dev_lag": 0,
                "cumulative_paid_cents": first,
                "cumulative_incurred_cents": first,
                "movement_count": counts.get(period, 2),
            },
            {
                "dev_lag": 1,
                "cumulative_paid_cents": second,
                "cumulative_incurred_cents": second,
                "movement_count": 2,
            },
        ]
    triangle["motor"][periods[6]] = [
        {
            "dev_lag": 0,
            "cumulative_paid_cents": 10000000,
            "cumulative_incurred_cents": 10000000,
            "movement_count": 2,
        }
    ]
    return triangle


STAGED_PAIRS = [
    (10000000, 30000000),   # 3.0000 -- the high outlier
    (10000000, 10000000),   # 1.0000 -- the low outlier
    (10000000, 12000000),   # 1.2000
    (10000000, 14000000),   # 1.4000
    (10000000, 16000000),   # 1.6000
    (10000000, 18000000),   # 1.8000
]


def _half_up(value: Fraction) -> int:
    scaled = value * BP
    return (2 * scaled.numerator + scaled.denominator) // (2 * scaled.denominator)


def test_selection_deviates_from_the_volume_weighted_chain_ladder(tmp_path: Path):
    """Trim-then-latest-three simple averaging disagrees with both textbook selections."""
    _, _, development, _ = _run_on_triangle(tmp_path, "staged", _staged_triangle(STAGED_PAIRS))
    green = [row for row in development["motor"] if row["latest_dev_lag"] == 0][-1]

    ratios = [Fraction(second, first) for first, second in STAGED_PAIRS]
    volume_weighted = _half_up(
        Fraction(sum(second for _, second in STAGED_PAIRS), sum(first for first, _ in STAGED_PAIRS))
    )
    all_year_average = _half_up(sum(ratios, Fraction(0)) / len(ratios))
    # the governed selection: drop the single highest and lowest, then average the latest three
    # surviving ratios by accident period.
    trimmed = [
        (order, ratio)
        for order, ratio in enumerate(ratios)
        if ratio not in (max(ratios), min(ratios))
    ]
    kept = [ratio for _, ratio in sorted(trimmed)[-3:]]
    governed = _half_up(sum(kept, Fraction(0)) / len(kept))

    assert volume_weighted == 16667 and all_year_average == 16667
    assert governed == 16000
    assert green["selected_factor_bp"] == governed
    assert green["cdf_bp"] == governed
    assert green["selected_factor_bp"] != volume_weighted


def test_thin_cells_are_excluded_from_selection(tmp_path: Path):
    """A cell below the maturity floor forms no ratio, which moves the selected factor."""
    periods = _load_json(CALENDAR_PATH)["periods"]
    triangle = _staged_triangle(STAGED_PAIRS, counts={periods[5]: 1})
    _, _, development, _ = _run_on_triangle(tmp_path, "thin", triangle)
    green = [row for row in development["motor"] if row["latest_dev_lag"] == 0][-1]
    # ratios 3.0, 1.0, 1.2, 1.4, 1.6 survive; the extremes drop; 1.2 1.4 1.6 average to 1.4
    assert green["selected_factor_bp"] == 14000


# --------------------------------------------------------------------------
# Anti-delegation: static AST ban
# --------------------------------------------------------------------------
def test_engine_does_not_import_dataframe_engines():
    """Verifies that engine does not import dataframe engines."""
    tree = ast.parse(WORKFLOW_PATH.read_text(encoding="utf-8"))
    banned = set(SPEC["workflow_repair"]["prohibited_imports"])
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    offending = banned & found
    assert not offending, f"the engine must not delegate to a dataframe engine: {offending}"


def test_ast_check_catches_a_pandas_importing_engine(tmp_path: Path):
    """The AST ban is real: a pandas-importing engine is detected."""
    shim = tmp_path / "delegating_engine.py"
    shim.write_text("import pandas\n\n\ndef run(a, b):\n    return pandas.DataFrame([a, b])\n")
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(shim.read_text()))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "pandas" in imported


# --------------------------------------------------------------------------
# Sources stay operational
# --------------------------------------------------------------------------
def test_governance_log_present():
    """Verifies that governance log present."""
    assert LOG_PATH.exists() and LOG_PATH.stat().st_size > 0


def test_shipped_contract_matches_the_golden_copy():
    """The output contract in the environment is unmodified.

    Field lists, container shapes and sort orders are golden metadata and are read
    from the verifier's own image; this proves the agent's copy still agrees with
    it, so the contract cannot be trimmed to weaken a schema check.
    """
    shipped = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    assert shipped == json.loads(GOLDEN_CONTRACT_PATH.read_text(encoding="utf-8"))
