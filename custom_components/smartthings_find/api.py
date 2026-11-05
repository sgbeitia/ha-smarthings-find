"""Cliente asíncrono (no oficial) para la web de SmartThings Find.

Este módulo NO depende de Home Assistant para que pueda probarse de forma
aislada. Implementa:

* El inicio de sesión de Samsung Account mediante código QR (el mismo flujo
  que usa https://smartthingsfind.samsung.com/ en un navegador).
* La lectura de la lista de dispositivos y de su última ubicación conocida.
* Las operaciones "solicitar ubicación" y "hacer sonar".

La API no es pública: se ha obtenido por ingeniería inversa de la web y
Samsung puede cambiarla en cualquier momento.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import html
import json as jsonlib
import logging
import re
import secrets
import string
import time
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

STF_BASE = "https://smartthingsfind.samsung.com"
STF_URL = URL(STF_BASE + "/")
ACCOUNT_BASE = "https://account.samsung.com"

# client_id público y fijo que usa la web de SmartThings Find.
_CLIENT_ID = "ntly6zvfpn"

URL_SIGNIN_GATE = (
    ACCOUNT_BASE + "/accounts/v1/FMM2/signInGate"
    "?state={state}"
    "&redirect_uri=https:%2F%2Fsmartthingsfind.samsung.com%2Flogin.do"
    "&response_type=code"
    f"&client_id={_CLIENT_ID}"
    "&scope=iot.client"
    "&locale={locale}"
    "&acr_values=urn:samsungaccount:acr:basic"
    "&goBackURL=https:%2F%2Fsmartthingsfind.samsung.com%2Flogin"
)
URL_QR_PAGE = ACCOUNT_BASE + "/accounts/v1/FMM2/signInWithQrCode"
URL_SIGNIN_XHR = ACCOUNT_BASE + "/accounts/v1/FMM2/signInXhr"
URL_QR_POLL = ACCOUNT_BASE + "/accounts/v1/FMM2/signInWithQrCodeProc"

URL_CHK_LOGIN = STF_BASE + "/chkLogin.do"
URL_DEVICE_LIST = STF_BASE + "/device/getDeviceList.do"
URL_ADD_OPERATION = STF_BASE + "/dm/addOperation.do"
URL_SET_LAST_SELECT = STF_BASE + "/device/setLastSelect.do"

_QR_URL_RE = re.compile(r"https://signin\.samsung\.com/key/[^'\"\s<>]+")
_REDIRECT_RE = re.compile(r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"]")

# Tipos de operación que contienen coordenadas.
LOCATION_OPERATIONS = ("LOCATION", "LASTLOC", "OFFLINE_LOC")

# La API devuelve la batería de los SmartTag como un nivel cualitativo.
BATTERY_LEVELS: dict[str, int] = {
    "FULL": 100,
    "MEDIUM": 50,
    "LOW": 15,
    "VERY_LOW": 5,
}

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class SmartThingsFindError(Exception):
    """Error genérico de SmartThings Find."""


class SmartThingsFindAuthError(SmartThingsFindError):
    """La sesión no es válida o ha caducado: hay que volver a iniciar sesión."""


class SmartThingsFindLoginTimeout(SmartThingsFindError):
    """El QR no se escaneó a tiempo."""


# --------------------------------------------------------------------------
# Modelos de datos
# --------------------------------------------------------------------------


@dataclass(slots=True)
class StfDevice:
    """Dispositivo registrado en SmartThings Find."""

    device_id: str
    user_id: str
    name: str
    model_id: str | None
    type_code: str | None
    icon_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_tag(self) -> bool:
        """True si es un SmartTag (v1, +, v2...)."""
        return self.type_code == "TAG"

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> StfDevice:
        """Crea el dispositivo a partir de un elemento de ``deviceList``."""
        # El nombre viene doblemente escapado: "Sergio&amp;#39;s tag".
        name = html.unescape(html.unescape(str(data.get("modelName") or "")))
        icons = data.get("icons") or {}
        return cls(
            device_id=str(data["dvceID"]),
            user_id=str(data.get("usrId") or ""),
            name=name or str(data["dvceID"]),
            model_id=data.get("modelID"),
            type_code=data.get("deviceTypeCode"),
            icon_url=icons.get("coloredIcon") if isinstance(icons, dict) else None,
            raw=data,
        )


@dataclass(slots=True)
class StfLocation:
    """Una posición reportada por la red SmartThings Find."""

    latitude: float
    longitude: float
    accuracy: float | None
    timestamp: datetime
    operation: str
    encrypted: bool = False


@dataclass(slots=True)
class StfDeviceState:
    """Estado de un dispositivo tras una consulta."""

    location: StfLocation | None
    battery: int | None
    battery_raw: str | None
    encrypted_location_pending: bool
    operations: list[dict[str, Any]] = field(default_factory=list, repr=False)


# --------------------------------------------------------------------------
# Funciones de parseo (puras, fáciles de testear)
# --------------------------------------------------------------------------


def parse_stf_date(value: str | None) -> datetime | None:
    """Convierte las fechas de STF (``YYYYmmddHHMMSS`` en UTC) a datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def calc_accuracy(horizontal: Any, vertical: Any = None) -> float | None:
    """Precisión en metros a partir de las incertidumbres que da la API.

    Si sólo hay incertidumbre horizontal se usa tal cual (es lo que pinta el
    círculo en el mapa); si hay ambas se combina de forma euclídea como hacía
    la web.
    """
    try:
        h = float(horizontal)
    except (TypeError, ValueError):
        return None
    try:
        v = float(vertical)
    except (TypeError, ValueError):
        return round(h, 1)
    return round((h**2 + v**2) ** 0.5, 1)


