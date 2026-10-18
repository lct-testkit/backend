"""B6-13: `tools/anonymize.sql` на временной базе, созданной прямо в тесте (не на
`TEST_DATABASE_URL` напрямую — своя база `crm_anon_test_<pid>` на том же кластере, чтобы
`TRUNCATE`/отключение триггеров не задевали параллельные тесты). Температура доказательства
здесь выше, чем у обычного юнит-теста: сам SQL-файл проверялся вручную против настоящей
восстановленной схемы (включая реальные `signatures`/`audit_log` с их триггерами
неизменяемости) при подготовке этой задачи — см. правки `ALTER TABLE ... DISABLE TRIGGER
USER` для обеих партиционированных/непартиционированных таблиц и правку `files.
original_filename` (явно названа в плане спринта, была пропущена в первой редакции).

Требует `TEST_DATABASE_URL` с правом `CREATEDB` (то же, чем мигрируют миграции).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с правом CREATEDB"
)

ROOT = Path(__file__).resolve().parent.parent
ANONYMIZE_SQL = ROOT / "tools" / "anonymize.sql"


def _dsn_parts(url: str) -> dict[str, Any]:
    """`postgresql+asyncpg://user:pass@host:port/db` -> компоненты для `asyncpg.connect`."""
    without_scheme = url.split("://", 1)[1]
    creds, hostdb = without_scheme.split("@", 1)
    user, _, password = creds.partition(":")
    hostport, _, dbname = hostdb.partition("/")
    host, _, port = hostport.partition(":")
    return {
        "user": user,
        "password": password or None,
        "host": host,
        "port": int(port) if port else 5432,
        "dbname": dbname,
    }


def _strip_psql_meta(sql: str) -> str:
    """Убирает психоль-специфичные метакоманды (`\\set`, `\\if`/`\\else`/`\\endif`,
    `\\echo`) перед выполнением через `asyncpg`, который понимает только чистый SQL.
    Тест всегда передаёт соль явно, поэтому ветка `\\else` (ошибка «соль обязательна»)
    просто вырезается вместе с остальными метакомандами — её код психоль всё равно не
    выполнил бы при заданной переменной."""
    out_lines: list[str] = []
    in_else_branch = False
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("\\if"):
            continue
        if stripped.startswith("\\else"):
            in_else_branch = True
            continue
        if stripped.startswith("\\endif"):
            in_else_branch = False
            continue
        if in_else_branch or stripped.startswith("\\"):
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def _render_sql(salt: str) -> str:
    sql = ANONYMIZE_SQL.read_text(encoding="utf-8")
    sql = _strip_psql_meta(sql)
    escaped = salt.replace("'", "''")
    return sql.replace(":'salt'", f"'{escaped}'")


async def _create_temp_db() -> tuple[str, dict[str, Any]]:
    import asyncpg

    admin = _dsn_parts(TEST_DATABASE_URL)
    db_name = f"crm_anon_test_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(
        user=admin["user"],
        password=admin["password"],
        host=admin["host"],
        port=admin["port"],
        database="postgres",
    )
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()
    return db_name, admin


async def _drop_temp_db(db_name: str, admin: dict[str, Any]) -> None:
    import asyncpg

    conn = await asyncpg.connect(
        user=admin["user"],
        password=admin["password"],
        host=admin["host"],
        port=admin["port"],
        database="postgres",
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    finally:
        await conn.close()


def _migrate(db_name: str, admin: dict[str, Any]) -> None:
    url = (
        f"postgresql+asyncpg://{admin['user']}:{admin['password'] or ''}"
        f"@{admin['host']}:{admin['port']}/{db_name}"
    )
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env["MIGRATIONS_DATABASE_URL"] = url
    env["PYTHONUTF8"] = "1"
    # Фиксированная команда (sys.executable -m alembic upgrade head), не пользовательский
    # ввод — S603 здесь ложное срабатывание.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"alembic upgrade head: {result.stdout}\n{result.stderr}"


class _Seeded:
    """id-ы созданных строк и ровно те оригинальные значения, которые должны пропасть."""

    def __init__(self) -> None:
        self.real_email = "ivan.petrov@real-mail.example"
        self.real_name = "Иван Петров"
        self.real_phone = "+79161234567"
        self.real_snils = "11223344595"
        self.real_filename = "Паспорт_Иванова_скан.pdf"
        self.real_ip = "203.0.113.77"
        self.real_user_agent = "RealBrowser/9.9 (real device fingerprint)"
        self.user_id: uuid.UUID | None = None
        self.contact_id: uuid.UUID | None = None


async def _seed(conn: Any, seeded: _Seeded) -> None:
    seeded.user_id = await conn.fetchval(
        "INSERT INTO users (id, keycloak_id, email, full_name, display_name, phone, "
        "position, role, status, consent_version) "
        "VALUES (gen_random_uuid(), 'kc-1', $1, $2, 'Иван П.', $3, 'KAM', 'KAM', 'active', '1.0') "
        "RETURNING id",
        seeded.real_email,
        seeded.real_name,
        seeded.real_phone,
    )
    seeded.contact_id = await conn.fetchval(
        "INSERT INTO contacts (id, first_name, last_name, middle_name, email, phone) "
        "VALUES (gen_random_uuid(), 'Пётр', $1, 'Ильич', $2, $3) RETURNING id",
        "СидоровРеал",
        seeded.real_email,
        seeded.real_phone,
    )
    await conn.execute(
        "INSERT INTO contact_channels (id, contact_id, type, value) "
        "VALUES (gen_random_uuid(), $1, 'telegram', $2)",
        seeded.contact_id,
        "@" + seeded.real_name.replace(" ", "_"),
    )
    await conn.execute(
        "INSERT INTO contact_learner_profiles "
        "(contact_id, snils, passport_series, passport_number, passport_issued_by, "
        "passport_issued_at, passport_dept_code, sex, birth_date, reg_region, reg_city, "
        "reg_street, reg_house, reg_zip, first_name_dative, last_name_dative, "
        "middle_name_dative, education) "
        "VALUES ($1, $2, '4510', '123456', 'ОВД Примерного района', '2010-05-01', '770-001', "
        "'M', '1990-01-01', 'Москва', 'Москва', 'ул. Реальная', '5', '101000', 'Петру', "
        "$3, 'Ильичу', 'higher_bachelor')",
        seeded.contact_id,
        seeded.real_snils,
        "СидоровуРеал",
    )
    # ИП с реальными контактными данными — ветка `org_type = 'individual_entrepreneur'`
    # в anonymize.sql; результат (id) здесь не нужен, достаточно того, что строка есть.
    await conn.execute(
        "INSERT INTO organizations (id, name, org_type, inn, main_phone, main_email) "
        "VALUES (gen_random_uuid(), 'ИП Сидоров Пётр Ильич', 'individual_entrepreneur', "
        "'770000000001', $1, $2)",
        seeded.real_phone,
        seeded.real_email,
    )
    uni_id = await conn.fetchval(
        "INSERT INTO organizations (id, name, org_type, inn) "
        "VALUES (gen_random_uuid(), 'РТ Университет', 'university', '7700000000') RETURNING id"
    )
    # Колонки, чьи имена не похожи на ПДн, но несут их: «Ответственные от ВУЗа» из xls лицензий
    # (ФИО + телефон + email одной строкой) и свободный текст реестра инцидентов (B6-12).
    await conn.execute(
        "INSERT INTO organization_licenses (id, organization_id, vendor, product_name, "
        "responsible_contacts) VALUES (gen_random_uuid(), $1, 'Вендор', 'Продукт', $2)",
        uni_id,
        f"{seeded.real_name}, {seeded.real_phone}, {seeded.real_email}",
    )
    await conn.execute(
        "INSERT INTO security_incidents (id, number, title, status, severity, source, "
        "causes, harm, measures, investigation_summary, rkn_24_reference) "
        "VALUES (gen_random_uuid(), $1, 'Инцидент', 'open', 'warning', 'manual', $2, $3, $4, "
        "$5, $6)",
        f"INC-TEST-{uuid.uuid4().hex[:8]}",
        f"Письмо отправлено {seeded.real_name} по ошибке",
        f"Затронут {seeded.real_name} ({seeded.real_phone})",
        f"Сброшен пароль {seeded.real_email}",
        f"Расследование: {seeded.real_name}",
        f"РКН-{seeded.real_name}",
    )
    workflow_id = await conn.fetchval(
        "INSERT INTO workflows (id, code, name, deal_type, state, is_default) "
        "VALUES (gen_random_uuid(), $1, 'Тестовая воронка', 'b2b', 'draft', false) RETURNING id",
        f"wf-{uuid.uuid4().hex[:8]}",
    )
    status_id = await conn.fetchval(
        "INSERT INTO workflow_statuses (id, workflow_id, code, name, type, sort_order) "
        "VALUES (gen_random_uuid(), $1, 'new', 'Новая', 'initial', 10) RETURNING id",
        workflow_id,
    )
    deal_id = await conn.fetchval(
        "INSERT INTO deals (id, number, title, deal_type, workflow_id, status_id, "
        "organization_id, owner_id) "
        "VALUES (gen_random_uuid(), $1, $2, 'b2b', $3, $4, $5, $6) RETURNING id",
        f"D-{uuid.uuid4().hex[:8]}",
        f"Сделка с {seeded.real_name}",
        workflow_id,
        status_id,
        uni_id,
        seeded.user_id,
    )
    await conn.execute(
        "INSERT INTO deal_status_history "
        "(id, deal_id, from_status_id, to_status_id, changed_by, reason, comment, changed_at) "
        "VALUES (gen_random_uuid(), $1, NULL, $2, $3, 'manual', $4, now())",
        deal_id,
        status_id,
        seeded.user_id,
        f"Договорились с {seeded.real_name}, email {seeded.real_email}",
    )
    file_id = await conn.fetchval(
        "INSERT INTO files "
        "(id, storage_key, bucket, original_filename, mime_type, size_bytes, sha256, status) "
        "VALUES (gen_random_uuid(), 'test/doc.pdf', 'files', $1, 'application/pdf', 10, "
        "repeat('a', 64), 'ready') RETURNING id",
        seeded.real_filename,
    )
    doc_id = await conn.fetchval(
        "INSERT INTO signature_documents "
        "(id, doc_type, title, entity_type, entity_id, file_id, content_hash, status) "
        "VALUES (gen_random_uuid(), 'kp', $1, 'deal', $2, $3, repeat('b', 64), 'signed') "
        "RETURNING id",
        f"КП для {seeded.real_name}",
        deal_id,
        file_id,
    )
    request_id = await conn.fetchval(
        "INSERT INTO signature_requests "
        "(id, document_id, signer_type, signer_user_id, signer_role_code, "
        "signer_name_snapshot, signer_identifier_masked, sign_order, access_token_hash, "
        "token_expires_at, status) "
        "VALUES (gen_random_uuid(), $1, 'internal', $2, 'KAM', $3, $4, 1, repeat('c', 64), "
        "now() + interval '7 days', 'signed') RETURNING id",
        doc_id,
        seeded.user_id,
        seeded.real_name,
        f"iv***@{seeded.real_email.split('@')[1]}",
    )
    await conn.execute(
        "INSERT INTO signatures "
        "(id, request_id, document_id, content_hash, method, signer_display, "
        "signature_value, algorithm, key_version, evidence, signed_at, time_source, ip, "
        "user_agent, hash) "
        "VALUES (gen_random_uuid(), $1, $2, repeat('b', 64), 'pep_otp', $3, 'sigvalue', "
        "'hmac-sha256', 1, $4::jsonb, now(), 'ntp', $5, $6, repeat('d', 64))",
        request_id,
        doc_id,
        seeded.real_name,
        (
            f'{{"signer": {{"name": "{seeded.real_name}"}}, '
            f'"auth": {{"phone": "{seeded.real_phone}"}}}}'
        ),
        seeded.real_ip,
        seeded.real_user_agent,
    )
    await conn.execute(
        "INSERT INTO consents "
        "(id, subject_type, subject_id, policy_version, policy_text_hash, ip, user_agent, "
        "accepted_at) "
        "VALUES (gen_random_uuid(), 'user', $1, '1.0', repeat('e', 64), $2, $3, now())",
        seeded.user_id,
        seeded.real_ip,
        seeded.real_user_agent,
    )


async def _text_columns(conn: Any) -> list[tuple[str, str]]:
    rows = await conn.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' "
        "AND data_type IN ('text', 'character varying', 'jsonb', 'json', 'inet')"
    )
    return [(r["table_name"], r["column_name"]) for r in rows]


async def _find_needle(conn: Any, columns: list[tuple[str, str]], needle: str) -> list[str]:
    hits: list[str] = []
    pattern = f"%{needle}%"
    for table, column in columns:
        # `table`/`column` приходят из information_schema.columns (`_text_columns` выше) —
        # имена реальных таблиц/колонок схемы, не пользовательский ввод; S608 здесь ложное
        # срабатывание (значение сравнения всё равно идёт параметром `$1`, не в тексте SQL).
        count = await conn.fetchval(
            f'SELECT count(*) FROM "{table}" WHERE "{column}"::text ILIKE $1',  # noqa: S608
            pattern,
        )
        if count:
            hits.append(f"{table}.{column}")
    return hits


@pytest.fixture
async def temp_db():
    import asyncpg

    db_name, admin = await _create_temp_db()
    try:
        _migrate(db_name, admin)
        conn = await asyncpg.connect(
            user=admin["user"],
            password=admin["password"],
            host=admin["host"],
            port=admin["port"],
            database=db_name,
        )
        try:
            yield conn
        finally:
            await conn.close()
    finally:
        await _drop_temp_db(db_name, admin)


class TestAnonymizeRemovesOriginalValues:
    async def test_no_original_value_survives_in_any_text_or_jsonb_column(self, temp_db) -> None:
        seeded = _Seeded()
        await _seed(temp_db, seeded)
        columns = await _text_columns(temp_db)

        # До анонимизации находим КАЖДУЮ подсаженную строку хотя бы раз — иначе сам тест
        # ничего не проверяет (ложноположительный «всё чисто» от опечатки в сиде).
        for needle in (
            seeded.real_email,
            seeded.real_name,
            seeded.real_phone,
            seeded.real_snils,
            seeded.real_filename,
            seeded.real_ip,
            seeded.real_user_agent,
        ):
            assert await _find_needle(
                temp_db, columns, needle
            ), f"сид не дошёл до БД: {needle!r} не найдено нигде до anonymize.sql"

        await temp_db.execute(_render_sql("test-salt-1"))

        for needle in (
            seeded.real_email,
            seeded.real_name,
            seeded.real_phone,
            seeded.real_snils,
            seeded.real_filename,
            seeded.real_ip,
            seeded.real_user_agent,
        ):
            hits = await _find_needle(temp_db, columns, needle)
            assert hits == [], f"{needle!r} осталось в: {hits}"


class TestDeterminism:
    async def test_the_same_salt_gives_the_same_pseudonym_different_salt_gives_a_different_one(
        self, temp_db
    ) -> None:
        # `anon_email(seed, salt)` — чистая функция от (id, соль), не от текущего значения
        # колонки: повторный прогон с той же солью на уже обезличенных данных детерминирован
        # ровно так же, как первый — транзакция/откат между прогонами не нужны.
        seeded = _Seeded()
        await _seed(temp_db, seeded)
        user_id = seeded.user_id

        await temp_db.execute(_render_sql("salt-one"))
        email_a = await temp_db.fetchval("SELECT email FROM users WHERE id = $1", user_id)

        await temp_db.execute(_render_sql("salt-one"))
        email_b = await temp_db.fetchval("SELECT email FROM users WHERE id = $1", user_id)

        await temp_db.execute(_render_sql("salt-two"))
        email_c = await temp_db.fetchval("SELECT email FROM users WHERE id = $1", user_id)

        assert email_a == email_b, "одна соль дважды дала разные псевдонимы"
        assert email_a != email_c, "разные соли дали одинаковый псевдоним"


class TestPiiScan:
    def test_a_real_email_and_phone_are_flagged(self) -> None:
        from tools.pii_scan import scan

        findings = scan(
            ["INSERT INTO x VALUES ('ivan.petrov@real-mail.example', '+79161234567');"],
            real_email_hashes=frozenset(),
        )

        assert findings["email"] == 1
        assert findings["phone"] == 1

    def test_the_anonymized_format_is_not_flagged(self) -> None:
        from tools.pii_scan import scan

        findings = scan(
            ["INSERT INTO x VALUES ('u" + "a" * 16 + "@example.invalid', '+7 900 123-4567');"],
            real_email_hashes=frozenset(),
        )

        assert not any(findings.values())

    @pytest.mark.parametrize(
        "text",
        [
            "+7 (999) 123-45-12",
            "8 999 123 45 67",
            "8-999-123-45-67",
            "89991234567",
            "тел.: +7.999.123.45.67, доб. 12",
        ],
    )
    def test_the_usual_phone_spellings_are_flagged(self, text: str) -> None:
        from tools.pii_scan import scan

        findings = scan([f"контакт: {text}\n"], real_email_hashes=frozenset())

        assert findings["phone"] == 1

    def test_ids_hashes_and_neighbouring_tab_columns_are_not_phones(self) -> None:
        """Скан идёт по тексту `pg_dump`: колонки разделены табуляцией, а UUID, sha256 и
        длинные числовые идентификаторы встречаются на каждой странице. Раньше `8` на конце
        UUID-группы или на конце колонки плюс цифры соседней давали сотни ложных находок."""
        from tools.pii_scan import scan

        lines = [
            "al\t01a14e9e-fdd2-7000-96d8-8132493078dd\tcontract\t\\N\n",
            "9588\t146731\t00100159731\t12\t34\t5678901234\n",
            "sha\t" + "5" * 20 + "8" + "1234567890" + "ab" + "\n",
            "2026-10-18 15:12:48.123456+00\t2026-10-18\n",
            "1234567890123456789012\t81234567890123\n",
        ]

        findings = scan(lines, real_email_hashes=frozenset())

        assert not any(findings.values()), dict(findings)

    def test_formatted_snils_is_flagged(self) -> None:
        from tools.pii_scan import scan

        findings = scan(["some text 112-233-445 95 trailing"], real_email_hashes=frozenset())

        assert findings["snils_formatted"] == 1

    def test_main_returns_3_on_findings_and_0_when_clean(self, tmp_path: Path) -> None:
        from tools.pii_scan import main

        dirty = tmp_path / "dirty.sql"
        dirty.write_text("'ivan.petrov@real-mail.example'", encoding="utf-8")
        clean = tmp_path / "clean.sql"
        clean.write_text("'u" + "a" * 16 + "@example.invalid'", encoding="utf-8")

        assert main([str(dirty)]) == 3
        assert main([str(clean)]) == 0


class TestDumpScript:
    """`tools/anonymized_dump.sh` целиком (с настоящим `pg_restore`/`pg_dump`) здесь не гоняется —
    ему нужен клиент PostgreSQL в PATH; проверяются два места, которые ломались молча."""

    _SCRIPT = ROOT / "tools" / "anonymized_dump.sh"

    def test_no_pg_restore_call_combines_dbname_and_file(self) -> None:
        """`pg_restore -d БАЗА -f -` — «options -d/--dbname and -f/--file cannot be used
        together»: шаг скана раньше получал пустой ввод, печатал «ok» и валил весь скрипт
        сообщением про найденные ПДн, так что обезличенный дамп нельзя было получить вовсе."""
        offenders = []
        for number, line in enumerate(self._SCRIPT.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "pg_restore" in code and re.search(r"\s-d\s", code) and re.search(r"\s-f\s", code):
                offenders.append(f"{number}: {line.strip()}")
        assert offenders == [], offenders

    @pytest.mark.skipif(
        sys.platform == "win32" or not shutil.which("bash"), reason="нужен bash (не WSL-заглушка)"
    )
    def test_encrypt_with_dev_key_fails_early_with_a_clear_message(self) -> None:
        """Флаг не реализован: падать надо сразу и понятно (код 2, что делать), не доходя ни до
        БД, ни до проверки остальных аргументов — и не молча его проигнорировать."""
        result = subprocess.run(  # noqa: S603
            ["bash", str(self._SCRIPT), "--encrypt-with-dev-key"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )

        assert result.returncode == 2
        assert "--encrypt-with-dev-key не реализован" in result.stderr
        assert "без этого флага" in result.stderr


# --- Защита от расхождения со схемой (без БД) --------------------------------------

#: `(таблица, колонка)`, которые `anonymize.sql` либо переписывает по имени, либо целиком
#: `TRUNCATE`-ит (тогда содержимое любой колонки этой таблицы безопасно по построению).
_TRUNCATED_TABLES = frozenset(
    {
        "egrul_entries",
        "notifications",
        "notification_deliveries",
        "user_invites",
        "signature_otp_codes",
        "idempotency_keys",
        "security_events",
        "org_lookup_log",
        "audit_log",
    }
)

_HANDLED_COLUMNS = frozenset(
    {
        ("users", "email"),
        ("users", "full_name"),
        ("users", "display_name"),
        ("users", "phone"),
        ("users", "status_reason"),
        ("contacts", "first_name"),
        ("contacts", "last_name"),
        ("contacts", "middle_name"),
        ("contacts", "email"),
        ("contacts", "phone"),
        ("contact_channels", "value"),
        ("contact_learner_profiles", "snils"),
        ("contact_learner_profiles", "passport_series"),
        ("contact_learner_profiles", "passport_number"),
        ("contact_learner_profiles", "passport_issued_by"),
        ("contact_learner_profiles", "passport_issued_at"),
        ("contact_learner_profiles", "passport_dept_code"),
        ("contact_learner_profiles", "first_name_dative"),
        ("contact_learner_profiles", "last_name_dative"),
        ("contact_learner_profiles", "middle_name_dative"),
        ("contact_learner_profiles", "diploma_surname"),
        ("organizations", "name"),
        ("organizations", "short_name"),
        ("organizations", "legal_address"),
        ("organizations", "actual_address"),
        ("organizations", "main_phone"),
        ("organizations", "main_email"),
        ("organizations", "registry_snapshot"),
        ("organization_licenses", "manager_full_name"),
        ("organization_licenses", "responsible_contacts"),
        ("organization_licenses", "comment"),
        ("deal_comments", "body"),
        ("deal_comment_revisions", "body"),
        ("deal_events", "payload"),
        ("deal_status_history", "comment"),
        ("deals", "title"),
        ("tasks", "title"),
        ("tasks", "description"),
        ("attachments", "description"),
        ("edm_agreements", "revoke_reason"),
        ("files", "original_filename"),
        ("import_row_results", "before_snapshot"),
        ("import_row_results", "row_data"),
        ("inbound_messages", "raw_payload"),
        ("outbox_events", "payload"),
        ("learning_progress", "raw"),
        ("data_erasure_requests", "reason"),
        ("data_erasure_requests", "rejection_reason"),
        ("admin_approvals", "reason"),
        ("admin_approvals", "payload"),
        ("signature_documents", "title"),
        ("signature_documents", "void_reason"),
        ("signature_requests", "signer_name_snapshot"),
        ("signature_requests", "signer_identifier_masked"),
        ("signature_requests", "reject_reason"),
        ("signatures", "signer_display"),
        ("signatures", "evidence"),
        ("signatures", "ip"),
        ("signatures", "user_agent"),
        ("consents", "ip"),
        ("consents", "user_agent"),
        ("security_incidents", "title"),
        ("security_incidents", "description"),
        ("security_incidents", "causes"),
        ("security_incidents", "harm"),
        ("security_incidents", "measures"),
        ("security_incidents", "investigation_summary"),
        ("security_incidents", "rkn_24_reference"),
        ("security_incidents", "rkn_72_reference"),
        ("system_settings", "value"),
    }
)

#: Совпадают по имени с шаблоном ПДн, но не являются ПДн — с причиной на каждую запись.
_NOT_PDN: dict[tuple[str, str], str] = {
    ("regions", "name"): "название региона РФ — справочник, не ПДн",
    ("dashboards", "name"): "название дашборда, придумано администратором",
    ("directions", "name"): "направление обучения — справочник",
    ("holidays", "name"): "название праздника — справочник календаря",
    ("import_presets", "name"): "название сохранённого шаблона импорта",
    ("integration_sources", "name"): "название источника интеграции (например, Bitrix)",
    ("loss_reasons", "name"): "справочник причин отказа",
    ("products", "name"): "название продукта каталога",
    ("products", "description"): "описание продукта каталога",
    ("report_templates", "name"): "название шаблона отчёта",
    ("report_templates", "description"): "описание шаблона отчёта",
    ("signature_templates", "name"): "название шаблона документа на подпись",
    ("signature_templates", "body_template"): "юридический текст шаблона, не о подписанте",
    ("notification_templates", "body_template"): "текст шаблона уведомления",
    ("teams", "name"): "название команды/отдела",
    ("workflow_statuses", "name"): "название статуса воронки",
    ("workflow_transitions", "name"): "название перехода воронки",
    ("workflows", "name"): "название воронки",
    ("deal_comments", "body_format"): "формат текста (plain/markdown), не содержимое",
    ("deal_status_history", "reason"): "код причины перехода (enum), не свободный текст",
    (
        "organization_branches",
        "name",
    ): "филиал вуза — название юрлица, не ПДн (допущение, см. anonymize.sql)",
    ("organization_branches", "address"): "адрес филиала вуза — то же допущение",
    ("organization_licenses", "product_name"): "название продукта лицензии, не ПДн",
    ("system_settings", "description"): "описание настройки, пишет администратор",
    ("feature_flags", "description"): "описание флага функциональности",
}

# `contacts$`/`investigation_summary`/`causes`/`harm`/`measures`/`_reference$` — после находки:
# `organization_licenses.responsible_contacts` (ФИО, телефоны и email сотрудников вуза одной
# строкой) и свободный текст `security_incidents` долго оставались в дампе, потому что имена этих
# колонок не совпадали ни с одним из шаблонов выше.
_PII_NAME_PATTERN = re.compile(
    r"email|phone|name|body|payload|snils|passport|address|^ip$|user_agent|evidence|"
    r"raw_payload|row_data|snapshot|reason|comment|title|description|"
    r"contacts$|investigation_summary|^causes$|^harm$|^measures$|_reference$",
    re.IGNORECASE,
)
_TEXT_TYPES = {"VARCHAR", "TEXT", "JSONB", "JSON", "INET"}


def _orm_pii_pattern_columns() -> set[tuple[str, str]]:
    os.environ.setdefault("APP_PROFILE", "dev")
    for raw in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

    from app.db.models import target_metadata

    found: set[tuple[str, str]] = set()
    for table_name, table in target_metadata.tables.items():
        for column in table.columns:
            type_name = str(column.type).split("(")[0]
            if _PII_NAME_PATTERN.search(column.name) and type_name in _TEXT_TYPES:
                found.add((table_name, column.name))
    return found


class TestColumnCoverageGuard:
    """Без БД: если в метаданных ORM появилась новая таблица/колонка с именем, похожим на
    ПДн, не учтённая ни в `_HANDLED_COLUMNS`, ни в `_TRUNCATED_TABLES`, ни в `_NOT_PDN` —
    тест падает и называет конкретную колонку. Так дамп не отстаёт от схемы молча."""

    def test_every_pii_shaped_column_is_covered(self) -> None:
        found = _orm_pii_pattern_columns()
        uncovered = sorted(
            f"{table}.{column}"
            for table, column in found
            if table not in _TRUNCATED_TABLES
            and (table, column) not in _HANDLED_COLUMNS
            and (table, column) not in _NOT_PDN
        )
        assert uncovered == [], (
            "новые колонки похожи на ПДн, но не упомянуты в anonymize.sql ни в "
            f"исключениях теста: {uncovered}"
        )

    def test_not_pdn_entries_still_exist_in_the_schema(self) -> None:
        found = _orm_pii_pattern_columns()
        stale = sorted(
            f"{table}.{column}" for table, column in _NOT_PDN if (table, column) not in found
        )
        assert stale == [], f"записи NOT_PDN для уже не существующих колонок: {stale}"
