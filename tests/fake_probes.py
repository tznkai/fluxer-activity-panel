# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fake game-query servers bound to 127.0.0.1 on port 0: Bedrock and A2S over UDP, Java status over TCP.

Each fake is an async context manager that yields `(port, recorder)`. The recorder keeps every request the
server received, so a test can check how many requests a probe sent and what they contained.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

LOOPBACK = "127.0.0.1"
Reply = Callable[[bytes, int], bytes | None]  # (datagram, index of this request) -> bytes to send, or None

_RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
_RAKNET_GUID = b"\x00" * 8
_A2S_HEADER = b"\xff\xff\xff\xff"
_A2S_REQUEST = _A2S_HEADER + b"\x54Source Engine Query\x00"


@dataclass
class Recorder:
    requests: list[bytes] = field(default_factory=list)  # UDP datagrams, or each TCP packet body in arrival order
    connections: int = 0


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, recorder: Recorder, reply: Reply) -> None:
        self._recorder = recorder
        self._reply = reply
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        index = len(self._recorder.requests)
        self._recorder.requests.append(data)
        answer = self._reply(data, index)
        if answer is not None and self._transport is not None:
            self._transport.sendto(answer, addr)


@asynccontextmanager
async def udp_server(reply: Reply) -> AsyncIterator[tuple[int, Recorder]]:
    """A UDP endpoint that answers each datagram with `reply`, or stays silent when `reply` returns None."""
    recorder = Recorder()
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _Datagrams(recorder, reply), local_addr=(LOOPBACK, 0)
    )
    try:
        port = transport.get_extra_info("sockname")[1]
        yield port, recorder
    finally:
        transport.close()


def _raknet_pong(fields: list[str]) -> Callable[[bytes, int], bytes | None]:
    payload = ";".join(fields).encode("utf-8")

    def reply(datagram: bytes, index: int) -> bytes | None:
        if not datagram.startswith(b"\x01"):
            return None
        time_bytes = datagram[1:9]
        return b"\x1c" + time_bytes + _RAKNET_GUID + _RAKNET_MAGIC + struct.pack(">H", len(payload)) + payload

    return reply


def bedrock_reply(fields: list[str]) -> Reply:
    """An unconnected pong. `fields` are the MOTD fields in this order, which mcstatus reads by index:

    edition; motd; protocol; version; online; max; server_id; map; gamemode
    """
    return _raknet_pong(fields)


def _cstring(text: str) -> bytes:
    return text.encode("utf-8") + b"\x00"


def _info_body(info: dict[str, object], truncate_after_map: bool) -> bytes:
    body = bytearray(b"\x11")  # protocol
    body += _cstring("Fake Server")
    body += _cstring(str(info["map"]))
    if truncate_after_map:
        return bytes(body)
    body += _cstring("fake") + _cstring("Fake Game")
    body += struct.pack("<H", 0)  # app id
    body += bytes([int(info["players"]), int(info["max"]), 0])  # players, max, bots
    body += b"dl"  # dedicated, linux
    body += bytes([0, 0])  # password, vac
    body += _cstring(str(info["version"]))
    body += b"\x00"  # extra data flag
    return bytes(body)


def a2s_reply(
    info: dict[str, object],
    *,
    challenge: bytes | None = None,
    always_challenge: bool = False,
    truncate_after_map: bool = False,
    unknown_header: bool = False,
) -> Reply:
    """A Source A2S_INFO responder.

    With `challenge` set, a request that does not already carry the challenge gets a 0x41 reply with it
    (`always_challenge` makes every request get one). Otherwise a 0x49 reply carries `info`, which needs the
    keys `players`, `max`, `map` and `version`.
    """

    def reply(datagram: bytes, index: int) -> bytes | None:
        if unknown_header:
            return _A2S_HEADER + b"\x99"
        if challenge is not None and (always_challenge or datagram != _A2S_REQUEST + challenge):
            return _A2S_HEADER + b"\x41" + challenge
        return _A2S_HEADER + b"\x49" + _info_body(info, truncate_after_map)

    return reply


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


async def read_varint(reader: asyncio.StreamReader) -> int:
    value = 0
    for position in range(5):
        byte = (await reader.readexactly(1))[0]
        value |= (byte & 0x7F) << (7 * position)
        if not byte & 0x80:
            return value
    raise ValueError("varint is longer than five bytes")


async def _read_packet(reader: asyncio.StreamReader) -> bytes:
    length = await read_varint(reader)
    return await reader.readexactly(length)


@asynccontextmanager
async def java_server(status_json: bytes | None) -> AsyncIterator[tuple[int, Recorder]]:
    """A Java status endpoint. It reads the handshake and the status request, then replies with `status_json`.

    With `status_json` None it reads both and then holds the connection open without replying.
    """
    recorder = Recorder()
    writers: list[asyncio.StreamWriter] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        recorder.connections += 1
        writers.append(writer)
        try:
            recorder.requests.append(await _read_packet(reader))  # handshake
            recorder.requests.append(await _read_packet(reader))  # status request
            if status_json is None:
                await reader.read()  # hold the connection until the client or the fake closes it
                return
            body = b"\x00" + varint(len(status_json)) + status_json
            writer.write(varint(len(body)) + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, LOOPBACK, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, recorder
    finally:
        server.close()
        for writer in writers:
            writer.close()
        await server.wait_closed()


def closed_port() -> int:
    """A loopback TCP port with nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]