def _location_from_dict(
    loc: dict[str, Any], operation: str, date_hint: str | None = None
) -> StfLocation | None:
    """Extrae una posición de un diccionario con lat/lon."""
    try:
        lat = float(loc["latitude"])
        lon = float(loc["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    ts = parse_stf_date(loc.get("gpsUtcDt") or date_hint)
    if ts is None:
        return None
    return StfLocation(
        latitude=lat,
        longitude=lon,
        accuracy=calc_accuracy(loc.get("horizontalUncertainty"), loc.get("verticalUncertainty")),
        timestamp=ts,
        operation=operation,
    )


def extract_location(operations: Iterable[dict[str, Any]]) -> tuple[StfLocation | None, bool]:
    """Devuelve la posición más reciente legible y si hay alguna cifrada.

    La respuesta de ``setLastSelect.do`` puede contener varias operaciones con
    posición (``LOCATION``, ``LASTLOC``, ``OFFLINE_LOC``). Algunas vienen en
    claro (``latitude``/``longitude`` directos) y otras dentro de
    ``encLocation``. Si el usuario ha activado "Cifrar ubicación sin conexión"
    en el SmartTag, ``encLocation.encrypted`` es ``true`` y no podemos leerla.
    """
    best: StfLocation | None = None
    encrypted_seen = False

    for op in operations:
        op_type = op.get("oprnType")
        if op_type not in LOCATION_OPERATIONS:
            continue

        candidate: StfLocation | None = None
        if "latitude" in op:
            extra = op.get("extra") or {}
            candidate = _location_from_dict(op, op_type, extra.get("gpsUtcDt") or op.get("oprnDt"))
        elif isinstance(op.get("encLocation"), dict):
            enc = op["encLocation"]
            if enc.get("encrypted"):
                encrypted_seen = True
                continue
            candidate = _location_from_dict(enc, op_type)

        if candidate and (best is None or candidate.timestamp > best.timestamp):
            best = candidate

    return best, encrypted_seen


def extract_battery(operations: Iterable[dict[str, Any]]) -> tuple[int | None, str | None]:
    """Extrae la batería (porcentaje aproximado, valor original)."""
    for op in operations:
        if op.get("oprnType") != "CHECK_CONNECTION" or "battery" not in op:
            continue
        raw = str(op["battery"])
        if raw in BATTERY_LEVELS:
            return BATTERY_LEVELS[raw], raw
        try:
            return max(0, min(100, int(float(raw)))), raw
        except ValueError:
            return None, raw
    return None, None


def parse_device_state(data: dict[str, Any]) -> StfDeviceState:
    """Convierte la respuesta de ``setLastSelect.do`` en un estado."""
    ops = data.get("operation") or []
    if not isinstance(ops, list):
        ops = []
    location, encrypted = extract_location(ops)
    battery, battery_raw = extract_battery(ops)
    return StfDeviceState(
        location=location,
        battery=battery,
        battery_raw=battery_raw,
        encrypted_location_pending=encrypted and location is None,
        operations=ops,
    )


# --------------------------------------------------------------------------
# Login por QR
# --------------------------------------------------------------------------


class QrLogin:
    """Flujo de inicio de sesión con QR de Samsung Account.

    Uso::

        login = QrLogin(session)
        qr_url = await login.start()          # mostrar este enlace como QR
        jsessionid = await login.wait()        # bloquea hasta escanear o timeout

    ``session`` debe ser un ``aiohttp.ClientSession`` con su PROPIO cookie
    jar (no el compartido de Home Assistant).
    """

    def __init__(self, session: aiohttp.ClientSession, locale: str = "es_ES") -> None:
        self._session = session
        self._locale = locale
        self._state = "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
        )
        self.qr_url: str | None = None

    async def start(self) -> str:
        """Abre la página de login y devuelve la URL que hay que codificar en el QR."""
        s = self._session
        s.cookie_jar.clear()
        try:
            async with s.get(
                URL_SIGNIN_GATE.format(state=self._state, locale=self._locale),
                timeout=_TIMEOUT,
            ) as res:
                if res.status != 200:
                    raise SmartThingsFindError(f"signInGate devolvió HTTP {res.status}")
            async with s.get(URL_QR_PAGE, timeout=_TIMEOUT) as res:
                text = await res.text()
                if res.status != 200:
                    raise SmartThingsFindError(f"signInWithQrCode devolvió HTTP {res.status}")
        except aiohttp.ClientError as err:
            raise SmartThingsFindError(f"Error de red al iniciar el login: {err}") from err

        match = _QR_URL_RE.search(text)
        if not match:
            raise SmartThingsFindError("No se encontró la URL del QR en la página de Samsung")
        self.qr_url = html.unescape(match.group(0))
        return self.qr_url

    async def wait(self, timeout: float = 180, poll_interval: float = 2) -> str:
        """Espera a que el usuario escanee el QR y devuelve el JSESSIONID de STF."""
        s = self._session
        try:
            async with s.get(URL_SIGNIN_XHR, timeout=_TIMEOUT) as res:
                if res.status != 200:
                    raise SmartThingsFindError(f"signInXhr devolvió HTTP {res.status}")
                payload = await res.json(content_type=None)
            csrf = (payload.get("_csrf") or {}).get("token")
            if not csrf:
                raise SmartThingsFindError("No se recibió token CSRF de Samsung Account")

            next_url: str | None = None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(poll_interval)
                async with s.post(
                    URL_QR_POLL, json={}, headers={"X-Csrf-Token": csrf}, timeout=_TIMEOUT
                ) as res:
                    if res.status != 200:
                        _LOGGER.debug("Polling QR: HTTP %s", res.status)
                        continue
                    js = await res.json(content_type=None)
                rtn = js.get("rtnCd")
                _LOGGER.debug("Polling QR: %s", rtn)
                if rtn == "SUCCESS":
                    next_url = js.get("nextURL")
                    break
                if rtn not in (None, "POLLING"):
                    raise SmartThingsFindError(f"Login QR rechazado: {rtn}")
            if not next_url:
                raise SmartThingsFindLoginTimeout("El código QR no se escaneó a tiempo")

            # signInComplete -> HTML con un redirect JS hacia STF login.do?code=...
            async with s.get(str(URL(ACCOUNT_BASE).join(URL(next_url))), timeout=_TIMEOUT) as res:
                text = await res.text()
                if res.status != 200:
                    raise SmartThingsFindError(f"signInComplete devolvió HTTP {res.status}")
            match = _REDIRECT_RE.search(text)
            if not match:
                raise SmartThingsFindError("No se encontró la redirección final del login")
            redirect = html.unescape(match.group(1))

            async with s.get(redirect, timeout=_TIMEOUT) as res:
                if res.status != 200:
                    raise SmartThingsFindError(f"login.do devolvió HTTP {res.status}")
        except aiohttp.ClientError as err:
            raise SmartThingsFindError(f"Error de red durante el login: {err}") from err

        cookie = s.cookie_jar.filter_cookies(STF_URL).get("JSESSIONID")
        if not cookie or not cookie.value:
            raise SmartThingsFindError("Samsung no devolvió la cookie JSESSIONID")
        return cookie.value


# --------------------------------------------------------------------------
# Cliente de datos
# --------------------------------------------------------------------------


class SmartThingsFindClient:
    """Cliente autenticado mediante la cookie JSESSIONID de SmartThings Find."""

    def __init__(self, session: aiohttp.ClientSession, jsessionid: str) -> None:
        self._session = session
        self._csrf: str | None = None
        session.cookie_jar.update_cookies({"JSESSIONID": jsessionid}, STF_URL)

    @property
    def jsessionid(self) -> str | None:
        """JSESSIONID actual (Samsung puede rotarlo)."""
        cookie = self._session.cookie_jar.filter_cookies(STF_URL).get("JSESSIONID")
        return cookie.value if cookie else None

    async def async_refresh_csrf(self) -> None:
        """Valida la sesión y obtiene el token CSRF (cabecera ``_csrf``)."""
        try:
            async with self._session.get(URL_CHK_LOGIN, timeout=_TIMEOUT) as res:
                csrf = res.headers.get("_csrf")
                body = await res.text()
                status = res.status
        except aiohttp.ClientError as err:
            raise SmartThingsFindError(f"Error de red en chkLogin: {err}") from err
        if status in (401, 403) or (status == 200 and not csrf):
            raise SmartThingsFindAuthError(f"Sesión no válida (HTTP {status}): {body[:120]!r}")
        if status != 200:
            raise SmartThingsFindError(f"chkLogin devolvió HTTP {status}")
        self._csrf = csrf

    async def _post(self, url: str, *, json: Any = None, data: Any = None) -> Any:
        """POST con el token CSRF; reintenta una vez si el token ha caducado."""
        for attempt in (1, 2):
            if self._csrf is None:
                await self.async_refresh_csrf()
            try:
                async with self._session.post(
                    url,
                    params={"_csrf": self._csrf},
                    json=json,
                    data=data,
                    headers={"Accept": "application/json"},
                    timeout=_TIMEOUT,
                ) as res:
                    text = await res.text()
                    status = res.status
                    ctype = res.content_type
            except aiohttp.ClientError as err:
                raise SmartThingsFindError(f"Error de red en {url}: {err}") from err

            auth_problem = status in (401, 403, 404) or text.strip() == "Logout"
            if auth_problem:
                if attempt == 1:
                    # Puede que sólo haya caducado el CSRF: lo renovamos.
                    self._csrf = None
                    continue
                raise SmartThingsFindAuthError(f"Sesión caducada (HTTP {status})")
            if status != 200:
                raise SmartThingsFindError(f"{url} devolvió HTTP {status}: {text[:200]}")
            if not text:
                return None
            if "json" not in ctype and not text.lstrip().startswith(("{", "[")):
                return text
            try:
                return jsonlib.loads(text)
            except ValueError as err:
                raise SmartThingsFindError(f"Respuesta no JSON de {url}") from err
        raise SmartThingsFindAuthError("Sesión caducada")  # pragma: no cover

    async def async_get_devices(self) -> list[StfDevice]:
        """Lista todos los dispositivos de la cuenta."""
        data = await self._post(URL_DEVICE_LIST, data={})
        if not isinstance(data, dict) or "deviceList" not in data:
            raise SmartThingsFindError("Respuesta inesperada de getDeviceList")
        devices = []
        for raw in data["deviceList"] or []:
            try:
                devices.append(StfDevice.from_api(raw))
            except KeyError:
                _LOGGER.debug("Dispositivo sin dvceID ignorado: %s", raw)
        return devices

    async def async_request_location(self, device: StfDevice) -> None:
        """Pide a la red que localice el dispositivo ("modo activo").

        Para un SmartTag esto hace que un Galaxy cercano (el tuyo o el de
        cualquier otro usuario) reporte su posición. La nueva posición suele
        aparecer en la siguiente consulta, no en esta.
        """
        await self._post(
            URL_ADD_OPERATION,
            json={
                "dvceId": device.device_id,
                "operation": "CHECK_CONNECTION_WITH_LOCATION",
                "usrId": device.user_id,
            },
        )

    async def async_get_state(self, device: StfDevice) -> StfDeviceState:
        """Lee la última posición/batería conocida del dispositivo."""
        data = await self._post(
            URL_SET_LAST_SELECT,
            json={"dvceId": device.device_id, "removeDevice": []},
        )
        if not isinstance(data, dict):
            raise SmartThingsFindError("Respuesta inesperada de setLastSelect")
        return parse_device_state(data)

    async def async_ring(self, device: StfDevice, start: bool = True) -> None:
        """Hace sonar el dispositivo (requiere un Galaxy cerca por Bluetooth)."""
        await self._post(
            URL_ADD_OPERATION,
            json={
                "dvceId": device.device_id,
                "operation": "RING",
                "usrId": device.user_id,
                "status": "start" if start else "stop",
                "lockMessage": "Home Assistant está buscando este dispositivo",
            },
        )
