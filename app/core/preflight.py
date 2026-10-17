"""Предполётная проверка `.env`-файла перед запуском стенда (B6-8).

Не поднимает приложение и не открывает БД — только читает файл и прогоняет
`check_secrets` (`app.core.secret_rules`) по всем секретам, включая переменные внешних
контейнеров (`POSTGRES_PASSWORD`, `KEYCLOAK_ADMIN_PASSWORD`), которые `Settings` не читает
как отдельные поля вовсе: `Settings._reject_demo_secrets_in_prod` видит их только если они
случайно долетели до окружения процесса api, а `preflight` читает файл целиком, независимо
от того, что из него будет передано контейнеру api. Значения никогда не печатаются — только
имена переменных и характер проблемы.

Запуск:
    python -m app.core.preflight --env-file .env

Код возврата 1, если найдена хотя бы одна проблема; 0 — файл чист.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.core.secret_rules import check_secrets

# Секреты, которые приложение читает через `Settings` как собственные поля.
_SETTINGS_SECRET_VARS = (
    "SIGNATURE_SERVER_SECRET",
    "KEYCLOAK_CLIENT_SECRET",
    "KEYCLOAK_ADMIN_CLIENT_SECRET",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "CRM_APP_PASSWORD",
    "AUDIT_HMAC_KEY",
    "SETTINGS_ENCRYPTION_KEY",
    "FIELD_ENCRYPTION_KEY",
    "FIELD_ENCRYPTION_KEY_PREV",
    "HEALTH_DETAILS_TOKEN",
)

# Секреты внешних контейнеров: Postgres и Keycloak читают их сами, `Settings` эти переменные
# как отдельные поля не парсит — но раз они сидят в том же `.env`, демо-значение там означает
# то же самое, что и в остальных секретах.
_EXTERNAL_SECRET_VARS = (
    "POSTGRES_PASSWORD",
    "KEYCLOAK_ADMIN_PASSWORD",
    "CMS_WEBHOOK_SECRET",
)

# Обязательны только при APP_PROFILE=prod — их отсутствие само по себе проблема, а не только
# демо-значение (та же граница, что и в `Settings._reject_demo_secrets_in_prod`).
_MANDATORY_IN_PROD = ("AUDIT_HMAC_KEY", "SETTINGS_ENCRYPTION_KEY", "FIELD_ENCRYPTION_KEY")

# Переменные со строкой подключения: проверяется пароль внутри строки, не строка целиком.
_CONNECTION_URL_VARS = ("DATABASE_URL", "KC_DATABASE_URL", "MIGRATIONS_DATABASE_URL")


def parse_env_file(path: Path) -> dict[str, str]:
    """Простой разбор `KEY=VALUE`: без `export`, без подстановки `${...}` — как `docker
    compose --env-file` читает файл после того, как сам оператор его сгенерировал. Пустые
    строки и строки, начинающиеся с `#`, пропускаются."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _password_from_url(url: str) -> str | None:
    password = urlsplit(url).password
    return unquote(password) if password else None


def check_env_values(values: dict[str, str]) -> list[str]:
    """Возвращает список проблем (без значений секретов) для разобранного `.env`."""
    secrets: dict[str, str] = {}
    for name in (*_SETTINGS_SECRET_VARS, *_EXTERNAL_SECRET_VARS):
        value = values.get(name, "")
        if value:
            secrets[name] = value
    for url_name in _CONNECTION_URL_VARS:
        url = values.get(url_name, "")
        if url:
            password = _password_from_url(url)
            if password:
                secrets[f"{url_name} (пароль)"] = password

    problems = check_secrets(secrets)

    if values.get("APP_PROFILE") == "prod":
        for name in _MANDATORY_IN_PROD:
            if not values.get(name):
                problems.append(f"{name}: обязателен в prod, не задан")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка .env на демо-секреты перед prod")
    parser.add_argument("--env-file", required=True, type=Path, help="путь к .env-файлу")
    args = parser.parse_args(argv)

    env_path: Path = args.env_file
    if not env_path.exists():
        print(f"preflight: файл не найден: {env_path}", file=sys.stderr)
        return 1

    problems = check_env_values(parse_env_file(env_path))

    if problems:
        print("preflight: найдены проблемы с секретами (значения не печатаются):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("preflight: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
