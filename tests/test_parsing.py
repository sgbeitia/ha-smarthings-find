"""Tests del parseo de respuestas de SmartThings Find."""

from datetime import UTC, datetime

import stf_api
from stf_api import (
    StfDevice,
    calc_accuracy,
    extract_battery,
    extract_location,
    parse_device_list,
    parse_device_state,
    parse_stf_date,
    safe_icon_url,
)


def test_parse_date():
    assert parse_stf_date("20261105183000") == datetime(2026, 11, 5, 18, 30, tzinfo=UTC)
    assert parse_stf_date(None) is None
    assert parse_stf_date("garbage") is None


def test_accuracy():
    assert calc_accuracy("30", None) == 30.0
    assert calc_accuracy("3", "4") == 5.0
    assert calc_accuracy(None, "4") is None


def test_device_from_api_unescapes_name():
    dev = StfDevice.from_api(
        {
            "dvceID": "abc",
            "usrId": "u1",
            "modelName": "Sergio&amp;#39;s llaves",
            "modelID": "EI-T5600",
            "deviceTypeCode": "TAG",
            "icons": {"coloredIcon": "https://img.samsungcloud.com/icon.png"},
        }
    )
    assert dev.name == "Sergio's llaves"
    assert dev.is_tag
    assert dev.icon_url == "https://img.samsungcloud.com/icon.png"


def test_newest_plain_location_wins():
    ops = [
        {
            "oprnType": "LASTLOC",
            "latitude": "40.0",
            "longitude": "-3.0",
            "horizontalUncertainty": "50",
            "extra": {"gpsUtcDt": "20261105100000"},
        },
        {
            "oprnType": "LOCATION",
            "latitude": "40.4168",
            "longitude": "-3.7038",
            "horizontalUncertainty": "12",
            "extra": {"gpsUtcDt": "20261105120000"},
        },
        {"oprnType": "CHECK_CONNECTION", "battery": "MEDIUM"},
    ]
    loc, enc = extract_location(ops)
    assert not enc
    assert loc.latitude == 40.4168
    assert loc.longitude == -3.7038
    assert loc.accuracy == 12.0
    assert loc.operation == "LOCATION"


def test_enc_location_plain_and_encrypted():
    ops = [
        {
            "oprnType": "OFFLINE_LOC",
            "encLocation": {"encrypted": True, "data": "xxxx"},
        },
        {
            "oprnType": "OFFLINE_LOC",
            "encLocation": {
                "encrypted": False,
                "latitude": "43.26",
                "longitude": "-2.93",
                "horizontalUncertainty": "20",
                "gpsUtcDt": "20261105130000",
            },
        },
    ]
    loc, enc = extract_location(ops)
    assert enc
    assert (loc.latitude, loc.longitude) == (43.26, -2.93)


def test_only_encrypted_location_is_flagged():
    state = parse_device_state(
        {"operation": [{"oprnType": "OFFLINE_LOC", "encLocation": {"encrypted": True}}]}
    )
    assert state.location is None
    assert state.encrypted_location_pending


def test_invalid_coordinates_ignored():
    ops = [
        {
            "oprnType": "LOCATION",
            "latitude": "0",
            "longitude": "0",
            "extra": {"gpsUtcDt": "20261105120000"},
        },
        {
            "oprnType": "LOCATION",
            "latitude": "abc",
            "longitude": "1",
            "extra": {"gpsUtcDt": "20261105120000"},
        },
        {"oprnType": "LOCATION", "latitude": "10", "longitude": "1"},  # sin fecha
    ]
    assert extract_location(ops) == (None, False)


def test_battery_levels():
    assert extract_battery([{"oprnType": "CHECK_CONNECTION", "battery": "FULL"}]) == (100, "FULL")
    assert extract_battery([{"oprnType": "CHECK_CONNECTION", "battery": "87"}]) == (87, "87")
    assert extract_battery([{"oprnType": "CHECK_CONNECTION", "battery": "??"}]) == (None, "??")
    assert extract_battery([{"oprnType": "LOCATION"}]) == (None, None)


def test_state_without_operations():
    state = parse_device_state({})
    assert state.location is None and state.battery is None


def test_icon_url_only_from_samsung_https():
    ok = "https://img.samsungcloud.com/tag.png"
    assert safe_icon_url(ok) == ok
    assert safe_icon_url("https://www.samsung.com/x.png")
    assert safe_icon_url("http://img.samsungcloud.com/tag.png") is None
    assert safe_icon_url("https://tracker.example/pixel.gif") is None
    assert safe_icon_url("https://samsung.com.evil.example/x.png") is None
    assert safe_icon_url("javascript:alert(1)") is None
    assert safe_icon_url(None) is None


def test_qr_regex_rejects_markdown_injection():
    page = "x='https://signin.samsung.com/key/AbC-12_3)](https://evil.example)'"
    assert stf_api._QR_URL_RE.search(page).group(0) == "https://signin.samsung.com/key/AbC-12_3"


def test_device_list_includes_other_members_and_new_fields():
    data = {
        "deviceList": [
            # Formato antiguo (SmartTag+)
            {
                "dvceID": "t1",
                "usrId": "me",
                "modelName": "Llaves",
                "modelID": "EI-T7300",
                "deviceTypeCode": "TAG",
            },
            # Formato actual de la web: deviceType en lugar de deviceTypeCode
            {
                "dvceID": "t2",
                "usrId": "me",
                "modelName": "Mochila",
                "modelID": "EI-T5600",
                "deviceType": "TAG",
                "subType": "TAG2",
            },
        ],
        "childrenDeviceList": [
            {
                "userName": "Ana",
                "deviceList": [
                    {
                        "dvceId": "t3",
                        "usrId": "ana",
                        "modelName": "Bici",
                        "modelID": "EI-T5600",
                        "deviceType": "TAG",
                    },
                    # Duplicado de un dispositivo propio: no debe repetirse
                    {"dvceID": "t2", "usrId": "me", "modelName": "Mochila", "deviceType": "TAG"},
                    {"usrId": "x"},  # sin id: se ignora
                ],
            }
        ],
    }
    devices, summary = parse_device_list(data)
    assert [d.device_id for d in devices] == ["t1", "t2", "t3"]
    assert all(d.is_tag for d in devices)
    assert devices[1].sub_type == "TAG2"
    assert devices[2].owner == "Ana" and devices[0].owner is None
    assert summary["lists"] == {"deviceList": 2, "childrenDeviceList[0]": 3}
    assert summary["skipped"] == 1
    assert summary["devices"][3]["duplicate"] is True

    # El resumen es anónimo: ni ids, ni nombres, ni usuarios.
    text = str(summary)
    for secret in ("t1", "t2", "t3", "Llaves", "Mochila", "Bici", "Ana", "me", "ana"):
        assert f"'{secret}'" not in text


def test_device_list_only_children():
    devices, _ = parse_device_list(
        {"childrenDeviceList": [{"userName": "Ana", "deviceList": [{"dvceID": "z"}]}]}
    )
    assert [d.device_id for d in devices] == ["z"]
