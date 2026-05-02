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
        # Subclasses (e.g. BinarySignalDrivenContinuousLoad) set this to
        # True to force every slot's relay var to be binary, overriding
        # the global RELAX_FUTURE_BINARIES optimisation. Required when
        # the load adds constraints (min-on / min-off) that only enforce
        # correctly under integrality.
        self._force_binary_relay: bool = False

    def _slot_state(self, slot_t: datetime) -> str:
        """Look up the schedule override for this slot's local date.

        Returns 'off', 'on', or 'auto' (default when the date isn't in
        the override map). Cheap when the map is empty — the common case.
        """
        if not self._cfg.schedule_overrides:
            return "auto"
        return self._cfg.schedule_overrides.get(utc_to_local(slot_t).date().isoformat(), "auto")

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
            if t == 0 or self._force_binary_relay or not RELAX_FUTURE_BINARIES:
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

        # ── Per-slot schedule overrides (off / on) ────────────────
        # Each slot's local-date is looked up in `schedule_overrides`.
        # 'off' forces relay[t]=0; 'on' forces relay[t]=1; 'auto' lets
        # the LP decide. Any in-progress min-on block carry-over (from
        # the subclass) defers to these — user intent overrides safety.
        for t in range(n):
            state = self._slot_state(slots[t])
            if state == "off":
                prob += (
                    relay[t] == 0,
                    f"{var_prefix}{self.load_id}_sched_off_t{t}",
                )
            elif state == "on":
                prob += (
                    relay[t] == 1,
                    f"{var_prefix}{self.load_id}_sched_on_t{t}",
                )

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

                # Schedule override: skipped/forced days drop the daily-
                # target constraint AND zero any rolled-forward shortfall
                # (a skipped day forgives, doesn't defer).
                day_date_iso = day_midnight_local.date().isoformat()
                if self._cfg.schedule_overrides.get(day_date_iso, "auto") != "auto":
                    rolled_forward_kwh = 0.0
                    continue

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

                if day_target <= 0:
                    continue

                # Feasibility check (uniform across days). The day's
                # in-horizon window has a hard kWh ceiling: every slot
                # ON × slot_hours × draw. If day_target exceeds that
                # ceiling — because the window is empty (deadline past,
                # or window beyond horizon end) or because Amber's
                # price coverage truncates the LP horizon partway
                # through this day — demanding it produces an
                # infeasible LP. Roll the unmet target into the next
                # iteration; the cap on day_offset >= 1 prevents
                # cascading days from piling up impossible amounts.
                # When day 2 (the last iteration) hits this branch the
                # roll-forward simply drops on the floor, which is the
                # right outcome — what falls beyond the horizon will
                # be re-decided on the next tick when the horizon
                # advances. (Min-on / min-off block constraints make
                # the *true* ceiling lower than this naive bound;
                # partial-fitting against blocks is fragile so we
                # don't try.)
                max_kwh = len(in_horizon_slots) * slot_hours * draw
                if not in_horizon_slots or day_target > max_kwh:
                    rolled_forward_kwh = day_target
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


# ── BinarySignalDrivenContinuousLoad (HW heat pump — runs in blocks) ──


