"""Tests del login QR y del cliente contra un servidor Samsung simulado."""

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
        self.polls = 0
        self.scanned_after = 2
        self.session_valid = True
        self.csrf_rotations = 0
        self.operations: list[dict] = []
        app = web.Application()
        r = app.router
        r.add_get("/gate", self.gate)
        r.add_get("/qr", self.qr_page)
        r.add_get("/xhr", self.xhr)
        r.add_post("/poll", self.poll)
        r.add_get("/complete", self.complete)
        r.add_get("/login.do", self.login_do)
        r.add_get("/chkLogin.do", self.chk)
        r.add_post("/getDeviceList.do", self.devices)
        r.add_post("/setLastSelect.do", self.last_select)
        r.add_post("/addOperation.do", self.add_operation)
        self.app = app

    async def gate(self, req):
        resp = web.Response(text="ok")
        resp.set_cookie("acct", "1")
        return resp

    async def qr_page(self, req):
        assert req.cookies.get("acct") == "1"
        return web.Response(
            text="<script>var u = 'https://signin.samsung.com/key/AbC123';</script>",
            content_type="text/html",
        )

    async def xhr(self, req):
        return web.json_response({"_csrf": {"token": "acct-csrf"}})

    async def poll(self, req):
        assert req.headers["X-Csrf-Token"] == "acct-csrf"
        self.polls += 1
        if self.polls < self.scanned_after:
            return web.json_response({"rtnCd": "POLLING"})
        return web.json_response({"rtnCd": "SUCCESS", "nextURL": "/complete"})

    async def complete(self, req):
        base = str(req.url.origin())
        return web.Response(
            text=f"<script>window.location.href = '{base}/login.do?code=xyz&amp;state=s';</script>",
            content_type="text/html",
        )

    async def login_do(self, req):
        assert req.query["code"] == "xyz"
        resp = web.Response(text="ok")
        resp.set_cookie("JSESSIONID", "SESSION-1")
        return resp

    def _authed(self, req) -> bool:
        return self.session_valid and req.cookies.get("JSESSIONID") == "SESSION-1"

    async def chk(self, req):
        if not self._authed(req):
            return web.Response(text="Logout")
        self.csrf_rotations += 1
        return web.Response(text="", headers={"_csrf": f"stf-csrf-{self.csrf_rotations}"})

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
    base = str(server.make_url(""))
    monkeypatch.setattr(stf_api, "ACCOUNT_BASE", base)
    monkeypatch.setattr(stf_api, "URL_SIGNIN_GATE", base + "/gate?state={state}&locale={locale}")
    monkeypatch.setattr(stf_api, "URL_QR_PAGE", base + "/qr")
    monkeypatch.setattr(stf_api, "URL_SIGNIN_XHR", base + "/xhr")
    monkeypatch.setattr(stf_api, "URL_QR_POLL", base + "/poll")
    monkeypatch.setattr(stf_api, "STF_URL", URL(base + "/"))
    monkeypatch.setattr(stf_api, "URL_CHK_LOGIN", base + "/chkLogin.do")
    monkeypatch.setattr(stf_api, "URL_DEVICE_LIST", base + "/getDeviceList.do")
    monkeypatch.setattr(stf_api, "URL_SET_LAST_SELECT", base + "/setLastSelect.do")
    monkeypatch.setattr(stf_api, "URL_ADD_OPERATION", base + "/addOperation.do")
    return fake


def _session():
    # unsafe=True permite cookies en 127.0.0.1 (sólo para los tests).
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


async def test_qr_login_flow(fake):
    async with _session() as session:
        login = stf_api.QrLogin(session)
        assert await login.start() == "https://signin.samsung.com/key/AbC123"
        jsessionid = await login.wait(timeout=5, poll_interval=0.01)
    assert jsessionid == "SESSION-1"
    assert fake.polls == 2


async def test_qr_login_timeout(fake):
    fake.scanned_after = 10_000
    async with _session() as session:
        login = stf_api.QrLogin(session)
        await login.start()
        with pytest.raises(stf_api.SmartThingsFindLoginTimeout):
            await login.wait(timeout=0.05, poll_interval=0.01)


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
