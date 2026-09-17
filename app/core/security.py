"""Локальная валидация JWT по кэшированному JWKS.

Раздел «Аутентификация»: FastAPI проверяет токен локально и не обращается
в Keycloak на каждый запрос. JWKS кэшируется в памяти процесса и в Redis,
TTL задаётся KEYCLOAK_JWKS_TTL. При неизвестном `kid` кэш обновляется
один раз (ротация ключей), и только потом токен признаётся невалидным.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import structlog
from jwt import PyJWK

from app.core.config import get_settings
from app.core.errors import AppError, DependencyStatus, ErrorCode, UnauthenticatedError
from app.core.redis_client import get_redis, key_jwks

logger = structlog.get_logger(__name__)

ALLOWED_ALGORITHMS = ("RS256", "RS512", "ES256", "ES384")


@dataclass(slots=True)
class TokenClaims:
    """Разобранные claims access-токена."""

    subject: str
    raw: dict[str, Any]
    email: str | None = None
    full_name: str | None = None
    preferred_username: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)
    session_state: str | None = None
    expires_at: int | None = None

    @property
    def crm_role(self) -> str | None:
        """Единственная бизнес-роль CRM из набора ролей Keycloak.

        Если Keycloak выдал несколько, берём самую привилегированную —
        иначе поведение зависело бы от порядка в множестве.
        """
        from app.modules.identity.models import Role

        priority = [Role.ADMIN, Role.AUDITOR, Role.HEAD, Role.INTEGRATION, Role.KAM]
        for role in priority:
            if role.value in self.roles:
                return role.value
        return None


class JwksCache:
    """Кэш ключей подписи realm'а."""

    def __init__(self) -> None:
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float = 0.0
        self._raw: dict[str, Any] | None = None

    @property
    def _ttl(self) -> int:
        return get_settings().keycloak_jwks_ttl

    def _is_fresh(self) -> bool:
        return bool(self._keys) and (time.monotonic() - self._fetched_at) < self._ttl

    async def _load_from_redis(self) -> bool:
        try:
            cached = await get_redis().get(key_jwks())
        except Exception:
            return False
        if not cached:
            return False
        try:
            self._ingest(json.loads(cached))
            return True
        except Exception:
            return False

    def _ingest(self, payload: dict[str, Any]) -> None:
        keys: dict[str, PyJWK] = {}
        for entry in payload.get("keys", []):
            # Для проверки подписи интересны только ключи с use=sig.
            if entry.get("use") not in (None, "sig"):
                continue
            try:
                keys[entry["kid"]] = PyJWK.from_dict(entry)
            except Exception as exc:  # noqa: BLE001 — один битый ключ не ломает набор
                logger.warning("jwks_key_skipped", kid=entry.get("kid"), error=str(exc))
        if not keys:
            raise ValueError("JWKS не содержит пригодных ключей подписи")
        self._keys = keys
        self._raw = payload
        self._fetched_at = time.monotonic()

    async def refresh(self) -> None:
        settings = get_settings()
        url = settings.keycloak_jwks_url
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        self._ingest(payload)
        try:
            await get_redis().setex(key_jwks(), self._ttl, json.dumps(payload))
        except Exception:
            # Redis недоступен — работаем на локальном кэше процесса.
            logger.warning("jwks_redis_cache_failed")
        logger.info("jwks_refreshed", keys=len(self._keys))

    async def get_key(self, kid: str) -> PyJWK:
        if not self._is_fresh() and not await self._load_from_redis():
            await self.refresh()
        if kid not in self._keys:
            # Возможна ротация ключей: обновляемся один раз и пробуем снова.
            await self.refresh()
        if kid not in self._keys:
            raise UnauthenticatedError("Ключ подписи токена не найден в JWKS")
        return self._keys[kid]

    async def check(self) -> DependencyStatus:
        started = time.perf_counter()
        try:
            if not self._is_fresh():
                await self.refresh()
            return DependencyStatus(
                name="keycloak_jwks",
                ok=True,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                details={"keys": len(self._keys)},
            )
        except Exception as exc:
            return DependencyStatus(name="keycloak_jwks", ok=False, error=type(exc).__name__)


jwks_cache = JwksCache()


