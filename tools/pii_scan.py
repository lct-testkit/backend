"""B6-13: последняя защита перед выгрузкой обезличенного дампа — сканирует SQL-текст,
который отдаёт `pg_restore -f -` по уже обезличенной временной базе, на остатки ПДн.

    pg_restore -f - --no-owner --no-privileges "$DUMP" | python tools/pii_scan.py
    python tools/pii_scan.py --real-email-hashes hashes.txt < dump.sql

Код выхода 3 и счётчик находок по типам, если найдено хоть что-то (без самих значений —
находки не печатаются, только тип и число); `tools/anonymized_dump.sh` при коде 3 удаляет
файл дампа и не публикует его. Код 0 — находок нет.

Что ловится надёжно:
  * email, не оканчивающийся на `@example.invalid` (ровно в этот домен `anonymize.sql`
    переписывает все адреса — `anon_email()`; RFC 2606 резервирует `.invalid` так, что он
    никогда не резолвится, поэтому формат однозначно отличает «обезличено» от «не успели»);
  * телефон в формате `+7`/`8` + 10 цифр, кроме точного формата анонимизатора `+7 900 XXX-XXXX`;
  * СНИЛС в ФОРМАТИРОВАННОМ виде `XXX-XXX-XXX XX` (с разделителями) — `anonymize.sql` хранит
    СНИЛС без разделителей (как в БД), поэтому любое совпадение этого шаблона означает, что
    где-то за пределами `contact_learner_profiles.snils` осталась отображаемая форма реального
    номера (например, в `evidence`/`raw_payload`, которые должны быть очищены отдельно).

Чего эта проверка НЕ ловит (сознательно, см. README «Обезличенный дамп»): серию и номер
паспорта по отдельности — это 4 и 6 произвольных цифр без признака, отличающего реальные от
синтетических (анонимизатор генерирует такой же формат), регулярка на «любые 4+6 цифр»
давала бы или шум на сериях документов/сумм, или пропускала бы настоящие. Контроль этого
поля — ревью `contact_learner_profiles` до подготовки бэкапа, не автоматический скан текста.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter
from collections.abc import Iterable

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ANON_EMAIL_SUFFIX = "@example.invalid"

# `(?:\+7|8)` — два принятых в РФ формата кода страны; разделители пробел/дефис/точка
# опциональны и могут повторяться неравномерно, поэтому разрешены после любой группы цифр.
_PHONE_RE = re.compile(r"(?:\+7|8)[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}")
_ANON_PHONE_RE = re.compile(r"^\+7 900 \d{3}-\d{4}$")

_SNILS_FORMATTED_RE = re.compile(r"\b\d{3}-\d{3}-\d{3} \d{2}\b")


def _real_email_hashes(path: str | None) -> frozenset[str]:
    """Множество sha256(email в нижнем регистре) исходной базы — строится ДО обезличивания
    (`anonymized_dump.sh` сохраняет его во временный файл и удаляет вместе с временной базой).
    Пустое множество, если файл не передан: проверка по формату `@example.invalid` всё равно
    работает, это только дополнительный слой для email, которые теоретически могли бы
    случайно получить такой же суффикс не через анонимизатор."""
    if not path:
        return frozenset()
    with open(path, encoding="utf-8") as fh:
        return frozenset(line.strip() for line in fh if line.strip())


def scan(lines: Iterable[str], *, real_email_hashes: frozenset[str]) -> Counter[str]:
    findings: Counter[str] = Counter()
    for line in lines:
        for match in _EMAIL_RE.finditer(line):
            email = match.group(0)
            if not email.lower().endswith(_ANON_EMAIL_SUFFIX):
                findings["email"] += 1
                continue
            if real_email_hashes and _hash_email(email) in real_email_hashes:
                # Теоретически невозможно (суффикс .invalid), но раз уж множество реальных
                # хэшей передано — сверяем и его: защита от будущей правки anon_email().
                findings["email_hash_match"] += 1

        for match in _PHONE_RE.finditer(line):
            if not _ANON_PHONE_RE.match(_normalize_phone(match.group(0))):
                findings["phone"] += 1

        snils_hits = len(_SNILS_FORMATTED_RE.findall(line))
        if snils_hits:
            findings["snils_formatted"] += snils_hits

    return findings


def _normalize_phone(raw: str) -> str:
    """Приводит найденный телефон к тому же виду, что отдаёт `anon_phone()`
    (`+7 900 XXX-XXXX`), чтобы сравнить без учёта формата разделителей."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if not digits.startswith("7") or len(digits) != 11:
        return raw
    return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:]}"


def _hash_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--real-email-hashes",
        default=None,
        help="файл с sha256(email) исходной базы, по одному на строку (необязательно)",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="файл для скана; без аргумента читает stdin (пайп из `pg_restore -f -`)",
    )
    args = parser.parse_args(argv)

    real_email_hashes = _real_email_hashes(args.real_email_hashes)

    if args.input:
        with open(args.input, encoding="utf-8", errors="replace") as fh:
            findings = scan(fh, real_email_hashes=real_email_hashes)
    else:
        findings = scan(sys.stdin, real_email_hashes=real_email_hashes)

    if any(findings.values()):
        print("pii_scan: найдены возможные ПДн (значения не печатаются):", file=sys.stderr)
        for kind, count in sorted(findings.items()):
            if count:
                print(f"  - {kind}: {count}", file=sys.stderr)
        return 3

    print("pii_scan: ok (email/phone/snils-формат — находок нет)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
