"""No-network unit tests for the `/lp.asp` client helpers.

Run with: ``python -m pytest tests/test_lp_api.py`` (requires ``aiohttp``,
which Home Assistant provides). These cover URL construction, the
set-value-vs-read-value mapping for sound programs, and boolean coercion --
all the pure logic that doesn't need a device.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "yamaha_soundbar")
)

import lp_api  # noqa: E402


def test_build_url_adk():
    url = lp_api.build_url(
        "1.2.3.4", lp_api.OBJ_SOUND, "setSoundProgram", {"soundProgram": "ALL MODE"}
    )
    assert url == (
        "https://1.2.3.4/lp.asp?dst=linkplay.adk&iface=linkplay.adk"
        "&obj=/qti/iotsys/soundSetting&method=setSoundProgram"
        "&payload=%7B%22soundProgram%22%3A%22ALL%20MODE%22%7D"
    )


def test_build_url_empty_payload():
    assert lp_api.build_url("h", lp_api.OBJ_SOUND, "getSoundSetting").endswith("payload=%7B%7D")


def test_build_url_system_omits_obj():
    # linkplay.system has no obj; an empty obj= makes the bar reset the connection.
    url = lp_api.build_url(
        "h", "", "getInfo", {"name": "none"}, dst=lp_api.DST_SYSTEM, iface=lp_api.IFACE_SYSTEM
    )
    assert "&obj=" not in url
    assert url == (
        "https://h/lp.asp?dst=linkplay.system&iface=linkplay.system"
        "&method=getInfo&payload=%7B%22name%22%3A%22none%22%7D"
    )


def test_program_from_read_mapping():
    assert lp_api.program_from_read("Stereo") == "STEREO"
    assert lp_api.program_from_read("Surround") == "SURROUND"
    assert lp_api.program_from_read("AllMode") == "ALL MODE"
    assert lp_api.program_from_read("ALL MODE") == "ALL MODE"


def test_program_from_read_passthrough_and_none():
    # Unknown future programs pass through unchanged rather than vanishing.
    assert lp_api.program_from_read("CinemaDSP") == "CinemaDSP"
    assert lp_api.program_from_read(None) is None


def test_coerce_bool():
    assert lp_api.coerce_bool(True) is True
    assert lp_api.coerce_bool("1") is True
    assert lp_api.coerce_bool("true") is True
    assert lp_api.coerce_bool(1) is True
    assert lp_api.coerce_bool("0") is False
    assert lp_api.coerce_bool(False) is False
    assert lp_api.coerce_bool("") is False
