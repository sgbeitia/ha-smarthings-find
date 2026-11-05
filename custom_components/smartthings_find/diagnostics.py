"""Diagnósticos de SmartThings Find (Ajustes → Integración → ⋮ → Descargar diagnósticos).

Sólo incluye información ANÓNIMA sobre la estructura de los datos: tipos y
modelos de dispositivo, de qué lista vienen y qué campos traen. NUNCA incluye
la cookie de sesión, identificadores, nombres, el usuario ni posiciones.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_JSESSIONID
from .coordinator import SmartThingsFindConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SmartThingsFindConfigEntry
) -> dict[str, Any]:
    """Devuelve los diagnósticos anónimos de la entrada."""
    data = entry.runtime_data
    coordinator = data.coordinator
    states = coordinator.data or {}

    devices = []
    for device in coordinator.devices.values():
        state = states.get(device.device_id)
        devices.append(
            {
                "device_type": device.type_code,
                "sub_type": device.sub_type,
                "model_id": device.model_id,
                "from_other_member": device.owner is not None,
                "has_state": state is not None,
                "has_location": bool(state and state.location),
                "location_source": state.location.operation if state and state.location else None,
                "encrypted_location_pending": bool(state and state.encrypted_location_pending),
                "battery_raw": state.battery_raw if state else None,
                "operation_types": sorted(
                    {str(op.get("oprnType")) for op in (state.operations if state else [])}
                ),
            }
        )

    return {
        "entry": {
            "has_session": bool(entry.data.get(CONF_JSESSIONID)),
            "options": dict(entry.options),
        },
        "last_update_success": coordinator.last_update_success,
        "device_list": data.client.device_list_summary,
        "devices": devices,
    }
