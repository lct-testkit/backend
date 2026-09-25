"""Каталог ошибок CRM-XXYY и базовое исключение приложения.

Раздел 3 спецификации: коды заводятся заранее, придумывать их на лету нельзя.
Любая ошибка наружу уходит только как RFC 7807 Problem Details (см. problem.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus


class ErrorCode(StrEnum):
    # --- Общие / валидация / конкурентность ---
    VALIDATION = "CRM-1001"
    VERSION_CONFLICT = "CRM-1002"
    IDEMPOTENCY_CONFLICT = "CRM-1003"

    # --- Аутентификация и авторизация ---
    UNAUTHENTICATED = "CRM-1101"
    FORBIDDEN = "CRM-1102"
    TOKEN_STALE = "CRM-1103"
    USER_BLOCKED = "CRM-1104"
    CONSENT_REQUIRED = "CRM-1105"
    # Расширение каталога: new_spec §4.4 «Ситуация B» требует отклонять
    # бизнес-запросы, пока не выполнено обязательное действие смены пароля.
    PASSWORD_CHANGE_REQUIRED = "CRM-1106"
    # Двойная отправка формы без CSRF-токена (new_spec §3.1 п.5).
    CSRF_FAILED = "CRM-1107"

    # --- Воронка и переходы ---
    TRANSITION_CONDITIONS = "CRM-1201"
    TRANSITION_FORBIDDEN = "CRM-1202"
    DEAL_NOT_ACTIVE = "CRM-1203"
    TRANSITION_COMMENT_REQUIRED = "CRM-1204"
    TRANSITION_FIELDS_REQUIRED = "CRM-1205"
    TRANSITION_SIGNATURE_REQUIRED = "CRM-1206"
    # П4: `DELETE /workflows/{id}` — раздел 4/ФТ.6 требует удаление воронки,
    # но только черновика, который никогда не публиковался (опубликованную,
    # с историей/сделками, удалять нельзя ни при каких условиях — только
    # архивация статусов, раздел 4.11).
    WORKFLOW_NOT_DRAFT = "CRM-1207"
    # `PUT /workflows/{id}/graph`: убранный с холста статус или переход, на
    # который уже ссылаются сделки или их история (FK RESTRICT). Статус
    # сначала архивируют через мастер сопоставления (раздел 4.11).
    WORKFLOW_STATUS_IN_USE = "CRM-1208"
    WORKFLOW_TRANSITION_IN_USE = "CRM-1209"

    # --- Дубликаты ---
    DUPLICATE = "CRM-1301"
    ORGANIZATION_INN_EXISTS = "CRM-1302"
    # П4: удаление справочника (направление/причина отказа/версия реестра),
    # на который есть ссылки — общий код для всех трёх, конкретика в
    # `detail`/`extra` (раздел 4: «разрешай удаление только когда ничего не
    # сломает»).
    ENTITY_IN_USE = "CRM-1303"

    # --- Файлы ---
    FILE_TYPE_NOT_ALLOWED = "CRM-1401"
    FILE_TOO_LARGE = "CRM-1402"
    FILE_INFECTED = "CRM-1403"
    FILE_ACCESS_DENIED = "CRM-1404"

    # --- ПЭП ---
    EDM_AGREEMENT_MISSING = "CRM-1501"
    DOCUMENT_HASH_MISMATCH = "CRM-1502"
    SIGNATURE_OTP_INVALID = "CRM-1503"
    SIGNATURE_TOKEN_INVALID = "CRM-1504"
    DOCUMENT_NOT_SIGNABLE = "CRM-1505"
    # dop.md §10.8 «Отсутствует доверенное время»: NTP ответил, но
    # рассинхрон превышает порог — единственный сценарий, который эта
    # строка требует блокировать (недоступность самого NTP — не он, см.
    # `signing/trusted_time.py`).
    SIGNATURE_TIME_UNTRUSTED = "CRM-1506"

    # --- Отчёты ---
    REPORTS_LIMIT_EXCEEDED = "CRM-1601"
    REPORT_NOT_READY = "CRM-1602"
    REPORT_EXPIRED = "CRM-1603"

    # --- Интеграции ---
    INTEGRATION_BAD_SIGNATURE = "CRM-1701"
    INTEGRATION_DUPLICATE = "CRM-1702"
    INTEGRATION_SOURCE_INACTIVE = "CRM-1703"
    # `POST /admin/integrations/outbox-events/{id}/retry` для события, которое
    # не в `failed`/`dead`: `pending` уже стоит в очереди, `sent` доставлено.
    INTEGRATION_EVENT_NOT_RETRYABLE = "CRM-1704"

    # --- Импорт ---
    IMPORT_BAD_FORMAT = "CRM-1801"
    IMPORT_MAPPING_INCOMPLETE = "CRM-1802"
    IMPORT_NOT_APPLICABLE = "CRM-1803"
    IMPORT_NOT_ROLLBACKABLE = "CRM-1804"

    # --- 152-ФЗ ---
    ERASURE_BLOCKED = "CRM-1901"
    SECOND_ADMIN_REQUIRED = "CRM-1902"
    LAST_ADMIN = "CRM-1903"

    # --- Инфраструктура (не из каталога, но 500 должен иметь код) ---
    RATE_LIMITED = "CRM-8429"
    INTERNAL = "CRM-9000"
    NOT_FOUND = "CRM-9004"
    DEPENDENCY_UNAVAILABLE = "CRM-9503"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    status: int
    title: str


# status + человекочитаемый title для каждого кода.
ERROR_CATALOG: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.VALIDATION: ErrorSpec(422, "Ошибка валидации запроса"),
    ErrorCode.VERSION_CONFLICT: ErrorSpec(409, "Конфликт версии"),
    ErrorCode.IDEMPOTENCY_CONFLICT: ErrorSpec(409, "Идемпотентный конфликт"),
    ErrorCode.UNAUTHENTICATED: ErrorSpec(401, "Пользователь не аутентифицирован"),
    ErrorCode.FORBIDDEN: ErrorSpec(403, "Недостаточно прав"),
    ErrorCode.TOKEN_STALE: ErrorSpec(409, "Токен устарел, требуется обновление"),
    ErrorCode.USER_BLOCKED: ErrorSpec(403, "Пользователь заблокирован или уволен"),
    ErrorCode.CONSENT_REQUIRED: ErrorSpec(403, "Требуется согласие на обработку ПДн"),
    ErrorCode.PASSWORD_CHANGE_REQUIRED: ErrorSpec(403, "Требуется смена пароля"),
    ErrorCode.CSRF_FAILED: ErrorSpec(403, "Не пройдена проверка CSRF-токена"),
    ErrorCode.TRANSITION_CONDITIONS: ErrorSpec(422, "Переход недоступен: условия не выполнены"),
    ErrorCode.TRANSITION_FORBIDDEN: ErrorSpec(403, "Переход запрещён ролью или настройками"),
    ErrorCode.DEAL_NOT_ACTIVE: ErrorSpec(409, "Сделка не активна"),
    ErrorCode.TRANSITION_COMMENT_REQUIRED: ErrorSpec(422, "Требуется комментарий при переходе"),
    ErrorCode.TRANSITION_FIELDS_REQUIRED: ErrorSpec(
        422, "Требуются обязательные поля или вложения"
    ),
    ErrorCode.TRANSITION_SIGNATURE_REQUIRED: ErrorSpec(422, "Требуется действующая подпись"),
    ErrorCode.WORKFLOW_NOT_DRAFT: ErrorSpec(
        409, "Удалить можно только черновик воронки, который не публиковался"
    ),
    ErrorCode.WORKFLOW_STATUS_IN_USE: ErrorSpec(
        409, "Статус используется в сделках или их истории и не может быть удалён"
    ),
    ErrorCode.WORKFLOW_TRANSITION_IN_USE: ErrorSpec(
        409, "Переход использован в истории сделок и не может быть удалён"
    ),
    ErrorCode.DUPLICATE: ErrorSpec(409, "Найден дубликат сущности"),
    ErrorCode.ORGANIZATION_INN_EXISTS: ErrorSpec(409, "Организация с таким ИНН уже существует"),
    ErrorCode.ENTITY_IN_USE: ErrorSpec(409, "Сущность используется и не может быть удалена"),
    ErrorCode.FILE_TYPE_NOT_ALLOWED: ErrorSpec(415, "Недопустимый тип файла"),
    ErrorCode.FILE_TOO_LARGE: ErrorSpec(413, "Превышен размер файла"),
    ErrorCode.FILE_INFECTED: ErrorSpec(422, "Файл не прошёл антивирусную проверку"),
    ErrorCode.FILE_ACCESS_DENIED: ErrorSpec(403, "Файл недоступен по правам"),
    ErrorCode.EDM_AGREEMENT_MISSING: ErrorSpec(409, "Отсутствует действующее соглашение об ЭДО"),
    ErrorCode.DOCUMENT_HASH_MISMATCH: ErrorSpec(409, "Хэш документа не совпадает"),
    ErrorCode.SIGNATURE_OTP_INVALID: ErrorSpec(422, "Код подтверждения неверен или истёк"),
    ErrorCode.SIGNATURE_TOKEN_INVALID: ErrorSpec(404, "Токен подписи истёк или не найден"),
    ErrorCode.DOCUMENT_NOT_SIGNABLE: ErrorSpec(409, "Документ нельзя подписать"),
    ErrorCode.SIGNATURE_TIME_UNTRUSTED: ErrorSpec(
        409, "Рассинхрон доверенного времени превышает допустимый порог"
    ),
    ErrorCode.REPORTS_LIMIT_EXCEEDED: ErrorSpec(429, "Превышен лимит параллельных отчётов"),
    ErrorCode.REPORT_NOT_READY: ErrorSpec(409, "Отчёт ещё не готов"),
    ErrorCode.REPORT_EXPIRED: ErrorSpec(410, "Срок хранения результата отчёта истёк"),
    ErrorCode.INTEGRATION_BAD_SIGNATURE: ErrorSpec(401, "Неверная подпись входящего запроса"),
    ErrorCode.INTEGRATION_DUPLICATE: ErrorSpec(200, "Сообщение уже обработано"),
    ErrorCode.INTEGRATION_SOURCE_INACTIVE: ErrorSpec(409, "Источник интеграции неактивен"),
    ErrorCode.INTEGRATION_EVENT_NOT_RETRYABLE: ErrorSpec(
        409, "Повторить можно только событие в статусе failed или dead"
    ),
    ErrorCode.IMPORT_BAD_FORMAT: ErrorSpec(422, "Недопустимый или повреждённый формат импорта"),
    ErrorCode.IMPORT_MAPPING_INCOMPLETE: ErrorSpec(422, "Маппинг импорта не завершён"),
    ErrorCode.IMPORT_NOT_APPLICABLE: ErrorSpec(422, "Импорт нельзя применить из-за ошибок"),
    ErrorCode.IMPORT_NOT_ROLLBACKABLE: ErrorSpec(409, "Импорт нельзя откатить"),
    ErrorCode.ERASURE_BLOCKED: ErrorSpec(409, "Запрос на удаление заблокирован"),
    ErrorCode.SECOND_ADMIN_REQUIRED: ErrorSpec(
        409, "Требуется подтверждение вторым администратором"
    ),
    ErrorCode.LAST_ADMIN: ErrorSpec(
        409, "Нельзя удалить или заблокировать последнего администратора"
    ),
    ErrorCode.RATE_LIMITED: ErrorSpec(429, "Превышен лимит частоты запросов"),
    ErrorCode.INTERNAL: ErrorSpec(500, "Внутренняя ошибка сервера"),
    ErrorCode.NOT_FOUND: ErrorSpec(404, "Ресурс не найден"),
    ErrorCode.DEPENDENCY_UNAVAILABLE: ErrorSpec(503, "Зависимость недоступна"),
}


@dataclass(slots=True)
class FieldError:
    """Элемент массива `errors` в Problem Details."""

    field: str
    reason: str
    code: str | None = None


class AppError(Exception):
    """Базовое исключение приложения. Всегда несёт код из каталога."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str | None = None,
        *,
        status: int | None = None,
        errors: list[FieldError] | None = None,
        extra: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        spec = ERROR_CATALOG[code]
        self.code = code
        self.status = status or spec.status
        self.title = spec.title
        self.detail = detail or spec.title
        self.errors = errors or []
        self.extra = extra or {}
        self.headers = headers or {}
        super().__init__(self.detail)

    @property
    def http_status_phrase(self) -> str:
        try:
            return HTTPStatus(self.status).phrase
        except ValueError:
            return "Error"


