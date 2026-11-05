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
import time
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

STF_BASE = "https://smartthingsfind.samsung.com"
STF_URL = URL(STF_BASE + "/")
ACCOUNT_BASE = "https://account.samsung.com"

# Parámetros OAuth públicos y fijos de la web de SmartThings Find.
_CLIENT_ID = "ntly6zvfpn"
_SCOPE = "iot.client"
_ACR_VALUES = "urn:samsungaccount:acr:basic"

# --- Samsung Account (nuevo login "IAM", 2026) -------------------------------
URL_AUTHORIZE = ACCOUNT_BASE + "/iam/oauth2/authorize"
ACCOUNT_API = ACCOUNT_BASE + "/api"

# --- SmartThings Find ---------------------------------------------------------
URL_GET_STATE = STF_BASE + "/getState.do"
URL_INIT = STF_BASE + "/init.do"
URL_CHK_LOGIN = STF_BASE + "/chkLogin.do"
URL_DEVICE_LIST = STF_BASE + "/device/getDeviceList.do"
URL_ADD_OPERATION = STF_BASE + "/dm/addOperation.do"
URL_SET_LAST_SELECT = STF_BASE + "/device/setLastSelect.do"

# Prefijo de las URLs de los QR de Samsung. El contenido del QR es este prefijo
# más un código corto; sólo aceptamos caracteres alfanuméricos para que la URL
# (que se muestra como enlace Markdown en el config flow) no pueda inyectar nada.
QR_URL_PREFIX = "https://signin.samsung.com/key/"
_QR_URL_RE = re.compile(r"https://signin\.samsung\.com/key/[A-Za-z0-9_-]+")
_USER_CODE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

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


# Dominios desde los que aceptamos iconos (el navegador del usuario de HA los
# descarga directamente; un dominio arbitrario sería un "píxel espía").
_ICON_DOMAINS = ("samsung.com", "samsungcloud.com", "samsungosp.com")


def _require_origin(url: URL, origin: URL) -> URL:
    """Comprueba que ``url`` tiene exactamente el mismo origen que ``origin``.

    Durante el login seguimos dos URLs que vienen dentro de las respuestas de
    Samsung. Sólo se aceptan si apuntan a account.samsung.com y
    smartthingsfind.samsung.com por https; cualquier otro destino se rechaza
    para que nunca se envíe el código de autorización a un tercero.
    """
    if (url.scheme, url.host, url.port) != (origin.scheme, origin.host, origin.port):
        raise SmartThingsFindError(f"Destino inesperado durante el login: {url.host!r}")
    return url


def safe_icon_url(value: Any) -> str | None:
    """Devuelve la URL del icono sólo si es https y de un dominio de Samsung."""
    if not isinstance(value, str):
        return None
    try:
        url = URL(value)
    except ValueError:
        return None
    host = (url.host or "").lower()
    if url.scheme != "https" or not any(host == d or host.endswith("." + d) for d in _ICON_DOMAINS):
        return None
    return str(url)


def _net_error(err: Exception) -> str:
    """Describe un error de red sin incluir la URL (lleva el token CSRF)."""
    return type(err).__name__


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
    sub_type: str | None = None
    # Nombre del miembro (familia/cuenta vinculada) al que pertenece, si no es tuyo.
    owner: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_tag(self) -> bool:
        """True si es un SmartTag (v1, +, v2...)."""
        return self.type_code == "TAG"

    @classmethod
    def from_api(cls, data: dict[str, Any], owner: str | None = None) -> StfDevice:
        """Crea el dispositivo a partir de un elemento de ``deviceList``.

        La web actual usa ``deviceType``; versiones anteriores usaban
        ``deviceTypeCode``. Se aceptan ambas variantes de cada campo.
        """
        device_id = data.get("dvceID") or data.get("dvceId")
        if not device_id:
            raise KeyError("dvceID")
        # El nombre viene doblemente escapado: "Sergio&amp;#39;s tag".
        name = html.unescape(
            html.unescape(str(data.get("modelName") or data.get("displayName") or ""))
        )
        icons = data.get("icons") or {}
        type_code = data.get("deviceType") or data.get("deviceTypeCode")
        return cls(
            device_id=str(device_id),
            user_id=str(data.get("usrId") or ""),
            name=name or str(device_id),
            model_id=data.get("modelID"),
            type_code=str(type_code).upper() if type_code else None,
            icon_url=safe_icon_url(icons.get("coloredIcon")) if isinstance(icons, dict) else None,
            sub_type=data.get("subType"),
            owner=owner,
            raw=data,
        )


