"""Integración SmartThings Find (SmartTag) para Home Assistant."""

from __future__ import annotations

import aiohttp
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import SmartThingsFindAuthError, SmartThingsFindClient, SmartThingsFindError
from .const import CONF_JSESSIONID
from .coordinator import (
    SmartThingsFindConfigEntry,
    SmartThingsFindCoordinator,
    SmartThingsFindData,
)

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER, Platform.SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: SmartThingsFindConfigEntry) -> bool:
    """Configura una cuenta de SmartThings Find."""
    # Sesión HTTP propia con su propio cookie jar: así la cookie de Samsung no
    # se mezcla con la sesión compartida de Home Assistant. HA la cierra
    # automáticamente al descargar la entrada (y con ella la cookie).
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    client = SmartThingsFindClient(session, entry.data[CONF_JSESSIONID])

    try:
        await client.async_refresh_csrf()
        devices = await client.async_get_devices()
    except SmartThingsFindAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except SmartThingsFindError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = SmartThingsFindCoordinator(hass, entry, client, devices)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SmartThingsFindData(
        client=client, coordinator=coordinator, options=dict(entry.options)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: SmartThingsFindConfigEntry) -> None:
    """Recarga la integración al cambiar las opciones.

    El listener también salta cuando guardamos un JSESSIONID rotado en
    ``entry.data``; en ese caso no hace falta recargar.
    """
    if dict(entry.options) == entry.runtime_data.options:
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SmartThingsFindConfigEntry) -> bool:
    """Descarga la integración."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