class BinarySignalDrivenContinuousLoad(BinarySignalDrivenLoad):
    """`BinarySignalDrivenLoad` that additionally enforces contiguous
    run-blocks: once the relay turns on, it must stay on for at least
    `min_on_slots` consecutive slots; once it turns off, it must stay off
    for at least `min_off_slots` before re-asserting.

    Required for appliances whose compressors / internal control don't
    tolerate stop-start (HW heat pumps in PV mode). The plain
    `BinarySignalDrivenLoad` is free to flap each slot, which is fine for
    a contactor-only EV charger but harmful for an HP.

    Limitation: the constraints apply *within* the LP horizon. A block
    started at slot 0 commits the next `min_on_slots-1` future slots; a
    block carried over from a prior tick (relay already on at slot 0) is
    not counted toward the minimum — slot 0 is free to turn off. In
    practice the daily-target constraint and the block's continuity over
    successive horizons keep the LP committed once it commits.
    """

    def __init__(self, config: ManagedLoadConfig) -> None:
        super().__init__(config)
        if config.min_on_slots is None or config.min_off_slots is None:
            raise ValueError(
                f"BinarySignalDrivenContinuousLoad {config.load_id!r} requires "
                f"min_on_slots and min_off_slots"
            )
        if config.min_on_slots < 1 or config.min_off_slots < 1:
            raise ValueError(
                f"BinarySignalDrivenContinuousLoad {config.load_id!r}: "
                f"min_on_slots and min_off_slots must be ≥ 1"
            )
        self._min_on = config.min_on_slots
        self._min_off = config.min_off_slots
        # Min-on/min-off only enforce contiguous blocks under integrality;
        # under LP relaxation the LP can satisfy `sum >= L * jump` with a
        # fractional band that the threshold-based block check then
        # mis-counts. Force every slot binary for this load.
        self._force_binary_relay = True

    def add_to(
        self,
        prob: pulp.LpProblem,
        slots: list[datetime],
        slot_hours: float,
        status: ManagedLoadStatus,
        var_prefix: str = "",
    ) -> LoadVars:
        vars = super().add_to(prob, slots, slot_hours, status, var_prefix)
        relay = vars.extras["relay"]
        n = len(relay)

        # Min-up time: if a turn-ON occurs at slot t (relay[t]=1, relay[t-1]=0),
        # the next L_on slots (including t) must all be 1.
        #   sum(relay[t..t+L_on-1]) ≥ L_on * (relay[t] - relay[t-1])
        # Window truncates at horizon end (sum/L drop together → still valid).
        for t in range(1, n):
            window = list(range(t, min(t + self._min_on, n)))
            block = len(window)
            prob += (
                pulp.lpSum(relay[k] for k in window) >= block * (relay[t] - relay[t - 1]),
                f"{var_prefix}{self.load_id}_min_on_t{t}",
            )

        # Min-down time: if a turn-OFF occurs at slot t (relay[t]=0, relay[t-1]=1),
        # the next L_off slots (including t) must all be 0.
        #   sum(relay[t..t+L_off-1]) ≤ block * (1 - (relay[t-1] - relay[t]))
        for t in range(1, n):
            window = list(range(t, min(t + self._min_off, n)))
            block = len(window)
            prob += (
                pulp.lpSum(relay[k] for k in window) <= block * (1 - (relay[t - 1] - relay[t])),
                f"{var_prefix}{self.load_id}_min_off_t{t}",
            )

        # ── Slot-0 carry-over (cross-tick block enforcement) ──────
        # The min-up/min-down constraints above only fire for t ≥ 1
        # because they reference relay[t-1]. Without binding slot 0 to
        # the prior tick's commitment, the LP rebuilds each tick with
        # no memory and could turn off mid-block. `relay_state_since`
        # carries that memory: how long has the relay been in its
        # current state? If we haven't yet served a full block, force
        # slot 0 to match the current state.
        if status.relay_state_since is not None and status.relay_on is not None and n > 0:
            slot_seconds = slot_hours * 3600.0
            elapsed_s = (slots[0] - status.relay_state_since).total_seconds()
            elapsed_slots = max(0, int(elapsed_s // slot_seconds))

            if status.relay_on:
                # Currently asserted — must hold for at least min_on slots
                # total (counting slots already served).
                remaining = self._min_on - elapsed_slots
                for k in range(min(remaining, n)):
                    # Defer to schedule overrides: a non-auto slot is
                    # already bound by the parent; forcing relay=1 here
                    # would conflict with an "off" override and produce
                    # an infeasible LP.
                    if self._slot_state(slots[k]) != "auto":
                        continue
                    prob += (
                        relay[k] == 1,
                        f"{var_prefix}{self.load_id}_carryover_on_{k}",
                    )
            else:
                # Currently de-asserted — must stay off for at least
                # min_off slots before re-asserting.
                remaining = self._min_off - elapsed_slots
                for k in range(min(remaining, n)):
                    if self._slot_state(slots[k]) != "auto":
                        continue
                    prob += (
                        relay[k] == 0,
                        f"{var_prefix}{self.load_id}_carryover_off_{k}",
                    )

        return vars


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
        elif cfg.category == LoadCategory.SIGNAL_DRIVEN_CONTINUOUS:
            loads.append(BinarySignalDrivenContinuousLoad(cfg))
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
