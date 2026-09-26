"""Клиент объектного хранилища (SeaweedFS, S3-совместимый API, раздел 9).

MinIO спецификацией запрещён (раздел «Стек»); код не зависит от конкретной
реализации — любой S3-совместимый бэкенд подходит без изменений.

`boto3` синхронный: вызовы, которые реально ходят по сети (`head_object`,
потоковое чтение тела для `sha256`/magic bytes), обёрнуты в
`asyncio.to_thread`, чтобы не блокировать event loop ASGI-воркера. Генерация
presigned URL — чистая криптография без сетевого вызова, но тоже вынесена в
поток ради единообразия и на случай, если botocore в будущем версии решит
сходить в сеть за метаданными региона.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from functools import lru_cache

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.errors import DependencyStatus

# Сколько байт объекта скачивать за один потоковый чанк при подсчёте sha256.
_HASH_CHUNK_SIZE = 1024 * 1024
# Достаточно для проверки самых длинных сигнатур из раздела 9 (OLE2 — 8 байт).
_MAGIC_BYTES_LEN = 16


@lru_cache(maxsize=1)
def _client():
    """Внутренний адрес (docker-сеть): для серверных вызовов, которые сами
    ходят по сети (`ensure_bucket`, `inspect_object`, `delete_object`,
    health-check)."""
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name=settings.s3_region,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@lru_cache(maxsize=1)
def _public_client():
    """Публичный адрес: только для подписи presigned PUT/GET — сам клиент
    по сети не ходит, `generate_presigned_url` — чистая криптография
    (раздел 9: ссылку разыменовывает браузер, а не сервер)."""
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_public_endpoint,
        aws_access_key_id=settings.s3_access_key.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name=settings.s3_region,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


async def ensure_bucket(bucket: str) -> None:
    """Идемпотентно создаёт бакет. SeaweedFS не создаёт бакеты на лету."""

    def _ensure() -> None:
        client = _client()
        try:
            client.head_bucket(Bucket=bucket)
        except ClientError:
            try:
                client.create_bucket(Bucket=bucket)
            except ClientError:
                pass  # гонка параллельных запросов — бакет уже создан

    await asyncio.to_thread(_ensure)


async def generate_presigned_put(
    *, bucket: str, key: str, content_type: str, expires_seconds: int
) -> str:
    def _sign() -> str:
        return _public_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_seconds,
        )

    return await asyncio.to_thread(_sign)


async def generate_presigned_get(
    *,
    bucket: str,
    key: str,
    expires_seconds: int,
    filename: str | None = None,
    inline: bool = False,
) -> str:
    """`inline=True` — ссылка для просмотра в браузере (PDF на странице подписи):
    без `attachment` файл открывается, а не скачивается."""

    def _sign() -> str:
        params: dict[str, str] = {"Bucket": bucket, "Key": key}
        if inline:
            params["ResponseContentDisposition"] = "inline"
        elif filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return _public_client().generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires_seconds
        )

    return await asyncio.to_thread(_sign)


@dataclass(slots=True)
class ObjectInspection:
    """Результат чтения объекта на этапе `commit` (раздел 9)."""

    exists: bool
    size_bytes: int = 0
    sha256: str = ""
    magic_bytes: bytes = b""


async def inspect_object(*, bucket: str, key: str) -> ObjectInspection:
    """Скачивает объект целиком потоково, считает `sha256` и снимает первые
    байты для проверки сигнатуры формата (magic bytes, раздел 9: «расширение
    врёт»). Для файлов до `deal_files_max_size_bytes` (по умолчанию 500 МБ)
    это секунды, не минуты — сеть внутри контура."""

    def _inspect() -> ObjectInspection:
        client = _client()
        try:
            response = client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                return ObjectInspection(exists=False)
            raise

        digest = hashlib.sha256()
        size = 0
        magic = b""
        body = response["Body"]
        try:
            while True:
                chunk = body.read(_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                if not magic:
                    magic = chunk[:_MAGIC_BYTES_LEN]
                digest.update(chunk)
                size += len(chunk)
        finally:
            body.close()

        return ObjectInspection(
            exists=True, size_bytes=size, sha256=digest.hexdigest(), magic_bytes=magic
        )

    return await asyncio.to_thread(_inspect)


async def download_object_bytes(*, bucket: str, key: str) -> bytes:
    """Скачивает объект целиком в память — используется фоновыми задачами
    импорта реестра ЕГРЮЛ и каталогов (раздел 4.12/5.11), которым нужен
    произвольный доступ (seek) для `openpyxl`/`lxml.iterparse`, а не только
    последовательное чтение. Оправдано лимитами размера файла на входе
    (`files_max_size_bytes`/`import_max_file_size_bytes`, десятки МБ, не
    гигабайты) — тот же приём, что уже применяет `inspect_object` для
    подсчёта `sha256` при `commit`."""

    def _download() -> bytes:
        response = _client().get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    return await asyncio.to_thread(_download)


async def download_object_to_file(*, bucket: str, key: str, path: str) -> str:
    """Скачивает объект в файл потоком (константная память) и возвращает его sha256.

    Для больших выгрузок (полный ЕГРЮЛ — гигабайты): `download_object_bytes` держит объект
    в памяти целиком, а `sha256` по нему считался отдельным проходом."""
    import hashlib

    def _download() -> str:
        response = _client().get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        digest = hashlib.sha256()
        try:
            with open(path, "wb") as target:
                for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                    target.write(chunk)
                    digest.update(chunk)
        finally:
            body.close()
        return digest.hexdigest()

    return await asyncio.to_thread(_download)


async def upload_object_bytes(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
    """Кладёт объект напрямую с сервера — в отличие от presigned PUT, здесь
    нет клиента, который сам грузит байты: это отчёты об ошибках импорта
    (раздел 4.12), сгенерированные фоновой задачей на сервере."""

    def _upload() -> None:
        _client().put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)

    await asyncio.to_thread(_upload)


async def delete_object(*, bucket: str, key: str) -> None:
    def _delete() -> None:
        try:
            _client().delete_object(Bucket=bucket, Key=key)
        except ClientError:
            pass

    await asyncio.to_thread(_delete)


async def check_storage() -> DependencyStatus:
    settings = get_settings()
    started = time.perf_counter()

    def _ping() -> bool:
        _client().list_buckets()
        return True

    try:
        await asyncio.to_thread(_ping)
        return DependencyStatus(
            name="seaweedfs",
            ok=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(
            name="seaweedfs",
            ok=False,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            error=type(exc).__name__,
            details={"endpoint": settings.s3_endpoint_url},
        )
