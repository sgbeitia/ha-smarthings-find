"""Config flow de SmartThings Find: inicio de sesión con QR de Samsung Account."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util.ssl import get_default_context
import segno
import voluptuous as vol

from .api import (
    QrLogin,
    SmartThingsFindClient,
    SmartThingsFindError,
    SmartThingsFindLoginTimeout,
)
from .const import (
    CONF_ACTIVE_MODE_OTHERS,
    CONF_ACTIVE_MODE_TAGS,
    CONF_JSESSIONID,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ACTIVE_MODE_OTHERS,
    DEFAULT_ACTIVE_MODE_TAGS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

QR_TIMEOUT = 180  # segundos


def _new_private_session() -> aiohttp.ClientSession:
    """Sesión HTTP desechable para el login.

    Tiene su propio cookie jar (no comparte cookies con el resto de HA),
    verifica TLS con el contexto por defecto de Home Assistant y la cerramos
    nosotros en cuanto termina el login, de modo que las cookies de Samsung
    Account no se quedan en memoria.
    """
    return aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(),
        connector=aiohttp.TCPConnector(ssl=get_default_context()),
    )


class SmartThingsFindConfigFlow(ConfigFlow, domain=DOMAIN):
    """Gestiona el alta y la re-autenticación."""

    VERSION = 1

    def __init__(self) -> None:
        self._login: QrLogin | None = None
        self._login_session: aiohttp.ClientSession | None = None
        self._login_task: asyncio.Task[None] | None = None
        self._qr_url: str | None = None
        self._qr_image: str | None = None
        self._jsessionid: str | None = None
        self._user_id: str | None = None
        self._error: str | None = None

    # ------------------------------------------------------------------ alta

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Paso inicial: explica el proceso antes de generar el QR."""
        if user_input is None:
            return self.async_show_form(step_id="user")
        return await self.async_step_qr()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """La sesión ha caducado."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pide confirmación antes de mostrar un nuevo QR."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_qr()

    def _locale(self) -> str:
        lang = (self.hass.config.language or "en").split("-")[0].lower()
        country = (self.hass.config.country or "").upper()
        return f"{lang}_{country}" if country else "en_US"

    async def async_step_qr(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Muestra el QR y espera (en segundo plano) a que se escanee."""
        if self._login_task is None:
            # Sesión HTTP desechable: guarda cookies de Samsung Account y se
            # cierra en cuanto termina el login (con éxito o no).
            await self._async_close_login_session()
            self._login_session = _new_private_session()
            self._login = QrLogin(self._login_session, locale=self._locale())
            try:
                self._qr_url = await self._login.start()
            except SmartThingsFindError as err:
                _LOGGER.error("No se pudo iniciar el login de Samsung: %s", err)
                await self._async_close_login_session()
                return self.async_abort(reason="cannot_connect")
            qr = segno.make(self._qr_url, error="m")
            self._qr_image = await self.hass.async_add_executor_job(
                lambda: qr.png_data_uri(scale=6, border=2)
            )
            self._error = None
            self._login_task = self.hass.async_create_task(self._async_wait_for_login())

        if not self._login_task.done():
            return self.async_show_progress(
                step_id="qr",
                progress_action="scan_qr",
                description_placeholders={
                    "qr_image": self._qr_image or "",
                    "qr_url": self._qr_url or "",
                },
                progress_task=self._login_task,
            )

        self._login_task = None
        if self._error:
            return self.async_show_progress_done(next_step_id="login_failed")
        return self.async_show_progress_done(next_step_id="finish")

    async def _async_wait_for_login(self) -> None:
        """Tarea de fondo: espera al QR y valida la sesión obtenida."""
        assert self._login is not None
        check_session: aiohttp.ClientSession | None = None
        try:
            jsessionid = await self._login.wait(timeout=QR_TIMEOUT)
            await self._async_close_login_session()
            # Validamos la sesión con una sesión HTTP limpia y obtenemos el id
            # de usuario (para identificar la cuenta de forma única).
            check_session = _new_private_session()
            client = SmartThingsFindClient(check_session, jsessionid)
            await client.async_refresh_csrf()
            devices = await client.async_get_devices()
            new_jsessionid = client.jsessionid or jsessionid
        except SmartThingsFindLoginTimeout:
            self._error = "timeout"
            return
        except SmartThingsFindError as err:
            _LOGGER.error("Error en el login de SmartThings Find: %s", err)
            self._error = "login_failed"
            return
        finally:
            await self._async_close_login_session()
            if check_session is not None:
                await check_session.close()
        self._jsessionid = new_jsessionid
        self._user_id = next((d.user_id for d in devices if d.user_id), None)

    async def async_step_login_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Permite reintentar si el QR caducó o el login falló."""
        if user_input is None:
            return self.async_show_form(
                step_id="login_failed", errors={"base": self._error or "login_failed"}
            )
        return await self.async_step_qr()

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Crea (o actualiza, si es reauth) la entrada."""
        data = {CONF_JSESSIONID: self._jsessionid}

        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            if self._user_id and entry.unique_id and entry.unique_id != self._user_id:
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(entry, data_updates=data)

        if self._user_id:
            await self.async_set_unique_id(self._user_id)
            self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(title="SmartThings Find", data=data)

    async def _async_close_login_session(self) -> None:
        if self._login_session is not None:
            session, self._login_session = self._login_session, None
            await session.close()

    @callback
    def async_remove(self) -> None:
        """Cancela la espera del QR si el usuario cierra el diálogo."""
        if self._login_task and not self._login_task.done():
            # Al cancelarse, el ``finally`` de la tarea cierra la sesión.
            self._login_task.cancel()
        elif self._login_session is not None:
            self.hass.async_create_task(self._async_close_login_session())

    # -------------------------------------------------------------- opciones

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> SmartThingsFindOptionsFlow:
        """Devuelve el flujo de opciones."""
        return SmartThingsFindOptionsFlow()


class SmartThingsFindOptionsFlow(OptionsFlow):
    """Opciones: intervalo de consulta y modo activo/pasivo."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=opts.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_UPDATE_INTERVAL,
                        max=MAX_UPDATE_INTERVAL,
                        step=10,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_ACTIVE_MODE_TAGS,
                    default=opts.get(CONF_ACTIVE_MODE_TAGS, DEFAULT_ACTIVE_MODE_TAGS),
                ): bool,
                vol.Required(
                    CONF_ACTIVE_MODE_OTHERS,
                    default=opts.get(CONF_ACTIVE_MODE_OTHERS, DEFAULT_ACTIVE_MODE_OTHERS),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