def _extract_roles(claims: dict[str, Any], client_id: str) -> frozenset[str]:
    """Роли берутся и из realm_access, и из resource_access клиента."""
    roles: set[str] = set(claims.get("realm_access", {}).get("roles", []))
    resource = claims.get("resource_access", {})
    if client_id in resource:
        roles.update(resource[client_id].get("roles", []))
    return frozenset(roles)


async def decode_access_token(token: str) -> TokenClaims:
    """Проверяет подпись, issuer, audience и срок действия токена."""
    settings = get_settings()

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise UnauthenticatedError("Токен повреждён") from exc

    kid = header.get("kid")
    if not kid:
        raise UnauthenticatedError("В заголовке токена отсутствует kid")
    if header.get("alg") not in ALLOWED_ALGORITHMS:
        # Защита от alg=none и подмены на HMAC с публичным ключом.
        raise UnauthenticatedError("Недопустимый алгоритм подписи токена")

    signing_key = await jwks_cache.get_key(kid)

    try:
        claims = jwt.decode(
            token,
            signing_key.key,  # type: ignore[arg-type]
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=settings.keycloak_issuer,
            audience=settings.keycloak_client_id if settings.keycloak_verify_audience else None,
            options={
                "verify_aud": settings.keycloak_verify_audience,
                "require": ["exp", "iat", "iss", "sub"],
            },
            leeway=30,
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthenticatedError("Срок действия токена истёк") from exc
    except jwt.InvalidAudienceError as exc:
        raise UnauthenticatedError("Токен выдан другому клиенту") from exc
    except jwt.InvalidIssuerError as exc:
        raise UnauthenticatedError("Токен выдан другим realm") from exc
    except jwt.MissingRequiredClaimError as exc:
        # Частая причина — не назначенный клиенту client scope `basic`:
        # начиная с Keycloak 24 именно он добавляет в токен claim `sub`.
        logger.warning("token_missing_claim", claim=exc.claim)
        raise UnauthenticatedError(
            f"В токене отсутствует обязательный claim {exc.claim}"
        ) from exc
    except jwt.InvalidSignatureError as exc:
        raise UnauthenticatedError("Подпись токена недействительна") from exc
    except jwt.PyJWTError as exc:
        # Не смешиваем разные причины в одно сообщение: иначе диагностика
        # превращается в угадывание.
        logger.warning("token_rejected", error_type=type(exc).__name__)
        raise UnauthenticatedError("Токен не принят") from exc

    return TokenClaims(
        subject=claims["sub"],
        raw=claims,
        email=claims.get("email"),
        full_name=claims.get("name") or claims.get("preferred_username"),
        preferred_username=claims.get("preferred_username"),
        roles=_extract_roles(claims, settings.keycloak_client_id),
        session_state=claims.get("sid") or claims.get("session_state"),
        expires_at=claims.get("exp"),
    )


async def decode_logout_token(token: str) -> dict[str, Any]:
    """Backchannel logout: у токена своё назначение, aud и events."""
    settings = get_settings()
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise UnauthenticatedError("В logout-токене отсутствует kid")
    signing_key = await jwks_cache.get_key(kid)
    try:
        claims = jwt.decode(
            token,
            signing_key.key,  # type: ignore[arg-type]
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=settings.keycloak_issuer,
            audience=settings.keycloak_client_id,
            options={"require": ["iss", "aud", "iat", "events"]},
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        raise UnauthenticatedError("Logout-токен недействителен") from exc

    events = claims.get("events", {})
    if "http://schemas.openid.net/event/backchannel-logout" not in events:
        raise AppError(ErrorCode.UNAUTHENTICATED, "Токен не является logout-токеном")
    return claims


@dataclass(slots=True)
class Principal:
    """Аутентифицированный субъект запроса: токен + локальная проекция."""

    user_id: uuid.UUID
    keycloak_id: str
    role: str
    status: str
    email: str | None
    full_name: str
    team_id: uuid.UUID | None
    perm_epoch: int
    session_id: str | None
    consent_required: bool
    claims: TokenClaims

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    def has_role(self, *roles: str) -> bool:
        return self.role in roles

    def require_role(self, *roles: str) -> None:
        if self.role not in roles:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Роль не позволяет выполнить операцию",
                extra={"required_roles": list(roles), "actual_role": self.role},
            )
