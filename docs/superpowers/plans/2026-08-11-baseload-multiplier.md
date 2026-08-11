# Baseload Multiplier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A validated `lp_baseload_multiplier` config knob that scales the baseload forecast the LP plans against, leaving managed loads exact.

**Architecture:** One new `PlannerConfig` field with parse-time bounds validation; one pure helper in `service.py` applied immediately after `build_load_profile()` returns, stamping the profile context when active. Managed loads are separate LP variables and are structurally unreachable from the helper; a constraint-level test pins that.

**Tech Stack:** Python 3.12, dataclasses, pytest. Spec: `docs/superpowers/specs/2026-08-11-baseload-multiplier-design.md`.

## Global Constraints

- Repo conventions (CLAUDE.md): full type hints, no `;` and no em-dash in comments/docstrings, no history-carrying docstrings, comments only where the WHY is non-obvious.
- Commit style: conventional-commit prefix, lowercase subject, no trailing period.
- TDD: every step's test is watched to fail before its implementation exists.
- Test suite invocation: `uv run pytest <file> -q` from the repo root.
- Deploy is NOT part of this plan. First deploy is rebuild path (b) — new `PlannerConfig` field, bind-mounted config naming an unknown field crash-loops (2026-04-29 lesson).

---

### Task 1: Config field, validation, example config

**Files:**
- Modify: `src/optimiser/config.py` (PlannerConfig, ~line 278)
- Modify: `config.example.toml` (planner section, after `lp_load_smoothing_slots`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `PlannerConfig.lp_baseload_multiplier: float` (default 1.0, valid range 0.2–3.0 inclusive, `ValueError` naming the field otherwise). Task 2 consumes this field.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`; add `from optimiser.config import PlannerConfig` and `import pytest` to imports if not present)

```python
class TestBaseloadMultiplier:
    def test_default_is_identity(self) -> None:
        assert PlannerConfig().lp_baseload_multiplier == 1.0

    def test_accepts_in_range(self) -> None:
        assert PlannerConfig(lp_baseload_multiplier=1.3).lp_baseload_multiplier == 1.3

    @pytest.mark.parametrize("bad", [0.1, 5.0, -1.0, 0.0])
    def test_rejects_out_of_range(self, bad: float) -> None:
        with pytest.raises(ValueError, match="lp_baseload_multiplier"):
            PlannerConfig(lp_baseload_multiplier=bad)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py::TestBaseloadMultiplier -q`
Expected: `test_default_is_identity` and `test_accepts_in_range` FAIL with `TypeError: unexpected keyword argument` / `AttributeError`; `test_rejects_out_of_range` FAILS (no ValueError raised).

- [ ] **Step 3: Implement the field and validation** (in `PlannerConfig`, next to `lp_load_statistic`)

```python
    # Manual nudge on the baseload forecast the LP plans against.
    # Managed loads are separate LP variables and are not scaled.
    # >1.0 reserves more (guests, cold snap, distrust of a young
    # profile); <1.0 plans an emptier house (vacation).
    lp_baseload_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if not 0.2 <= self.lp_baseload_multiplier <= 3.0:
            raise ValueError(
                f"lp_baseload_multiplier must be between 0.2 and 3.0, "
                f"got {self.lp_baseload_multiplier!r}"
            )
```

If `PlannerConfig` already has a `__post_init__`, add the check to it instead of defining a second one.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -q`
Expected: all PASS (whole file, to catch collateral damage).

- [ ] **Step 5: Refresh the stale statistic comment**

The comment block above `lp_load_statistic` (config.py ~lines 253–277) still argues median is the better default and says "Defaults preserve historical behaviour". That predates the 2026-08-11 flip to mean (decision log: median under-forecast winter by a third). Replace the block's argument for median with:

```python
    # Per-slot aggregation for the load profile ("mean" | "median") and
    # boxcar smoothing width (odd, 1 = off).
    #
    # Mean matches realised daily energy. Median under-forecasts
    # right-skewed load (winter 2026: sum of slot medians planned
    # 24-25 kWh/day against 37 realised) because it discards recurring
    # peak mass. Single-outlier-day protection comes from smoothing
    # plus the hard SOC floor, not the statistic. See the CLAUDE.md
    # decision log entry for the sweep evidence.
```

- [ ] **Step 6: Add the example-config entry** (in `config.example.toml`, directly after `lp_load_smoothing_slots = 3`)

```toml
# Manual nudge on the baseload forecast the LP plans against. Managed
# loads are planned exactly and are not scaled. >1.0 reserves more
# (guests, cold snap); <1.0 plans an emptier house (vacation mode).
# Stacks with presence detection: the away profile, when active, is
# scaled too. Range 0.2-3.0, validated at startup. The profile context
# is stamped (e.g. "cold+occ+wd x0.6") whenever the value is not 1.0.
lp_baseload_multiplier = 1.0
```

- [ ] **Step 7: Commit**

```bash
git add src/optimiser/config.py config.example.toml tests/test_config.py
git commit -m "feat(planner): lp_baseload_multiplier config knob with bounds validation"
```

---

### Task 2: Apply the multiplier in the service

**Files:**
- Modify: `src/optimiser/service.py` (module-level helper + call site after `build_load_profile(...)`, ~line 435)
- Test: `tests/test_service_lp.py`

**Interfaces:**
- Consumes: `PlannerConfig.lp_baseload_multiplier` (Task 1).
- Produces: `apply_baseload_multiplier(profile: LoadProfile, multiplier: float) -> LoadProfile` — module-level pure function in `service.py`. Identity returns the same object; otherwise slots scaled and context stamped `" x{multiplier:g}"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_service_lp.py`; imports: `from optimiser.service import apply_baseload_multiplier`, `from optimiser.types import LoadProfile`, `import pytest` as needed)

