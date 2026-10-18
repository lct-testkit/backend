"""B1-15: `Content-Disposition` с именем файла вне ASCII (RFC 6266) — чистая функция, без
сети/БД."""

from __future__ import annotations

from app.core.storage import _content_disposition


class TestContentDisposition:
    def test_ascii_filename_round_trips_unchanged_in_the_fallback_field(self) -> None:
        header = _content_disposition("attachment", "report.pdf")
        assert 'filename="report.pdf"' in header

    def test_cyrillic_filename_gets_both_fields(self) -> None:
        header = _content_disposition("attachment", "Отчёт.xlsx")
        assert header.startswith("attachment; filename=")
        assert "filename*=UTF-8''" in header
        # Кириллица percent-encoded в `filename*`, не как есть.
        assert "%D0%9E%D1%82%D1%87%D1%91%D1%82" in header

    def test_ascii_fallback_field_has_no_raw_cyrillic(self) -> None:
        header = _content_disposition("attachment", "Отчёт.xlsx")
        fallback = header.split('filename="')[1].split('"')[0]
        assert all(ord(ch) < 128 for ch in fallback)

    def test_embedded_quote_does_not_break_the_header_value(self) -> None:
        header = _content_disposition("attachment", 'a".pdf')
        fallback = header.split('filename="')[1].split('"')[0]
        assert '"' not in fallback

    def test_empty_ascii_result_falls_back_to_a_placeholder(self) -> None:
        header = _content_disposition("attachment", "Отчёт")
        fallback = header.split('filename="')[1].split('"')[0]
        assert fallback  # не пустая строка в кавычках
