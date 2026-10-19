"""Тексты шаблонов уведомлений: пустая причина и блокировка, адресованная руководителю

* `USER_PASSWORD_RESET` и `USER_ACCOUNT_BLOCKED`: строка «Причина: .» при пустой причине убирается
  (`{% if reason %}`);
* `USER_ACCOUNT_BLOCKED`: уведомление блокируемого сотрудника эскалируется руководителю
  (`payload.escalated_from_name`), а текст говорил «Ваша учётная запись заблокирована».

Меняются только строки, равные прежним дефолтам из `notification.seed`: шаблон, который
администратор уже отредактировал вручную, не трогается. Откат возвращает прежние дефолты тем же
способом.

Revision ID: 0053_mail_template_texts
Revises: 0052_b4_access_requests
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_mail_template_texts"
down_revision: str | None = "0052_b4_access_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BLOCKED_IN_APP_NEW = (
    "{% if escalated_from_name %}Сотрудник {{ escalated_from_name }} заблокирован."
    "{% else %}Ваша учётная запись заблокирована.{% endif %}"
    "{% if reason %} Причина: {{ reason }}.{% endif %}"
)
_BLOCKED_EMAIL_SUBJECT_NEW = (
    "{% if escalated_from_name %}Сотрудник заблокирован{% else %}Учётная запись "
    "заблокирована{% endif %}"
)
_BLOCKED_EMAIL_NEW = (
    "{% if escalated_from_name %}Сотрудник {{ escalated_from_name }} заблокирован."
    "{% else %}Ваша учётная запись заблокирована.{% endif %}"
    "{% if reason %} Причина: {{ reason }}.{% endif %}"
    "{% if not escalated_from_name %} По вопросам обращайтесь к руководителю.{% endif %}"
)

# (code, channel, прежняя тема, прежний текст, новая тема, новый текст)
_CHANGES: list[tuple[str, str, str | None, str, str | None, str]] = [
    (
        "USER_PASSWORD_RESET",
        "in_app",
        None,
        "Администратор сбросил пароль вашей учётной записи. Причина: "
        "{{ reason }}. Требуется повторный вход.",
        None,
        "Администратор сбросил пароль вашей учётной записи."
        "{% if reason %} Причина: {{ reason }}.{% endif %} Требуется повторный вход.",
    ),
    (
        "USER_PASSWORD_RESET",
        "email",
        "Пароль вашей учётной записи сброшен",
        "Пароль вашей учётной записи сброшен администратором. Причина: "
        "{{ reason }}. Все сессии завершены, потребуется вход заново.",
        "Пароль вашей учётной записи сброшен",
        "Пароль вашей учётной записи сброшен администратором."
        "{% if reason %} Причина: {{ reason }}.{% endif %} Все сессии завершены, "
        "потребуется вход заново.",
    ),
    (
        "USER_ACCOUNT_BLOCKED",
        "in_app",
        None,
        "Ваша учётная запись заблокирована. Причина: {{ reason }}.",
        None,
        _BLOCKED_IN_APP_NEW,
    ),
    (
        "USER_ACCOUNT_BLOCKED",
        "email",
        "Учётная запись заблокирована",
        "Ваша учётная запись заблокирована. Причина: {{ reason }}. По "
        "вопросам обращайтесь к руководителю.",
        _BLOCKED_EMAIL_SUBJECT_NEW,
        _BLOCKED_EMAIL_NEW,
    ),
]

_UPDATE = sa.text(
    """
    UPDATE notification_templates
       SET subject_template = :new_subject, body_template = :new_body, version = version + 1
     WHERE code = :code AND channel = :channel
       AND body_template = :old_body
       AND subject_template IS NOT DISTINCT FROM CAST(:old_subject AS text)
    """
)


def _apply(*, forward: bool) -> None:
    bind = op.get_bind()
    for code, channel, old_subject, old_body, new_subject, new_body in _CHANGES:
        before = (old_subject, old_body) if forward else (new_subject, new_body)
        after = (new_subject, new_body) if forward else (old_subject, old_body)
        bind.execute(
            _UPDATE,
            {
                "code": code,
                "channel": channel,
                "old_subject": before[0],
                "old_body": before[1],
                "new_subject": after[0],
                "new_body": after[1],
            },
        )


def upgrade() -> None:
    _apply(forward=True)


def downgrade() -> None:
    _apply(forward=False)
