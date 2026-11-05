"""Constants for the SmartThings Find integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "smartthings_find"

CONF_JSESSIONID: Final = "jsessionid"
CONF_ACTIVE_MODE_TAGS: Final = "active_mode_tags"
CONF_ACTIVE_MODE_OTHERS: Final = "active_mode_others"
CONF_UPDATE_INTERVAL: Final = "update_interval"

DEFAULT_ACTIVE_MODE_TAGS: Final = True
DEFAULT_ACTIVE_MODE_OTHERS: Final = False
DEFAULT_UPDATE_INTERVAL: Final = 120  # seconds
MIN_UPDATE_INTERVAL: Final = 30
MAX_UPDATE_INTERVAL: Final = 3600

CONFIGURATION_URL: Final = "https://smartthingsfind.samsung.com/"
