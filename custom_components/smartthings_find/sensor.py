"""Sensores: batería y hora de la última posición."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import StfDevice, StfDeviceState
from .coordinator import SmartThingsFindConfigEntry, SmartThingsFindCoordinator
from .entity import SmartThingsFindEntity


@dataclass(frozen=True, kw_only=True)
class StfSensorDescription(SensorEntityDescription):
    """Descripción de un sensor de STF."""

    value_fn: Callable[[StfDeviceState], int | datetime | None]
    supported_fn: Callable[[StfDevice], bool] = lambda _: True


SENSORS: tuple[StfSensorDescription, ...] = (
    StfSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery,
    ),
    StfSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.location.timestamp if s.location else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartThingsFindConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SmartThingsFindSensor(coordinator, device, description)
        for device in coordinator.devices.values()
        for description in SENSORS
        if description.supported_fn(device)
    )


class SmartThingsFindSensor(SmartThingsFindEntity, SensorEntity):
    """Sensor genérico basado en una descripción."""

    entity_description: StfSensorDescription

    def __init__(
        self,
        coordinator: SmartThingsFindCoordinator,
        device: StfDevice,
        description: StfSensorDescription,
    ) -> None:
        super().__init__(coordinator, device, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | datetime | None:
        state = self.state_data
        return self.entity_description.value_fn(state) if state else None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        state = self.state_data
        if self.entity_description.key == "battery" and state and state.battery_raw:
            return {"raw_level": state.battery_raw}
        return None
