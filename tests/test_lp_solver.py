"""Solver-wrapper configuration tests.

These guard the deliberate solver knobs in `lp/solver.py::_solver` — the
ones whose absence shows up as a production fallback rather than a unit
failure. The relative MIP gap is the load-bearing one: without it, HiGHS
solves the 5 h hot-water-block MILP to gap=0 and blows past the
wall-clock timeout on evening ticks (→ SELF_CONSUME fallback). See the
`MIP_REL_GAP` comment in `lp/constants.py` for the measurements and the
rationale behind the chosen value.
"""

from __future__ import annotations

from optimiser.lp.solver import _solver


def test_solver_configures_nonzero_relative_mip_gap() -> None:
    """`_solver` must hand HiGHS a small non-zero relative MIP gap.

    A gap of 0 (the solver default) forces full optimality proof, which
    is intractable within the timeout for the hot-water block MILP. We
    accept a small ceiling (realized cost delta ~0) to stay well under
    the wall clock.
    """
    solver = _solver(20.0)
    assert solver is not None
    assert solver.gapRel is not None, "no relative MIP gap configured (gap=0 → timeouts)"
    assert 0.0 < solver.gapRel < 1.0
