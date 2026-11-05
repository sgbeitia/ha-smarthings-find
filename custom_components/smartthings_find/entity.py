"""Entidad base de SmartThings Find."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import StfDevice, StfDeviceState
from .const import CONFIGURATION_URL, DOMAIN
from .coordinator import SmartThingsFindCoordinator


class SmartThingsFindEntity(CoordinatorEntity[SmartThingsFindCoordinator]):
    """Base común: asocia la entidad a un dispositivo de STF."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SmartThingsFindCoordinator, device: StfDevice, key: str
    ) -> None:
        super().__init__(coordinator)
        self.device = device
        self._attr_unique_id = f"{device.device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer="Samsung",
            name=device.name,
            model=device.model_id,
            configuration_url=CONFIGURATION_URL,
        )

    @property
    def state_data(self) -> StfDeviceState | None:
        """Estado actual del dispositivo en el coordinador."""
        return (self.coordinator.data or {}).get(self.device.device_id)

    @property
    def available(self) -> bool:
        return super().available and self.state_data is not None
