"""Tests de la integración dentro de Home Assistant.

Requieren ``pytest-homeassistant-custom-component``; si no está instalado
se omiten (los tests del cliente siguen funcionando sin HA).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.smartthings_find.api import (  # noqa: E402
    SmartThingsFindAuthError,
    StfDevice,
    StfDeviceState,
    StfLocation,
)
from custom_components.smartthings_find.const import CONF_JSESSIONID, DOMAIN  # noqa: E402

CLIENT = "custom_components.smartthings_find.api.SmartThingsFindClient"

DEVICES = [
    StfDevice("tag1", "user-9", "Llaves", "EI-T5300", "TAG"),
    StfDevice("tag2", "user-9", "Mochila", "EI-T5600", "TAG"),
]
STATE = StfDeviceState(
    location=StfLocation(43.263, -2.935, 15.0, datetime(2026, 11, 5, 12, tzinfo=UTC), "LOCATION"),
    battery=50,
    battery_raw="MEDIUM",
    encrypted_location_pending=False,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _patch_client(**overrides):
    methods = {
        "async_refresh_csrf": AsyncMock(),
        "async_get_devices": AsyncMock(return_value=DEVICES),
        "async_get_state": AsyncMock(return_value=STATE),
        "async_request_location": AsyncMock(),
        "async_ring": AsyncMock(),
    }
    methods.update(overrides)
    patcher = patch.multiple(CLIENT, **methods)
    patcher.mocks = methods
    return patcher


async def test_setup_creates_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user-9", data={CONF_JSESSIONID: "S"})
    entry.add_to_hass(hass)
    patcher = _patch_client()
    mocks = patcher.mocks
    with patcher:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        tracker = hass.states.get("device_tracker.llaves")
        assert tracker is not None
        assert tracker.attributes["latitude"] == 43.263
        assert tracker.attributes["longitude"] == -2.935
        assert tracker.attributes["gps_accuracy"] == 15
        assert tracker.attributes["source_type"] == "gps"

        assert hass.states.get("sensor.llaves_battery").state == "50"
        assert hass.states.get("sensor.llaves_last_seen").state == "2026-11-05T12:00:00+00:00"

        await hass.services.async_call(
            "button", "press", {"entity_id": "button.mochila_ring"}, blocking=True
        )
        assert mocks["async_ring"].await_args.args[0].device_id == "tag2"
        # Modo activo por defecto para SmartTags.
        assert mocks["async_request_location"].await_count == 2

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_expired_session_starts_reauth(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user-9", data={CONF_JSESSIONID: "S"})
    entry.add_to_hass(hass)
    with _patch_client(async_refresh_csrf=AsyncMock(side_effect=SmartThingsFindAuthError)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == config_entries.SOURCE_REAUTH for f in flows)


async def test_config_flow_qr(hass: HomeAssistant) -> None:
    scanned = asyncio.Event()

    async def fake_wait(*args, **kwargs):
        await scanned.wait()
        return "NEW-SESSION"

    with (
        patch(
            "custom_components.smartthings_find.config_flow.QrLogin.start",
            AsyncMock(return_value="https://signin.samsung.com/key/AbC123"),
        ),
        patch(
            "custom_components.smartthings_find.config_flow.QrLogin.wait",
            fake_wait,
        ),
        _patch_client(),
        patch(f"{CLIENT}.jsessionid", "NEW-SESSION"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS, result
        assert result["description_placeholders"]["qr_image"].startswith("data:image/png;base64,")
        scanned.set()  # el usuario escanea el QR
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {CONF_JSESSIONID: "NEW-SESSION"}
        assert result["result"].unique_id == "user-9"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user-9", data={CONF_JSESSIONID: "S"})
    entry.add_to_hass(hass)
    with _patch_client():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"update_interval": 60, "active_mode_tags": False, "active_mode_others": False},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["update_interval"] == 60
    assert entry.state is ConfigEntryState.LOADED
