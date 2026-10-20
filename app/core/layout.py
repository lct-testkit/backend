"""Q-1: исправление раскладки клавиатуры в строке поиска.

Человек ищет «ппу», а набрал `gge` — клавиши те же, раскладка английская. Если по набранному
ничего не нашлось, поиск повторяется со строкой, переведённой в другую раскладку (стандартные
QWERTY ↔ ЙЦУКЕН). Переводятся буквы и знаки на буквенных клавишах; цифры и пробелы остаются
как есть. Смешанную строку (латиница и кириллица вперемешку) не переводим: непонятно, что
имелось в виду.
"""

from __future__ import annotations

_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`" + 'QWERTYUIOP{}ASDFGHJKL:"ZXCVBNM<>~'
_RU = "йцукенгшщзхъфывапролджэячсмитьбюё" + "ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮЁ"

_EN_TO_RU = dict(zip(_EN, _RU, strict=True))
_RU_TO_EN = dict(zip(_RU, _EN, strict=True))

_LATIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_CYRILLIC = frozenset(_RU)


def switch_layout(text: str) -> str | None:
    """Строка, набранная в другой раскладке, или `None`, если переводить нечего: нет букв,
    буквы обеих раскладок вперемешку, либо результат совпал с исходной строкой."""
    has_latin = any(ch in _LATIN for ch in text)
    has_cyrillic = any(ch in _CYRILLIC for ch in text)
    if has_latin == has_cyrillic:
        return None
    table = _EN_TO_RU if has_latin else _RU_TO_EN
    converted = "".join(table.get(ch, ch) for ch in text)
    return converted if converted != text else None
