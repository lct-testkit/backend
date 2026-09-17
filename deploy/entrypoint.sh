#!/usr/bin/env bash
# Единая точка входа контейнера: api | worker | migrate | shell
set -euo pipefail

MODE="${1:-api}"

wait_for() {
  local name="$1" host="$2" port="$3" attempts="${4:-60}"
  echo "ожидание ${name} (${host}:${port})..."
  for _ in $(seq 1 "${attempts}"); do
    if (echo > "/dev/tcp/${host}/${port}") >/dev/null 2>&1; then
      echo "${name} доступен"
      return 0
    fi
    sleep 1
  done
  echo "${name} недоступен после ${attempts} попыток" >&2
  return 1
}

if [[ -n "${WAIT_FOR_POSTGRES:-}" ]]; then
  wait_for "postgres" "${WAIT_FOR_POSTGRES%%:*}" "${WAIT_FOR_POSTGRES##*:}"
fi
if [[ -n "${WAIT_FOR_REDIS:-}" ]]; then
  wait_for "redis" "${WAIT_FOR_REDIS%%:*}" "${WAIT_FOR_REDIS##*:}"
fi

case "${MODE}" in
  migrate)
    echo "применение миграций..."
    exec alembic upgrade head
    ;;
  api)
    # Миграции применяет отдельный сервис migrate, поэтому здесь только запуск.
    #
    # Один воркер на контейнер. prometheus_client держит реестр в памяти
    # процесса, поэтому при нескольких воркерах scrape попадает в случайный
    # из них и показывает лишь часть трафика. Масштабирование — репликами
    # контейнера, тогда каждая реплика остаётся отдельной целью Prometheus.
    exec uvicorn app.main:app \
      --host 0.0.0.0 \
      --port 8000 \
      --workers "${UVICORN_WORKERS:-1}" \
      --proxy-headers \
      --forwarded-allow-ips '*' \
      --no-access-log
    ;;
  worker)
    exec arq app.worker.main.WorkerSettings
    ;;
  seed)
    # Демо-воронки b2b_university_v1 и b2c_individual_v1 (раздел 7).
    # Идемпотентно: повторный запуск на заполненной базе ничего не меняет.
    echo "заполнение демо-воронок..."
    exec python -m app.modules.workflow.seed
    ;;
  shell)
    exec python
    ;;
  *)
    echo "неизвестный режим: ${MODE}" >&2
    echo "доступно: api | worker | migrate | seed | shell" >&2
    exit 1
    ;;
esac
