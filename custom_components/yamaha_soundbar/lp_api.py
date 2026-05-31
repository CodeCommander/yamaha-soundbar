"""Client for the LinkPlay `/lp.asp` RPC interface.

Newer Yamaha LinkPlay sound bars (SR-X40A / SR-X50A, Qualcomm `qcs-evb`
firmware) do not expose the classic flat `httpapi.asp?command=...` interface
-- it returns `404 Not Found` from the on-device `Boa` server. Instead they
use a JSON-RPC style endpoint:

    GET https://<host>/lp.asp?dst=<dst>&iface=<iface>&obj=<obj>&method=<method>&payload=<urlencoded-json>

The TLS server requires the same LinkPlay client certificate already shipped
with this integration as ``client.pem`` (mutual TLS), and presents a
self-signed server certificate, so verification must be disabled -- identical
to the existing ``httpapi`` code path.

This module is intentionally self-contained so the protocol can be unit tested
without Home Assistant. See issues #16 (SR-X50A) and #20 (SR-X40A).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from urllib.parse import quote

import aiohttp
import async_timeout

_LOGGER = logging.getLogger(__name__)

CERT_FILENAME = "client.pem"

# RPC routing constants observed from the official Sound Bar Controller app.
DST_ADK = "linkplay.adk"
IFACE_ADK = "linkplay.adk"
DST_SYSTEM = "linkplay.system"
IFACE_SYSTEM = "linkplay.system"

OBJ_SOUND = "/qti/iotsys/soundSetting"
OBJ_ALLPLAY = "/qti/iotsys/allplay"

# Sound programs for this firmware family. These are the values you SET.
SOUND_PROGRAM_STEREO = "STEREO"
SOUND_PROGRAM_SURROUND = "SURROUND"
SOUND_PROGRAM_ALL = "ALL MODE"
SOUND_PROGRAMS = [SOUND_PROGRAM_STEREO, SOUND_PROGRAM_SURROUND, SOUND_PROGRAM_ALL]

# getSoundSetting reports a normalized casing that differs from the set value,
# e.g. you set "ALL MODE" but read back "AllMode". Map read -> canonical set.
_READ_TO_PROGRAM = {
    "stereo": SOUND_PROGRAM_STEREO,
    "surround": SOUND_PROGRAM_SURROUND,
    "allmode": SOUND_PROGRAM_ALL,
    "all mode": SOUND_PROGRAM_ALL,
}

# Boolean soundSetting fields settable via set<Field> with payload {"<field>":"1"|"0"}.
TOGGLE_FIELDS = ("clearVoice", "bassExtension", "powerSaving", "nightMode")


def build_url(host, obj, method, payload=None, dst=DST_ADK, iface=IFACE_ADK):
    """Build an `/lp.asp` request URL. `payload` is a dict (default {}).

    `obj` is omitted entirely when empty -- the `linkplay.system` interface
    has no object and the bar resets the connection if an empty `obj=` is sent.
    """
    encoded = quote(json.dumps(payload if payload is not None else {}, separators=(",", ":")))
    obj_part = f"&obj={obj}" if obj else ""
    return (
        f"https://{host}/lp.asp?dst={dst}&iface={iface}"
        f"{obj_part}&method={method}&payload={encoded}"
    )


def program_from_read(value):
    """Map a getSoundSetting `soundProgram` read value to a canonical set value.

    Returns the original value unchanged if it isn't recognized, so unknown
    future programs still surface rather than disappearing.
    """
    if value is None:
        return None
    return _READ_TO_PROGRAM.get(str(value).strip().lower(), value)


def coerce_bool(value):
    """Normalize the bar's truthy representations ("1"/"0"/true/false) to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


class LinkPlayLpApiError(Exception):
    """Raised when an `/lp.asp` call fails or returns an unusable response."""


class LinkPlayLpClient:
    """Minimal async client for the `/lp.asp` interface (one bar, one host)."""

    def __init__(self, host, cert_path=None, timeout=10):
        self._host = host
        self._cert_path = cert_path or os.path.join(os.path.dirname(__file__), CERT_FILENAME)
        self._timeout = timeout
        self._ssl_ctx = None

    async def _ssl_context(self):
        """Build (once) the mTLS context with the LinkPlay client cert."""
        if self._ssl_ctx is not None:
            return self._ssl_ctx
        loop = asyncio.get_event_loop()
        ctx = await loop.run_in_executor(None, ssl.create_default_context, ssl.Purpose.SERVER_AUTH)
        await loop.run_in_executor(None, ctx.load_cert_chain, self._cert_path)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self._ssl_ctx = ctx
        return ctx

    async def call(self, obj, method, payload=None, dst=DST_ADK, iface=IFACE_ADK):
        """Perform one `/lp.asp` call; return the parsed JSON dict.

        Returns None on transport failure or an empty body (the bar returns an
        empty body for unknown obj/method combinations).
        """
        url = build_url(self._host, obj, method, payload, dst, iface)
        ssl_ctx = await self._ssl_context()
        conn = aiohttp.TCPConnector(ssl_context=ssl_ctx)
        session = aiohttp.ClientSession(connector=conn)
        try:
            async with async_timeout.timeout(self._timeout):
                response = await session.get(url)
                text = await response.text()
        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            _LOGGER.warning("lp.asp call failed (%s %s): %s", obj, method, type(error).__name__)
            return None
        finally:
            await session.close()

        if not text or not text.strip():
            _LOGGER.debug("lp.asp empty response for %s %s (unknown obj/method?)", obj, method)
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            _LOGGER.debug("lp.asp non-JSON response for %s %s: %s", obj, method, text[:120])
            return None

    # -- High level helpers -------------------------------------------------

    async def get_system_info(self):
        """Device info (replacement for `getStatusEx`)."""
        return await self.call(
            "", "getInfo", {"name": "none"}, dst=DST_SYSTEM, iface=IFACE_SYSTEM
        )

    async def get_sound_setting(self):
        """Return the `soundSetting` dict, or None."""
        data = await self.call(OBJ_SOUND, "getSoundSetting")
        if isinstance(data, dict):
            return data.get("soundSetting")
        return None

    async def get_allplay_setting(self):
        """Return the `allplaySetting` dict (surround/sub speakers), or None."""
        data = await self.call(OBJ_ALLPLAY, "getAllplaySetting")
        if isinstance(data, dict):
            return data.get("allplaySetting")
        return None

    async def set_sound_program(self, program):
        """Set the sound program. `program` should be one of SOUND_PROGRAMS."""
        return await self.call(
            OBJ_SOUND, "setSoundProgram", {"soundProgram": program}
        )

    async def set_toggle(self, field, on):
        """Set a boolean soundSetting field (e.g. 'nightMode', 'clearVoice')."""
        if field not in TOGGLE_FIELDS:
            raise LinkPlayLpApiError(f"unknown toggle field: {field}")
        method = "set" + field[0].upper() + field[1:]
        return await self.call(OBJ_SOUND, method, {field: "1" if on else "0"})

    async def set_subwoofer_volume(self, level):
        """Set subwoofer volume (0..10)."""
        return await self.call(
            OBJ_ALLPLAY, "setSubwooferVolume", {"subwooferVolume": str(level)}
        )

    async def set_surround_r_volume(self, level):
        """Set surround-rear volume (0..10)."""
        return await self.call(
            OBJ_ALLPLAY, "setSurroundRVolume", {"surroundRVolume": str(level)}
        )
