"""Письма RTK School CRM: HTML по общему макету + текстовая копия.

Шаблоны лежат в `app/assets/templates/email/`: `layout.html.j2` (рамка, бренд, подвал), `_macros.j2`
(кнопка, таблица «ключ — значение», абзацы) и по паре `<тип>.html.j2` / `<тип>.txt.j2` на каждый тип
письма. Типы: приглашение, сброс пароля, ссылка на подпись, код подписи, ссылка на подписанный файл,
уведомление (`notice`: заголовок, абзац из админского шаблона, таблица деталей, одна кнопка).

HTML рендерится отдельным окружением Jinja2 с `autoescape=True`: название документа, имя или
причина с `<script>` в письме остаются текстом. Текстовое окружение без экранирования. Обе копии
получают одни и те же данные, поэтому ссылки в них совпадают. Логотип едет как `cid:logo`
(inline-вложение `multipart/related`).

Время показывается по Москве («21.10.2026 12:09»), сроки — по-русски («3 дня», «15 минут»).
Публичный адрес берётся из `BASE_URL`: им же строятся ссылки `/sign/{token}` и
`/public/signed/{token}`.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jinja2
from markupsafe import Markup, escape

from app.core.config import get_settings

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "templates" / "email"
_LOGO = _TEMPLATES_DIR / "logo.png"

# Москва без перехода на летнее время с 2014 года: смещение фиксировано, если в системе нет базы
# часовых поясов (минимальный образ, Windows без `tzdata`).
_MOSCOW_FALLBACK = dt.timezone(dt.timedelta(hours=3), "MSK")
_MOSCOW_FORMAT = "%d.%m.%Y %H:%M"

ROLE_LABELS: dict[str, str] = {
    "KAM": "Менеджер по вузам (KAM)",
    "HEAD": "Руководитель (HEAD)",
    "ADMIN": "Администратор (ADMIN)",
    "AUDITOR": "Аудитор (AUDITOR)",
    "INTEGRATION": "Интеграция",
}

_LEAD_LIMIT = 140


@cache
def _moscow() -> dt.tzinfo:
    try:
        return ZoneInfo("Europe/Moscow")
    except Exception:  # noqa: BLE001 — нет базы часовых поясов
        return _MOSCOW_FALLBACK


def _as_datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    # Время без пояса в базе и в payload — UTC (`datetime.now(UTC)` везде).
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def msk_datetime(value: Any) -> str:
    """«21.10.2026 12:09» по Москве. Принимает дату-время или ISO-строку. Не разобравшееся
    значение возвращается как есть (строка уже могла быть человекочитаемой), пустое даёт ""."""
    if value is None or value == "":
        return ""
    parsed = _as_datetime(value)
    if parsed is None:
        return str(value)
    return parsed.astimezone(_moscow()).strftime(_MOSCOW_FORMAT)


def _plural(number: int, forms: tuple[str, str, str]) -> str:
    n = abs(number) % 100
    if 11 <= n <= 14:
        return forms[2]
    tail = n % 10
    if tail == 1:
        return forms[0]
    if 2 <= tail <= 4:
        return forms[1]
    return forms[2]


def ru_duration(value: dt.timedelta | int | float) -> str:
    """Срок словами: 3 дня, 1 час, 15 минут. Целые сутки и часы не дробятся на минуты
    («4320 минут» читать никто не должен)."""
    seconds = int(value.total_seconds() if isinstance(value, dt.timedelta) else value)
    if seconds >= 86400 and seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} {_plural(days, ('день', 'дня', 'дней'))}"
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} {_plural(hours, ('час', 'часа', 'часов'))}"
    minutes = max(math.ceil(seconds / 60), 1)
    return f"{minutes} {_plural(minutes, ('минута', 'минуты', 'минут'))}"


def ru_elapsed(value: dt.timedelta) -> str:
    """Просрочка короткой записью: «6 ч 20 мин», «2 д 3 ч», «15 мин»."""
    total = max(int(value.total_seconds()) // 60, 0)
    days, rest = divmod(total, 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days} д {hours} ч" if hours else f"{days} д"
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    return f"{minutes} мин"


def public_ip(value: Any) -> str | None:
    """IP для показа человеку: внутренние адреса (docker-сеть, loopback, частные диапазоны) за
    адрес пользователя не считаются — их в письме нет."""
    if not value:
        return None
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    ):
        return None
    return str(address)


def public_url(path: str) -> str:
    """Абсолютная ссылка на страницу CRM: `BASE_URL` + путь."""
    return f"{get_settings().base_url.rstrip('/')}/{path.lstrip('/')}"


def _br(value: Any) -> Markup:
    """Экранирует текст и превращает переводы строк в `<br>`."""
    return Markup("<br>").join(escape(line) for line in str(value).split("\n"))


@cache
def _html_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATES_DIR),
        autoescape=True,
        # Забытая переменная — ошибка, а не пустое место в письме или слово «None».
        undefined=jinja2.StrictUndefined,
    )
    env.filters["msk_datetime"] = msk_datetime
    env.filters["ru_duration"] = ru_duration
    env.filters["br"] = _br
    return env


@cache
def _text_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATES_DIR),
        autoescape=False,  # noqa: S701 — текст/plain: экранировать HTML нечего
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )
    env.filters["msk_datetime"] = msk_datetime
    env.filters["ru_duration"] = ru_duration
    return env


@cache
def _logo() -> bytes:
    return _LOGO.read_bytes()


@dataclass(frozen=True)
class RenderedMail:
    """Готовое письмо: тема, текстовая и HTML-копии, inline-картинки и заголовки."""

    subject: str
    text: str
    html: str
    inline: Mapping[str, tuple[str, bytes]] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)

    def send_kwargs(self) -> dict[str, Any]:
        """Аргументы для `email_transport.send_email` (без адресата)."""
        return {
            "subject": self.subject,
            "text": self.text,
            "html": self.html,
            "inline": dict(self.inline),
            "headers": dict(self.headers),
        }


def _render(
    template: str,
    context: Mapping[str, Any],
    *,
    subject: str,
    headers: Mapping[str, str] | None = None,
) -> RenderedMail:
    ctx = {**context, "subject": subject}
    html = _html_env().get_template(f"{template}.html.j2").render(**ctx)
    text = _text_env().get_template(f"{template}.txt.j2").render(**ctx).strip() + "\n"
    return RenderedMail(
        subject=subject,
        text=text,
        html=html,
        inline={"logo": ("image/png", _logo())},
        headers=dict(headers or {}),
    )


def _clean_rows(rows: Iterable[tuple[str, Any]]) -> list[tuple[str, str]]:
    """Строки таблицы без пустых значений: ни «None», ни подписи без значения."""
    cleaned: list[tuple[str, str]] = []
    for label, value in rows:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            cleaned.append((label, text))
    return cleaned


def _ttl_text(ttl: dt.timedelta | int | float) -> str:
    return ru_duration(ttl)


def _when(value: dt.datetime | str | None) -> str | None:
    text = msk_datetime(value)
    return text or None


def render_invite(
    *,
    name: str,
    email: str,
    role: str | None,
    url: str,
    ttl: dt.timedelta | int | float,
) -> RenderedMail:
    return _render(
        "invite",
        {
            "name": name,
            "email": email,
            "role": ROLE_LABELS.get(role or "", role) if role else None,
            "url": url,
            "ttl": _ttl_text(ttl),
        },
        subject="Вас пригласили в RTK School CRM",
    )


def render_password_reset(
    *,
    name: str,
    url: str,
    ttl: dt.timedelta | int | float,
    reason: str | None = None,
) -> RenderedMail:
    return _render(
        "password_reset",
        {
            "name": name,
            "url": url,
            "ttl": _ttl_text(ttl),
            "reason": (reason or "").strip() or None,
        },
        subject="Пароль вашей учётной записи сброшен",
    )


def render_sign_link(
    *,
    title: str,
    url: str,
    deadline_at: dt.datetime | str | None = None,
    link_expires_at: dt.datetime | str | None = None,
    reminder: bool = False,
) -> RenderedMail:
    prefix = "Напоминание: документ на подпись" if reminder else "Документ на подпись"
    subject = f"{prefix}: {title}"
    return _render(
        "sign_link",
        {
            "title": title,
            "url": url,
            "deadline": _when(deadline_at),
            "link_until": _when(link_expires_at),
            "reminder": reminder,
        },
        subject=subject,
    )


def render_sign_otp(*, code: str, ttl: dt.timedelta | int | float) -> RenderedMail:
    return _render(
        "sign_otp",
        {"code": code, "ttl": _ttl_text(ttl)},
        subject=f"Код подтверждения: {code}",
    )


def render_signed_download(
    *, title: str, url: str, expires_at: dt.datetime | str | None = None
) -> RenderedMail:
    return _render(
        "signed_download",
        {"title": title, "url": url, "until": _when(expires_at)},
        subject=f"Документ подписан: {title}",
    )


def _lead(body: str) -> str:
    first = next((part.strip() for part in body.split("\n\n") if part.strip()), "")
    flat = " ".join(first.split())
    return flat if len(flat) <= _LEAD_LIMIT else flat[: _LEAD_LIMIT - 1].rstrip() + "…"


def render_notice(
    *,
    subject: str,
    body: str,
    rows: Sequence[tuple[str, Any]] = (),
    url: str | None = None,
    button_label: str | None = None,
) -> RenderedMail:
    """Письмо-уведомление: заголовок = тема, абзац = текст шаблона из БД, детали таблицей,
    кнопка — только если есть куда вести. `List-Unsubscribe` ставится только здесь: служебные
    письма (приглашение, код, ссылка) уведомлениями не являются и отписки не имеют."""
    settings_url = public_url("/notifications/settings")
    # Не показываем в таблице то, что уже сказано в самом тексте (причина, срок).
    cleaned = [(k, v) for k, v in _clean_rows(rows) if v not in body]
    return _render(
        "notice",
        {
            "body": body.strip(),
            "lead": _lead(body),
            "rows": cleaned,
            "url": url,
            "button_label": button_label or "Открыть в CRM",
            "settings_url": settings_url,
        },
        subject=subject,
        headers={"List-Unsubscribe": f"<{settings_url}>"},
    )