# --- Частые сокращения, чтобы не плодить AppError(...) по коду ------------


class ValidationError(AppError):
    def __init__(self, detail: str, errors: list[FieldError] | None = None) -> None:
        super().__init__(ErrorCode.VALIDATION, detail, errors=errors)


class NotFoundError(AppError):
    def __init__(self, entity: str, entity_id: object = None) -> None:
        detail = f"{entity} не найден" + (f": {entity_id}" if entity_id is not None else "")
        super().__init__(ErrorCode.NOT_FOUND, detail, extra={"entity_type": entity})


class VersionConflictError(AppError):
    """409 с актуальными значениями конфликтующих полей (раздел 2)."""

    def __init__(self, current_version: int, conflicting: dict[str, object] | None = None) -> None:
        super().__init__(
            ErrorCode.VERSION_CONFLICT,
            "Объект был изменён другим пользователем. Обновите данные и повторите.",
            extra={"current_version": current_version, "current_values": conflicting or {}},
        )


class UnauthenticatedError(AppError):
    def __init__(self, detail: str = "Требуется аутентификация") -> None:
        super().__init__(
            ErrorCode.UNAUTHENTICATED,
            detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


class ForbiddenError(AppError):
    def __init__(self, detail: str = "Недостаточно прав для операции", **extra: object) -> None:
        super().__init__(ErrorCode.FORBIDDEN, detail, extra=extra)


@dataclass(slots=True)
class DependencyStatus:
    """Результат проверки одной зависимости для /health/ready."""

    name: str
    ok: bool
    latency_ms: float | None = None
    error: str | None = None
    details: dict[str, object] = field(default_factory=dict)