def parse_device_list(data: dict[str, Any]) -> tuple[list[StfDevice], dict[str, Any]]:
    """Extrae los dispositivos de ``getDeviceList.do``.

    La respuesta contiene ``deviceList`` (dispositivos propios) y
    ``childrenDeviceList``: grupos ``{userName, deviceList}`` con los
    dispositivos de otros miembros (familia / cuentas vinculadas).

    Devuelve también un resumen ANÓNIMO (sin ids, nombres ni posiciones) de la
    estructura recibida, útil para diagnosticar dispositivos que no aparecen.
    """
    devices: list[StfDevice] = []
    seen: set[str] = set()
    summary: dict[str, Any] = {
        "top_level_keys": sorted(data),
        "lists": {},
        "devices": [],
        "skipped": 0,
    }

    def add(raw_list: Any, source: str, owner: str | None) -> None:
        if not isinstance(raw_list, list):
            return
        summary["lists"][source] = len(raw_list)
        for raw in raw_list:
            if not isinstance(raw, dict):
                summary["skipped"] += 1
                continue
            try:
                dev = StfDevice.from_api(raw, owner=owner)
            except KeyError:
                summary["skipped"] += 1
                continue
            summary["devices"].append(
                {
                    "source": source,
                    "device_type": dev.type_code,
                    "sub_type": dev.sub_type,
                    "model_id": dev.model_id,
                    "fields": sorted(raw),
                    "duplicate": dev.device_id in seen,
                }
            )
            if dev.device_id in seen:
                continue
            seen.add(dev.device_id)
            devices.append(dev)

    add(data.get("deviceList"), "deviceList", None)
    for idx, group in enumerate(data.get("childrenDeviceList") or []):
        if isinstance(group, dict):
            owner = html.unescape(str(group.get("userName") or "")) or None
            add(group.get("deviceList"), f"childrenDeviceList[{idx}]", owner)
    return devices, summary


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
    """Inicio de sesión en Samsung Account con código QR.

    Reproduce el flujo de la web de Samsung Account (2026)::

        login = QrLogin(session)
        qr_url = await login.start()             # mostrar esta URL como QR
        pin = await login.wait_for_scan()        # PIN a confirmar en el móvil (o None)
        await login.wait_for_approval()          # el usuario aprueba en el móvil
        jsessionid = await login.finish()        # cookie de SmartThings Find

    ``session`` debe ser un ``aiohttp.ClientSession`` con su PROPIO cookie
    jar (no el compartido de Home Assistant): el login usa cookies de
    account.samsung.com y de smartthingsfind.samsung.com.
    """

    def __init__(self, session: aiohttp.ClientSession, locale: str = "es-ES") -> None:
        self._session = session
        locale = locale.replace("_", "-")
        self._locale = locale
        lang, _, country = locale.partition("-")
        self._language = lang.lower() or "en"
        self._country = country.upper() or "US"
        self._state: str | None = None
        self._context_key: str | None = None
        self._challenge_key: str | None = None
        self._login_id: str | None = None
        self._poll_interval = 2.0
        self._expires_monotonic: float | None = None
        self.qr_url: str | None = None
        self.pin: str | None = None

    # -- utilidades HTTP ----------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        # Las mismas cabeceras que añade el interceptor de la web de Samsung.
        return {
            "Accept": "application/json",
            "x-app-id": _CLIENT_ID,
            "x-locale": self._locale,
        }

    async def _api(self, method: str, path: str, json: Any = None) -> tuple[int, Any]:
        """Llama a la API de Samsung Account y devuelve (status, json|None)."""
        async with self._session.request(
            method, ACCOUNT_API + path, json=json, headers=self._headers, timeout=_TIMEOUT
        ) as res:
            text = await res.text()
            status = res.status
        try:
            data = jsonlib.loads(text) if text else None
        except ValueError:
            data = None
        return status, data

    @staticmethod
    def _error_code(data: Any) -> str | None:
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return data["error"].get("code")
        return None

    def _oauth_params(self) -> dict[str, str]:
        assert self._state is not None
        return {
            "client_id": _CLIENT_ID,
            "redirect_uri": STF_BASE + "/login.do",
            "response_type": "code",
            "scope": _SCOPE,
            "state": self._state,
            "locale": self._locale,
            "acr_values": _ACR_VALUES,
            "goBackURL": STF_BASE + "/login",
        }

    # -- pasos del login ----------------------------------------------------

    async def start(self) -> str:
        """Prepara el login y devuelve la URL que hay que codificar en el QR."""
        s = self._session
        s.cookie_jar.clear()
        try:
            # 1) SmartThings Find emite un ``state`` ligado a su sesión previa.
            async with s.get(URL_GET_STATE, params={"payload": "hound"}, timeout=_TIMEOUT) as res:
                if res.status != 200:
                    raise SmartThingsFindError(f"getState devolvió HTTP {res.status}")
                state_data = jsonlib.loads(await res.text())
            self._state = str(state_data.get("state") or "")
            if not self._state:
                raise SmartThingsFindError("SmartThings Find no devolvió 'state'")

            # 2) Página de autorización (fija cookies de Samsung Account).
            async with s.get(URL_AUTHORIZE, params=self._oauth_params(), timeout=_TIMEOUT) as res:
                if res.status != 200:
                    raise SmartThingsFindError(f"authorize devolvió HTTP {res.status}")

            # Identificador de dispositivo: la web lo genera en el navegador.
            s.cookie_jar.update_cookies({"sa_did": secrets.token_hex(16)}, URL(ACCOUNT_BASE + "/"))

            # 3) Contexto de autenticación OAuth2.
            status, data = await self._api(
                "POST",
                "/auths",
                {
                    "authType": "OAUTH2",
                    "clientId": _CLIENT_ID,
                    "redirectUri": STF_BASE + "/login.do",
                    "responseType": "code",
                    "state": self._state,
                    "countryCode": self._country,
                    "languageCode": self._language,
                    "scope": _SCOPE,
                    "acrValues": _ACR_VALUES,
                    "action": "signin",
                },
            )
            if status not in (200, 201) or not isinstance(data, dict) or not data.get("key"):
                raise SmartThingsFindError(f"No se pudo crear el contexto de login (HTTP {status})")
            self._context_key = str(data["key"])

            # 4) Reto de credenciales y 5) código QR.
            status, data = await self._api(
                "POST", "/challenges/credentials", {"contextKey": self._context_key}
            )
            if status not in (200, 201) or not isinstance(data, dict) or not data.get("key"):
                raise SmartThingsFindError(f"No se pudo crear el reto de login (HTTP {status})")
            self._challenge_key = str(data["key"])

            status, data = await self._api(
                "POST",
                f"/challenges/credentials/{self._challenge_key}/qr-codes",
                {"deviceType": "Web"},
            )
        except aiohttp.ClientError as err:
            raise SmartThingsFindError(
                f"Error de red al iniciar el login: {_net_error(err)}"
            ) from err
        except ValueError as err:
            raise SmartThingsFindError("Respuesta inesperada de SmartThings Find") from err

        if status not in (200, 201) or not isinstance(data, dict):
            raise SmartThingsFindError(
                f"No se pudo generar el QR (HTTP {status}, {self._error_code(data)})"
            )
        url, code = str(data.get("url") or ""), str(data.get("userCode") or "")
        if url != QR_URL_PREFIX or not _USER_CODE_RE.fullmatch(code):
            raise SmartThingsFindError("Samsung devolvió un QR con formato inesperado")

        try:
            self._poll_interval = max(1.0, float(data.get("pollingInterval") or 2))
        except (TypeError, ValueError):
            self._poll_interval = 2.0
        try:
            expiry = float(data.get("expiryInSeconds") or 600)
        except (TypeError, ValueError):
            expiry = 600.0
        self._expires_monotonic = time.monotonic() + expiry
        self.qr_url = url + code
        return self.qr_url

    async def _poll(self, timeout: float, poll_interval: float | None, want_pin: bool) -> None:
        """Consulta el estado del QR hasta tener PIN (si ``want_pin``) o loginId."""
        if self._challenge_key is None:
            raise SmartThingsFindError("Hay que llamar antes a start()")
        interval = poll_interval if poll_interval is not None else self._poll_interval
        deadline = time.monotonic() + timeout
        if self._expires_monotonic is not None:
            deadline = min(deadline, self._expires_monotonic)
        path = f"/challenges/credentials/{self._challenge_key}/qr-codes"
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                status, data = await self._api("GET", path)
                if status == 410:  # Gone: QR caducado
                    break
                if status == 400:
                    if self._error_code(data) == "InvalidCredentials":
                        break
                    raise SmartThingsFindError(f"Login QR rechazado ({self._error_code(data)})")
                if status == 409:  # Conflict: aún no listo
                    continue
                if status == 429:
                    raise SmartThingsFindError("Demasiados intentos de login; espera unos minutos")
                if status != 200 or not isinstance(data, dict):
                    _LOGGER.debug("Polling QR: HTTP %s", status)
                    continue
                if data.get("loginId"):
                    self._login_id = str(data["loginId"])
                    return
                pin = data.get("userPin")
                if pin and want_pin and str(pin) != self.pin:
                    self.pin = str(pin)
                    return
        except aiohttp.ClientError as err:
            raise SmartThingsFindError(f"Error de red durante el login: {_net_error(err)}") from err
        raise SmartThingsFindLoginTimeout("El código QR caducó antes de completarse el login")

    async def wait_for_scan(
        self, timeout: float = 600, poll_interval: float | None = None
    ) -> str | None:
        """Espera a que se escanee el QR.

        Devuelve el PIN que el usuario debe confirmar en el móvil, o ``None``
        si Samsung aprobó directamente el login sin PIN.
        """
        await self._poll(timeout, poll_interval, want_pin=True)
        return None if self._login_id else self.pin

    async def wait_for_approval(
        self, timeout: float = 600, poll_interval: float | None = None
    ) -> None:
        """Espera a que el usuario apruebe el login en el móvil."""
        if self._login_id is None:
            await self._poll(timeout, poll_interval, want_pin=False)

    async def finish(self) -> str:
        """Completa el login y devuelve el JSESSIONID de SmartThings Find."""
        if self._login_id is None or self._context_key is None:
            raise SmartThingsFindError("El login todavía no se ha aprobado")
        s = self._session
        try:
            status, data = await self._api(
                "PATCH", f"/challenges/credentials/{self._challenge_key}/qr-codes"
            )
            if status >= 400:
                raise SmartThingsFindError(f"No se pudo confirmar el QR (HTTP {status})")
            status, data = await self._api(
                "PATCH",
                f"/contexts/auths/{self._context_key}",
                {"challengeKey": self._challenge_key, "step": "CREDENTIAL"},
            )
            if status >= 400:
                raise SmartThingsFindError(
                    f"Samsung rechazó el login (HTTP {status}, {self._error_code(data)})"
                )
            status, data = await self._api("GET", f"/auths/{self._context_key}")
            page = data.get("page") if isinstance(data, dict) else None
            if page != "complete":
                # Otros valores son pasos extra de Samsung (verificación en dos
                # pasos, aceptar términos, cuenta bloqueada…) que hay que
                # completar una vez en la web de SmartThings Find.
                raise SmartThingsFindError(
                    f"Samsung pide un paso adicional ('{page}'). Inicia sesión una vez en "
                    "https://smartthingsfind.samsung.com/ desde el navegador y vuelve a intentarlo"
                )

            # Samsung responde con una redirección a STF login.do?code=...
            async with s.get(
                f"{ACCOUNT_API}/auths/{self._context_key}/url",
                params=self._oauth_params(),
                headers=self._headers,
                allow_redirects=False,
                timeout=_TIMEOUT,
            ) as res:
                location = res.headers.get("Location")
                status = res.status
            if not location:
                raise SmartThingsFindError(
                    f"Samsung no devolvió la redirección final (HTTP {status})"
                )
            redirect = _require_origin(URL(ACCOUNT_BASE).join(URL(location)), STF_URL)
            async with s.get(redirect, timeout=_TIMEOUT) as res:
                if res.status >= 400:
                    raise SmartThingsFindError(f"login.do devolvió HTTP {res.status}")
        except aiohttp.ClientError as err:
            raise SmartThingsFindError(f"Error de red durante el login: {_net_error(err)}") from err

        cookie = s.cookie_jar.filter_cookies(STF_URL).get("JSESSIONID")
        if not cookie or not cookie.value:
            raise SmartThingsFindError("Samsung no devolvió la cookie JSESSIONID")
        return cookie.value

    async def wait(self, timeout: float = 600, poll_interval: float | None = None) -> str:
        """Atajo: espera escaneo y aprobación y devuelve el JSESSIONID."""
        await self.wait_for_scan(timeout, poll_interval)
        await self.wait_for_approval(timeout, poll_interval)
        return await self.finish()


