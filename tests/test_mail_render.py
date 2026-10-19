"""Письма RTK School CRM: шаблоны, фильтры, согласованность HTML и текста.

БД не нужна: рендер чистый, публичный адрес подставляется настройкой `base_url`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable

import pytest

from app.core.config import get_settings
from app.modules.notification import mail_render
from app.modules.notification.mail_render import (
    RenderedMail,
    msk_datetime,
    public_ip,
    ru_duration,
    ru_elapsed,
)

BASE = "https://crm.rt-it-school.ru"
TOKEN = "Q7x2mP9aLk3VtR8nZc4wYb1HjD6sFe0u"
UTC = dt.UTC


@pytest.fixture(autouse=True)
def _public_base(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "base_url", BASE)


def _invite() -> RenderedMail:
    return mail_render.render_invite(
        name="Анна Аудитова",
        email="anna@example.org",
        role="KAM",
        url=f"{BASE}/invite/{TOKEN}",
        ttl=dt.timedelta(hours=72),
    )


def _reset(reason: str | None = "Подозрение на компрометацию") -> RenderedMail:
    return mail_render.render_password_reset(
        name="Анна Аудитова",
        url=f"{BASE}/invite/{TOKEN}",
        ttl=dt.timedelta(minutes=60),
        reason=reason,
    )


def _sign_link() -> RenderedMail:
    return mail_render.render_sign_link(
        title="КП по сделке D-2026-000039",
        url=f"{BASE}/sign/{TOKEN}",
        deadline_at=dt.datetime(2026, 10, 21, 9, 9, tzinfo=UTC),
        link_expires_at=dt.datetime(2026, 10, 26, 9, 9, tzinfo=UTC),
    )


def _otp() -> RenderedMail:
    return mail_render.render_sign_otp(code="164710", ttl=300)


def _download() -> RenderedMail:
    return mail_render.render_signed_download(
        title="КП по сделке D-2026-000039",
        url=f"{BASE}/public/signed/{TOKEN}",
        expires_at=dt.datetime(2026, 10, 26, 9, 9, tzinfo=UTC),
    )


def _notice() -> RenderedMail:
    return mail_render.render_notice(
        subject="SLA по сделке нарушен",
        body="Срок текущего статуса сделки истёк без перехода.",
        rows=[("Сделка", "D-2026-000039"), ("Вуз", "КГТУ"), ("Просрочено на", "6 ч 20 мин")],
        url=f"{BASE}/deals/01a1515e-9b35-7000-bff6-3290279dff2d",
        button_label="Открыть сделку",
    )


ALL: dict[str, Callable[[], RenderedMail]] = {
    "invite": _invite,
    "password_reset": _reset,
    "sign_link": _sign_link,
    "sign_otp": _otp,
    "signed_download": _download,
    "notice": _notice,
}
WITH_LINK = [name for name in ALL if name != "sign_otp"]


def _hrefs(html: str) -> set[str]:
    return set(re.findall(r'href="([^"]+)"', html))


def _urls(text: str) -> set[str]:
    return set(re.findall(r"https?://\S+", text))


class TestEveryTemplateRenders:
    @pytest.mark.parametrize("name", ALL)
    def test_it_renders_a_complete_letter(self, name: str) -> None:
        mail = ALL[name]()

        assert mail.subject
        assert mail.html.lstrip().lower().startswith("<!doctype html>")
        assert 'lang="ru"' in mail.html
        assert mail.text.strip()
        assert mail.text.endswith("\n")
        # Подвал и логотип есть в каждом письме.
        assert "отвечать на него не нужно" in mail.html
        assert "отвечать на него не нужно" in mail.text
        assert 'src="cid:logo"' in mail.html
        assert mail.inline["logo"][0] == "image/png"
        assert mail.inline["logo"][1].startswith(b"\x89PNG")

    @pytest.mark.parametrize("name", ALL)
    def test_there_is_no_none_and_no_unfilled_placeholder(self, name: str) -> None:
        mail = ALL[name]()

        for part in (mail.html, mail.text, mail.subject):
            assert "None" not in part
            assert "{{" not in part and "{%" not in part

    @pytest.mark.parametrize("name", ALL)
    def test_there_is_no_internal_address_in_any_link(self, name: str) -> None:
        mail = ALL[name]()

        for link in _hrefs(mail.html) | _urls(mail.text):
            if link.startswith("cid:"):
                continue
            assert link.startswith(BASE), link
            assert "localhost" not in link and "keycloak:" not in link and "127.0.0.1" not in link


class TestLinksAgree:
    @pytest.mark.parametrize("name", WITH_LINK)
    def test_html_and_text_carry_the_same_links(self, name: str) -> None:
        mail = ALL[name]()

        html_links = {link for link in _hrefs(mail.html) if not link.startswith("cid:")}
        assert html_links
        assert html_links <= _urls(mail.text)

    @pytest.mark.parametrize("name", WITH_LINK)
    def test_there_is_exactly_one_main_button(self, name: str) -> None:
        mail = ALL[name]()

        assert mail.html.count('class="btn-a"') == 1

    def test_the_code_letter_has_no_button_and_no_link_to_follow(self) -> None:
        mail = _otp()

        assert 'class="btn-a"' not in mail.html
        assert "164710" in mail.html and "164710" in mail.text
        assert "164710" in mail.subject

    def test_the_code_is_in_the_preheader(self) -> None:
        # Preheader — первый скрытый блок письма: код виден рядом с темой в списке писем.
        preheader = _otp().html.split('mso-hide:all;">', 1)[1].split("&#8199;", 1)[0]

        assert "164710" in preheader


class TestEscaping:
    def test_markup_in_a_title_is_escaped_in_html_and_kept_in_text(self) -> None:
        title = "<script>alert(1)</script> & «КП»"

        for mail in (
            mail_render.render_sign_link(title=title, url=f"{BASE}/sign/{TOKEN}"),
            mail_render.render_signed_download(title=title, url=f"{BASE}/public/signed/{TOKEN}"),
        ):
            assert "<script>" not in mail.html
            assert "&lt;script&gt;" in mail.html
            assert "<script>alert(1)</script>" in mail.text

    def test_markup_in_a_name_or_a_reason_is_escaped(self) -> None:
        invite = mail_render.render_invite(
            name="<img src=x onerror=1>",
            email="a@b.ru",
            role=None,
            url=f"{BASE}/invite/{TOKEN}",
            ttl=3600,
        )
        reset = _reset("<script>x</script>")

        assert "<img src=x" not in invite.html and "<script>" not in reset.html

    def test_markup_in_a_notification_body_and_rows_is_escaped(self) -> None:
        mail = mail_render.render_notice(
            subject="<b>Тема</b>",
            body="Строка 1\nСтрока <i>2</i>\n\nАбзац <script>1</script>",
            rows=[("Сделка", "<script>2</script>")],
            url=f"{BASE}/deals/1",
            button_label="Открыть",
        )

        assert "<script>" not in mail.html and "<i>2</i>" not in mail.html
        assert "<b>Тема</b>" not in mail.html
        # Перевод строки внутри абзаца — <br>, пустая строка — новый абзац.
        assert "Строка 1<br>Строка &lt;i&gt;2&lt;/i&gt;" in mail.html
        assert mail.html.count("<p style=") >= 2


class TestDatesAndDurations:
    def test_moscow_time_not_utc_and_not_iso(self) -> None:
        mail = _sign_link()

        assert "21.10.2026 12:09" in mail.html and "21.10.2026 12:09" in mail.text
        assert "26.10.2026 12:09" in mail.text
        assert "T09:09" not in mail.html and "UTC" not in mail.html

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-10-21T09:09:00+00:00", "21.10.2026 12:09"),
            ("2026-10-21T09:09:00Z", "21.10.2026 12:09"),
            (dt.datetime(2026, 1, 1, 21, 30), "02.01.2026 00:30"),  # без пояса — UTC
            (
                dt.datetime(2026, 10, 21, 12, 9, tzinfo=dt.timezone(dt.timedelta(hours=3))),
                "21.10.2026 12:09",
            ),
            ("21.10.2026 12:09", "21.10.2026 12:09"),  # уже человекочитаемое — как есть
            (None, ""),
            ("", ""),
        ],
    )
    def test_msk_datetime(self, value, expected: str) -> None:
        assert msk_datetime(value) == expected

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (72 * 3600, "3 дня"),
            (24 * 3600, "1 день"),
            (5 * 86400, "5 дней"),
            (3600, "1 час"),
            (2 * 3600, "2 часа"),
            (900, "15 минут"),
            (60, "1 минута"),
            (300, "5 минут"),
            (120, "2 минуты"),
            (20, "1 минута"),
            (11 * 60, "11 минут"),
            (21 * 60, "21 минута"),
        ],
    )
    def test_ru_duration(self, seconds: int, expected: str) -> None:
        assert ru_duration(seconds) == expected
        assert ru_duration(dt.timedelta(seconds=seconds)) == expected

    def test_ru_elapsed(self) -> None:
        assert ru_elapsed(dt.timedelta(hours=6, minutes=20)) == "6 ч 20 мин"
        assert ru_elapsed(dt.timedelta(days=2, hours=3)) == "2 д 3 ч"
        assert ru_elapsed(dt.timedelta(minutes=15)) == "15 мин"

    def test_the_invite_says_three_days_not_minutes(self) -> None:
        mail = _invite()

        assert "3 дня" in mail.html and "3 дня" in mail.text
        assert "4320" not in mail.html

    def test_the_code_lives_five_minutes_in_words(self) -> None:
        assert "5 минут" in _otp().html


class TestReason:
    def test_the_reason_row_is_there_when_there_is_a_reason(self) -> None:
        mail = _reset("Подозрение на компрометацию")

        assert "Причина" in mail.html and "Подозрение на компрометацию" in mail.html
        assert "Причина: Подозрение на компрометацию" in mail.text

    @pytest.mark.parametrize("reason", [None, "", "   "])
    def test_without_a_reason_there_is_no_reason_row_at_all(self, reason) -> None:
        mail = _reset(reason)

        assert "Причина" not in mail.html and "Причина" not in mail.text


class TestIpFilter:
    @pytest.mark.parametrize(
        "value",
        [
            "172.18.0.5",
            "10.0.0.7",
            "192.168.1.10",
            "127.0.0.1",
            "::1",
            "169.254.1.1",
            "",
            None,
            "abc",
        ],
    )
    def test_internal_and_garbage_addresses_are_hidden(self, value) -> None:
        assert public_ip(value) is None

    def test_a_public_address_is_kept(self) -> None:
        assert public_ip("93.184.216.34") == "93.184.216.34"


class TestNotice:
    def test_empty_rows_make_no_table_and_no_button_without_a_target(self) -> None:
        mail = mail_render.render_notice(
            subject="Пароль изменён",
            body="Пароль вашей учётной записи изменён.",
            rows=[("Когда", None), ("IP-адрес", "")],
        )

        assert 'class="btn-a"' not in mail.html
        assert "Когда" not in mail.html and "IP-адрес" not in mail.html
        assert "None" not in mail.html

    def test_a_row_already_said_in_the_text_is_not_repeated(self) -> None:
        mail = mail_render.render_notice(
            subject="Сброс", body="Сброшен. Причина: утечка.", rows=[("Причина", "утечка")]
        )

        # Только абзац и preheader; строки «Причина» в таблице нет.
        assert ">Причина</td>" not in mail.html

    def test_only_notices_carry_list_unsubscribe(self) -> None:
        notice = _notice()

        assert notice.headers["List-Unsubscribe"] == f"<{BASE}/notifications/settings>"
        for name in ("invite", "password_reset", "sign_link", "sign_otp", "signed_download"):
            assert "List-Unsubscribe" not in ALL[name]().headers

    def test_a_long_first_paragraph_is_cut_for_the_preheader(self) -> None:
        mail = mail_render.render_notice(subject="Тема", body="слово " * 100)

        assert "…" in mail.html

    def test_the_reminder_variant_of_the_sign_link(self) -> None:
        mail = mail_render.render_sign_link(
            title="Договор", url=f"{BASE}/sign/{TOKEN}", reminder=True
        )

        assert mail.subject == "Напоминание: документ на подпись: Договор"
        assert "Напоминаем" in mail.text


class TestSubjects:
    def test_subjects_follow_the_catalogue(self) -> None:
        assert _sign_link().subject == "Документ на подпись: КП по сделке D-2026-000039"
        assert _otp().subject == "Код подтверждения: 164710"
        assert _download().subject == "Документ подписан: КП по сделке D-2026-000039"
        assert _invite().subject == "Вас пригласили в RTK School CRM"
        assert _reset().subject == "Пароль вашей учётной записи сброшен"


class TestPackaging:
    def test_every_template_and_the_logo_are_in_the_image_tree(self) -> None:
        # Dockerfile копирует `app/` целиком; тест страхует от шаблона вне этого дерева.
        base = mail_render._TEMPLATES_DIR  # noqa: SLF001
        names = {p.name for p in base.iterdir()}

        assert {"layout.html.j2", "_macros.j2", "logo.png", "_footer.txt.j2"} <= names
        for kind in (
            "invite",
            "password_reset",
            "sign_link",
            "sign_otp",
            "signed_download",
            "notice",
        ):
            assert f"{kind}.html.j2" in names and f"{kind}.txt.j2" in names
