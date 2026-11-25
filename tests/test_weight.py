from datetime import date
from typing import Any
from unittest.mock import Mock

import pytest

from dreeve_garmin_connector.weight import DreeveWeightClient, WeightSync, WeightSyncError, latest_daily_weights
from dreeve_garmin_connector.window import Window
from tests.stubs import FakeGarminClient, StubGarminApi
from tests.test_sync import harness_for

WINDOW = Window(date(2026, 11, 24), date(2026, 11, 25))


class WeightApi(StubGarminApi):
    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__()
        self.response = response
        self.weight_requests: list[tuple[str, str]] = []

    def get_weigh_ins(self, startdate: str, enddate: str) -> dict[str, Any]:
        self.weight_requests.append((startdate, enddate))
        return self.response


class Destination:
    def __init__(self, *, enabled: bool = True, error: Exception | None = None) -> None:
        self.enabled = enabled
        self.error = error
        self.deliveries: list[list[dict[str, str | float]]] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def deliver(self, weights: list[dict[str, str | float]]) -> None:
        if self.error:
            raise self.error
        self.deliveries.append(weights)


def test_grams_are_converted_to_kg_and_latest_measurement_wins_per_local_day() -> None:
    result = latest_daily_weights(
        {
            "dateWeightList": [
                {"calendarDate": "2026-11-25", "date": 2000, "weight": 70125},
                {"calendarDate": "2026-11-25", "date": 1000, "weight": 71000},
                {"calendarDate": "2026-11-24", "date": 900, "weight": 70500},
            ]
        },
        WINDOW,
    )
    assert result == [{"on": "2026-11-24", "weightKg": 70.5}, {"on": "2026-11-25", "weightKg": 70.125}]


def test_invalid_and_out_of_window_measurements_are_ignored() -> None:
    assert (
        latest_daily_weights(
            {
                "dateWeightList": [
                    {"calendarDate": "2026-11-23", "weight": 70125},
                    {"calendarDate": "2026-02-29", "weight": 70125},
                    {"calendarDate": "2026-11-25", "weight": -1},
                    {"calendarDate": "2026-11-25", "weight": float("nan")},
                    {"calendarDate": "2026-11-25", "weight": "invalid"},
                ]
            },
            WINDOW,
        )
        == []
    )


def test_range_response_uses_latest_measurement_in_daily_weight_summaries() -> None:
    response = {
        "dailyWeightSummaries": [
            {
                "summaryDate": "2026-11-25",
                "numOfWeightEntries": 2,
                "latestWeight": {"calendarDate": "2026-11-25", "date": 2000, "weight": 70125},
                "allWeightMetrics": [
                    {"calendarDate": "2026-11-25", "date": 2000, "weight": 70125},
                    {"calendarDate": "2026-11-25", "date": 1000, "weight": 71000},
                ],
            }
        ],
        "totalAverage": {"weight": 70562.5},
    }
    assert latest_daily_weights(response, WINDOW) == [{"on": "2026-11-25", "weightKg": 70.125}]


def test_range_response_accepts_latest_weight_with_summary_calendar_date() -> None:
    response = {
        "dailyWeightSummaries": [{"summaryDate": "2026-11-25", "latestWeight": {"date": 2000, "weight": 70125}}]
    }
    assert latest_daily_weights(response, WINDOW) == [{"on": "2026-11-25", "weightKg": 70.125}]


def test_empty_range_response_contains_no_weights() -> None:
    assert latest_daily_weights({"dailyWeightSummaries": []}, WINDOW) == []


def test_unknown_weight_response_is_an_error_instead_of_silent_success() -> None:
    with pytest.raises(WeightSyncError, match="unrecognized"):
        latest_daily_weights({"unexpected": []}, WINDOW)


def test_disabled_sync_does_not_read_garmin_weight() -> None:
    api = WeightApi({})
    destination = Destination(enabled=False)
    WeightSync(api, destination).run_once(WINDOW)
    assert api.weight_requests == []
    assert destination.deliveries == []


def test_sync_delivers_daily_weights_without_activity_downloads() -> None:
    api = WeightApi(
        {
            "dailyWeightSummaries": [
                {"summaryDate": "2026-11-25", "allWeightMetrics": [{"calendarDate": "2026-11-25", "weight": 70125}]}
            ]
        }
    )
    destination = Destination()
    WeightSync(api, destination).run_once(WINDOW)
    assert api.weight_requests == [("2026-11-24", "2026-11-25")]
    assert destination.deliveries == [[{"on": "2026-11-25", "weightKg": 70.125}]]
    assert api.downloads == []


def test_empty_day_does_not_post_an_empty_batch() -> None:
    destination = Destination()
    WeightSync(WeightApi({"dateWeightList": []}), destination).run_once(WINDOW)
    assert destination.deliveries == []


def test_weight_sync_runs_on_every_activity_cycle_even_without_new_activities(tmp_path: Any) -> None:
    harness = harness_for(tmp_path, FakeGarminClient())
    weight_sync = Mock(spec=WeightSync)
    harness.sync._weight_sync = weight_sync
    harness.sync.run_once()
    harness.sync.run_once()
    assert weight_sync.run_once.call_count == 2


def test_dry_run_does_not_deliver_weights(tmp_path: Any) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(), DRY_RUN="true")
    weight_sync = Mock(spec=WeightSync)
    harness.sync._weight_sync = weight_sync
    harness.sync.run_once()
    weight_sync.run_once.assert_not_called()


def test_weight_failure_does_not_advance_the_successful_sync_window(tmp_path: Any) -> None:
    harness = harness_for(tmp_path, FakeGarminClient())
    weight_sync = Mock(spec=WeightSync)
    weight_sync.run_once.side_effect = WeightSyncError("Dreeve offline")
    harness.sync._weight_sync = weight_sync
    with pytest.raises(WeightSyncError):
        harness.sync.run_once()
    assert harness.ledger.last_successful_sync is None


def test_http_client_uses_bearer_auth_and_posts_kilograms() -> None:
    client = DreeveWeightClient("http://dreeve:8080", "private-key")
    response = Mock()
    response.read.return_value = b'{"imported":1}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    client._opener = opener
    client.deliver([{"on": "2026-11-25", "weightKg": 70.125}])
    request = opener.open.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer private-key"
    assert request.full_url == "http://dreeve:8080/api/v1/athlete/weights/garmin"
    assert request.data == b'{"weights": [{"on": "2026-11-25", "weightKg": 70.125}]}'
