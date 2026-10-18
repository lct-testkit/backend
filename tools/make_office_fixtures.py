"""Генератор фикстур `.doc`/`.xls` для тестов глубокой проверки форматов (B2-9).

На машине разработки нет LibreOffice (`soffice --headless --convert-to`, которым
исходный план спринта предлагал собрать фикстуры), поэтому `.xls` собирается
настоящим писателем BIFF8 (`xlwt`, уже в зависимостях проекта — `pyproject.toml`),
а `.doc` — минимальным компаунд-файлом OLE2 (CFB, тот же контейнер, что у `.xls`),
собранным вручную по MS-CFB: ровно столько структуры, сколько нужно `xlrd.compdoc.
CompDoc(...).dirlist`, чтобы увидеть поток `WordDocument` — именно так его различает
`app.modules.files.service.office_kind`. Содержимое потока не несёт настоящего текста
Word (это не нужно проверке по именам потоков), это ЧЕСТНО отражено в README рядом с
фикстурами и здесь в докстринге.

Запуск: `python tools/make_office_fixtures.py` из корня `backend/` — перезапишет
`tests/fixtures/files/sample.xls` и `tests/fixtures/files/sample.doc` и напечатает их
SHA256 (та же строка, что должна быть в README рядом с фикстурами).
"""

from __future__ import annotations

import hashlib
import io
import struct
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "files"

_CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_SECTOR_SIZE = 512
_FREESID = -1
_EOCSID = -2
_SATSID = -3


def _dir_entry(
    name: str,
    *,
    etype: int,
    left_did: int = _FREESID,
    right_did: int = _FREESID,
    root_did: int = _FREESID,
) -> bytes:
    """Один 128-байтовый элемент каталога CFB (MS-CFB 2.6.1).

    `first_SID`/`tot_size` для наших записей всегда нулевые: `CompDoc.__init__` их не
    разыменовывает при построении `dirlist` (это делает только `get_named_stream`,
    который `office_kind` не вызывает) — реальных секторов данных файлу не нужно.
    """
    name_utf16 = name.encode("utf-16-le")
    cbufsize = len(name_utf16) + 2 if name else 0  # включая терминирующий ноль
    entry = bytearray(128)
    entry[0 : len(name_utf16)] = name_utf16
    struct.pack_into(
        "<HBBiii",
        entry,
        64,
        cbufsize,
        etype,
        1,  # colour — не проверяется при чтении, только при записи настоящего CFB
        left_did,
        right_did,
        root_did,
    )
    # entry[80:116] — CLSID и штампы времени, нули допустимы.
    struct.pack_into("<ii", entry, 116, _EOCSID, 0)  # first_SID, tot_size
    return bytes(entry)


def build_minimal_cfb(stream_names: list[str]) -> bytes:
    """Минимальный валидный компаунд-файл OLE2 с корнем и `stream_names` как прямыми
    потоками-детьми корня (без вложенных storage). Потоки пустые (см. `_dir_entry`).

    Структура: заголовок (512 б) → сектор 0 = FAT → сектор 1 = каталог (до 4 записей:
    корень + до 3 потоков). Для `doc`/`xls` этого достаточно: оба нужных набора потоков
    (`WordDocument`+`1Table`, `Workbook`) укладываются в 3 записи.
    """
    if len(stream_names) > 3:
        raise ValueError("минимальный каталог вмещает не больше 3 потоков")

    header = bytearray(512)
    header[0:8] = _CFB_SIGNATURE
    # CLSID (8:24) — нули.
    # minor, major version (v3 => 512-байтовые секторы)
    struct.pack_into("<HH", header, 24, 0x003E, 0x0003)
    header[28:30] = b"\xfe\xff"  # byte order marker
    struct.pack_into("<HH", header, 30, 9, 6)  # sector shift, mini sector shift
    # 34:40 reserved, 40:44 число directory sectors (0 для v3) — нули по умолчанию.
    struct.pack_into(
        "<iiiiiiii",
        header,
        44,
        1,  # число секторов FAT
        1,  # первый сектор каталога (sid 1)
        0,  # transaction signature — не используется
        4096,  # mini stream cutoff — не используется (root tot_size=0 ниже)
        _EOCSID,  # первый сектор mini-FAT — нет
        0,  # число секторов mini-FAT
        _EOCSID,  # первый сектор DIFAT — нет расширения MSAT
        0,  # число секторов DIFAT
    )
    # MSAT (76:512) — 109 записей по 4 байта: первая указывает на наш единственный
    # сектор FAT (sid 0), остальные — FREESID.
    msat = [_FREESID] * 109
    msat[0] = 0
    struct.pack_into("<109i", header, 76, *msat)

    fat_sector = bytearray(_SECTOR_SIZE)
    fat_entries = [_FREESID] * (_SECTOR_SIZE // 4)
    fat_entries[0] = _SATSID  # сектор 0 — сам FAT
    fat_entries[1] = _EOCSID  # сектор 1 — каталог, цепочка длиной 1
    struct.pack_into(f"<{len(fat_entries)}i", fat_sector, 0, *fat_entries)

    entries = [_dir_entry("Root Entry", etype=5, root_did=1 if stream_names else _FREESID)]
    for index, name in enumerate(stream_names):
        right = index + 2 if index + 1 < len(stream_names) else _FREESID
        entries.append(_dir_entry(name, etype=2, right_did=right))
    while len(entries) < 4:
        entries.append(_dir_entry("", etype=0))
    dir_sector = b"".join(entries)
    if len(dir_sector) != _SECTOR_SIZE:
        raise AssertionError("каталог CFB должен быть ровно одним сектором")

    return bytes(header) + bytes(fat_sector) + dir_sector


def build_sample_xls() -> bytes:
    import xlwt

    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet("Лист1")
    sheet.write(0, 0, "Договор")
    sheet.write(0, 1, "Сумма")
    sheet.write(1, 0, "Тестовая фикстура B2-9")
    sheet.write(1, 1, 1500)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def build_sample_doc() -> bytes:
    return build_minimal_cfb(["WordDocument", "1Table"])


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    xls = build_sample_xls()
    doc = build_sample_doc()
    (FIXTURES_DIR / "sample.xls").write_bytes(xls)
    (FIXTURES_DIR / "sample.doc").write_bytes(doc)
    for name, content in (("sample.xls", xls), ("sample.doc", doc)):
        print(f"{name}: {len(content)} байт, sha256={hashlib.sha256(content).hexdigest()}")


if __name__ == "__main__":
    main()
