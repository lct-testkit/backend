"""Реальный антивирус ClamAV через протокол INSTREAM (B6-4, группа АВЗ приказа ФСТЭК № 117).

`NullAntivirusScanner` (`files/service.py`) всегда отвечает «чисто» — заглушка, явно
помеченная как временная ещё в разделе 2.2 спецификации. Эта задача подключает настоящий
сканер без системных пакетов (принцип `Dockerfile`/`pyproject.toml`): clamd — отдельный
контейнер (спринт D, не в этом репозитории), сюда ходим по TCP своим asyncio-клиентом,
никакой `pyclamd`/`clamd`-библиотеки не тянем.

Протокол INSTREAM (https://docs.clamav.net/manual/Usage/Scanning.html):

    zINSTREAM\\0  →  <4 байта big-endian длина чанка><чанк>  ...  <4 байта 0x00000000>
    ←  "stream: OK\\0"  (чисто)  |  "stream: <имя> FOUND\\0"  (заражён)  |  "... ERROR\\0"
"""

from __future__ import annotations

import asyncio
import struct

import structlog

from app.core.config import Settings
from app.core.storage import iter_object_chunks
from app.modules.files.service import ScanResult

logger = structlog.get_logger(__name__)

_INSTREAM_COMMAND = b"zINSTREAM\x00"
_END_OF_STREAM = struct.pack("!I", 0)
#: Клиент сам режет файл на эти чанки для передачи clamd (раздел 9: лимит потока на стороне
#: сервера настраивается через `clamd.conf StreamMaxLength`, тут — просто размер TCP-записи).
_DEFAULT_CHUNK_SIZE = 262144


async def _read_until_null(reader: asyncio.StreamReader) -> bytes:
    """Таймаут — забота вызывающей стороны (`async with asyncio.timeout(...)`), не этой
    функции: ruff ASYNC109 запрещает `timeout`-параметр у корутины именно по этой причине —
    `asyncio.wait_for` внутри и `asyncio.timeout` снаружи иначе дают два независимых таймера."""
    data = await reader.readuntil(b"\x00")
    return data[:-1]  # без замыкающего \0


class ClamAvScanner:
    """Сканер по протоколу INSTREAM. Один объект на процесс (как и `NullAntivirusScanner`),
    соединение открывается на каждый вызов `scan` — пул соединений clamd не требует (та же
    логика, что у `_client()` в `core/storage.py`: простота важнее микро-оптимизации, скан —
    не самая частая операция).
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        connect_timeout: float,
        scan_timeout: float,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> None:
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._scan_timeout = scan_timeout
        self._chunk_size = chunk_size

    async def ping(self) -> bool:
        """`PING` → `PONG` — для `/health/ready` (не подключён в этом эпике, см. отчёт)."""
        try:
            async with asyncio.timeout(self._connect_timeout):
                reader, writer = await asyncio.open_connection(self._host, self._port)
        except (OSError, TimeoutError):
            return False
        try:
            async with asyncio.timeout(self._scan_timeout):
                writer.write(b"zPING\x00")
                await writer.drain()
                reply = await _read_until_null(reader)
            return reply == b"PONG"
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            return False
        finally:
            writer.close()

    async def scan_bytes(self, content: bytes) -> ScanResult:
        """Сканирует уже прочитанные байты — используется тестами на фейковом сервере и может
        пригодиться для небольших объектов без похода в S3 за ними повторно."""

        async def _chunks():
            for start in range(0, len(content), self._chunk_size):
                yield content[start : start + self._chunk_size]

        return await self._scan_stream(_chunks())

    async def scan(self, *, bucket: str, key: str) -> ScanResult:
        chunks = iter_object_chunks(bucket=bucket, key=key, chunk_size=self._chunk_size)
        return await self._scan_stream(chunks)

    async def _scan_stream(self, chunks) -> ScanResult:
        try:
            async with asyncio.timeout(self._connect_timeout):
                reader, writer = await asyncio.open_connection(self._host, self._port)
        except (OSError, TimeoutError) as exc:
            logger.warning("clamav_connect_failed", error=str(exc))
            return ScanResult(clean=False, error=True, detail="connect_failed")

        try:
            async with asyncio.timeout(self._scan_timeout):
                writer.write(_INSTREAM_COMMAND)
                async for chunk in chunks:
                    if not chunk:
                        continue
                    writer.write(struct.pack("!I", len(chunk)) + chunk)
                    await writer.drain()
                writer.write(_END_OF_STREAM)
                await writer.drain()

                reply = await _read_until_null(reader)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            logger.warning("clamav_scan_failed", error=str(exc))
            return ScanResult(clean=False, error=True, detail="scan_failed")
        finally:
            writer.close()

        text = reply.decode("utf-8", errors="replace")
        return self._parse_reply(text)

    @staticmethod
    def _parse_reply(text: str) -> ScanResult:
        # "stream: OK" | "stream: <signature> FOUND" | "stream: ... ERROR"
        body = text.split(":", 1)[-1].strip() if ":" in text else text.strip()
        if body == "OK":
            return ScanResult(clean=True, detail="clean")
        if body.endswith("FOUND"):
            signature = body.removesuffix("FOUND").strip()
            return ScanResult(clean=False, signature=signature or "unknown", detail=signature)
        # Включая "INSTREAM size limit exceeded. ERROR" и любой другой ERROR-ответ clamd.
        logger.warning("clamav_error_reply", reply=text)
        return ScanResult(clean=False, error=True, detail=body or "clamd_error")


def build_scanner(settings: Settings) -> ClamAvScanner | None:
    """`None`, если `CLAMAV_ENABLED=false` — вызывающая сторона (`app.main`) тогда не трогает
    `NullAntivirusScanner`, зарегистрированный по умолчанию."""
    if not settings.clamav_enabled:
        return None
    return ClamAvScanner(
        host=settings.clamav_host,
        port=settings.clamav_port,
        connect_timeout=settings.clamav_connect_timeout_seconds,
        scan_timeout=settings.clamav_scan_timeout_seconds,
    )
