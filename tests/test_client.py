"""Tests del login QR y del cliente contra un servidor Samsung simulado.

El servidor simulado reproduce el flujo observado en los servidores reales de
Samsung Account (login "IAM", 2026) y de SmartThings Find.
"""

from __future__ import annotations

import aiohttp
from aiohttp import web
import pytest
from yarl import URL

import stf_api

pytestmark = pytest.mark.asyncio


class FakeSamsung:
    """Simula account.samsung.com + smartthingsfind.samsung.com en un único host."""

    def __init__(self) -> None:
        # Comportamiento configurable desde los tests.
        self.pin_after = 2  # nº de consultas hasta que aparece el PIN
        self.login_after = 4  # nº de consultas hasta que se aprueba
        self.send_pin = True
        self.final_page = "complete"
        self.redirect_to: str | None = None  # Location final (None = login.do válido)
        self.require_sa_did = True
        self.session_valid = True

        self.polls = 0
        self.csrf_rotations = 0
        self.operations: list[dict] = []
        self.calls: list[str] = []
        app = web.Application()
        r = app.router
        # SmartThings Find (pre-login)
        r.add_get("/getState.do", self.get_state)
        # Samsung Account
        r.add_get("/iam/oauth2/authorize", self.authorize)
        r.add_post("/api/auths", self.create_auth)
        r.add_post("/api/challenges/credentials", self.create_challenge)
        r.add_post("/api/challenges/credentials/{key}/qr-codes", self.create_qr)
        r.add_get("/api/challenges/credentials/{key}/qr-codes", self.poll_qr)
        r.add_patch("/api/challenges/credentials/{key}/qr-codes", self.confirm_qr)
        r.add_patch("/api/contexts/auths/{ctx}", self.update_context)
        r.add_get("/api/auths/{ctx}", self.get_auth)
        r.add_get("/api/auths/{ctx}/url", self.auth_url)
        # SmartThings Find
        r.add_get("/login.do", self.login_do)
        r.add_get("/init.do", self.init)
        r.add_get("/chkLogin.do", self.chk)
        r.add_post("/device/getDeviceList.do", self.devices)
        r.add_post("/device/setLastSelect.do", self.last_select)
        r.add_post("/dm/addOperation.do", self.add_operation)
        self.app = app

    # ---- login ---------------------------------------------------------

    async def get_state(self, req):
        assert req.query["payload"] == "hound"
        resp = web.Response(text='{"state":"STATE-123::hound"}', content_type="text/plain")
        resp.set_cookie("JSESSIONID", "PRELOGIN")
        return resp

    async def authorize(self, req):
        assert req.query["state"] == "STATE-123::hound"
        assert req.query["client_id"] == "ntly6zvfpn"
        return web.Response(text="<html></html>", content_type="text/html")

    async def create_auth(self, req):
        body = await req.json()
        assert req.headers["x-app-id"] == "ntly6zvfpn"
        assert body["authType"] == "OAUTH2"
        assert body["state"] == "STATE-123::hound"
        self.calls.append("auths")
        return web.json_response({"key": "CTX1"}, status=201)

    async def create_challenge(self, req):
        assert (await req.json()) == {"contextKey": "CTX1"}
        return web.json_response({"key": "CH1"}, status=201)

    async def create_qr(self, req):
        if self.require_sa_did and not req.cookies.get("sa_did"):
            return web.json_response(
                {"error": {"code": "BadArgument", "message": "Required cookie 'sa_did'"}},
                status=400,
            )
        assert (await req.json()) == {"deviceType": "Web"}
        return web.json_response(
            {
                "userCode": "abcd1234",
                "expiryInSeconds": "600",
                "expiresAt": 0,
                "pollingInterval": "2",
                "url": "https://signin.samsung.com/key/",
            },
            status=201,
        )

    async def poll_qr(self, req):
        self.polls += 1
        if self.polls >= self.login_after:
            return web.json_response({"userPin": "42", "loginId": "LOGIN-1"})
        if self.send_pin and self.polls >= self.pin_after:
            return web.json_response({"userPin": "42"})
        return web.json_response({})

    async def confirm_qr(self, req):
        self.calls.append("confirm_qr")
        return web.Response(status=204)

    async def update_context(self, req):
        assert (await req.json()) == {"challengeKey": "CH1", "step": "CREDENTIAL"}
        self.calls.append("update_context")
        return web.Response(status=204)

    async def get_auth(self, req):
        return web.json_response({"page": self.final_page})

    async def auth_url(self, req):
        assert req.query["state"] == "STATE-123::hound"
        location = self.redirect_to or (
            f"{req.url.origin()}/login.do?code=xyz&state=STATE-123::hound"
        )
        return web.Response(status=302, headers={"Location": location})

    async def login_do(self, req):
        assert req.query["code"] == "xyz"
        assert req.cookies.get("JSESSIONID") == "PRELOGIN"
        resp = web.Response(text="ok")
        resp.set_cookie("JSESSIONID", "SESSION-1")
        return resp

    # ---- datos ---------------------------------------------------------

    def _authed(self, req) -> bool:
        return self.session_valid and req.cookies.get("JSESSIONID") == "SESSION-1"

    async def init(self, req):
        if not self._authed(req):
            return web.Response(status=403, text="logout")
        self.csrf_rotations += 1
        return web.json_response({}, headers={"_csrf": f"stf-csrf-{self.csrf_rotations}"})

    async def chk(self, req):
        return web.Response(text="fail")

    def _check_csrf(self, req):
        if not self._authed(req):
            return web.Response(status=401, text="Logout")
        if req.query.get("_csrf") != f"stf-csrf-{self.csrf_rotations}":
            return web.Response(status=404, text="bad csrf")
        return None

    async def devices(self, req):
        if (err := self._check_csrf(req)) is not None:
            return err
        return web.json_response(
            {
                "deviceList": [
                    {
                        "dvceID": "tag1",
                        "usrId": "user-9",
                        "modelName": "Llaves",
                        "modelID": "EI-T5300",
                        "deviceTypeCode": "TAG",
                    },
                    {
                        "dvceID": "tag2",
                        "usrId": "user-9",
                        "modelName": "Mochila",
                        "modelID": "EI-T5600",
                        "deviceTypeCode": "TAG",
                    },
                ]
            }
        )

    async def last_select(self, req):
        if (err := self._check_csrf(req)) is not None:
            return err
        body = await req.json()
        assert body["dvceId"] in ("tag1", "tag2")
        return web.json_response(
            {
                "operation": [
                    {
                        "oprnType": "LOCATION",
                        "latitude": "43.2630",
                        "longitude": "-2.9350",
                        "horizontalUncertainty": "15",
                        "extra": {"gpsUtcDt": "20261105120000"},
                    },
                    {"oprnType": "CHECK_CONNECTION", "battery": "LOW"},
                ]
            }
        )

    async def add_operation(self, req):
        if (err := self._check_csrf(req)) is not None:
            return err
        self.operations.append(await req.json())
        return web.Response(text="")


