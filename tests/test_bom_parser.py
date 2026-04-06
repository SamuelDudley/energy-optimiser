"""Tests for BOM defensive parsing (issue #15).

The `_parse_temperature` method is exercised directly with hand-built
malformed payloads — no HTTP needed.
"""

from __future__ import annotations

from optimiser.clients.bom import BOMClient
from optimiser.config import WeatherConfig


def _client() -> BOMClient:
    return BOMClient(WeatherConfig())


class TestBOMParser:
    def test_well_formed_response(self) -> None:
        c = _client()
        data = {
            "observations": {
                "data": [
                    {"air_temp": 18.5, "local_date_time_full": "20260403170000"},
                    {"air_temp": 18.0, "local_date_time_full": "20260403163000"},
                ]
            }
        }
        assert c._parse_temperature(data) == 18.5

    def test_top_level_not_a_dict(self, capsys) -> None:
        c = _client()
        assert c._parse_temperature("string-not-dict") is None
        captured = capsys.readouterr()
        assert "validation_warning" in captured.out.lower()
        assert "not a json object" in captured.out.lower()

    def test_top_level_is_a_list(self, capsys) -> None:
        c = _client()
        assert c._parse_temperature([{"air_temp": 18.5}]) is None
        captured = capsys.readouterr()
        assert "not a json object" in captured.out.lower()

    def test_observations_key_missing(self, capsys) -> None:
        c = _client()
        assert c._parse_temperature({"forecast": {}}) is None
        captured = capsys.readouterr()
        assert "observations" in captured.out.lower()

    def test_observations_wrong_type(self, capsys) -> None:
        c = _client()
        assert c._parse_temperature({"observations": "not-a-dict"}) is None
        captured = capsys.readouterr()
        assert "observations" in captured.out.lower()

    def test_data_key_missing(self, capsys) -> None:
        c = _client()
        assert c._parse_temperature({"observations": {}}) is None
        captured = capsys.readouterr()
        assert "observations.data" in captured.out.lower()

    def test_data_wrong_type(self, capsys) -> None:
        c = _client()
        assert c._parse_temperature({"observations": {"data": "not-a-list"}}) is None

    def test_empty_observations_list(self) -> None:
        """Empty data is not malformed — just nothing to report yet."""
        c = _client()
        assert c._parse_temperature({"observations": {"data": []}}) is None

    def test_skips_null_air_temp(self) -> None:
        """First entry has null temp, second has a real value."""
        c = _client()
        data = {
            "observations": {
                "data": [
                    {"air_temp": None, "local_date_time_full": "20260403170000"},
                    {"air_temp": 17.3, "local_date_time_full": "20260403163000"},
                ]
            }
        }
        assert c._parse_temperature(data) == 17.3

    def test_skips_missing_air_temp_key(self) -> None:
        c = _client()
        data = {
            "observations": {
                "data": [
                    {"local_date_time_full": "20260403170000"},  # no air_temp key
                    {"air_temp": 17.3},
                ]
            }
        }
        assert c._parse_temperature(data) == 17.3

    def test_skips_non_numeric_air_temp(self) -> None:
        c = _client()
        data = {
            "observations": {
                "data": [
                    {"air_temp": "n/a"},
                    {"air_temp": 17.3},
                ]
            }
        }
        assert c._parse_temperature(data) == 17.3

    def test_skips_non_dict_entries(self) -> None:
        """Entries that aren't dicts don't crash the walk."""
        c = _client()
        data = {
            "observations": {
                "data": [
                    None,
                    "string-entry",
                    {"air_temp": 17.3},
                ]
            }
        }
        assert c._parse_temperature(data) == 17.3

    def test_all_invalid_returns_none(self) -> None:
        """List of records but no valid temp anywhere."""
        c = _client()
        data = {
            "observations": {
                "data": [
                    {"air_temp": None},
                    {"air_temp": "broken"},
                    {"other_field": 1},
                ]
            }
        }
        assert c._parse_temperature(data) is None

    def test_string_air_temp_parses(self) -> None:
        """BOM sometimes returns numeric strings — we accept those."""
        c = _client()
        data = {"observations": {"data": [{"air_temp": "18.5"}]}}
        assert c._parse_temperature(data) == 18.5
