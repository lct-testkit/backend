#!/usr/bin/env bash
# B6-13: обезличенный дамп для dev/тестовых стендов (паспорт проекта,
# .github/docs/new_spec.md:607 — «на тестовых стендах реальные ПДн не появляются никогда»).
#
#   bash tools/anonymized_dump.sh --backup ./backups/rtk-crm-20261018.dump --out ./crm-anon.dump
#
# Шаги: временная база crm_anon_<дата+pid> -> pg_restore боевого бэкапа в неё ->
# tools/anonymize.sql с одноразовой солью -> tools/pii_scan.py по тексту результата ->
# pg_dump -Fc -> SHA256SUMS -> временная база удаляется ВСЕГДА (trap, в т.ч. при ошибке).
# Запускать может только тот, кто уже имеет доступ к исходному боевому бэкапу — сам скрипт
# доступа не даёт и не проверяет, откуда взят файл `--backup`.
#
# Нужны в PATH: psql, pg_restore, pg_dump (пакет postgresql-client той же старшей версии,
# что сервер) и python3 (для tools/pii_scan.py). Подключение — стандартные переменные libpq
# (PGHOST/PGPORT/PGUSER/PGPASSWORD или ~/.pgpass) либо флаги ниже; соединяется с СУЩЕСТВУЮЩИМ
# сервером Postgres как с суперпользователем (`CREATE DATABASE`/`DROP DATABASE`) — это должен
# быть НЕ боевой сервер, а временный/dev-кластер с местом под восстановленный бэкап.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Использование:
  anonymized_dump.sh --backup FILE [--out FILE] [--salt STRING] [--host H] [--port P]
                      [--user U] [--link-demo-accounts] [--encrypt-with-dev-key]

  --backup FILE             pg_dump -Fc боевой базы crm (обязателен).
  --out FILE                куда положить результат (по умолчанию ./crm-anon-YYYYMMDD.dump).
  --salt STRING              соль для anonymize.sql (по умолчанию случайная, НЕ сохраняется
                              никуда кроме как в это значение переменной — не печатается,
                              не пишется в файл).
  --host/--port/--user       подключение к серверу для временной базы (по умолчанию
                              localhost/5432/postgres — переопределяется PGHOST/PGPORT/PGUSER).
  --link-demo-accounts        переименовать по одному самому раннему пользователю каждой
                              роли (KAM/HEAD/ADMIN/AUDITOR) в email демо-учётки из
                              deploy/keycloak/realm-crm.json, чтобы входить под ними сразу
                              после восстановления дампа (вход привязывает keycloak_id по
                              email при первом логине).
  --encrypt-with-dev-key      не реализован: профиль учащегося всегда остаётся в дампе обычными
                              значениями (они уже вымышленные после anonymize.sql). Флаг
                              принимается, но сразу завершает скрипт с кодом 2 и пояснением —
                              молча проигнорировать его нельзя: создалось бы ложное ощущение,
                              что шифрование включено.
EOF
}

backup=""
out=""
salt=""
host="${PGHOST:-localhost}"
port="${PGPORT:-5432}"
user="${PGUSER:-postgres}"
link_demo_accounts=0
encrypt_with_dev_key=0

while [ $# -gt 0 ]; do
    case "$1" in
        --backup) backup="$2"; shift 2 ;;
        --out) out="$2"; shift 2 ;;
        --salt) salt="$2"; shift 2 ;;
        --host) host="$2"; shift 2 ;;
        --port) port="$2"; shift 2 ;;
        --user) user="$2"; shift 2 ;;
        --link-demo-accounts) link_demo_accounts=1; shift ;;
        --encrypt-with-dev-key) encrypt_with_dev_key=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "anonymized_dump.sh: неизвестный аргумент: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$encrypt_with_dev_key" -eq 1 ]; then
    echo "anonymized_dump.sh: --encrypt-with-dev-key не реализован — дамп НЕ создан." >&2
    echo "  Профиль учащегося остаётся в дампе обычными значениями (после anonymize.sql они" >&2
    echo "  вымышленные). Повторите запуск без этого флага." >&2
    exit 2
fi
if [ -z "$backup" ]; then
    echo "anonymized_dump.sh: --backup обязателен" >&2
    usage >&2
    exit 2
fi
if [ ! -r "$backup" ]; then
    echo "anonymized_dump.sh: не удаётся прочитать бэкап: $backup" >&2
    exit 2
fi
if [ -z "$out" ]; then
    out="./crm-anon-$(date +%Y%m%d).dump"
fi
if [ -z "$salt" ]; then
    salt="$(openssl rand -hex 16)"
fi

PSQL=(psql -h "$host" -p "$port" -U "$user" -v ON_ERROR_STOP=1)
tmp_db="crm_anon_$(date +%Y%m%d)_$$"

cleanup() {
    local status=$?
    # В trap — всегда, даже при ошибке посередине восстановления/обезличивания/скана: во
    # временной базе лежат реальные ПДн из боевого бэкапа до самого шага anonymize.sql.
    "${PSQL[@]}" -d postgres -c "DROP DATABASE IF EXISTS ${tmp_db};" >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT

echo "anonymized_dump.sh: временная база ${tmp_db}" >&2
"${PSQL[@]}" -d postgres -c "CREATE DATABASE ${tmp_db} OWNER \"${user}\";"

echo "anonymized_dump.sh: восстановление ${backup}" >&2
pg_restore -h "$host" -p "$port" -U "$user" -d "$tmp_db" --no-owner --no-privileges "$backup"

echo "anonymized_dump.sh: обезличивание" >&2
"${PSQL[@]}" -d "$tmp_db" -v salt="$salt" -f "${SCRIPT_DIR}/anonymize.sql"

echo "anonymized_dump.sh: pii_scan" >&2
scan_status=0
# Текст для скана — `pg_dump` в простом SQL-формате по уже обезличенной временной базе (COPY-блоки
# со всеми значениями). Не `pg_restore -f -`: ему нужен файл архива, а не база, и вместе с `-d` он
# отказывается работать ("options -d/--dbname and -f/--file cannot be used together") — скан
# получал бы пустой ввод.
pg_dump -h "$host" -p "$port" -U "$user" -d "$tmp_db" --no-owner --no-privileges \
    | python3 "${SCRIPT_DIR}/pii_scan.py" || scan_status=$?
if [ "$scan_status" -eq 3 ]; then
    echo "anonymized_dump.sh: pii_scan нашёл возможные ПДн — дамп НЕ сохранён" >&2
    exit 3
elif [ "$scan_status" -ne 0 ]; then
    echo "anonymized_dump.sh: скан не удалось выполнить (код ${scan_status}) — дамп НЕ сохранён" >&2
    exit "$scan_status"
fi

# После скана: демо-email из realm-crm.json — публичные учебные адреса, не ПДн, но по формату они
# не `@example.invalid` и скан их бы отверг.
if [ "$link_demo_accounts" -eq 1 ]; then
    echo "anonymized_dump.sh: привязка демо-учёток (deploy/keycloak/realm-crm.json)" >&2
    "${PSQL[@]}" -d "$tmp_db" -f "${SCRIPT_DIR}/link_demo_accounts.sql"
fi

echo "anonymized_dump.sh: выгрузка в ${out}" >&2
pg_dump -h "$host" -p "$port" -U "$user" -d "$tmp_db" -Fc --no-owner --no-privileges -f "$out"
chmod 600 "$out"
(cd "$(dirname "$out")" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256") \
    || shasum -a 256 "$out" > "${out}.sha256"

echo "anonymized_dump.sh: готово — ${out} (600, SHA256SUMS: ${out}.sha256)"
