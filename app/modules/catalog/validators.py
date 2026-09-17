"""Контрольные суммы ИНН/КПП/ОГРН/ОГРНИП (dop.md §11.4).

Отсекает опечатки бесплатно, без сети и без обращения к реестру ЕГРЮЛ
(последний появится в спринте 5). Используется и при создании организации
(раздел 6: «валидирует контрольную сумму»), и будет переиспользовано
`POST /api/org-lookup/validate` спринта 5 — поэтому вынесено в отдельный
модуль, а не спрятано внутри `catalog.service`.
"""

from __future__ import annotations

from dataclasses import dataclass

_INN10_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_WEIGHTS_11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_WEIGHTS_12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


@dataclass(slots=True, frozen=True)
class RequisiteCheck:
    ok: bool
    reason: str | None = None


def _checksum_digit(digits: str, weights: tuple[int, ...]) -> int:
    total = sum(int(d) * w for d, w in zip(digits, weights, strict=True))
    return (total % 11) % 10


def _valid_region_code(digits: str) -> bool:
    code = int(digits[:2])
    return 1 <= code <= 99


def validate_inn(value: str | None) -> RequisiteCheck:
    if not value or not value.isdigit():
        return RequisiteCheck(False, "ИНН должен состоять только из цифр")
    if len(value) not in (10, 12):
        return RequisiteCheck(False, "ИНН должен содержать 10 или 12 цифр")
    if value == value[0] * len(value):
        return RequisiteCheck(False, "ИНН не может состоять из одинаковых цифр")
    if not _valid_region_code(value):
        return RequisiteCheck(False, "Некорректный код региона в ИНН")

    if len(value) == 10:
        expected = _checksum_digit(value[:9], _INN10_WEIGHTS)
        if expected != int(value[9]):
            return RequisiteCheck(False, "Неверная контрольная сумма ИНН")
        return RequisiteCheck(True)

    check11 = _checksum_digit(value[:10], _INN12_WEIGHTS_11)
    check12 = _checksum_digit(value[:11], _INN12_WEIGHTS_12)
    if check11 != int(value[10]) or check12 != int(value[11]):
        return RequisiteCheck(False, "Неверная контрольная сумма ИНН")
    return RequisiteCheck(True)


def validate_kpp(value: str | None) -> RequisiteCheck:
    if not value or not value.isdigit() or len(value) != 9:
        return RequisiteCheck(False, "КПП должен состоять из 9 цифр")
    return RequisiteCheck(True)


def validate_ogrn(value: str | None) -> RequisiteCheck:
    if not value or not value.isdigit() or len(value) != 13:
        return RequisiteCheck(False, "ОГРН должен состоять из 13 цифр")
    expected = (int(value[:12]) % 11) % 10
    if expected != int(value[12]):
        return RequisiteCheck(False, "Неверная контрольная сумма ОГРН")
    return RequisiteCheck(True)


def validate_ogrnip(value: str | None) -> RequisiteCheck:
    if not value or not value.isdigit() or len(value) != 15:
        return RequisiteCheck(False, "ОГРНИП должен состоять из 15 цифр")
    expected = (int(value[:14]) % 13) % 10
    if expected != int(value[14]):
        return RequisiteCheck(False, "Неверная контрольная сумма ОГРНИП")
    return RequisiteCheck(True)


_VALIDATORS = {
    "inn": validate_inn,
    "kpp": validate_kpp,
    "ogrn": validate_ogrn,
    "ogrnip": validate_ogrnip,
}


def validate_requisite(kind: str, value: str | None) -> RequisiteCheck:
    validator = _VALIDATORS.get(kind)
    if validator is None:
        return RequisiteCheck(False, f"Неизвестный тип реквизита: {kind}")
    return validator(value)
