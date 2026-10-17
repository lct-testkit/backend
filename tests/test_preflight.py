"""`python -m app.core.preflight --env-file ...` (B6-8): .env с демо-секретами даёт код 1,
чистый — код 0. Значения в выводе не печатаются."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import preflight


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = preflight.main(["--env-file", str(tmp_path / "missing.env")])
    assert code == 1
    assert "не найден" in capsys.readouterr().err


def test_demo_keycloak_admin_password_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "APP_PROFILE=demo\nKEYCLOAK_ADMIN_PASSWORD=admin\n")
    code = preflight.main(["--env-file", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert "KEYCLOAK_ADMIN_PASSWORD" in out.err
    assert "admin" not in out.err  # значение не печатается


def test_clean_file_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(
        tmp_path,
        "APP_PROFILE=demo\n"
        "KEYCLOAK_ADMIN_PASSWORD=Really-Strong-Admin-Password-2026\n"
        "POSTGRES_PASSWORD=Really-Strong-Postgres-Password-2026\n",
    )
    code = preflight.main(["--env-file", str(path)])
    assert code == 0
    assert "ok" in capsys.readouterr().out


def test_prod_requires_audit_and_encryption_keys(tmp_path: Path) -> None:
    path = _write(tmp_path, "APP_PROFILE=prod\n")
    code = preflight.main(["--env-file", str(path)])
    assert code == 1


def test_prod_passes_with_mandatory_keys_present(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "APP_PROFILE=prod\n"
        "AUDIT_HMAC_KEY=Really-Strong-Audit-Hmac-Key-0123456789abcdef\n"
        "SETTINGS_ENCRYPTION_KEY=5GhIE6Z0qz3m1C8n9F2k7V4bN6yQpR1sT8uW0xY2zA4=\n"
        "FIELD_ENCRYPTION_KEY=5GhIE6Z0qz3m1C8n9F2k7V4bN6yQpR1sT8uW0xY2zA4=\n",
    )
    code = preflight.main(["--env-file", str(path)])
    assert code == 0


def test_demo_value_in_database_url_password_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "APP_PROFILE=demo\nDATABASE_URL=postgresql+asyncpg://crm_app:crm_app@db:5432/crm\n",
    )
    code = preflight.main(["--env-file", str(path)])
    assert code == 1


def test_quoted_and_comment_lines_are_parsed(tmp_path: Path) -> None:
    content = (
        '# комментарий\nAPP_PROFILE="demo"\n\n'
        "SIGNATURE_SERVER_SECRET='Really-Strong-Value-2026'\n"
    )
    path = _write(tmp_path, content)
    values = preflight.parse_env_file(path)
    assert values["APP_PROFILE"] == "demo"
    assert values["SIGNATURE_SERVER_SECRET"] == "Really-Strong-Value-2026"
