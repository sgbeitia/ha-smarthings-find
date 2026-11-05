"""DataUpdateCoordinator de SmartThings Find."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SmartThingsFindAuthError,
    SmartThingsFindClient,
    SmartThingsFindError,
    StfDevice,
    StfDeviceState,
)
from .const import (
    CONF_ACTIVE_MODE_OTHERS,
    CONF_ACTIVE_MODE_TAGS,
    CONF_JSESSIONID,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ACTIVE_MODE_OTHERS,
    DEFAULT_ACTIVE_MODE_TAGS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

type SmartThingsFindConfigEntry = ConfigEntry[SmartThingsFindData]


@dataclass
class SmartThingsFindData:
    """Datos de ejecución guardados en ``entry.runtime_data``."""

    client: SmartThingsFindClient
    coordinator: SmartThingsFindCoordinator
    options: dict


class SmartThingsFindCoordinator(DataUpdateCoordinator[dict[str, StfDeviceState]]):
    """Consulta periódicamente la posición de todos los dispositivos."""

    config_entry: SmartThingsFindConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SmartThingsFindConfigEntry,
        client: SmartThingsFindClient,
        devices: list[StfDevice],
    ) -> None:
        interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.devices: dict[str, StfDevice] = {d.device_id: d for d in devices}

    def _is_active(self, device: StfDevice) -> bool:
        opts = self.config_entry.options
        if device.is_tag:
            return opts.get(CONF_ACTIVE_MODE_TAGS, DEFAULT_ACTIVE_MODE_TAGS)
        return opts.get(CONF_ACTIVE_MODE_OTHERS, DEFAULT_ACTIVE_MODE_OTHERS)

    async def _async_update_data(self) -> dict[str, StfDeviceState]:
        previous = self.data or {}
        result: dict[str, StfDeviceState] = {}
        errors: list[str] = []

        for device in self.devices.values():
            try:
                # 1) Leemos la última posición conocida.
                result[device.device_id] = await self.client.async_get_state(device)
                # 2) En modo activo pedimos una posición nueva; el resultado
                #    llegará al servidor en unos segundos y lo leeremos en la
                #    siguiente iteración.
                if self._is_active(device):
                    await self.client.async_request_location(device)
            except SmartThingsFindAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except SmartThingsFindError as err:
                _LOGGER.debug("Error actualizando %s: %s", device.name, err)
                errors.append(f"{device.name}: {err}")
                if device.device_id in previous:
                    result[device.device_id] = previous[device.device_id]

        if errors and len(errors) == len(self.devices):
            raise UpdateFailed("; ".join(errors))

        self._persist_rotated_session()
        return result

    def _persist_rotated_session(self) -> None:
        """Si Samsung ha rotado el JSESSIONID lo guardamos para el próximo arranque."""
        current = self.client.jsessionid
        entry = self.config_entry
        if current and current != entry.data.get(CONF_JSESSIONID):
            _LOGGER.debug("JSESSIONID rotado; guardando el nuevo valor")
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_JSESSIONID: current}
            )
