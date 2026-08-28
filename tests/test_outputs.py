"""Verifier tests for the claims-reserving development task."""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import shutil
import tempfile
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


def _file_sha256(path: Path) -> str:
    """Digest of a file's raw bytes.

    instruction.md promises the operational inputs come back *byte*-identical, so
    a parsed-content comparison is not enough: it would accept a reformat or a key
    reorder that the sentence rules out.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Documented wall-clock budget for one full run on the graded book.
# instruction.md and report_spec.json state the same number. The reference
# buckets every movement into its cell and claim in a single pass; re-scanning
# the movement file per cell is the cell count times the movement count.
RUNTIME_BUDGET_SEC = 120.0
# Wall-clock of each graded run, keyed by the PROGRAM and the input it was given.
# Keying on the input alone let a later test that runs a different program over the
# same default input overwrite the graded run's timing, so a slow submission was
# measured by the frozen original's clock instead of its own.
_ELAPSED: dict[tuple[str, str], float] = {}


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path):
    """Read a contracted JSONL artifact, taking every line as written.

    Skipping blank lines here softened a contract that says one compact object
    per line: a run that padded its output with empty lines read back the same
    as a clean one and scored full marks. A blank line is a malformed line and
    is read as one.
    """
    text = Path(path).read_text(encoding="utf-8")
    if not text:
        return []
    assert text.endswith("\n"), f"{Path(path).name} has no trailing newline"
    lines = text.split("\n")[:-1]
    for number, line in enumerate(lines, start=1):
        assert line.strip(), f"{Path(path).name} line {number} is blank"
    return [json.loads(line) for line in lines]


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
# The budget is enforced by killing the run, not by timing it afterwards: a run
# that overruns never returns a result to grade. RUNTIME_BUDGET_SEC is defined
# below and this is asserted equal to it, so the two cannot drift apart.
_CANDIDATE_TIMEOUT = 120


def _candidate_dir() -> Path:
    """A fresh work area for one run, created where nothing can pre-empt it.

    /candidate-work is world-writable, so a predictable name here was an opening:
    a submission could plant the next `run-N` as a symlink to the sealed fixtures
    and wait. The root-side mkdir(exist_ok=True) would succeed through the link
    and the chmod would follow it, since os.chmod resolves symlinks and Linux has
    no lchmod. mkdtemp closes both halves -- the name is unpredictable and the
    directory is created fresh or not at all.
    """
    directory = Path(tempfile.mkdtemp(prefix=f"run-{next(_run_ctr)}-", dir=str(_CWORK)))
    assert not directory.is_symlink(), directory
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
    _ELAPSED[(str(script_path), str(input_path))] = time.monotonic() - started
    assert result.returncode == 0
    summary = _load_json(out_dir / "summary.json")
    development = _load_json(out_dir / "line_development.json")
    queue = _load_jsonl(out_dir / "reserve_queue.jsonl")
    return out_dir, summary, development, queue


def _run_on_triangle(tmp_path: Path, label: str, triangle: dict):
    staged = tmp_path / f"{label}.json"
    _write_json(staged, triangle)
    return _run_pipeline(input_path=staged)


@pytest.fixture(scope="session", autouse=True)
def _clear_default_output_dir():
    """Empty /app/output before anything is graded.

    Every graded run is given an explicit --output-dir under /candidate-work, so
    nothing here is needed. Left in place, though, the artifacts solve.sh wrote are
    a correct summary.json, line_development.json and reserve_queue.jsonl sitting
    readable on disk, and an engine handed a staged triangle could copy them
    instead of projecting it.
    """
    default_out = Path("/app/output")
    if default_out.exists():
        for path in sorted(default_out.iterdir()):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def primary_outputs():
    return _run_pipeline()


# --------------------------------------------------------------------------
# Step 1: the cumulative development triangle must be rebuilt in place
# --------------------------------------------------------------------------
def test_aggregation_sources_are_intact():
    """The operational inputs a run reads come back byte-identical.

    instruction.md names the movement extract, the period calendar and the
    reserving policy as files a run must leave alone, so all three are checked,
    and checked over their raw bytes rather than their parsed content.
    """
    assert _file_sha256(MOVEMENTS_PATH) == FIXTURE["input_bytes_sha256"]["claim_movements.json"]
    assert _file_sha256(CALENDAR_PATH) == FIXTURE["input_bytes_sha256"]["period_calendar.json"]
    assert _file_sha256(POLICY_PATH) == FIXTURE["input_bytes_sha256"]["reserving_policy.json"]
    # The parsed digests stay as a second, redundant reading of the same promise.
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
    """Even a correctly repaired engine emits wrong artifacts on a wrongly built triangle.

    This is what makes the two steps dependent rather than merely sequential: the
    engine has to project the triangle it was handed. instruction.md says so in as
    many words -- the engine neither rebuilds nor rewrites a triangle, and the one
    replacement under /app/data belongs to the first step -- because an engine that
    recomputed the triangle from the movements each run would produce the right
    answer here for the wrong reason and fail this test.
    """
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
def test_cli_exists(primary_outputs):
    """The engine is present and honours the options it must keep.

    Existence alone would pass a program that had dropped either option, so the
    graded run -- made with an explicit --input and --output-dir -- is required
    to have written its artifacts to that destination rather than the default.
    """
    assert WORKFLOW_PATH.exists()
    out_dir = primary_outputs[0]
    assert out_dir != Path("/app/output"), "the graded run must use the directory it was given"
    assert sorted(q.name for q in out_dir.iterdir() if q.is_file()) == [
        "line_development.json", "reserve_queue.jsonl", "summary.json"]


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
        # a blank line is not skipped here: the contract says one compact
        # object per line, so an empty one is a malformed line
        assert line.strip(), "the queue carries a blank line"
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
    # Enforced twice over. Every candidate run is executed with the published
    # budget as a hard timeout, so a run that overruns is killed and its test
    # fails rather than being measured after the fact...
    assert _CANDIDATE_TIMEOUT == int(RUNTIME_BUDGET_SEC)
    # ...and the graded run's own wall-clock, under its own key, is checked here.
    elapsed = _ELAPSED[(str(WORKFLOW_PATH), str(DEFAULT_INPUT))]
    assert elapsed <= RUNTIME_BUDGET_SEC, (
        f"graded run took {elapsed:.1f}s, over the {RUNTIME_BUDGET_SEC}s budget"
    )
    assert primary_outputs[1]["total_reserve_cents"] != 0, "the graded run produced nothing"


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
    # all three artifacts, not just the summary: a run that special-cases its
    # default mode must not be able to leave the other two behind or broken
    assert sorted(q.name for q in default_out.iterdir() if q.is_file()) == [
        "line_development.json", "reserve_queue.jsonl", "summary.json"]
    _, explicit_summary2, explicit_development, explicit_queue = _run_pipeline()
    assert explicit_summary2 == explicit_summary
    assert _load_json(default_out / "summary.json") == explicit_summary
    # The comment above promised all three; only the summary was actually compared,
    # so a run that special-cased its default mode could emit anything at all in the
    # other two files and pass.
    assert _digest(_load_json(default_out / "line_development.json")) == _digest(
        explicit_development)
    assert _digest(_load_jsonl(default_out / "reserve_queue.jsonl")) == _digest(
        explicit_queue)


def test_submitted_program_runs_unprivileged_and_cannot_write_reward():
    """Code run the way the verifier runs the agent is unprivileged (uid 65534)
    and cannot reach the reward path.

    The channel's modes are left exactly as test.sh set them, so this asserts the
    isolation really in force rather than relaxing it to be measured, and the
    probe sits in a root-owned directory the candidate uid can read and execute
    but not write.
    """
    os.makedirs("/logs/verifier", exist_ok=True)
    reward = Path("/logs/verifier/reward.txt")
    if not reward.exists():
        reward.write_text("0")
    probe_dir = Path("/probe-work")
    probe_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(probe_dir, 0o755)
    probe = probe_dir / "probe.py"
    probe.write_text(
        "import os\n"
        "print(os.getuid())\n"
        "try:\n"
        "    open('/logs/verifier/reward.txt').read()\n"
        "    print('readable')\n"
        "except OSError:\n"
        "    print('unreadable')\n"
        "try:\n"
        "    open('/logs/verifier/reward.txt', 'w').write('1')\n"
        "    print('writable')\n"
        "except OSError:\n"
        "    print('unwritable')\n",
        encoding="utf-8")
    os.chmod(probe, 0o644)
    result = subprocess.run(
        _SETPRIV + [sys.executable, str(probe)],
        capture_output=True, text=True, cwd=str(probe_dir), check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.splitlines() == ["65534", "unreadable", "unwritable"], result.stdout


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
    # #RSV-4114 chains the selected factor of every transition from this lag through
    # the last observed one and THEN the tail, reducing to whole basis points at each
    # step. This cohort sits at lag 0 of a two-lag triangle, so exactly one transition
    # remains -- but the tail still applies. Asserting cdf_bp == the selected factor
    # would silently require a unit tail, which the log does not promise for a
    # triangle this sparse, and would fail an engine that derived a different one.
    expected_cdf = -(-(governed * green["tail_factor_bp"]) // BP)
    assert green["cdf_bp"] == expected_cdf, (
        green["cdf_bp"], governed, green["tail_factor_bp"])
    assert green["selected_factor_bp"] != volume_weighted


def test_the_tail_comes_from_the_last_transition_in_the_horizon(tmp_path: Path):
    """#RSV-4112 keys the tail to the highest lag a factor is selected at, and #RSV-4104
    selects par wherever no ratio survives, so every transition the calendar spans counts.

    The staged triangle stops at lag 1 while the calendar runs far past it, so the
    last transition in the horizon selects par and the tail is par with it. The
    cohort's own selected factor still comes from the transition it sits on, which
    is what separates this from an engine that keys the tail to the last
    transition that happened to carry ratios.
    """
    _, _, development, _ = _run_on_triangle(tmp_path, "tail", _staged_triangle(STAGED_PAIRS))
    green = [row for row in development["motor"] if row["latest_dev_lag"] == 0][-1]
    selected = green["selected_factor_bp"]
    assert selected == 16000, selected
    # the last transition in the horizon carries no surviving ratio, so it selects
    # par under #RSV-4104 and half of nothing is nothing
    assert green["tail_factor_bp"] == BP, green["tail_factor_bp"]
    # and the cohort's cdf is still the chain out of its own lag, not a bare par
    assert green["cdf_bp"] >= selected, green["cdf_bp"]


def test_a_boolean_figure_is_worth_nothing(tmp_path: Path):
    """report_spec.json converts a cents figure through int(str(value).strip()).

    "True" is not a number and falls to the zero the contract names on failure.
    Read as the 1 it is worth in Python arithmetic, a boolean paid figure turns an
    immature cell into a developing one and moves the selected factor with it.
    The engine is exercised as a subprocess like every other graded run; nothing
    here imports the submitted module.
    """
    pairs = list(STAGED_PAIRS)
    triangle = _staged_triangle(pairs)
    periods = _load_json(CALENDAR_PATH)["periods"]
    # the earlier cell of the oldest period reads as a boolean: nil paid, so
    # #RSV-4106 forms no ratio there and the highest ratio drops out of the trim
    triangle["motor"][periods[0]][0]["cumulative_paid_cents"] = True
    _, _, development, _ = _run_on_triangle(tmp_path, "boolean", triangle)
    green = [row for row in development["motor"] if row["latest_dev_lag"] == 0][-1]

    ratios = [Fraction(second, first) for first, second in pairs[1:]]
    surviving = list(ratios)
    surviving.remove(max(surviving))
    surviving.remove(min(surviving))
    kept = surviving[-3:] if len(surviving) >= 3 else surviving
    expected = _half_up(sum(kept, Fraction(0)) / len(kept))
    assert green["selected_factor_bp"] == expected, (
        "a boolean paid figure was read as a number rather than as the contract's zero",
        green["selected_factor_bp"], expected,
    )


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
def _imported_roots(source: str) -> set:
    """Top-level module names the source imports, read from the parse tree."""
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    return roots


def test_engine_imports_only_the_standard_library():
    """instruction.md says standard library only, and the named ban is not that.

    The contract names three dataframe engines for a reason of its own. It says
    nothing about any other package, which is what "standard library only"
    actually means, and leaving the rule to the image's package set makes it an
    accident of the image rather than something the task states and grades.
    """
    found = _imported_roots(WORKFLOW_PATH.read_text(encoding="utf-8"))
    local = {path.stem for path in WORKFLOW_PATH.parent.glob("*.py")}
    outside = {name for name in found
               if name not in sys.stdlib_module_names and name not in local}
    assert not outside, f"the engine imports outside the standard library: {sorted(outside)}"


def test_the_standard_library_check_catches_a_third_party_import(tmp_path: Path):
    """The check above is real: an engine reaching for a package is detected."""
    shim = tmp_path / "vendored_engine.py"
    shim.write_text("import json\nimport polars as pl\nfrom scipy import stats\n")
    found = _imported_roots(shim.read_text())
    assert {name for name in found if name not in sys.stdlib_module_names} == {"polars", "scipy"}


def test_engine_does_not_load_modules_dynamically():
    """A module loaded at run time is not an import the scan above can see."""
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "importlib" not in _imported_roots(source), "the engine imports importlib"
    called = {node.func.id for node in ast.walk(ast.parse(source))
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    attributes = {node.attr for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Attribute)}
    banned = {"__import__", "eval", "exec", "compile"}
    assert not (banned & called), f"the engine loads code at run time: {sorted(banned & called)}"
    assert "import_module" not in attributes, "the engine loads a module at run time"


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
    assert json.loads(SPEC_PATH.read_text(encoding="utf-8")) == json.loads(
        GOLDEN_CONTRACT_PATH.read_text(encoding="utf-8"))
    # instruction.md promises this file comes back byte-identical too, not merely
    # equal once parsed.
    assert _file_sha256(SPEC_PATH) == _file_sha256(GOLDEN_CONTRACT_PATH)
