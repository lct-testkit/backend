"""Дефолтные шаблоны для кодов событий, на которые уже ссылаются 7 модулей
(см. докстринг `service.py`). Без них `RealNotificationService.notify_user`
создаёт `notifications`, но не создаёт ни одной `notification_deliveries` —
событие видно только через сырой `payload`, без человекочитаемого текста в
`GET /api/notifications`.

Запускается один раз при подготовке демо-окружения, тем же способом, что
`app.modules.workflow.seed`:

    python -m app.modules.notification.seed

Идемпотентно по паре (code, channel) — повторный запуск не трогает уже
существующие шаблоны (в т.ч. отредактированные администратором вручную
через `/api/admin/notification-templates`).

Только `email`+`in_app`: `telegram` не сеется — ни у `users`, ни у
`contacts` в этом репозитории нет поля с telegram-идентификатором, заводить
демо-шаблон для канала, у которого физически нет адреса получателя, было бы
нечестной декорацией. Канал в БД поддержан (см. `NotificationChannel`),
шаблон под него завести может администратор, когда появится canonical
источник telegram-id.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import configure_logging
from app.modules.notification.models import NotificationTemplate

logger = structlog.get_logger(__name__)

# (code, channel, subject, body)
_DEFAULT_TEMPLATES: list[tuple[str, str, str | None, str]] = [
    (
        "USER_PASSWORD_CHANGED",
        "in_app",
        None,
        "Пароль вашей учётной записи изменён {{ changed_at }}"
        "{% if ip %} (IP {{ ip }}){% endif %}. Если это были не вы — "
        "свяжитесь с администратором.",
    ),
    (
        "USER_PASSWORD_CHANGED",
        "email",
        "Пароль вашей учётной записи изменён",
        "Пароль изменён {{ changed_at }}{% if ip %}, IP {{ ip }}{% endif %}. "
        "Если это были не вы — свяжитесь с администратором. Ссылка на "
        "самостоятельный откат не предоставляется в целях безопасности.",
    ),
    (
        "USER_PASSWORD_RESET",
        "in_app",
        None,
        "Администратор сбросил пароль вашей учётной записи. Причина: "
        "{{ reason }}. Требуется повторный вход.",
    ),
    (
        "USER_PASSWORD_RESET",
        "email",
        "Пароль вашей учётной записи сброшен",
        "Пароль вашей учётной записи сброшен администратором. Причина: "
        "{{ reason }}. Все сессии завершены, потребуется вход заново.",
    ),
    (
        "USER_ACCOUNT_BLOCKED",
        "in_app",
        None,
        "Ваша учётная запись заблокирована. Причина: {{ reason }}.",
    ),
    (
        "USER_ACCOUNT_BLOCKED",
        "email",
        "Учётная запись заблокирована",
        "Ваша учётная запись заблокирована. Причина: {{ reason }}. По "
        "вопросам обращайтесь к руководителю.",
    ),
    (
        "USER_ACCOUNT_UNBLOCKED",
        "in_app",
        None,
        "Ваша учётная запись разблокирована. {{ reason }}",
    ),
    (
        "USER_ROLE_CHANGED",
        "in_app",
        None,
        "Ваша роль изменена: {{ old_role }} → {{ new_role }}.",
    ),
    (
        "USER_INVITE_EXPIRED",
        "in_app",
        None,
        "Приглашение не принято за 30 дней: {{ full_name }}"
        "{% if email %} ({{ email }}){% endif %}. Учётная запись отключена; чтобы "
        "пригласить снова, нажмите «Отправить повторно».",
    ),
    (
        "USER_OFFBOARD_SUCCESSOR",
        "in_app",
        None,
        "Вам переданы сделки уволенного сотрудника"
        "{% if deals %} ({{ deals|length }} шт.){% endif %}.",
    ),
    (
        "ERASURE_REQUEST_BLOCKED",
        "in_app",
        None,
        "Запрос на удаление/обезличивание данных заблокирован"
        "{% if blockers %}: {{ blockers|join(', ') }}{% endif %}.",
    ),
    (
        "ERASURE_COMPLETED",
        "in_app",
        None,
        "Запрос на удаление/обезличивание исполнен (режим: {{ mode }}). "
        "Акт об уничтожении ПДн доступен в карточке запроса.",
    ),
    (
        "SIGNATURE_REQUESTED",
        "in_app",
        None,
        "Вам направлен документ на подпись{% if template %} (шаблон «{{ template }}»){% endif %}.",
    ),
    (
        "SIGNATURE_DOCUMENT_SIGNED",
        "in_app",
        None,
        "Документ подписан всеми участниками.",
    ),
    (
        "SIGNATURE_DOCUMENT_REJECTED",
        "in_app",
        None,
        "Подписание документа отклонено.{% if reason %} Причина: {{ reason }}.{% endif %}",
    ),
    (
        "SIGNATURE_DOCUMENT_EXPIRED",
        "in_app",
        None,
        "Истёк срок запроса на подпись документа. Требуется пересоздать запрос.",
    ),
    (
        "SIGNATURE_OTP_LOCKED",
        "in_app",
        None,
        "Подписант трижды ввёл неверный код подтверждения — запрос на подпись заблокирован.",
    ),
    (
        "EDM_AGREEMENT_MISSING",
        "in_app",
        None,
        "Подписание документа заблокировано: у контрагента нет действующего "
        "соглашения об ЭДО. Оформите соглашение и повторите отправку.",
    ),
    (
        "ORG_REQUISITES_DRIFT_DETECTED",
        "in_app",
        None,
        "У организации изменились реквизиты"
        "{% if fields %}: {{ fields|join(', ') }}{% endif %}. Проверьте карточку организации.",
    ),
    (
        "ORG_DRIFT_APPLIED",
        "in_app",
        None,
        "Изменения реквизитов организации применены из реестра ЕГРЮЛ.",
    ),
    (
        "ORG_LIQUIDATION_DETECTED",
        "in_app",
        None,
        "Организация контрагента по этой сделке переходит в статус ликвидации. "
        "Требуется юридическая проверка перед дальнейшими действиями.",
    ),
    (
        "ORG_LIQUIDATION_DETECTED",
        "email",
        "Требуется юридическая проверка контрагента",
        "Организация контрагента по вашей сделке меняет статус на "
        "«ликвидируется/ликвидирована». Подписание документов с таким "
        "контрагентом — юридический риск: проверьте карточку сделки.",
    ),
    (
        "DEAL_SLA_WARNING",
        "in_app",
        None,
        "По сделке истекает срок SLA текущего статуса — осталось меньше 25% времени.",
    ),
    (
        "DEAL_SLA_BREACHED",
        "in_app",
        None,
        "SLA по сделке нарушен: срок текущего статуса истёк.",
    ),
    (
        "DEAL_SLA_BREACHED",
        "email",
        "SLA по сделке нарушен",
        "Срок текущего статуса сделки истёк без перехода. Требуется внимание "
        "ответственного или руководителя.",
    ),
    (
        "DEAL_REASSIGNED",
        "in_app",
        None,
        "Вам назначена сделка в качестве ответственного.",
    ),
    (
        "DEAL_EVENT",
        "in_app",
        None,
        "Событие по сделке — подробности в карточке сделки.",
    ),
]


async def seed_notification_templates(session: AsyncSession) -> int:
    created = 0
    for code, channel, subject, body in _DEFAULT_TEMPLATES:
        existing = await session.scalar(
            select(NotificationTemplate.id).where(
                NotificationTemplate.code == code, NotificationTemplate.channel == channel
            )
        )
        if existing is not None:
            continue
        session.add(
            NotificationTemplate(
                code=code, channel=channel, subject_template=subject, body_template=body
            )
        )
        created += 1
    if created:
        await session.flush()
    logger.info("notification_templates_seeded", created=created, total=len(_DEFAULT_TEMPLATES))
    return created


async def _main() -> None:
    async with session_scope() as session:
        await seed_notification_templates(session)


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
