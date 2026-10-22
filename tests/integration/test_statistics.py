"""Tests for the statistics the coordinator imports, against the real recorder.

The API is replaced by a fake that serves synthetic daily reads and load
curves through the real parsing code, so the whole path from Life's JSON to
the recorder is exercised.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.recorder.util import get_instance
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.gazdebordeaux import coordinator as coordinator_module
from custom_components.gazdebordeaux.coordinator import GdbCoordinator
from custom_components.gazdebordeaux.gazdebordeaux import (
    Gazdebordeaux,
    HouseDetails,
    OffPeakTimes,
    paris_tz,
)

CONSUMPTION = "gazdebordeaux:energy_consumption"
COST = "gazdebordeaux:energy_cost"
HC = "gazdebordeaux:energy_consumption_hc"
COST_HP = "gazdebordeaux:energy_cost_hp"
ALL_IDS = {
    COST,
    CONSUMPTION,
    "gazdebordeaux:volume",
    "gazdebordeaux:energy_consumption_hp",
    HC,
    COST_HP,
    "gazdebordeaux:energy_cost_hc",
}

# A day: 12 kWh, 0.25 kWh per half hour, 4 of them between 22:00 and 6:00
P_HP, P_HC, SUBSCRIPTION = 0.177, 0.137, 0.52
DAY_PRICE = 8 * P_HP + 4 * P_HC + SUBSCRIPTION
OFFPEAK = OffPeakTimes(all=[["22:00", "6:00"]], high=[], low=[])


class FakeApi(Gazdebordeaux):
    """Serve synthetic Life responses to the real parsing code."""

    def __init__(self, hourly_start: date | None, offpeak: OffPeakTimes | None = OFFPEAK):
        super().__init__(None, "user@example.com", "secret", token="token", house="/houses/a")  # type: ignore[arg-type]
        self.today = datetime.now(paris_tz).date()
        self.last_day = self.today - timedelta(days=2)
        self.hourly_start = hourly_start
        self.offpeak = offpeak
        self.fail_hourly = False
        self.hourly_requests = 0
        # Last day with a load curve; later days answer without points
        self.hourly_until = self.last_day

    async def async_login(self) -> None:
        return None

    async def async_get_house_details(self) -> HouseDetails:
        return HouseDetails(offpeak=self.offpeak, hourly_start=self.hourly_start)

    async def async_get_data(self, start: Any, end: Any, scale: str) -> Any:
        if scale == "month":
            day = start.date()
            data: dict[str, Any] = {"total": {}}
            while day <= min(end.date(), self.last_day):
                data[day.isoformat()] = {
                    "kwh": 12.0,
                    "price": DAY_PRICE,
                    "volumeOfEnergy": 0,
                    "ratio": 0,
                    "temperature": 12,
                    "isHphc": True,
                    "consumptionHp": 8.0,
                    "consumptionHc": 4.0,
                    "priceHp": 8 * P_HP,
                    "priceHc": 4 * P_HC,
                    "contractPrice": SUBSCRIPTION,
                }
                day += timedelta(days=1)
            return data
        assert scale == "day"
        self.hourly_requests += 1
        if self.fail_hourly:
            raise Exception("Data response was not JSON (status=403)")
        day = start.date()
        if day > min(self.last_day, self.hourly_until):
            return {"total": {}}
        midnight = datetime(day.year, day.month, day.day)
        return {
            (midnight + timedelta(minutes=30 * i)).strftime("%Y-%m-%d %H:%M:%S"): {
                "kwh": 0.5,
                "price": 0,
                "volumeOfEnergy": 0,
            }
            for i in range(49)
        }


@pytest.fixture(autouse=True)
def _no_request_delay(monkeypatch):
    monkeypatch.setattr(coordinator_module, "HOURLY_REQUEST_DELAY", 0)


def _coordinator(hass: HomeAssistant, api: FakeApi) -> GdbCoordinator:
    coordinator = GdbCoordinator(hass, {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "x"})
    coordinator.api = api
    return coordinator


async def _rows(hass: HomeAssistant) -> dict[str, list[dict]]:
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2000, 1, 1, tzinfo=paris_tz),
        None,
        ALL_IDS,
        "hour",
        None,
        {"state", "sum"},
    )
    return {k: list(v) for k, v in stats.items()}


def _days(rows: list[dict]) -> dict[date, list[dict]]:
    by_day: dict[date, list[dict]] = {}
    for row in rows:
        by_day.setdefault(datetime.fromtimestamp(row["start"], paris_tz).date(), []).append(row)
    return by_day


async def test_first_import_hourly_since_curve_start(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(paris_tz).date() - timedelta(days=10))
    await _coordinator(hass, api)._insert_statistics()
    rows = await _rows(hass)

    assert set(rows) == ALL_IDS
    days = _days(rows[CONSUMPTION])
    hourly_days = sorted(d for d, r in days.items() if len(r) > 1)
    assert hourly_days[0] == api.hourly_start
    assert hourly_days[-1] == api.last_day
    assert all(len(days[d]) == 24 for d in hourly_days)
    first_day = date(api.today.year - 1, 1, 1)
    day_count = (api.last_day - first_day).days + 1
    assert rows[CONSUMPTION][-1]["sum"] == pytest.approx(12.0 * day_count)
    assert rows[HC][-1]["sum"] == pytest.approx(4.0 * day_count)
    assert rows[COST][-1]["sum"] == pytest.approx(DAY_PRICE * day_count)
    # Each hour at its own rate, the subscription spread over the day
    noon = next(r for r in _days(rows[COST])[api.last_day] if r["start"] % 86400 == 10 * 3600)
    assert noon["state"] == pytest.approx(0.5 * P_HP + SUBSCRIPTION / 24)


async def test_second_update_adds_nothing_twice(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(paris_tz).date() - timedelta(days=5))
    coordinator = _coordinator(hass, api)
    await coordinator._insert_statistics()
    before = await _rows(hass)
    await coordinator._insert_statistics()
    after = await _rows(hass)

    assert after[CONSUMPTION][-1]["sum"] == pytest.approx(before[CONSUMPTION][-1]["sum"])
    assert len(after[CONSUMPTION]) == len(before[CONSUMPTION])


async def test_failed_rebuild_keeps_the_existing_statistics(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(paris_tz).date() - timedelta(days=5))
    coordinator = _coordinator(hass, api)
    await coordinator._insert_statistics()
    before = await _rows(hass)

    coordinator.reset = True
    api.fail_hourly = True
    with pytest.raises(Exception, match="not JSON"):
        await coordinator._insert_statistics()
    after = await _rows(hass)

    assert len(after[CONSUMPTION]) == len(before[CONSUMPTION])
    assert after[CONSUMPTION][-1]["sum"] == pytest.approx(before[CONSUMPTION][-1]["sum"])
    assert coordinator.reset is True


async def test_missing_offpeak_ranges_skip_the_update(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(paris_tz).date() - timedelta(days=5))
    coordinator = _coordinator(hass, api)
    await coordinator._insert_statistics()
    before = await _rows(hass)

    # One more day published, but the house comes back without its ranges
    api.last_day += timedelta(days=1)
    api.offpeak = None
    await coordinator._insert_statistics()
    after = await _rows(hass)

    assert after[CONSUMPTION][-1]["start"] == before[CONSUMPTION][-1]["start"]
    assert after[HC][-1]["start"] == before[HC][-1]["start"]


async def test_reset_on_empty_database_is_not_replayed(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(paris_tz).date() - timedelta(days=3))
    coordinator = _coordinator(hass, api)
    coordinator.reset = True
    await coordinator._insert_statistics()
    assert coordinator.reset is False

    requests = api.hourly_requests
    await coordinator._insert_statistics()
    # No second full rebuild: only the last day is asked again
    assert api.hourly_requests - requests <= 2


async def test_resume_after_a_daily_read_with_a_utc_process(
    hass: HomeAssistant, monkeypatch
) -> None:
    # Day boundaries must not depend on the time zone HA's process runs in
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        api = FakeApi(hourly_start=None)
        api.hourly_until = date(2000, 1, 1)  # no load curve yet: daily reads only
        coordinator = _coordinator(hass, api)
        await coordinator._insert_statistics()
        first = await _rows(hass)
        assert all(len(r) == 1 for r in _days(first[CONSUMPTION]).values())

        api.hourly_until = api.last_day  # the curve shows up
        await coordinator._insert_statistics()
        rows = await _rows(hass)
    finally:
        monkeypatch.undo()
        time.tzset()

    first_day = date(api.today.year - 1, 1, 1)
    day_count = (api.last_day - first_day).days + 1
    assert rows[CONSUMPTION][-1]["sum"] == pytest.approx(12.0 * day_count)
    for day, day_rows in _days(rows[CONSUMPTION]).items():
        assert sum(r["state"] for r in day_rows) == pytest.approx(12.0), day
