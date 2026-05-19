"""Tests for `Service._run_lp` — the LP execution wrapper.

Coverage strategy: bypass the heavyweight `Service.__init__` (which wires up
real Modbus/HTTP clients) by constructing the instance via `__new__` and
hand-injecting only the attributes the LP path touches: `_sigenergy`,
`_loads`, `_lp_runtime`, `_lp_loads`, `_config`. The solver is patched at
the import site inside `service` so we can drive every branch (OPTIMAL,
INFEASIBLE, TIMEOUT, ERROR, slot_0=None, exception during solve, wall-clock
timeout via `asyncio.wait_for`).

This isn't end-to-end — it doesn't exercise the breaker decision tree in
`_tick`, only the LP execution wrapper. End-to-end `_tick` tests are
deferred per the agreed scope; the breaker logic itself is covered by
`test_lp_integration.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from optimiser.config import BatteryConfig
from optimiser.lp.dispatch import DispatchKind
from optimiser.lp.result import LPSolution, SlotDecision, SolveStatus
from optimiser.lp.runtime import FallbackReason, LPRuntime
from optimiser.modes import ModeManager
from optimiser.service import Service
from optimiser.types import EventType

UTC = UTC
NOW = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)


# ── Test scaffolding ─────────────────────────────────────────────


def _make_slot(*, battery_kw: float = -2.0) -> SlotDecision:
    return SlotDecision(
        slot_start=NOW,
        battery_kw=battery_kw,
        grid_import_kw=0.0,
        grid_export_kw=2.0,
        pv_to_house_kw=0.0,
        pv_to_battery_kw=0.0,
        pv_to_export_kw=0.0,
        soc_pct_end=60.0,
    )


def _make_solution(
    *,
    status: SolveStatus = SolveStatus.OPTIMAL,
    slot_0: SlotDecision | None = None,
    reason: str | None = None,
) -> LPSolution:
    if slot_0 is None and status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
        slot_0 = _make_slot()
    return LPSolution(
        status=status,
        slot_0=slot_0,
        forward_trajectory=[],
        load_commands=[],
        grid_export_limit_kw=None,
        expected_total_cost_cents=100.0,
        solve_time_ms=42,
        reason=reason,
    )


def _make_service() -> Service:
    """Build a Service with only the attributes the LP path needs.

    Uses `Service.__new__` to skip the real `__init__` (which would try to
    connect to Modbus, fetch Amber, etc.). Each attribute is set explicitly
    so a missing one fails loudly rather than reaching for a real client.
    """
    svc = Service.__new__(Service)

    # LP wiring
    svc._lp_runtime = LPRuntime()
    svc._lp_loads = []  # No managed loads → simpler LP, no LoadCommand churn

    # Metrics — `_run_lp` and `_lp_fallback` record to this; a real
    # instance is cheap and avoids sprinkling MagicMock no-ops across
    # every recording call site.
    from optimiser.api.metrics import Metrics

    svc._metrics = Metrics()

    # Config — `battery` and `planner` are read by `_run_lp`
    svc._config = MagicMock()
    svc._config.battery = BatteryConfig()
    svc._config.planner.lp_wall_clock_timeout_s = 12.0
    svc._config.planner.lp_scenario_weights = {
        "p10": 0.20,
        "p50": 0.60,
        "p90": 0.20,
    }

    # Sigenergy mock — `_lp_fallback` calls `set_fallback`
    svc._sigenergy = MagicMock()
    svc._sigenergy.set_fallback = AsyncMock(return_value=True)

    # Amber mock — `_lp_fallback` reads `last_5min_prices` to pick a safe
    # export cap (curtail if current export price is negative).
    svc._amber = MagicMock()
    svc._amber.last_5min_prices = None

    # Loads mock — `_lp_fallback` reads `controllers` (list of relay ctrls)
    svc._loads = MagicMock()
    svc._loads.controllers = []  # No managed loads → no relays to open

    # Mode manager — `_run_lp` builds the slot grid and asks the
    # manager for overrides each tick. Empty manager → empty overrides,
    # so the LP path behaves exactly as it did pre-T12.
    import tempfile
    from pathlib import Path

    svc._mode_manager = ModeManager(Path(tempfile.mkdtemp()) / "active_modes.json")

    return svc


def _solver_args() -> dict:
    """Minimal kwargs for `_run_lp`. Solver is mocked so the actual
    contents of state/prices/etc. don't drive any logic — they're just
    forwarded to the patched `solve_stochastic`. `state.pv_power_kw` is a
    real float (not a MagicMock) because dispatch_from_slot compares it
    against a threshold to pick mode 5 vs 6."""
    state = MagicMock()
    state.pv_power_kw = 0.0
    # `_run_lp` reads `state.timestamp` to build a slot grid for the
    # ModeManager. Must be a real datetime so `.minute` and arithmetic
    # work (MagicMock defaults blow up `timedelta(minutes=...)`).
    state.timestamp = NOW
    return {
        "state": state,
        "prices_planning": [MagicMock()],
        "pv_forecast": None,
        "load_profile": MagicMock(),
        "managed_loads": [],
    }


# ── Happy path ───────────────────────────────────────────────────


class TestRunLPSuccess:
    @pytest.mark.asyncio
    async def test_optimal_returns_solution_and_dispatch(self) -> None:
        svc = _make_service()
        solution = _make_solution(status=SolveStatus.OPTIMAL)

        with patch("optimiser.service.solve_stochastic", return_value=solution):
            result_sol, result_dispatch = await svc._run_lp(**_solver_args())

        assert result_sol is solution
        assert result_dispatch is not None
        # battery_kw = -2.0 → DISCHARGE_ESS_FIRST. Cap is the physical
        # max_discharge_kw so transient loads above the LP forecast stay
        # on battery instead of leaking to grid.
        assert result_dispatch.kind == DispatchKind.DISCHARGE
        assert result_dispatch.cap_kw == BatteryConfig().max_discharge_kw
        assert result_dispatch.signed_intent_kw == -2.0
        # No fallback triggered
        svc._sigenergy.set_fallback.assert_not_awaited()
        assert not svc._lp_runtime.breaker.latched

    @pytest.mark.asyncio
    async def test_feasible_treated_same_as_optimal(self) -> None:
        # FEASIBLE means the solver hit its time limit but found a usable
        # solution — we still apply it. Only INFEASIBLE/TIMEOUT/ERROR fall back.
        svc = _make_service()
        solution = _make_solution(status=SolveStatus.FEASIBLE)

        with patch("optimiser.service.solve_stochastic", return_value=solution):
            result_sol, result_dispatch = await svc._run_lp(**_solver_args())

        assert result_sol is solution
        assert result_dispatch is not None
        assert not svc._lp_runtime.breaker.latched

    @pytest.mark.asyncio
    async def test_emits_lp_solve_complete_with_metadata(self) -> None:
        svc = _make_service()
        solution = _make_solution(status=SolveStatus.OPTIMAL, reason="all good")

        with (
            patch("optimiser.service.solve_stochastic", return_value=solution),
            patch("optimiser.service.emit") as mock_emit,
        ):
            await svc._run_lp(**_solver_args())

        # Find the LP_SOLVE_COMPLETE call
        complete_calls = [
            c for c in mock_emit.call_args_list if c.args[0] == EventType.LP_SOLVE_COMPLETE
        ]
        assert len(complete_calls) == 1
        payload = complete_calls[0].args[1]
        assert payload["status"] == "optimal"
        assert payload["cost_cents"] == 100.0
        assert payload["solve_ms"] == 42
        assert payload["reason"] == "all good"


# ── Failure paths ────────────────────────────────────────────────


class TestRunLPFailures:
    @pytest.mark.asyncio
    async def test_solver_raises_triggers_lp_error_fallback(self) -> None:
        svc = _make_service()

        with patch(
            "optimiser.service.solve_stochastic",
            side_effect=RuntimeError("HiGHS exploded"),
        ):
            result_sol, result_dispatch = await svc._run_lp(**_solver_args())

        assert result_sol is None
        assert result_dispatch is None
        svc._sigenergy.set_fallback.assert_awaited_once()
        assert svc._lp_runtime.breaker.latched
        assert svc._lp_runtime.breaker.last_fallback_reason == FallbackReason.LP_ERROR

    @pytest.mark.asyncio
    async def test_wall_clock_timeout_triggers_lp_timeout_fallback(self) -> None:
        """If solve_stochastic takes longer than the configured wall-clock
        timeout, `asyncio.wait_for` raises and we fall back."""
        svc = _make_service()
        svc._config.planner.lp_wall_clock_timeout_s = 0.05  # 50ms

        def slow_solve(**_kwargs):
            import time

            time.sleep(0.5)  # 10× the timeout — definitely fires
            return _make_solution()

        with patch("optimiser.service.solve_stochastic", side_effect=slow_solve):
            result_sol, result_dispatch = await svc._run_lp(**_solver_args())

        assert result_sol is None
        assert result_dispatch is None
        assert svc._lp_runtime.breaker.last_fallback_reason == FallbackReason.LP_TIMEOUT

    @pytest.mark.asyncio
    async def test_infeasible_status_triggers_fallback(self) -> None:
        svc = _make_service()
        solution = _make_solution(status=SolveStatus.INFEASIBLE, slot_0=None)

        with patch("optimiser.service.solve_stochastic", return_value=solution):
            result_sol, result_dispatch = await svc._run_lp(**_solver_args())

        assert result_sol is None
        assert result_dispatch is None
        assert svc._lp_runtime.breaker.last_fallback_reason == FallbackReason.LP_INFEASIBLE

    @pytest.mark.asyncio
    async def test_unbounded_maps_to_infeasible_fallback(self) -> None:
        # UNBOUNDED is logically a misspecified problem — same fallback class
        svc = _make_service()
        solution = _make_solution(status=SolveStatus.UNBOUNDED, slot_0=None)

        with patch("optimiser.service.solve_stochastic", return_value=solution):
            await svc._run_lp(**_solver_args())

        assert svc._lp_runtime.breaker.last_fallback_reason == FallbackReason.LP_INFEASIBLE

    @pytest.mark.asyncio
    async def test_solver_internal_timeout_status_triggers_timeout_fallback(self) -> None:
        # SolveStatus.TIMEOUT is the solver returning "I gave up" cleanly,
        # distinct from the wall-clock timeout that raises. Both map to the
        # same FallbackReason but via different code paths.
        svc = _make_service()
        solution = _make_solution(status=SolveStatus.TIMEOUT, slot_0=None)

        with patch("optimiser.service.solve_stochastic", return_value=solution):
            await svc._run_lp(**_solver_args())

        assert svc._lp_runtime.breaker.last_fallback_reason == FallbackReason.LP_TIMEOUT

    @pytest.mark.asyncio
    async def test_error_status_triggers_lp_error_fallback(self) -> None:
        svc = _make_service()
        solution = _make_solution(status=SolveStatus.ERROR, slot_0=None)

        with patch("optimiser.service.solve_stochastic", return_value=solution):
            await svc._run_lp(**_solver_args())

        assert svc._lp_runtime.breaker.last_fallback_reason == FallbackReason.LP_ERROR

    @pytest.mark.asyncio
    async def test_optimal_with_no_slot_0_triggers_error_fallback(self) -> None:
        """Defensive: status says OK but slot_0 is None. Treat as ERROR
        rather than crashing — the inverter still gets put into a safe state."""
        svc = _make_service()
        solution = _make_solution(status=SolveStatus.OPTIMAL, slot_0=None)
        # _make_solution default-sets slot_0 for OPTIMAL; explicit None here
        solution = LPSolution(
            status=SolveStatus.OPTIMAL,
            slot_0=None,
            forward_trajectory=[],
            load_commands=[],
            grid_export_limit_kw=None,
            expected_total_cost_cents=0.0,
            solve_time_ms=10,
            reason=None,
        )

        with patch("optimiser.service.solve_stochastic", return_value=solution):
            result_sol, result_dispatch = await svc._run_lp(**_solver_args())

        assert result_sol is None
        assert result_dispatch is None
        assert svc._lp_runtime.breaker.last_fallback_reason == FallbackReason.LP_ERROR


# ── Side effects on fallback ─────────────────────────────────────


class TestRunLPFallbackSideEffects:
    """All failure paths share `_lp_fallback`. These check the side
    effects happen consistently regardless of which trigger fired."""

    @pytest.mark.asyncio
    async def test_fallback_emits_breaker_latched_with_reason(self) -> None:
        svc = _make_service()
        solution = _make_solution(status=SolveStatus.INFEASIBLE, slot_0=None)

        with (
            patch("optimiser.service.solve_stochastic", return_value=solution),
            patch("optimiser.service.emit") as mock_emit,
        ):
            await svc._run_lp(**_solver_args())

        latched_calls = [
            c for c in mock_emit.call_args_list if c.args[0] == EventType.BREAKER_LATCHED
        ]
        assert len(latched_calls) == 1
        assert latched_calls[0].args[1]["reason"] == "lp_infeasible"

    @pytest.mark.asyncio
    async def test_fallback_calls_set_fallback_on_inverter(self) -> None:
        svc = _make_service()
        solution = _make_solution(status=SolveStatus.ERROR, slot_0=None)

        with patch("optimiser.service.solve_stochastic", return_value=solution):
            await svc._run_lp(**_solver_args())

        svc._sigenergy.set_fallback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fallback_clears_commanded_state(self) -> None:
        """After fallback, runtime.commanded must be None so the watcher
        idles instead of trying to verify a stale dispatch."""
        svc = _make_service()
        # Pretend a previous tick had recorded a command
        from optimiser.lp.dispatch import dispatch_from_slot

        await svc._lp_runtime.record_command(
            dispatch_from_slot(_make_slot(), BatteryConfig(), current_soc_pct=50.0)
        )
        assert svc._lp_runtime.commanded is not None

        solution = _make_solution(status=SolveStatus.INFEASIBLE, slot_0=None)
        with patch("optimiser.service.solve_stochastic", return_value=solution):
            await svc._run_lp(**_solver_args())

        assert svc._lp_runtime.commanded is None


# ── Pre-LP PV probe gating ───────────────────────────────────────


class TestMaybeRunPVProbe:
    """`_maybe_run_pv_probe` is the gate that decides whether to pay
    the 5 s Phase-A measurement cost on a given tick. Skips at night,
    skips when planner disabled (caller checks elsewhere), passes
    measurement to LP via `slot_0_pv_override_kw` only if unsaturated."""

    def _state(self, pv_kw: float | None) -> MagicMock:
        s = MagicMock()
        s.pv_power_kw = pv_kw
        return s

    def _service_with_sigenergy(
        self, measure_return, last_export_kw: float | None = 5.0
    ) -> Service:
        svc = _make_service()
        svc._sigenergy.measure_uncapped_pv = AsyncMock(return_value=measure_return)
        svc._last_export_limit_kw = last_export_kw
        return svc

    @pytest.mark.asyncio
    async def test_skips_when_pv_below_threshold(self) -> None:
        """At night / dusk, pv reads ~0; the probe is uninformative."""
        svc = self._service_with_sigenergy(measure_return=None)
        result = await svc._maybe_run_pv_probe(self._state(pv_kw=0.1), "tick-1")
        assert result is None
        svc._sigenergy.measure_uncapped_pv.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_pv_unknown(self) -> None:
        """Telemetry blind at tick start → no probe (we'd be writing
        without context)."""
        svc = self._service_with_sigenergy(measure_return=None)
        result = await svc._maybe_run_pv_probe(self._state(pv_kw=None), "tick-1")
        assert result is None
        svc._sigenergy.measure_uncapped_pv.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_and_returns_probe_when_pv_above_threshold(self) -> None:
        from optimiser.types import PVProbeResult

        probe = PVProbeResult(
            pv_kw=8.0,
            saturated=False,
            bat_kw=7.5,
            bat_avail_kw=13.0,
            grid_export_kw=0.4,
            export_cap_kw=5.0,
            house_kw=0.1,
            soc_pct=50.0,
        )
        svc = self._service_with_sigenergy(measure_return=probe)
        result = await svc._maybe_run_pv_probe(self._state(pv_kw=8.0), "tick-1")
        assert result is probe
        svc._sigenergy.measure_uncapped_pv.assert_awaited_once_with(export_cap_kw=5.0)

    @pytest.mark.asyncio
    async def test_passes_none_export_cap_on_first_tick(self) -> None:
        """No `_last_export_limit_kw` yet → probe still runs but
        saturation check has no export term to compare against."""
        from optimiser.types import PVProbeResult

        probe = PVProbeResult(
            pv_kw=6.0,
            saturated=False,
            bat_kw=5.5,
            bat_avail_kw=13.0,
            grid_export_kw=0.0,
            export_cap_kw=None,
            house_kw=0.1,
            soc_pct=40.0,
        )
        svc = self._service_with_sigenergy(measure_return=probe, last_export_kw=None)
        result = await svc._maybe_run_pv_probe(self._state(pv_kw=6.0), "tick-1")
        assert result is probe
        svc._sigenergy.measure_uncapped_pv.assert_awaited_once_with(export_cap_kw=None)

    @pytest.mark.asyncio
    async def test_returns_none_on_uncap_write_failure(self) -> None:
        """Hard probe failure → None propagates (caller falls back to
        Solcast for slot-0 PV; dispatch's own fallback handles the
        register state)."""
        svc = self._service_with_sigenergy(measure_return=None)
        # PV high enough to gate IN, but measure returns None
        result = await svc._maybe_run_pv_probe(self._state(pv_kw=5.0), "tick-1")
        assert result is None
        svc._sigenergy.measure_uncapped_pv.assert_awaited_once()