@pytest.fixture
def _allow_localhost_sockets():
    """pytest-homeassistant bloquea sockets; aquí usamos un servidor local."""
    try:
        import pytest_socket
    except ImportError:
        return
    pytest_socket.enable_socket()


@pytest.fixture
async def fake(_allow_localhost_sockets, monkeypatch, aiohttp_server):
    fake = FakeSamsung()
    server = await aiohttp_server(fake.app)
    base = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(stf_api, "ACCOUNT_BASE", base)
    monkeypatch.setattr(stf_api, "ACCOUNT_API", base + "/api")
    monkeypatch.setattr(stf_api, "URL_AUTHORIZE", base + "/iam/oauth2/authorize")
    monkeypatch.setattr(stf_api, "STF_BASE", base)
    monkeypatch.setattr(stf_api, "STF_URL", URL(base + "/"))
    monkeypatch.setattr(stf_api, "URL_GET_STATE", base + "/getState.do")
    monkeypatch.setattr(stf_api, "URL_INIT", base + "/init.do")
    monkeypatch.setattr(stf_api, "URL_CHK_LOGIN", base + "/chkLogin.do")
    monkeypatch.setattr(stf_api, "URL_DEVICE_LIST", base + "/device/getDeviceList.do")
    monkeypatch.setattr(stf_api, "URL_SET_LAST_SELECT", base + "/device/setLastSelect.do")
    monkeypatch.setattr(stf_api, "URL_ADD_OPERATION", base + "/dm/addOperation.do")
    return fake


def _session():
    # unsafe=True permite cookies en 127.0.0.1 (sólo para los tests).
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


async def test_qr_login_flow_with_pin(fake):
    async with _session() as session:
        login = stf_api.QrLogin(session)
        assert await login.start() == "https://signin.samsung.com/key/abcd1234"
        assert await login.wait_for_scan(timeout=5, poll_interval=0.01) == "42"
        await login.wait_for_approval(timeout=5, poll_interval=0.01)
        assert await login.finish() == "SESSION-1"
    assert fake.calls == ["auths", "confirm_qr", "update_context"]


