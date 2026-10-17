"""Общие правила проверки секретов: демо-значения и минимальная длина.

Раньше список демо-значений и порог длины жили только внутри
`core.config._reject_demo_secrets_in_prod` и видели лишь те секреты, что
знает `Settings` (поля приложения). `app.core.preflight` (B6-8) проверяет
весь `.env`-файл целиком, включая переменные внешних контейнеров
(`POSTGRES_PASSWORD`, `KEYCLOAK_ADMIN_PASSWORD`, пароль Grafana), которые
`Settings` не читает вовсе — поэтому правила вынесены сюда и используются
в обоих местах одним кодом.
"""

from __future__ import annotations

# Демо-значения из .env.example / docker-compose / realm-crm.json. В prod их
# наличие означает, что оператор забыл сменить секреты: приложение обязано не
# стартовать, а не работать с публично известными ключами.
DEMO_SECRET_VALUES: frozenset[str] = frozenset(
    {
        "change-me-in-prod",
        "crm-bff-secret",
        "crm-admin-secret",
        "crm_access",
        "crm_secret_key",
        "crm_sign_access",
        "crm_sign_secret_key",
        "crm_app",
        "crm",
        "admin",
        "secret",
        "password",
    }
)

MIN_PROD_SECRET_LENGTH = 16


def check_secrets(mapping: dict[str, str]) -> list[str]:
    """Возвращает список проблем вида `"ИМЯ: демо-значение"`/`"ИМЯ: слишком короткий"`.

    Пустые значения не считаются проблемой здесь — обязательность конкретного секрета
    решает вызывающий код (`Settings` знает, какие поля обязательны в prod; `preflight`
    проверяет файл целиком и пропускает переменные, которых в нём нет). Значения в
    сообщении никогда не печатаются — только имена переменных.
    """
    problems: list[str] = []
    for name, value in mapping.items():
        if not value:
            continue
        if value in DEMO_SECRET_VALUES:
            problems.append(f"{name}: демо-значение")
        elif len(value) < MIN_PROD_SECRET_LENGTH:
            problems.append(f"{name}: слишком короткий")
    return problems
