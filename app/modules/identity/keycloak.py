"""Клиент Keycloak: OIDC-поток и Admin API.

Используется конфиденциальный клиент с PKCE. Admin API нужен для создания
пользователей, ролей, блокировки и сброса пароля (раздел 6.2). Пароли
через этот клиент проходят транзитом и никогда не логируются.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.core.config import get_settings
from app.core.errors import AppError, DependencyStatus, ErrorCode, FieldError

logger = structlog.get_logger(__name__)


def _keycloak_error_text(response: httpx.Response) -> str | None:
    """Человекочитаемый текст ошибки Admin API (политика паролей и т.п.), не длиннее 300 знаков."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    text = body.get("error_description") or body.get("errorMessage") or body.get("error")
    return str(text)[:300] if text else None


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

    async def find_by_email(self, email: str) -> dict[str, Any] | None:
        response = await self.admin_request(
            "GET", "/users", params={"email": email, "exact": "true"}
        )
        response.raise_for_status()
        found = response.json()
        return found[0] if found else None

    async def create_user(
        self,
        *,
        email: str,
        full_name: str,
        required_actions: list[str],
        attributes: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> str:
        """Создаёт пользователя и возвращает его `sub`.

        new_spec §4.1: без пароля, `emailVerified=false`, с обязательными
        действиями. Порядок «сначала IdP, потом локальная запись» обязателен:
        при недоступном Keycloak локальной записи-сироты не остаётся.
        """
        first_name, _, last_name = full_name.partition(" ")
        payload: dict[str, Any] = {
            "username": email,
            "email": email,
            "firstName": first_name or full_name,
            "lastName": last_name or "",
            "enabled": enabled,
            "emailVerified": False,
            "requiredActions": required_actions,
        }
        if attributes:
            payload["attributes"] = {k: [str(v)] for k, v in attributes.items()}

        response = await self.admin_request("POST", "/users", json_body=payload)
        if response.status_code == 409:
            raise AppError(
                ErrorCode.DUPLICATE,
                "Учётная запись с таким email уже существует в Keycloak",
                extra={"email": email},
            )
        if response.status_code not in (201, 204):
            logger.warning("keycloak_create_user_failed", status=response.status_code)
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE, "Не удалось создать пользователя в Keycloak"
            )

        location = response.headers.get("Location", "")
        keycloak_id = location.rstrip("/").rsplit("/", 1)[-1]
        if not keycloak_id:
            created = await self.find_by_email(email)
            if not created:
                raise AppError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "Keycloak не вернул идентификатор созданного пользователя",
                )
            keycloak_id = created["id"]
        return keycloak_id

    async def delete_user(self, keycloak_id: str) -> None:
        """Компенсация SAGA при откате создания и физическое удаление
        учётки при обезличивании (new_spec §4.8.2, режим B)."""
        response = await self.admin_request("DELETE", f"/users/{keycloak_id}")
        if response.status_code not in (204, 404):
            response.raise_for_status()

    async def update_user(self, keycloak_id: str, payload: dict[str, Any]) -> None:
        response = await self.admin_request("PUT", f"/users/{keycloak_id}", json_body=payload)
        response.raise_for_status()

    async def set_required_actions(self, keycloak_id: str, actions: list[str]) -> None:
        """Заменяет список целиком. Чтобы не стереть чужие обязательные действия (`CONFIGURE_TOTP`),
        для добавления и снятия одного действия есть `update_required_actions`."""
        await self.update_user(keycloak_id, {"requiredActions": actions})

    async def update_required_actions(
        self, keycloak_id: str, *, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        """Добавляет и снимает обязательные действия, не трогая остальные.

        Сброс пароля раньше заменял список на `UPDATE_PASSWORD`, а смена пароля — на пустой:
        требование настроить TOTP пропадало при первом же сбросе."""
        user = await self.get_user(keycloak_id)
        current = list((user or {}).get("requiredActions") or [])
        updated = [a for a in current if a not in set(remove)]
        updated += [a for a in add if a not in updated]
        if updated != current:
            await self.update_user(keycloak_id, {"requiredActions": updated})

    async def set_attribute(self, keycloak_id: str, key: str, value: Any) -> None:
        """Обновляет один атрибут, сохраняя остальные.

        `perm_epoch` попадает в токен через протокол-мэппер, поэтому его
        значение обязано жить в Keycloak, а не только в нашей БД.
        """
        user = await self.get_user(keycloak_id)
        attributes = dict((user or {}).get("attributes") or {})
        attributes[key] = [str(value)]
        await self.update_user(keycloak_id, {"attributes": attributes})

    async def get_realm_role(self, name: str) -> dict[str, Any]:
        response = await self.admin_request("GET", f"/roles/{name}")
        response.raise_for_status()
        return response.json()

    async def get_user_realm_roles(self, keycloak_id: str) -> list[dict[str, Any]]:
        response = await self.admin_request("GET", f"/users/{keycloak_id}/role-mappings/realm")
        response.raise_for_status()
        return response.json()

    async def set_realm_role(self, keycloak_id: str, *, role: str, known_roles: list[str]) -> None:
        """Приводит набор ролей CRM к одной. Чужие роли realm'а не трогаем."""
        current = await self.get_user_realm_roles(keycloak_id)
        to_remove = [r for r in current if r["name"] in known_roles and r["name"] != role]
        if to_remove:
            response = await self.admin_request(
                "DELETE", f"/users/{keycloak_id}/role-mappings/realm", json_body=to_remove
            )
            response.raise_for_status()
        if not any(r["name"] == role for r in current):
            target = await self.get_realm_role(role)
            response = await self.admin_request(
                "POST",
                f"/users/{keycloak_id}/role-mappings/realm",
                json_body=[{"id": target["id"], "name": target["name"]}],
            )
            response.raise_for_status()

    async def execute_actions_email(
        self, keycloak_id: str, actions: list[str], *, lifespan_seconds: int
    ) -> bool:
        """Просит Keycloak отправить письмо с одноразовой ссылкой.

        В закрытом контуре SMTP может быть не настроен — тогда Keycloak
        отвечает ошибкой, и мы честно возвращаем False: приглашение уйдёт
        администратору ссылкой в ответе API, а не молча потеряется.
        """
        response = await self.admin_request(
            "PUT",
            f"/users/{keycloak_id}/execute-actions-email",
            json_body=actions,
            params={"lifespan": lifespan_seconds},
        )
        if response.status_code in (200, 204):
            return True
        logger.info("keycloak_actions_email_unavailable", status=response.status_code)
        return False

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
        if response.status_code == 400:
            # Политика паролей realm (длина, классы символов, история): текст политики — ответ
            # пользователю, а не «Внутренняя ошибка». Сам пароль в ответ Keycloak не возвращает.
            raise AppError(
                ErrorCode.VALIDATION,
                _keycloak_error_text(response) or "Пароль не соответствует политике безопасности",
                errors=[FieldError(field="new_password", reason="не соответствует политике")],
            )
        if response.status_code >= 400:
            logger.warning("keycloak_set_password_failed", status=response.status_code)
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Не удалось сменить пароль в Keycloak")

    async def logout_all_sessions(self, keycloak_id: str) -> None:
        response = await self.admin_request("POST", f"/users/{keycloak_id}/logout")
        response.raise_for_status()

    async def list_user_sessions(self, keycloak_id: str) -> list[dict[str, Any]]:
        """Активные SSO-сессии пользователя в Keycloak (`start`/`lastAccess` — миллисекунды).

        Нужны для режима Bearer: SPA получает токены напрямую, серверной cookie-сессии
        в Redis нет, и единственное место, где видны входы, — сам Keycloak."""
        response = await self.admin_request("GET", f"/users/{keycloak_id}/sessions")
        response.raise_for_status()
        found = response.json()
        return found if isinstance(found, list) else []

    async def delete_session(self, kc_session_id: str) -> None:
        """Завершает одну SSO-сессию realm'а (её refresh-токены перестают работать)."""
        response = await self.admin_request("DELETE", f"/sessions/{kc_session_id}")
        if response.status_code not in (200, 204, 404):
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