async def test_qr_login_without_pin(fake):
    fake.send_pin = False
    async with _session() as session:
        login = stf_api.QrLogin(session, locale="en_GB")
        await login.start()
        assert await login.wait_for_scan(timeout=5, poll_interval=0.01) is None
        await login.wait_for_approval(timeout=5, poll_interval=0.01)  # no espera más
        assert await login.finish() == "SESSION-1"


async def test_qr_login_shortcut(fake):
    async with _session() as session:
        login = stf_api.QrLogin(session)
        await login.start()
        assert await login.wait(timeout=5, poll_interval=0.01) == "SESSION-1"


async def test_qr_login_timeout(fake):
    fake.pin_after = fake.login_after = 10_000
    async with _session() as session:
        login = stf_api.QrLogin(session)
        await login.start()
        with pytest.raises(stf_api.SmartThingsFindLoginTimeout):
            await login.wait_for_scan(timeout=0.05, poll_interval=0.01)


async def test_qr_login_extra_step_is_reported(fake):
    fake.final_page = "mfaVerification"
    async with _session() as session:
        login = stf_api.QrLogin(session)
        await login.start()
        await login.wait_for_scan(timeout=5, poll_interval=0.01)
        await login.wait_for_approval(timeout=5, poll_interval=0.01)
        with pytest.raises(stf_api.SmartThingsFindError, match="mfaVerification"):
            await login.finish()


async def test_qr_requires_device_id_cookie(fake):
    """Regresión: sin la cookie ``sa_did`` Samsung no genera el QR."""
    async with _session() as session:
        login = stf_api.QrLogin(session)
        assert (await login.start()).startswith(stf_api.QR_URL_PREFIX)
        assert session.cookie_jar.filter_cookies(URL(stf_api.ACCOUNT_BASE + "/")).get("sa_did")


async def test_client_devices_state_ring(fake):
    async with _session() as session:
        client = stf_api.SmartThingsFindClient(session, "SESSION-1")
        await client.async_refresh_csrf()
        devices = await client.async_get_devices()
        assert [d.name for d in devices] == ["Llaves", "Mochila"]

        state = await client.async_get_state(devices[0])
        assert state.location.latitude == 43.263
        assert state.battery == 15

        await client.async_request_location(devices[0])
        await client.async_ring(devices[1])
    assert [op["operation"] for op in fake.operations] == [
        "CHECK_CONNECTION_WITH_LOCATION",
        "RING",
    ]


async def test_client_renews_expired_csrf(fake):
    async with _session() as session:
        client = stf_api.SmartThingsFindClient(session, "SESSION-1")
        await client.async_refresh_csrf()
        fake.csrf_rotations += 5  # el servidor invalida el CSRF que tenemos
        devices = await client.async_get_devices()
        assert len(devices) == 2


async def test_client_detects_expired_session(fake):
    async with _session() as session:
        client = stf_api.SmartThingsFindClient(session, "SESSION-1")
        await client.async_refresh_csrf()
        fake.session_valid = False
        with pytest.raises(stf_api.SmartThingsFindAuthError):
            await client.async_get_devices()

    async with _session() as session:
        client = stf_api.SmartThingsFindClient(session, "WRONG")
        with pytest.raises(stf_api.SmartThingsFindAuthError):
            await client.async_refresh_csrf()


@pytest.mark.parametrize(
    "redirect_to",
    [
        "https://evil.example/login.do?code=xyz",
        "//evil.example/login.do?code=xyz",
        "http://smartthingsfind.samsung.com/login.do?code=xyz",
    ],
)
async def test_login_refuses_foreign_redirects(fake, redirect_to):
    """El código de autorización nunca debe enviarse fuera de SmartThings Find."""
    fake.redirect_to = redirect_to
    async with _session() as session:
        login = stf_api.QrLogin(session)
        await login.start()
        await login.wait_for_scan(timeout=5, poll_interval=0.01)
        await login.wait_for_approval(timeout=5, poll_interval=0.01)
        with pytest.raises(stf_api.SmartThingsFindError, match="Destino inesperado"):
            await login.finish()


async def test_errors_do_not_leak_csrf_or_body(fake, monkeypatch):
    async with _session() as session:
        client = stf_api.SmartThingsFindClient(session, "SESSION-1")
        await client.async_refresh_csrf()
        monkeypatch.setattr(
            stf_api, "URL_DEVICE_LIST", stf_api.URL_DEVICE_LIST.replace("getDeviceList", "missing")
        )
        with pytest.raises(stf_api.SmartThingsFindError) as exc:
            await client.async_get_devices()
    assert "csrf" not in str(exc.value).lower()
