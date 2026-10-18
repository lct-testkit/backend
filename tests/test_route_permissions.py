"""B6-15: каждый закрытый маршрут обязан нести `require_permission` (или быть в
явном списке исключений) — иначе новая ручка без права молча открывается любому
залогиненному пользователю, и ни один существующий тест этого не замечает.

`backend/tests/test_openapi_contract.py::test_public_list_matches_the_routes_that_
skip_authentication` проверяет АУТЕНТИФИКАЦИЮ (нужен ли вообще токен/сессия) по
статической схеме. Этот файл проверяет АВТОРИЗАЦИЮ (какое именно право) по живому
дереву зависимостей FastAPI (`route.dependant`) — `CurrentUser`/`ConsentedUser`
пропускают любую роль, и только обход дерева это видит.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute, _IncludedRouter

from app.api.openapi import is_public_path
from app.core.config import Settings, get_settings
from app.core.deps import CurrentUser, get_principal, require_permission
from app.core.permissions import Permission
from app.modules.files.router import _download_permission
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

#: Функции, которые сами проверяют право НЕ через `require_permission(...)` как
#: прямую зависимость, а вызывая его внутри своего тела (раздел «Что сделать» п.1).
#: `_download_permission` (`app/modules/files/router.py`) выбирает EDM_READ или
#: FILE_DOWNLOAD по параметру запроса и делегирует `require_permission(needed)(...)`.
EXPLICIT_PERMISSION_DEPENDENCIES: frozenset[Any] = frozenset({_download_permission})

#: Маршруты, для которых "любой залогиненный и согласившийся на обработку ПДн
#: пользователь" — осознанное решение, не пропущенное право. Каждая запись несёт
#: причину; `/api/admin/*` здесь запрещён (`test_admin_routes_never_allowlisted`).
ANY_CONSENTED_USER: dict[tuple[str, str], str] = {
    ("GET", "/api/me"): "собственный профиль",
    ("PATCH", "/api/me"): "собственный профиль",
    ("GET", "/api/me/policy"): "текущая политика ПДн — нужна, чтобы её принять",
    ("POST", "/api/me/consent"): "принятие политики ПДн — до него права по роли те же",
    ("POST", "/api/me/password"): "смена собственного пароля",
    ("GET", "/api/me/sessions"): "список собственных сессий",
    ("DELETE", "/api/me/sessions/{sid}"): "завершение собственной сессии",
    ("POST", "/api/me/sessions/terminate-others"): "завершение собственных сессий",
    ("GET", "/api/me/recent"): "собственный список недавних записей",
    ("GET", "/api/me/notification-prefs"): "собственные настройки уведомлений",
    ("PUT", "/api/me/notification-prefs"): "собственные настройки уведомлений",
    ("GET", "/api/notifications"): "собственная лента уведомлений",
    ("GET", "/api/notifications/unread-count"): "счётчик непрочитанных у себя",
    ("GET", "/api/notifications/event-codes"): "справочник кодов событий, не ПДн",
    ("POST", "/api/notifications/read"): "отметка прочитанным в своей ленте",
    ("GET", "/api/users/directory"): "справочник сотрудников, нужен всем ролям",
    (
        "POST",
        "/api/signatures/verify",
    ): "проверка файла по хэшу раскрывает только факт существования подписи",
}


#: Используется только в `TestTheScannerCatchesAMissingPermission` ниже, на модульном
#: уровне: с `from __future__ import annotations` (действует на весь файл) FastAPI резолвит
#: строковую аннотацию параметра через `safe_route.__globals__` — локальной переменной внутри
#: тестовой функции там не было бы видно, и `Depends(...)` молча не подключился бы.
_DealReadForTest = Annotated[Any, Depends(require_permission(Permission.DEAL_READ))]


def iter_api_routes(routes: list[Any], prefix: str = "") -> list[tuple[str, APIRoute]]:
    """Полный путь и `APIRoute` для каждого маршрута, включая вложенные `include_router`.

    Начиная с FastAPI 0.141 `include_router` не копирует `APIRoute` в `app.routes` —
    кладёт туда `_IncludedRouter`, который лениво мэтчит исходный роутер (`original_router`,
    БЕЗ префикса) через `include_context.prefix`. Без учёта этого все пути получаются
    относительными (`/me` вместо `/api/me`) и `is_public_path` работает неверно."""
    found: list[tuple[str, APIRoute]] = []
    for r in routes:
        if isinstance(r, APIRoute):
            found.append((prefix + r.path, r))
        elif isinstance(r, _IncludedRouter):
            sub_prefix = prefix + r.include_context.prefix
            found.extend(iter_api_routes(r.original_router.routes, sub_prefix))
        elif hasattr(r, "routes"):
            found.extend(iter_api_routes(r.routes, prefix))
    return found


def _iter_dependants(dependant: Any):
    yield dependant
    for sub in dependant.dependencies:
        yield from _iter_dependants(sub)


def _is_permission_check(call: Any) -> bool:
    qualname = getattr(call, "__qualname__", "")
    return qualname.endswith("require_permission.<locals>.dependency") or call in (
        EXPLICIT_PERMISSION_DEPENDENCIES
    )


def _has_permission_check(route: APIRoute) -> bool:
    return any(
        d.call is not None and _is_permission_check(d.call)
        for d in _iter_dependants(route.dependant)
    )


def _private_routes(app: FastAPI, settings: Settings) -> list[tuple[str, str, APIRoute]]:
    """`(method, path, route)` для каждый маршрут, который реально требует внимания:
    есть в схеме (не служебный Swagger UI, `include_in_schema=False`) и не публичный
    по `is_public_path` (раздел `app/api/openapi.py`, проверено `test_openapi_contract.py`)."""
    result: list[tuple[str, str, APIRoute]] = []
    for path, route in iter_api_routes(app.routes):
        if not route.include_in_schema or is_public_path(path, settings):
            continue
        for method in route.methods or []:
            if method in ("HEAD", "OPTIONS"):
                continue
            result.append((method, path, route))
    return result


def _violations(app: FastAPI, settings: Settings) -> list[str]:
    violations = []
    for method, path, route in _private_routes(app, settings):
        if _has_permission_check(route):
            continue
        if (method, path) in ANY_CONSENTED_USER:
            continue
        violations.append(
            f"{method} {path}: нет require_permission, добавьте право или запись "
            "в ANY_CONSENTED_USER с причиной"
        )
    return violations


class TestEveryPrivateRouteDeclaresAPermission:
    def test_no_route_is_left_without_a_permission_check(self, client) -> None:
        settings = get_settings()

        violations = _violations(client.app, settings)

        assert violations == []


class TestAllowlistHasNoStaleEntries:
    def test_every_allowlisted_route_still_exists(self, client) -> None:
        settings = get_settings()
        live = {(method, path) for method, path, _ in _private_routes(client.app, settings)}

        stale = [key for key in ANY_CONSENTED_USER if key not in live]

        assert stale == [], f"в ANY_CONSENTED_USER есть маршруты, которых больше нет: {stale}"


class TestAdminRoutesNeverAllowlisted:
    def test_no_admin_route_is_in_the_allowlist(self, client) -> None:
        settings = get_settings()
        admin_prefix = f"{settings.api_prefix}/admin"

        offenders = [key for key in ANY_CONSENTED_USER if key[1].startswith(admin_prefix)]

        assert offenders == [], f"/admin не может сидеть в ANY_CONSENTED_USER: {offenders}"


class TestTheScannerCatchesAMissingPermission:
    def test_a_route_with_only_current_user_is_flagged(self) -> None:
        """Контрольный негативный случай: временное приложение с одним маршрутом,
        где из прав есть только `CurrentUser` (без `require_permission`), обязано
        попасть в список нарушителей — иначе сканер сам сломан."""
        router = APIRouter()

        @router.get("/x")
        async def leaky_route(_: CurrentUser) -> dict[str, bool]:
            return {"ok": True}

        app = FastAPI()
        app.dependency_overrides[get_principal] = lambda: None
        app.include_router(router, prefix="/api")

        settings = get_settings()
        violations = _violations(app, settings)

        assert any("/api/x" in v for v in violations), violations

    def test_a_route_with_require_permission_is_not_flagged(self) -> None:
        router = APIRouter()

        @router.get("/y")
        async def safe_route(_: _DealReadForTest) -> dict[str, bool]:
            return {"ok": True}

        app = FastAPI()
        app.include_router(router, prefix="/api")

        settings = get_settings()
        violations = _violations(app, settings)

        assert not any("/api/y" in v for v in violations), violations
