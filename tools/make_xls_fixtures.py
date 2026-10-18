"""B3-10: генератор бинарных .xls-фикстур для тестов на реальные байты формата.

    python tools/make_xls_fixtures.py

Запускается один раз разработчиком; файлы коммитятся в `tests/fixtures/imports/` — CI их не
генерирует (раздел приёмки задачи). Все данные выдуманные, без настоящих ПДн.

Фикстуры:

* `licenses_10cols.xls` — 10 колонок ТЗ, ячейка-дата (16.03.2024) и ячейка-число (2027), текстовый
  номер договора («Д-12/2024») — покрывает ветки `parse_xls`/`_xls_row` для `XL_CELL_DATE` и
  `XL_CELL_NUMBER`.
* `licenses_multisheet.xls` — три листа («2024», «2025», «Черновик» скрытый), шапка на листе
  «2024» в строке 3 (титул + пустая строка перед ней).
* `licenses_date1904.xls` — та же дата, но книга в режиме `datemode=1` (база 1904 года, Mac Excel):
  `xlrd.xldate_as_datetime(..., workbook.datemode)` должен дать тот же календарный день.
* `empty.xls` — лист без единой строки (кроме обязательной BIFF-структуры): `parse_xls` должен
  вернуть «Файл пуст», а не упасть.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import xlwt

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "tests" / "fixtures" / "imports"

LICENSE_HEADERS = [
    "Название ВУЗа",
    "Вендор",
    "ПО",
    "Номер договора",
    "Подписание лицензии",
    "Срок действия лицензии (год)",
    "Статус по передаче",
    "ФИО Менеджера",
    "Ответственные от ВУЗа",
    "Комментарий",
]


def _bold() -> xlwt.XFStyle:
    return xlwt.easyxf("font: bold on")


def _date_style() -> xlwt.XFStyle:
    return xlwt.easyxf(num_format_str="DD.MM.YYYY")


def make_licenses_10cols() -> bytes:
    workbook = xlwt.Workbook(encoding="utf-8")
    sheet = workbook.add_sheet("Лицензии")
    bold = _bold()
    date_style = _date_style()
    for col, header in enumerate(LICENSE_HEADERS):
        sheet.write(0, col, header, bold)
    sheet.write(1, 0, "МГТУ им. Н.Э. Баумана")
    sheet.write(1, 1, "ООО «Базис»")
    sheet.write(1, 2, "RT.DataLake")
    sheet.write(1, 3, "Д-12/2024")
    sheet.write(1, 4, dt.date(2024, 3, 16), date_style)  # ячейка-дата -> XL_CELL_DATE
    sheet.write(1, 5, 2027)  # ячейка-число -> XL_CELL_NUMBER
    sheet.write(1, 6, "передано")
    sheet.write(1, 7, "Иванов Иван Иванович")
    sheet.write(1, 8, "")
    sheet.write(1, 9, "")
    buffer = _save(workbook)
    return buffer


def make_licenses_multisheet() -> bytes:
    workbook = xlwt.Workbook(encoding="utf-8")
    bold = _bold()

    sheet_2024 = workbook.add_sheet("2024")
    sheet_2024.write(0, 0, "Каталог лицензий 2024")
    # Строка 1 (индекс 1) оставлена пустой намеренно — титул перед шапкой.
    for col, header in enumerate(LICENSE_HEADERS):
        sheet_2024.write(2, col, header, bold)
    sheet_2024.write(3, 0, "МГТУ им. Н.Э. Баумана")
    sheet_2024.write(3, 1, "ООО «Базис»")
    sheet_2024.write(3, 2, "RT.DataLake")
    sheet_2024.write(3, 3, "Д-1-2024")

    sheet_2025 = workbook.add_sheet("2025")
    for col, header in enumerate(LICENSE_HEADERS):
        sheet_2025.write(0, col, header, bold)
    sheet_2025.write(1, 0, "СПбГУ")
    sheet_2025.write(1, 1, "ПАО «Ростелеком»")
    sheet_2025.write(1, 2, "RT.CRM")
    sheet_2025.write(1, 3, "Д-1-2025")

    draft = workbook.add_sheet("Черновик")
    draft.write(0, 0, "Не готово")
    # `xlwt` помечает лист скрытым через сам `Sheet`, а не через `Workbook` — см. `visibility`.
    draft.set_visibility(1)  # 1 = hidden (как в xlrd: 0 видимый, 1 скрытый, 2 очень скрытый)

    return _save(workbook)


def make_licenses_date1904() -> bytes:
    workbook = xlwt.Workbook(encoding="utf-8")
    workbook.dates_1904 = True
    sheet = workbook.add_sheet("Лицензии")
    bold = _bold()
    date_style = _date_style()
    for col, header in enumerate(LICENSE_HEADERS):
        sheet.write(0, col, header, bold)
    sheet.write(1, 0, "МГТУ им. Н.Э. Баумана")
    sheet.write(1, 1, "ООО «Базис»")
    sheet.write(1, 2, "RT.DataLake")
    sheet.write(1, 3, "Д-12/2024")
    sheet.write(1, 4, dt.date(2024, 3, 16), date_style)
    return _save(workbook)


def make_empty() -> bytes:
    workbook = xlwt.Workbook(encoding="utf-8")
    workbook.add_sheet("Пусто")
    return _save(workbook)


def _save(workbook: xlwt.Workbook) -> bytes:
    import io

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


FIXTURES = {
    "licenses_10cols.xls": make_licenses_10cols,
    "licenses_multisheet.xls": make_licenses_multisheet,
    "licenses_date1904.xls": make_licenses_date1904,
    "empty.xls": make_empty,
}


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    import hashlib

    readme_lines = [
        "# Фикстуры .xls (B3-10)",
        "",
        "Сгенерированы `tools/make_xls_fixtures.py` (xlwt), не редактируются руками.",
        "Перегенерировать: `python tools/make_xls_fixtures.py`.",
        "",
        "| Файл | SHA-256 | Описание |",
        "| --- | --- | --- |",
    ]
    descriptions = {
        "licenses_10cols.xls": "10 колонок ТЗ, ячейка-дата и ячейка-число",
        "licenses_multisheet.xls": "три листа, шапка в строке 3, один лист скрыт",
        "licenses_date1904.xls": "книга в режиме datemode=1 (Mac Excel)",
        "empty.xls": "лист без единой строки данных",
    }
    for name, builder in FIXTURES.items():
        content = builder()
        path = FIXTURES_DIR / name
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        readme_lines.append(f"| `{name}` | `{digest}` | {descriptions[name]} |")
        print(f"{name}: {len(content)} bytes, sha256={digest}")

    (FIXTURES_DIR / "README.md").write_text("\n".join(readme_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
