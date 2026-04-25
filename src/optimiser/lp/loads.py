"""LP load abstractions.

Each managed load type implements the `LPLoad` protocol: it adds its
own decision variables and constraints to a PuLP problem, exposes its
power draw as an LpAffineExpression for the energy-balance constraint,
and extracts a `LoadCommand` from the solved variables.

This keeps the formulation generic — adding a new load type (EV, aircon,
pool pump) is a new class implementing the protocol; the formulation
code doesn't change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import pulp

from ..config import ManagedLoadConfig
from ..time_utils import local_to_utc, utc_to_local
from ..types import LoadCommand, ManagedLoadStatus
from .constants import RELAX_FUTURE_BINARIES, ROLL_FORWARD_CAP_MULTIPLIER

logger = logging.getLogger(__name__)

# ── Protocol ─────────────────────────────────────────────────────


@dataclass
class LoadVars:
    """Container for an LPLoad's decision variables.

    Each load can stash whatever PuLP variables it needs here; the
    formulation only ever looks at `power_kw[t]` for the energy-balance.
    """

    power_kw: list[pulp.LpAffineExpression]  # one per slot
    extras: dict[str, list[pulp.LpVariable]] = field(default_factory=dict)


class LPLoad(Protocol):
    """A managed load that participates in the LP optimisation."""

    load_id: str

    def add_to(
        self,
        prob: pulp.LpProblem,
        slots: list[datetime],
        slot_hours: float,
        status: ManagedLoadStatus,
        var_prefix: str = "",
    ) -> LoadVars:
        """Add this load's variables and constraints to the problem.

        `slots`: list of slot start times (slot 0 is "now").
        `slot_hours`: duration of each slot in hours.
        `status`: current observed state of the load (energy_today, relay_on).
        `var_prefix`: prepended to variable names so multiple scenarios
            can build into the same problem without name collisions.
        Returns a `LoadVars` whose `power_kw[t]` contributes to the
        energy-balance constraint at each slot.
        """
        ...

    def extract_command(
        self,
        vars: LoadVars,
        status: ManagedLoadStatus,
        slot_0_start: datetime,
    ) -> LoadCommand:
        """After solve: pull the slot-0 decision and turn it into a LoadCommand."""
        ...


# ── BinarySignalDrivenLoad (HW heat pump in PV mode, future EV) ──


class BinarySignalDrivenLoad:
    """A load with a binary on/off relay where the appliance manages its
    own internal cycles. The LP decides whether to assert the relay each
    slot, subject to a daily-energy target by deadline.

    Power model: `power_kw[t] = relay[t] × draw_kw` (constant draw when on).

    Constraints:
      - `Σ relay[t] × draw_kw × slot_hours ≥ daily_target_kwh − energy_today`
        across slots before today's deadline (safety floor).
      - Slot 0 binary is integer-constrained; future slots are LP-relaxed
        per `RELAX_FUTURE_BINARIES` (we re-decide every tick).

    Maps to: hot-water heat pump in PV mode, future EV charger (when
    represented as a single-rate contactor — for variable-rate EV
    charging, use a continuous-modulated load type instead).
    """

    def __init__(self, config: ManagedLoadConfig) -> None:
        if config.draw_kw is None or config.daily_target_kwh is None:
            raise ValueError(
                f"BinarySignalDrivenLoad {config.load_id!r} requires draw_kw and daily_target_kwh"
            )
        self._cfg = config
        self.load_id = config.load_id

    def add_to(
        self,
        prob: pulp.LpProblem,
        slots: list[datetime],
        slot_hours: float,
        status: ManagedLoadStatus,
        var_prefix: str = "",
    ) -> LoadVars:
        n = len(slots)
        draw = self._cfg.draw_kw or 0.0
        target = self._cfg.daily_target_kwh or 0.0
        deadline_hour = self._cfg.deadline_hour_local

        # Decision variables: relay state per slot.
        # Slot 0 is binary (we commit to it this tick). Future slots are
        # continuous in [0, 1] when RELAX_FUTURE_BINARIES is True.
        relay: list[pulp.LpVariable] = []
        for t in range(n):
            if t == 0 or not RELAX_FUTURE_BINARIES:
                v = pulp.LpVariable(
                    f"{var_prefix}{self.load_id}_relay_{t}",
                    cat=pulp.LpBinary,
                )
            else:
                v = pulp.LpVariable(
                    f"{var_prefix}{self.load_id}_relay_{t}",
                    lowBound=0.0,
                    upBound=1.0,
                    cat=pulp.LpContinuous,
                )
            relay.append(v)

        # ── Daily-target constraints ──────────────────────────────
        # A constraint is added for every local-calendar-day deadline
        # that overlaps the LP horizon. For each such day, the sum of
        # energy delivered in that day's pre-deadline window (i.e. the
        # local-midnight → deadline window, intersected with the
        # horizon) must meet the day's effective target.
        #
        # Today's deadline gets special handling:
        #   - If still in the future: target = daily_target − energy
        #     already delivered today (per Shelly CT accumulator).
        #   - If already past: no slots remain to constrain; the unmet
        #     energy is ROLLED FORWARD into tomorrow's target, capped at
        #     `ROLL_FORWARD_CAP_MULTIPLIER × daily_target` (see
        #     lp/constants.py for rationale) so a service outage can't
        #     pile several days of unmet target onto one day's
        #     physically-achievable window. Excess beyond the cap is
        #     forgiven (a tank that's been cold for 2+ days needs
        #     operator attention, not a frantic LP).

        if slots and target > 0 and draw > 0:
            horizon_start = slots[0]
            horizon_end_excl = slots[-1] + timedelta(minutes=int(slot_hours * 60))
            already_today = max(0.0, status.energy_today_kwh)
            rolled_forward_kwh = 0.0

            # Today's local midnight, as the anchor for day iteration.
            local_now = utc_to_local(horizon_start)
            today_midnight_local = local_now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

            # Iterate at most 3 days — covers any horizon up to ~72h.
            for day_offset in range(3):
                day_midnight_local = today_midnight_local + timedelta(days=day_offset)
                deadline_local = day_midnight_local.replace(hour=deadline_hour)
                day_start_utc = local_to_utc(day_midnight_local)
                deadline_utc = local_to_utc(deadline_local)

                # This day's pre-deadline window, intersected with horizon.
                window_start = max(day_start_utc, horizon_start)
                window_end = min(deadline_utc, horizon_end_excl)

                in_horizon_slots = [
                    t for t, s in enumerate(slots) if window_start <= s < window_end
                ]

                # Figure out the target for THIS day's constraint.
                if day_offset == 0:
                    # Today. Already-delivered comes from Shelly.
                    day_target = max(0.0, target - already_today)
                    if not in_horizon_slots:
                        # Deadline already passed for today. Roll the
                        # unmet remainder forward to the next deadline.
                        rolled_forward_kwh = day_target
                        continue
                else:
                    # Future day. Already-delivered at that day's start
                    # is zero (the Shelly counter resets at midnight).
                    # Apply any rolled-forward shortfall from earlier
                    # iterations, and cap the combined target so we
                    # never demand an impossible amount in one day.
                    day_target = target + rolled_forward_kwh
                    rolled_forward_kwh = 0.0  # claimed (or forgiven below)
                    cap = ROLL_FORWARD_CAP_MULTIPLIER * target
                    if day_target > cap:
                        # Excess beyond the cap is forgiven — logging
                        # this from inside LP build is noisy; the
                        # replay/snapshot record will show the day's
                        # target vs the cap applied.
                        day_target = cap

                    if not in_horizon_slots:
                        # This day's window lies outside the horizon
                        # entirely. Nothing to constrain.
                        continue

                if day_target <= 0:
                    continue

                prob += (
                    pulp.lpSum(relay[t] * draw * slot_hours for t in in_horizon_slots)
                    >= day_target,
                    f"{var_prefix}{self.load_id}_daily_target_d{day_offset}",
                )

        # Power expression per slot: relay × draw_kw
        power = [relay[t] * draw for t in range(n)]
        return LoadVars(power_kw=power, extras={"relay": relay})

    def extract_command(
        self,
        vars: LoadVars,
        status: ManagedLoadStatus,
        slot_0_start: datetime,
    ) -> LoadCommand:
        relay_0 = vars.extras["relay"][0]
        val = relay_0.varValue if relay_0.varValue is not None else 0.0
        on = val > 0.5  # binary, but be defensive against solver noise
        target = self._cfg.daily_target_kwh or 0.0
        return LoadCommand(
            load_id=self.load_id,
            start_cycle=False,  # legacy field; LP doesn't use it
            desired_relay_on=on,
            reason=(
                f"LP slot-0: relay={'on' if on else 'off'} "
                f"(energy_today={status.energy_today_kwh:.2f}/"
                f"{target:.2f} kWh)"
            ),
        )


# ── ObservableLoad (mains, oven — measured, not controlled) ──────


class ObservableLoad:
    """A load we can measure but not control. Adds zero decision variables.
    Its expected per-slot power is built from the load profile and fed
    into the energy-balance as a constant.

    For v1 we use the load profile only (option (a) from the architecture
    discussion). Future iterations may build per-load profiles from
    historical Shelly CT data.
    """

    def __init__(self, config: ManagedLoadConfig) -> None:
        self._cfg = config
        self.load_id = config.load_id

    def add_to(
        self,
        prob: pulp.LpProblem,
        slots: list[datetime],
        slot_hours: float,
        status: ManagedLoadStatus,
        var_prefix: str = "",
    ) -> LoadVars:
        # No decision variables. The "power" is the load profile, which
        # the formulation incorporates separately (this load's profile is
        # already inside `house_load_kw` for the energy balance).
        # We still register a LoadVars with zero contribution so the
        # formulation can iterate over `loads` uniformly.
        zero = pulp.LpAffineExpression(0.0)
        return LoadVars(power_kw=[zero] * len(slots))

    def extract_command(
        self,
        vars: LoadVars,
        status: ManagedLoadStatus,
        slot_0_start: datetime,
    ) -> LoadCommand:
        return LoadCommand(
            load_id=self.load_id,
            start_cycle=False,
            desired_relay_on=None,  # observable — no relay control
            reason="observable load",
        )


# ── Factory ──────────────────────────────────────────────────────


def build_lp_loads(configs: list[ManagedLoadConfig]) -> list[LPLoad]:
    """Construct LPLoad instances from the user's managed_load configs.

    Maps `LoadCategory` to the appropriate LP load implementation.
    Unknown categories are skipped with a warning rather than crashing
    the LP build.
    """
    from ..types import LoadCategory

    loads: list[LPLoad] = []
    for cfg in configs:
        if cfg.category == LoadCategory.SIGNAL_DRIVEN:
            loads.append(BinarySignalDrivenLoad(cfg))
        elif cfg.category == LoadCategory.OBSERVABLE:
            loads.append(ObservableLoad(cfg))
        else:
            # SHIFTABLE / PRECONDITIONABLE / DEADLINE_BIDIR: deferred
            # to v2. Surface skipped configs at startup so an
            # operator who configures one of these doesn't have it
            # silently disappear from the LP.
            logger.warning(
                "Skipping managed load %r: category %r has no LPLoad "
                "implementation (deferred to v2)",
                cfg.load_id,
                cfg.category.value,
            )
    return loads


# ── Helpers ──────────────────────────────────────────────────────

# (The previous `_todays_deadline_utc` helper was removed when the daily-
# target constraint was generalised to iterate over all in-horizon
# deadlines. See `BinarySignalDrivenLoad.add_to`.)
