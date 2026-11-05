"""Botón para hacer sonar un SmartTag."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import SmartThingsFindAuthError, SmartThingsFindError
from .coordinator import SmartThingsFindConfigEntry
from .entity import SmartThingsFindEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartThingsFindConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SmartThingsFindRingButton(coordinator, device) for device in coordinator.devices.values()
    )


class SmartThingsFindRingButton(SmartThingsFindEntity, ButtonEntity):
    """Hace sonar el dispositivo (necesita un Galaxy cerca)."""

    _attr_translation_key = "ring"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "ring")

    @property
    def available(self) -> bool:
        # Se puede intentar sonar aunque aún no tengamos posición.
        return self.coordinator.last_update_success

    async def async_press(self) -> None:
        try:
            await self.coordinator.client.async_ring(self.device)
        except SmartThingsFindAuthError as err:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError("La sesión de SmartThings Find ha caducado") from err
        except SmartThingsFindError as err:
            raise HomeAssistantError(f"No se pudo hacer sonar {self.device.name}: {err}") from err
