"""Клиент Keycloak: OIDC-поток и Admin API.

Используется конфиденциальный клиент с PKCE. Admin API нужен для создания
пользователей, ролей, блокировки и сброса пароля (раздел 6.2). Пароли
через этот клиент проходят транзитом и никогда не логируются.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.core.config import get_settings
from app.core.errors import AppError, DependencyStatus, ErrorCode

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    expires_in: int
    session_state: str | None
    raw: dict[str, Any]

    @property
    def access_expires_at(self) -> int:
        return int(time.time()) + self.expires_in


class KeycloakClient:
    def __init__(self) -> None:
        self._admin_token: str | None = None
        self._admin_token_expires_at: float = 0.0

    # --- OIDC ------------------------------------------------------------

    async def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str | None
    ) -> TokenResponse:
        settings = get_settings()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": settings.keycloak_client_id,
            "client_secret": settings.keycloak_client_secret.get_secret_value(),
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        return await self._token_request(data, failure="Не удалось обменять код на токены")

    async def refresh(self, refresh_token: str) -> TokenResponse:
        settings = get_settings()
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.keycloak_client_id,
            "client_secret": settings.keycloak_client_secret.get_secret_value(),
        }
        return await self._token_request(data, failure="Не удалось обновить токен")

    async def password_grant(self, *, username: str, password: str) -> TokenResponse:
        """Проверка текущего пароля при добровольной смене (POST /api/me/password).

        Полноценный вход через этот grant не используется: он нужен только
        чтобы подтвердить владение текущим паролем.
        """
        settings = get_settings()
        data = {
            "grant_type": "password",
            "username": username,
            "password": password,
            "scope": "openid",
            "client_id": settings.keycloak_client_id,
            "client_secret": settings.keycloak_client_secret.get_secret_value(),
        }
        return await self._token_request(data, failure="Текущий пароль неверен")

    async def _token_request(self, data: dict[str, str], *, failure: str) -> TokenResponse:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                settings.keycloak_token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code != 200:
            # Тело ответа Keycloak может содержать имя пользователя — в лог не пишем.
            logger.warning("keycloak_token_failed", status=response.status_code)
            raise AppError(ErrorCode.UNAUTHENTICATED, failure)
        payload = response.json()
        return TokenResponse(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            id_token=payload.get("id_token"),
            expires_in=int(payload.get("expires_in", 300)),
            session_state=payload.get("session_state"),
            raw=payload,
        )

    async def logout(self, refresh_token: str) -> None:
        """Single logout: завершает сессию пользователя в realm."""
        settings = get_settings()
        data = {
            "client_id": settings.keycloak_client_id,
            "client_secret": settings.keycloak_client_secret.get_secret_value(),
            "refresh_token": refresh_token,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                settings.keycloak_logout_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code not in (200, 204):
            # Локальная сессия уже удалена, поэтому это предупреждение, не ошибка.
            logger.warning("keycloak_logout_failed", status=response.status_code)

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        scope: str = "openid profile email",
    ) -> str:
        settings = get_settings()
        params = httpx.QueryParams(
            {
                "client_id": settings.keycloak_client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{settings.keycloak_auth_url}?{params}"

    # --- Admin API -------------------------------------------------------

    async def _admin_access_token(self) -> str:
        settings = get_settings()
        if not settings.keycloak_admin_client_id or not settings.keycloak_admin_client_secret:
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Admin-клиент Keycloak не настроен",
            )
        if self._admin_token and time.monotonic() < self._admin_token_expires_at:
            return self._admin_token

        data = {
            "grant_type": "client_credentials",
            "client_id": settings.keycloak_admin_client_id,
            "client_secret": settings.keycloak_admin_client_secret.get_secret_value(),
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(settings.keycloak_token_url, data=data)
        if response.status_code != 200:
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Keycloak Admin API недоступен")
        payload = response.json()
        self._admin_token = payload["access_token"]
        # Обновляем чуть раньше истечения, чтобы не попасть в гонку.
        self._admin_token_expires_at = time.monotonic() + int(payload["expires_in"]) - 30
        return self._admin_token  # type: ignore[return-value]

    async def admin_request(
        self, method: str, path: str, *, json_body: Any = None, params: Any = None
    ) -> httpx.Response:
        settings = get_settings()
        token = await self._admin_access_token()
        url = f"{settings.keycloak_admin_base}{path}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await client.request(
                method,
                url,
                json=json_body,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )

    async def get_user(self, keycloak_id: str) -> dict[str, Any] | None:
        response = await self.admin_request("GET", f"/users/{keycloak_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def set_enabled(self, keycloak_id: str, *, enabled: bool) -> None:
        response = await self.admin_request(
            "PUT", f"/users/{keycloak_id}", json_body={"enabled": enabled}
        )
        response.raise_for_status()

    async def set_password(
        self, keycloak_id: str, *, password: str, temporary: bool = False
    ) -> None:
        response = await self.admin_request(
            "PUT",
            f"/users/{keycloak_id}/reset-password",
            json_body={"type": "password", "value": password, "temporary": temporary},
        )
        response.raise_for_status()

    async def logout_all_sessions(self, keycloak_id: str) -> None:
        response = await self.admin_request("POST", f"/users/{keycloak_id}/logout")
        response.raise_for_status()

    # --- Health ----------------------------------------------------------

    async def check(self) -> DependencyStatus:
        settings = get_settings()
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(settings.keycloak_discovery_url)
                response.raise_for_status()
            return DependencyStatus(
                name="keycloak",
                ok=True,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        except Exception as exc:
            return DependencyStatus(name="keycloak", ok=False, error=type(exc).__name__)


keycloak_client = KeycloakClient()
