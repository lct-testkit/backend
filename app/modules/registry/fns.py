"""Внешний источник автоподстановки по ИНН: публичный поиск ФНС (egrul.nalog.ru).

Подключается в цепочку `resolve_chain` только при включённом флаге функции
`external_org_lookup` (админка, «Настройки → Флаги»): закрытому контуру внешний адрес открывать
нельзя без решения администратора (dop.md §11.1), поэтому по умолчанию флаг выключен.

Протокол у сервиса двухшаговый: `POST /` с запросом возвращает токен поиска, `GET
/search-result/{токен}` — найденные строки (сразу или после короткого ожидания). Ключи строки:
`c` — краткое наименование, `n` — полное, `i` — ИНН, `o` — ОГРН, `p` — КПП, `g` — руководитель
(«ДОЛЖНОСТЬ: ФИО»), `r` — дата регистрации `дд.мм.гггг`, `e` — дата прекращения (есть только у
ликвидированных), `rn` — регион, `k` — вид (`ul` юрлицо, `fl` ИП). Адреса и ОКВЭД в выдаче нет —
эти поля остаются пустыми, их заполнит локальный реестр.

Любая ошибка сети или разбора — не ошибка пользователя: провайдер отвечает «ничего не нашёл», и
цепочка идёт дальше. После серии подряд неудач источник на минуту «остывает» (не тратим таймаут
на каждый запрос, пока сервис недоступен). ИНН в логи попадает только в маске.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import httpx
import structlog

from app.core.masking import mask_inn
from app.modules.registry.providers import OrgDetails, OrgSuggestion

_log = structlog.get_logger(__name__)

# Сколько раз подряд источник может не ответить, прежде чем «остыть», и на сколько секунд.
_FAILURES_BEFORE_COOLDOWN = 3
_COOLDOWN_SECONDS = 60.0
# Поиск иногда отвечает не сразу: результат забираем до нескольких раз с короткой паузой.
_RESULT_ATTEMPTS = 4
_RESULT_PAUSE_SECONDS = 0.6

_consecutive_failures = 0
_cooldown_until = 0.0


def _reset_state() -> None:
    """Для тестов: сбрасывает счётчик неудач и «остывание»."""
    global _consecutive_failures, _cooldown_until
    _consecutive_failures = 0
    _cooldown_until = 0.0


def _register_success() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


def _register_failure() -> None:
    global _consecutive_failures, _cooldown_until
    _consecutive_failures += 1
    if _consecutive_failures >= _FAILURES_BEFORE_COOLDOWN:
        _cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
        _consecutive_failures = 0
        _log.warning("fns_lookup_cooldown", seconds=_COOLDOWN_SECONDS)


def _parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value.strip(), "%d.%m.%Y").date()  # noqa: DTZ007
    except ValueError:
        return None


def _split_director(value: Any) -> tuple[str | None, str | None]:
    """`«ПРЕЗИДЕНТ: Иванов Иван Иванович»` -> (ФИО, должность)."""
    if not isinstance(value, str) or not value.strip():
        return None, None
    position, sep, name = value.partition(":")
    if not sep:
        return value.strip(), None
    return name.strip() or None, position.strip().capitalize() or None


def _short_name(row: dict[str, Any]) -> str:
    return str(row.get("c") or row.get("n") or "")


def _to_suggestion(row: dict[str, Any]) -> OrgSuggestion:
    liquidated = bool(row.get("e"))
    return OrgSuggestion(
        inn=str(row["i"]),
        name=_short_name(row),
        region=row.get("rn") or None,
        status="liquidated" if liquidated else "active",
        is_liquidated=liquidated,
        provider=FnsEgrulProvider.name,
    )


def _to_details(row: dict[str, Any]) -> OrgDetails:
    director_name, director_position = _split_director(row.get("g"))
    liquidated = bool(row.get("e"))
    return OrgDetails(
        inn=str(row["i"]),
        ogrn=row.get("o") or None,
        kpp=row.get("p") or None,
        full_name=str(row.get("n") or row.get("c") or ""),
        short_name=row.get("c") or None,
        opf_name=None,
        status="liquidated" if liquidated else "active",
        legal_address=None,
        okved_main=None,
        director_name=director_name,
        director_position=director_position,
        registration_date=_parse_date(row.get("r")),
        registry_version_id=None,
        provider=FnsEgrulProvider.name,
        # Токен строки живёт у сервиса минуты и ничего нам не даёт: в сырые данные не кладём.
        raw={k: v for k, v in row.items() if k != "t"},
    )


class FnsEgrulProvider:
    """Поиск по ИНН и по названию через публичный сервис ФНС."""

    name = "fns_egrul"

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def _search(self, query: str) -> list[dict[str, Any]]:
        """Строки выдачи; при любой неудаче — пустой список (см. докстринг модуля)."""
        if time.monotonic() < _cooldown_until:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                headers={"Accept": "application/json"},
            ) as client:
                started = await client.post(
                    f"{self._base_url}/",
                    data={"vyp3CaptchaToken": "", "page": "", "query": query, "region": ""},
                )
                started.raise_for_status()
                token = started.json().get("t")
                if not token:
                    # Сервис потребовал капчу или отказал: пользовательской ошибки тут нет.
                    _register_failure()
                    return []
                for attempt in range(_RESULT_ATTEMPTS):
                    result = await client.get(f"{self._base_url}/search-result/{token}")
                    result.raise_for_status()
                    body = result.json()
                    rows = body.get("rows")
                    if isinstance(rows, list):
                        _register_success()
                        return [row for row in rows if isinstance(row, dict) and row.get("i")]
                    if attempt + 1 < _RESULT_ATTEMPTS:
                        await asyncio.sleep(_RESULT_PAUSE_SECONDS)
        except (httpx.HTTPError, ValueError) as exc:
            _log.warning("fns_lookup_failed", error=type(exc).__name__)
        _register_failure()
        return []

    async def suggest(self, query: str, limit: int) -> list[OrgSuggestion]:
        query = query.strip()
        if len(query) < 3:
            return []
        rows = await self._search(query)
        return [_to_suggestion(row) for row in rows[:limit]]

    async def get_by_inn(self, inn: str) -> OrgDetails | None:
        rows = await self._search(inn)
        exact = next((row for row in rows if str(row.get("i")) == inn), None)
        if exact is None:
            _log.info("fns_lookup_not_found", inn=mask_inn(inn))
            return None
        return _to_details(exact)

    async def health(self) -> bool:
        # Своего пинга у сервиса нет; «здоров», пока не в режиме остывания.
        return time.monotonic() >= _cooldown_until
