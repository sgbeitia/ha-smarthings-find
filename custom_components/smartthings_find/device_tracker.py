"""Entidades device_tracker: la posición de cada SmartTag en el mapa."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SmartThingsFindConfigEntry
from .entity import SmartThingsFindEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartThingsFindConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SmartThingsFindTracker(coordinator, device) for device in coordinator.devices.values()
    )


class SmartThingsFindTracker(SmartThingsFindEntity, TrackerEntity):
    """Ubicación de un dispositivo de SmartThings Find."""

    _attr_name = None  # usa el nombre del dispositivo
    _attr_translation_key = "location"

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "tracker")
        if device.icon_url:
            self._attr_entity_picture = device.icon_url
        self._attr_icon = "mdi:tag" if device.is_tag else None

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def _location(self):
        state = self.state_data
        return state.location if state else None

    @property
    def latitude(self) -> float | None:
        loc = self._location
        return loc.latitude if loc else None

    @property
    def longitude(self) -> float | None:
        loc = self._location
        return loc.longitude if loc else None

    @property
    def location_accuracy(self) -> float:
        loc = self._location
        return float(loc.accuracy) if loc and loc.accuracy is not None else 0.0

    @property
    def battery_level(self) -> int | None:
        state = self.state_data
        return state.battery if state else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.state_data
        attrs: dict[str, Any] = {
            "device_type": self.device.type_code,
            "model_id": self.device.model_id,
        }
        if state and state.location:
            attrs["last_seen"] = state.location.timestamp.isoformat()
            attrs["location_source"] = state.location.operation
        if state and state.encrypted_location_pending:
            attrs["encrypted_location"] = True
        return attrs