```python
class TestApplyBaseloadMultiplier:
    def test_scales_slots_and_stamps_context(self) -> None:
        profile = LoadProfile(slots=[1.0] * 48, maturity_level=3, context="cold+occ+wd")
        out = apply_baseload_multiplier(profile, 1.3)
        assert out.slots == pytest.approx([1.3] * 48)
        assert out.context == "cold+occ+wd x1.3"
        assert out.maturity_level == 3

    def test_identity_returns_profile_unchanged(self) -> None:
        profile = LoadProfile(slots=[1.0] * 48, maturity_level=3, context="cold+occ+wd")
        assert apply_baseload_multiplier(profile, 1.0) is profile
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_service_lp.py::TestApplyBaseloadMultiplier -q`
Expected: FAIL at import (`ImportError: cannot import name 'apply_baseload_multiplier'`).

- [ ] **Step 3: Implement the helper** (module level in `service.py`; add `import dataclasses` if absent — check existing imports first)

```python
def apply_baseload_multiplier(profile: LoadProfile, multiplier: float) -> LoadProfile:
    """Scale the non-managed baseload forecast by the operator's nudge.

    Managed loads are separate LP variables and are unaffected. The
    context stamp marks nudged plans in snapshots and /explain-plan.
    """
    if multiplier == 1.0:
        return profile
    return dataclasses.replace(
        profile,
        slots=[s * multiplier for s in profile.slots],
        context=f"{profile.context} x{multiplier:g}",
    )
```

`LoadProfile` may already be imported in `service.py`; if not, add it to the existing `from .types import ...` line.

- [ ] **Step 4: Wire the call site** (immediately after the `build_load_profile(...)` call, ~line 443)

```python
        load_profile = apply_baseload_multiplier(
            load_profile, self._config.planner.lp_baseload_multiplier
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_service_lp.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/optimiser/service.py tests/test_service_lp.py
git commit -m "feat(planner): apply baseload multiplier to the LP load profile"
```

---

### Task 3: Pin managed-load exactness

**Files:**
- Modify: `tests/test_lp_hw_scheduling.py` (`_build` helper + new test)
- Modify: `docs/superpowers/specs/2026-08-11-baseload-multiplier-design.md` (one test-wording fix)

**Interfaces:**
- Consumes: `_build(state, load_status, daily_target_kwh=4.0, draw_kw=1.0)` and `_constraint_rhs_by_day(prob)` in `tests/test_lp_hw_scheduling.py`, plus its `_flat_profile()` file-local helper.

- [ ] **Step 1: Parameterise the profile in `_build`**

Add a `profile_kw: float = 2.0` parameter to `_build` and pass it through to the profile helper. If `_flat_profile()` takes no argument, give it one: `def _flat_profile(kw: float = 2.0)` scaling its slots accordingly, and change `_build`'s call to `load_profile=_flat_profile(profile_kw)`. Existing callers are unaffected (default preserves current values).

- [ ] **Step 2: Write the failing test** (append to `tests/test_lp_hw_scheduling.py`)

```python
class TestBaseloadNudgeExactness:
    def test_profile_scale_does_not_change_managed_target(self) -> None:
        """A nudged baseload forecast must not scale managed-load
        demand: the daily-target constraint RHS is identical whatever
        the profile says. Placement may shift; the delivered total may
        not."""
        rhs_base = _constraint_rhs_by_day(
            _build(_state(NOW_MORNING), _hw_status(energy_today_kwh=0.0))
        )
        rhs_nudged = _constraint_rhs_by_day(
            _build(_state(NOW_MORNING), _hw_status(energy_today_kwh=0.0), profile_kw=3.0)
        )
        assert rhs_nudged == rhs_base
```

- [ ] **Step 3: Run the test — it should PASS immediately**

Run: `uv run pytest tests/test_lp_hw_scheduling.py::TestBaseloadNudgeExactness -q`

This is a pin, not a behaviour change: the property already holds and the test guards it. To watch it fail meaningfully (TDD's verify-the-test-can-fail), temporarily change `rhs_nudged == rhs_base` to `rhs_nudged != rhs_base`, run, confirm FAIL, revert. Do not skip this inversion check.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest tests/ -q`
Expected: all PASS (~735).

- [ ] **Step 5: Fix the spec's over-strong test wording**

In `docs/superpowers/specs/2026-08-11-baseload-multiplier-design.md`, testing item 4 says "the managed-load plan in the LP solution is unchanged". Too strong: a bigger baseload can legitimately move WHEN the relay runs. Replace item 4 with:

```markdown
4. Managed-load exactness: with a multiplier set, the managed-load
   demand constraint (daily target) is unchanged versus multiplier
   1.0. Placement within the window may shift; the delivered total
   may not.
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_lp_hw_scheduling.py docs/superpowers/specs/2026-08-11-baseload-multiplier-design.md
git commit -m "test(lp): pin managed-load target invariance under baseload nudge"
```
