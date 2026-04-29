"""Rising-edge alert when Amber's 30-min planning horizon shrinks.

Amber's visible horizon usually sits at ~79 30-min intervals (~40 h, the
AEMO pre-dispatch ceiling) but transiently dips to ~30 during AEMO's
daily refresh. The threshold sits between those so the daily blip is
silent and sustained shrinkage emits exactly one rising-edge event.
"""

from __future__ import annotations

from unittest.mock import patch

from optimiser.clients.amber import AmberClient
from optimiser.config import AmberConfig
from optimiser.types import EventType


def _client(threshold: int = 50) -> AmberClient:
    return AmberClient(
        AmberConfig(
            api_key="k",
            site_id="s",
            horizon_alert_threshold_30min=threshold,
        )
    )


class TestHorizonAlert:
    def test_no_event_when_count_at_or_above_threshold(self) -> None:
        c = _client(threshold=50)
        with patch("optimiser.clients.amber.emit") as mock_emit:
            c._check_horizon_alert(79)
            c._check_horizon_alert(50)
        assert mock_emit.call_count == 0
        assert c._horizon_short is False

    def test_emits_short_event_on_first_dip_below_threshold(self) -> None:
        c = _client(threshold=50)
        with patch("optimiser.clients.amber.emit") as mock_emit:
            c._check_horizon_alert(32)
        assert mock_emit.call_count == 1
        evt, payload = mock_emit.call_args.args
        assert evt == EventType.AMBER_HORIZON_SHORT
        assert payload == {"interval_count": 32, "threshold": 50}
        assert c._horizon_short is True

    def test_subsequent_short_fetches_silent_until_recovery(self) -> None:
        """One rising edge — re-arm only after the recovery edge.
        Without this, every fetch during a multi-hour outage would
        spam the event log."""
        c = _client(threshold=50)
        with patch("optimiser.clients.amber.emit") as mock_emit:
            c._check_horizon_alert(32)  # rising edge → SHORT
            c._check_horizon_alert(33)
            c._check_horizon_alert(31)
            c._check_horizon_alert(34)
        assert mock_emit.call_count == 1
        assert mock_emit.call_args.args[0] == EventType.AMBER_HORIZON_SHORT

    def test_emits_recovered_event_when_count_climbs_back(self) -> None:
        c = _client(threshold=50)
        with patch("optimiser.clients.amber.emit") as mock_emit:
            c._check_horizon_alert(32)   # SHORT
            c._check_horizon_alert(50)   # boundary — recovered
        assert mock_emit.call_count == 2
        events = [call.args[0] for call in mock_emit.call_args_list]
        assert events == [
            EventType.AMBER_HORIZON_SHORT,
            EventType.AMBER_HORIZON_RECOVERED,
        ]
        assert c._horizon_short is False

    def test_can_re_emit_short_after_recovery(self) -> None:
        """Two distinct shrinkage incidents → two SHORT events."""
        c = _client(threshold=50)
        with patch("optimiser.clients.amber.emit") as mock_emit:
            c._check_horizon_alert(32)   # SHORT
            c._check_horizon_alert(79)   # RECOVERED
            c._check_horizon_alert(40)   # SHORT again
        events = [call.args[0] for call in mock_emit.call_args_list]
        assert events == [
            EventType.AMBER_HORIZON_SHORT,
            EventType.AMBER_HORIZON_RECOVERED,
            EventType.AMBER_HORIZON_SHORT,
        ]

    def test_threshold_zero_disables_alert(self) -> None:
        """Operator opt-out: setting threshold to 0 silences the
        edge-detection entirely."""
        c = _client(threshold=0)
        with patch("optimiser.clients.amber.emit") as mock_emit:
            c._check_horizon_alert(0)    # would normally fire SHORT
            c._check_horizon_alert(79)
            c._check_horizon_alert(10)
        assert mock_emit.call_count == 0
