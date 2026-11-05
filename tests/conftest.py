"""Carga ``api.py`` sin necesidad de tener Home Assistant instalado."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_API = Path(__file__).parents[1] / "custom_components" / "smartthings_find" / "api.py"
_spec = importlib.util.spec_from_file_location("stf_api", _API)
stf_api = importlib.util.module_from_spec(_spec)
sys.modules["stf_api"] = stf_api
_spec.loader.exec_module(stf_api)
