#!/usr/bin/env bash
# Единая точка входа контейнера: api | worker | migrate | seed |
# sms-gateway-mock | shell
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
  sms-gateway-mock)
    # dop.md §13: мок внешнего SMS-провайдера, тот же образ, отдельная
    # команда (см. комментарий у `migrate`/`seed` ниже по духу).
    echo "запуск мок-шлюза SMS..."
    exec uvicorn app.mocks.sms_gateway:app \
      --host 0.0.0.0 \
      --port 8090 \
      --no-access-log
    ;;
  seed)
    # Демо-воронки b2b_university_v1 и b2c_individual_v1 (раздел 7), шаблоны
    # отчётов + пара строк демо-истории запусков, шаблоны уведомлений,
    # источники интеграций + служебная учётка INTEGRATION. Каждый сид
    # идемпотентен по коду/маркеру — повторный запуск на заполненной базе
    # ничего не меняет (docs/backend-issues.md #29: раньше засевались только
    # воронки, остальные три модуля запускались только руками).
    echo "заполнение демо-данных..."
    python -m app.modules.workflow.seed
    python -m app.modules.reporting.seed
    python -m app.modules.notification.seed
    python -m app.modules.integration.seed
    ;;
  shell)
    exec python
    ;;
  *)
    echo "неизвестный режим: ${MODE}" >&2
    echo "доступно: api | worker | migrate | seed | sms-gateway-mock | shell" >&2
    exit 1
    ;;
esac
