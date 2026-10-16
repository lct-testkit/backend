"""B6-4: протокол INSTREAM клиента ClamAV на фейковом TCP-сервере.

Без БД, без настоящего clamd: `asyncio.start_server` читает чанки по тому же протоколу и
отвечает заранее заданной строкой — ровно то, что реальный clamd вернул бы, но без сети и
без антивирусной базы на машине разработчика.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from app.modules.files.antivirus import ClamAvScanner

# EICAR собирается конкатенацией из кусков (не хранить строку целиком — некоторые локальные
# антивирусы на машине разработчика реагируют на неё прямо в исходниках репозитория).
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


async def _read_instream_payload(reader: asyncio.StreamReader) -> bytes:
    """Читает команду `zINSTREAM\\0` и все чанки до нулевого — то, что прислал клиент."""
    command = await reader.readuntil(b"\x00")
    assert command == b"zINSTREAM\x00"
    payload = bytearray()
    while True:
        header = await reader.readexactly(4)
        (length,) = struct.unpack("!I", header)
        if length == 0:
            break
        payload += await reader.readexactly(length)
    return bytes(payload)


async def _serve_once(reply: bytes, *, read_payload: bool = True):
    """Поднимает сервер на localhost со случайным портом, отвечает `reply` на первое
    подключение. Возвращает `(host, port, server)`; вызывающий сам закрывает `server`."""
    received: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if read_payload:
                received.append(await _read_instream_payload(reader))
            writer.write(reply)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return host, port, server, received


class TestCleanAndInfected:
    async def test_clean_file_gives_ok(self) -> None:
        host, port, server, received = await _serve_once(b"stream: OK\x00")
        try:
            scanner = ClamAvScanner(
                host=host, port=port, connect_timeout=2, scan_timeout=2, chunk_size=1024
            )
            result = await scanner.scan_bytes(b"%PDF-1.4 just a clean pdf " * 200)
        finally:
            server.close()
            await server.wait_closed()

        assert result.clean is True
        assert result.error is False
        # Переданное клиентом совпадает по длине с суммой чанков (протокол не теряет байты).
        assert len(received[0]) == len(b"%PDF-1.4 just a clean pdf " * 200)

    async def test_eicar_payload_is_found(self) -> None:
        host, port, server, _ = await _serve_once(b"stream: Win.Test.EICAR_HDB-1 FOUND\x00")
        try:
            scanner = ClamAvScanner(host=host, port=port, connect_timeout=2, scan_timeout=2)
            result = await scanner.scan_bytes(EICAR)
        finally:
            server.close()
            await server.wait_closed()

        assert result.clean is False
        assert result.error is False
        assert result.signature == "Win.Test.EICAR_HDB-1"

    async def test_long_signature_name_is_not_truncated_by_the_client(self) -> None:
        long_name = "A" * 100
        host, port, server, _ = await _serve_once(f"stream: {long_name} FOUND\x00".encode())
        try:
            scanner = ClamAvScanner(host=host, port=port, connect_timeout=2, scan_timeout=2)
            result = await scanner.scan_bytes(b"irrelevant")
        finally:
            server.close()
            await server.wait_closed()

        assert result.signature == long_name


class TestErrors:
    async def test_server_error_reply_is_not_clean_and_is_marked_as_error(self) -> None:
        host, port, server, _ = await _serve_once(
            b"stream: INSTREAM size limit exceeded. ERROR\x00"
        )
        try:
            scanner = ClamAvScanner(host=host, port=port, connect_timeout=2, scan_timeout=2)
            result = await scanner.scan_bytes(b"x" * 10)
        finally:
            server.close()
            await server.wait_closed()

        assert result.clean is False
        assert result.error is True

    async def test_closed_port_is_a_connect_error(self) -> None:
        # Порт не прослушивается никем — быстрый ECONNREFUSED, не таймаут.
        scanner = ClamAvScanner(host="127.0.0.1", port=1, connect_timeout=1, scan_timeout=1)

        result = await scanner.scan_bytes(b"x")

        assert result.clean is False
        assert result.error is True

    async def test_server_accepts_and_stays_silent_is_a_timeout_within_scan_timeout(
        self,
    ) -> None:
        released = asyncio.Event()
        server_delay = 2  # заметно дольше scan_timeout ниже, но не раздувает тест
        scan_timeout = 0.3

        async def handle(reader, writer) -> None:
            await _read_instream_payload(reader)
            await asyncio.sleep(server_delay)
            released.set()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        host, port = server.sockets[0].getsockname()[:2]
        try:
            scanner = ClamAvScanner(
                host=host, port=port, connect_timeout=1, scan_timeout=scan_timeout
            )
            started = asyncio.get_event_loop().time()
            result = await scanner.scan_bytes(b"x" * 10)
            elapsed = asyncio.get_event_loop().time() - started
        finally:
            # `server.close()` останавливает слушающий socket, но не уже принятое соединение
            # (сервер держит его все server_delay секунд) — `wait_closed()` ждал бы этого
            # соединения и свёл бы тест обратно к ожиданию полного server_delay, поэтому здесь
            # просто дожидаемся завершения обработчика напрямую, не трогая wait_closed().
            server.close()
            await asyncio.wait_for(released.wait(), timeout=server_delay + 5)

        assert result.clean is False
        assert result.error is True
        assert elapsed < server_delay
        assert elapsed < 2  # с запасом над scan_timeout=0.3, но много меньше sleep(10)


@pytest.mark.parametrize(
    ("reply", "expected_clean", "expected_signature"),
    [
        (b"stream: OK\x00", True, None),
        (b"stream: Eicar-Signature FOUND\x00", False, "Eicar-Signature"),
    ],
)
async def test_parse_reply_matches_real_protocol_strings(reply, expected_clean, expected_signature):
    host, port, server, _ = await _serve_once(reply)
    try:
        scanner = ClamAvScanner(host=host, port=port, connect_timeout=2, scan_timeout=2)
        result = await scanner.scan_bytes(b"irrelevant payload")
    finally:
        server.close()
        await server.wait_closed()

    assert result.clean is expected_clean
    assert result.signature == expected_signature