# --------------------------------------------------------------------------
# Cliente de datos
# --------------------------------------------------------------------------


class SmartThingsFindClient:
    """Cliente autenticado mediante la cookie JSESSIONID de SmartThings Find."""

    def __init__(self, session: aiohttp.ClientSession, jsessionid: str) -> None:
        self._session = session
        self._csrf: str | None = None
        # Resumen anónimo de la última lista de dispositivos (diagnóstico).
        self.device_list_summary: dict[str, Any] | None = None
        session.cookie_jar.update_cookies({"JSESSIONID": jsessionid}, STF_URL)

    @property
    def jsessionid(self) -> str | None:
        """JSESSIONID actual (Samsung puede rotarlo)."""
        cookie = self._session.cookie_jar.filter_cookies(STF_URL).get("JSESSIONID")
        return cookie.value if cookie else None

    async def async_refresh_csrf(self) -> None:
        """Valida la sesión y obtiene el token CSRF (cabecera ``_csrf``).

        La web actual lo obtiene de ``/init.do``; ``/chkLogin.do`` (web
        anterior) se usa como respaldo.
        """
        for url in (URL_INIT, URL_CHK_LOGIN):
            try:
                async with self._session.get(
                    url, headers={"Accept": "application/json"}, timeout=_TIMEOUT
                ) as res:
                    csrf = res.headers.get("_csrf")
                    body = await res.text()
                    status = res.status
            except aiohttp.ClientError as err:
                raise SmartThingsFindError(
                    f"Error de red en {URL(url).path}: {_net_error(err)}"
                ) from err
            if csrf and status == 200:
                self._csrf = csrf
                return
            if status in (401, 403) or body.strip().lower() in ("logout", "fail"):
                _LOGGER.debug("%s sin sesión (HTTP %s)", URL(url).path, status)
                raise SmartThingsFindAuthError(f"Sesión no válida (HTTP {status})")
            _LOGGER.debug("%s sin token CSRF (HTTP %s)", URL(url).path, status)
        raise SmartThingsFindError("SmartThings Find no devolvió token CSRF")

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
                raise SmartThingsFindError(
                    f"Error de red en {URL(url).path}: {_net_error(err)}"
                ) from err

            auth_problem = status in (401, 403, 404) or text.strip() == "Logout"
            if auth_problem:
                if attempt == 1:
                    # Puede que sólo haya caducado el CSRF: lo renovamos.
                    self._csrf = None
                    continue
                raise SmartThingsFindAuthError(f"Sesión caducada (HTTP {status})")
            if status != 200:
                # No incluimos el cuerpo: podría contener datos de ubicación.
                raise SmartThingsFindError(f"{URL(url).path} devolvió HTTP {status}")
            if not text:
                return None
            if "json" not in ctype and not text.lstrip().startswith(("{", "[")):
                return text
            try:
                return jsonlib.loads(text)
            except ValueError as err:
                raise SmartThingsFindError(f"Respuesta no JSON de {URL(url).path}") from err
        raise SmartThingsFindAuthError("Sesión caducada")  # pragma: no cover

    async def async_get_devices(self) -> list[StfDevice]:
        """Lista todos los dispositivos de la cuenta."""
        data = await self._post(URL_DEVICE_LIST, data={})
        if not isinstance(data, dict) or (
            "deviceList" not in data and "childrenDeviceList" not in data
        ):
            raise SmartThingsFindError("Respuesta inesperada de getDeviceList")
        devices, summary = parse_device_list(data)
        self.device_list_summary = summary
        _LOGGER.debug(
            "getDeviceList: listas=%s, tipos=%s",
            summary["lists"],
            [(d["device_type"], d["model_id"], d["source"]) for d in summary["devices"]],
        )
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
