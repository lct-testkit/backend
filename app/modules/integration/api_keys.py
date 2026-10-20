"""B5-10: API-ключи для систем, которые не умеют OAuth2 client_credentials.

`core.deps._principal_from_api_key` проверяет ключ на каждый запрос (чтение по хэшу); этот
модуль — администрирование (создание, список, отзыв), только роль ADMIN (`integration:admin`).
Ключ показывается РОВНО ОДИН РАЗ, при создании — так же, как секрет webhook-подписки
(`integration.webhooks`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import FieldError, NotFoundError, ValidationError
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.identity.models import Role, User, UserStatus
from app.modules.integration.models import ApiKey
from app.modules.integration.schemas import ApiKeyCreateRequest

_KEY_PREFIX = "crm_"
_KEY_BYTES = 32
_PREFIX_DISPLAY_LEN = 12


def _generate_api_key() -> tuple[str, str, str]:
    """Возвращает `(raw_key, key_hash, key_prefix)`. `raw_key` отдаётся вызывающему РОВНО ОДИН
    РАЗ; в БД — только `key_hash` (SHA-256) и `key_prefix` (первые символы, не секрет — для
    различения ключей в списке)."""
    raw_key = f"{_KEY_PREFIX}{secrets.token_urlsafe(_KEY_BYTES)}"
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return raw_key, key_hash, raw_key[:_PREFIX_DISPLAY_LEN]


class ApiKeyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def list_all(self) -> list[ApiKey]:
        rows = (
            (await self._session.execute(select(ApiKey).order_by(ApiKey.created_at.desc())))
            .scalars()
            .all()
        )
        return list(rows)

    async def get_or_404(self, api_key_id: uuid.UUID) -> ApiKey:
        api_key = await self._session.get(ApiKey, api_key_id)
        if api_key is None:
            raise NotFoundError("API-ключ", api_key_id)
        return api_key

    async def create(
        self, principal: Principal, payload: ApiKeyCreateRequest
    ) -> tuple[ApiKey, str]:
        ttl_days = payload.expires_in_days or get_settings().api_key_default_ttl_days

        user = await self._session.get(User, payload.user_id)
        if (
            user is None
            or user.role != Role.INTEGRATION.value
            or user.status != UserStatus.ACTIVE.value
        ):
            raise ValidationError(
                "Ключ можно выпустить только активной учётной записи интеграции",
                [
                    FieldError(
                        field="user_id",
                        reason="Учётная запись не найдена или не принадлежит интеграции",
                    )
                ],
            )

        raw_key, key_hash, key_prefix = _generate_api_key()
        api_key = ApiKey(
            name=payload.name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            user_id=user.id,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=ttl_days),
            created_by=principal.user_id,
        )
        self._session.add(api_key)
        await self._session.flush()
        await self._audit.record(
            AuditAction.API_KEY_CREATED,
            entity_type="api_key",
            entity_id=api_key.id,
            changes={
                "name": {"old": None, "new": api_key.name},
                "user_id": {"old": None, "new": str(user.id)},
                "key_prefix": {"old": None, "new": api_key.key_prefix},
                "expires_at": {"old": None, "new": api_key.expires_at.isoformat()},
            },
        )
        return api_key, raw_key

    async def revoke(self, api_key: ApiKey) -> ApiKey:
        if api_key.revoked_at is None:
            api_key.revoked_at = dt.datetime.now(dt.UTC)
            await self._session.flush()
            await self._audit.record(
                AuditAction.API_KEY_REVOKED,
                entity_type="api_key",
                entity_id=api_key.id,
                changes={"revoked_at": {"old": None, "new": api_key.revoked_at.isoformat()}},
            )
        return api_key
