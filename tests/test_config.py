"""Tests for TOML configuration loading."""

from __future__ import annotations

import tempfile

from optimiser.config import load_config
from optimiser.types import LoadCategory

MINIMAL_CONFIG = """
[amber]
api_key = "test-key"
site_id = "test-site"

[solcast]
enabled = false

[sigenergy]
host = "192.168.1.100"

[battery]
capacity_kwh = 40.0

[[managed_load]]
load_id = "hot_water"
category = "shiftable"
shelly_host = "192.168.1.101"
has_relay = true
daily_energy_kwh = 3.0
draw_kw = 1.2
cycle_duration_min = 90

[[managed_load]]
load_id = "mains"
category = "observable"
shelly_host = "192.168.1.101"
shelly_channel = 1

[weather]
[occupancy]
[storage]
[planner]
"""


class TestConfigLoading:
    def test_loads_minimal_config(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(MINIMAL_CONFIG)
            f.flush()
            config = load_config(f.name)

        assert config.amber.api_key == "test-key"
        assert config.amber.site_id == "test-site"
        assert config.sigenergy.host == "192.168.1.100"
        assert config.sigenergy.port == 502  # default
        assert config.battery.capacity_kwh == 40.0
        assert not config.solcast.enabled

    def test_loads_managed_loads(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(MINIMAL_CONFIG)
            f.flush()
            config = load_config(f.name)

        assert len(config.managed_loads) == 2
        hw = config.managed_loads[0]
        assert hw.load_id == "hot_water"
        assert hw.category == LoadCategory.SHIFTABLE
        assert hw.has_relay is True
        assert hw.daily_energy_kwh == 3.0

        mains = config.managed_loads[1]
        assert mains.load_id == "mains"
        assert mains.category == LoadCategory.OBSERVABLE
        assert mains.shelly_channel == 1
        assert mains.has_relay is False

    def test_defaults_applied(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(MINIMAL_CONFIG)
            f.flush()
            config = load_config(f.name)

        assert config.planner.tick_interval_s == 60
        assert config.planner.telemetry_write_interval_s == 300
        assert config.planner.lp_wall_clock_timeout_s == 12.0
        assert config.battery.round_trip_efficiency == 0.90
        assert config.occupancy.away_threshold_min == 30
