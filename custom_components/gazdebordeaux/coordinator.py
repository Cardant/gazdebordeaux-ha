"""Coordinator to handle Opower connections."""

import logging
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any, cast

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    CURRENCY_EURO,
    UnitOfEnergy,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, HOUSE, RESET_STATISTICS
from .gazdebordeaux import DailyUsageRead, Gazdebordeaux, TotalUsageRead

_LOGGER = logging.getLogger(__name__)

# Days imported hour by hour on the first import (one API request per day)
HOURLY_DAYS = 30
# Hourly data of the last days may not be published yet: wait for it
HOURLY_GRACE_DAYS = 3


class GdbCoordinator(DataUpdateCoordinator[TotalUsageRead]):
    """Handle fetching GazdeBordeaux data, updating sensors and inserting statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: MappingProxyType[str, Any],
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name="gazdebordeaux",
            # Data is updated daily.
            # Refresh every 12h to be at most 12h behind.
            update_interval=timedelta(hours=12),
        )

        # Initialisation de la date de dernière actualisation
        self.last_update: datetime | None = None

        house: Any = None
        if HOUSE in entry_data:
            house = entry_data[HOUSE]

        self.api = Gazdebordeaux(
            aiohttp_client.async_get_clientsession(hass),
            entry_data[CONF_USERNAME],
            entry_data[CONF_PASSWORD],
            None,
            house,
        )
        self.reset = False
        if RESET_STATISTICS in entry_data:
            self.reset = bool(entry_data[RESET_STATISTICS])
            if self.reset:
                _LOGGER.debug("Asked to reset all statistics...")
                entries = self.hass.config_entries.async_entries(DOMAIN)

                _LOGGER.debug("Updating config...")
                self.hass.config_entries.async_update_entry(
                    entries[0],
                    data={
                        CONF_USERNAME: entry_data[CONF_USERNAME],
                        CONF_PASSWORD: entry_data[CONF_PASSWORD],
                        RESET_STATISTICS: False,
                        HOUSE: house,
                    },
                )

        @callback
        def _dummy_listener() -> None:
            pass

        # Force the coordinator to periodically update by registering at least one listener.
        # Needed when the _async_update_data below returns {} for utilities that don't provide
        # forecast, which results to no sensors added, no registered listeners, and thus
        # _async_update_data not periodically getting called which is needed for _insert_statistics.
        self.async_add_listener(_dummy_listener)

    async def _async_update_data(
        self,
    ) -> TotalUsageRead:
        """Fetch data from API endpoint."""
        try:
            # Login expires after a few minutes.
            # Given the infrequent updating (every 12h)
            # assume previous session has expired and re-login.
            await self.api.async_login()
        except Exception as err:
            raise ConfigEntryAuthFailed from err

        total_usage: TotalUsageRead = await self.api.async_get_total_usage()

        # Because Opower provides historical usage/cost with a delay of a couple of days
        # we need to insert data into statistics.
        await self._insert_statistics()

        # Mise à jour de la date de dernière actualisation
        self.last_update = datetime.now()
        _LOGGER.debug("Last update: %s", self.last_update.strftime("%Y-%m-%d %H:%M:%S"))

        return total_usage

    async def _insert_statistics(self) -> None:
        """Insert gdb statistics."""
        cost_statistic_id = f"{DOMAIN}:energy_cost"
        consumption_statistic_id = f"{DOMAIN}:energy_consumption"
        volume_statistic_id = f"{DOMAIN}:volume"
        _LOGGER.debug(
            "Updating Statistics for %s, %s and %s",
            cost_statistic_id,
            consumption_statistic_id,
            volume_statistic_id,
        )

        if self.reset:
            _LOGGER.debug("Resetting all statistics...")

        all_ids = [cost_statistic_id, consumption_statistic_id, volume_statistic_id]
        last_stats = {}
        for statistic_id in all_ids:
            last_stats.update(
                await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
                )
            )

        last_stat_ts = None
        if consumption_statistic_id in last_stats:
            last_stat_ts = last_stats[consumption_statistic_id][0]["start"]
            # Rows written before the time zone fix start at hh:51 (Paris mean
            # time): rebuild the whole series once, on the hour.
            if datetime.fromtimestamp(last_stat_ts).minute != 0:
                _LOGGER.warning("Rebuilding Gaz de Bordeaux statistics on whole hours")
                get_instance(self.hass).async_clear_statistics(all_ids)
                last_stat_ts = None
            elif self.reset:
                # The "reset statistics" option: rebuild everything, with the
                # last HOURLY_DAYS days hour by hour
                _LOGGER.warning("Rebuilding Gaz de Bordeaux statistics on request")
                get_instance(self.hass).async_clear_statistics(all_ids)
                last_stat_ts = None
            self.reset = False

        if last_stat_ts is None:
            _LOGGER.debug("Updating statistic for the first time")
            daily_reads = await self._async_get_all_data()
            cost_sum = consumption_sum = volume_sum = 0.0
            hourly_from = datetime.now().date() - timedelta(days=HOURLY_DAYS)
        else:
            last_stat_date = datetime.fromtimestamp(last_stat_ts)
            _LOGGER.debug("Last stat found for %s...", last_stat_date.strftime("%Y-%m-%d %H:%M"))
            daily_reads = await self._async_get_recent_usage_reads(last_stat_ts)
            cost_sum = cast(float, last_stats[cost_statistic_id][0]["sum"])
            consumption_sum = cast(float, last_stats[consumption_statistic_id][0]["sum"])
            volume_sum = cast(float, last_stats[volume_statistic_id][0]["sum"])
            hourly_from = last_stat_date.date()
            if last_stat_date.hour == 0:
                # A daily read sits at midnight: that day is complete
                hourly_from += timedelta(days=1)

        usage_reads = await self._async_with_hourly_reads(daily_reads, hourly_from)
        if not usage_reads:
            _LOGGER.debug("No recent usage/cost data. Skipping update")
            return

        cost_statistics = []
        consumption_statistics = []
        volume_statistics = []

        for usage_read in usage_reads:
            start = usage_read.date
            if last_stat_ts is not None and (
                start.timestamp() <= last_stat_ts or start.date() < hourly_from
            ):
                _LOGGER.debug("Skipping data for %s (timestamp)", start.strftime("%Y-%m-%d %H:%M"))
                continue

            _LOGGER.debug("Importing data for %s...", start.strftime("%Y-%m-%d %H:%M"))

            cost_sum += usage_read.price
            consumption_sum += usage_read.amountOfEnergy
            volume_sum += usage_read.volumeOfEnergy

            cost_statistics.append(StatisticData(start=start, state=usage_read.price, sum=cost_sum))
            consumption_statistics.append(
                StatisticData(start=start, state=usage_read.amountOfEnergy, sum=consumption_sum)
            )
            volume_statistics.append(
                StatisticData(start=start, state=usage_read.volumeOfEnergy, sum=volume_sum)
            )

        name_prefix = " ".join(("Gaz de Bordeaux",))

        cost_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            unit_class=None,  # HA has no monetary unit class
            has_sum=True,
            name=f"{name_prefix} cost",
            source=DOMAIN,
            statistic_id=cost_statistic_id,
            unit_of_measurement=CURRENCY_EURO,
        )
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            unit_class="energy",
            has_sum=True,
            name=f"{name_prefix} consumption",
            source=DOMAIN,
            statistic_id=consumption_statistic_id,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        volume_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            unit_class="volume",
            has_sum=True,
            name=f"{name_prefix} volume",
            source=DOMAIN,
            statistic_id=volume_statistic_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )

        async_add_external_statistics(self.hass, cost_metadata, cost_statistics)
        async_add_external_statistics(self.hass, consumption_metadata, consumption_statistics)
        async_add_external_statistics(self.hass, volume_metadata, volume_statistics)

    async def _async_get_all_data(self) -> list[DailyUsageRead]:
        """Get all cost reads since account activation, at different resolutions by age.

        - month resolution for all years (since account activation)
        - day resolution for past 3 years (if account's read resolution supports it)
        - hour resolution for past 2 months (if account's read resolution supports it)
        """
        usage_reads = []

        # if start=None it will only default to beginning of current year, let's import 1 year more
        start = datetime(datetime.today().year - 1, 1, 1)
        end = datetime.now()
        usage_reads = await self.api.async_get_daily_usage(start, end)
        return usage_reads

    async def _async_with_hourly_reads(
        self, daily_reads: list[DailyUsageRead], hourly_from: date
    ) -> list[DailyUsageRead]:
        """Replace the daily reads from hourly_from on by hourly reads.

        A day whose hours do not add up to its daily read keeps the daily
        read. A recent day without hours yet stops the import there, so the
        next update picks it up hour by hour once Enedis has published it.
        """
        reads: list[DailyUsageRead] = []
        recent = datetime.now().date() - timedelta(days=HOURLY_GRACE_DAYS)
        for daily in sorted(daily_reads, key=lambda r: r.date):
            day = daily.date.date()
            if day < hourly_from:
                reads.append(daily)
                continue
            hourly = await self.api.async_get_hourly_usage(day)
            if not hourly:
                if day >= recent:
                    _LOGGER.debug("No hourly data yet for %s, waiting for it", day)
                    break
                _LOGGER.debug("No hourly data for %s, keeping the daily read", day)
                reads.append(daily)
                continue
            energy = sum(r.amountOfEnergy for r in hourly)
            if abs(energy - daily.amountOfEnergy) > 0.1 * daily.amountOfEnergy + 0.05:
                _LOGGER.warning(
                    "Hourly reads of %s add up to %.3f kWh, daily read says %.3f: "
                    "keeping the daily read",
                    day,
                    energy,
                    daily.amountOfEnergy,
                )
                reads.append(daily)
                continue
            # Spread the daily cost by consumption when the hours carry none
            if not any(r.price for r in hourly) and daily.price and energy:
                for r in hourly:
                    r.price = daily.price * r.amountOfEnergy / energy
            reads.extend(hourly)
        return reads

    async def _async_get_recent_usage_reads(self, last_stat_time: float) -> list[DailyUsageRead]:
        """Get cost reads within the past 30 days to allow corrections in data from utilities."""
        return await self.api.async_get_daily_usage(
            # datetime.fromtimestamp(last_stat_time) - timedelta(days=30),
            datetime.fromtimestamp(last_stat_time),
            datetime.now(),
        )
