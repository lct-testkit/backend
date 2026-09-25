#!/usr/bin/env bash
# Нагрузочный прогон против уже поднятого стека (docker compose up -d --wait) с
# проверкой SLO из спеки: p95 ≤ 300 мс при 50 RPS для «перехода по статусу» и
# «добавления комментария».
#
#   bash loadtest/run_ci.sh
#
# Параметры (переменные окружения, значения по умолчанию — из критерия спеки):
#   LOADTEST_HOST          http://localhost:8080
#   LOADTEST_USERS         50           число виртуальных пользователей
#   LOADTEST_RPS_PER_USER  1            целевой RPS на пользователя (итого users × rps)
#   LOADTEST_DURATION      90s          длительность каждого сценария
#   LOADTEST_P95_MS        300          порог p95
#   LOADTEST_PASSWORD      Kam123456789!  пароль демо-КАМа из realm-crm.json
#
# Порядок важен (loadtest/README.md): provision.py запускается ПРЯМО ПЕРЕД
# прогоном transition — фоновый SLA-скан воркера трогает версии сделок, и
# «состарившиеся» сделки дают честные 409, не имеющие отношения к latency.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

HOST="${LOADTEST_HOST:-http://localhost:8080}"
USERS="${LOADTEST_USERS:-50}"
RPS_PER_USER="${LOADTEST_RPS_PER_USER:-1}"
DURATION="${LOADTEST_DURATION:-90s}"
P95_MS="${LOADTEST_P95_MS:-300}"
export LOADTEST_RPS_PER_USER="${RPS_PER_USER}"
export LOADTEST_KEYCLOAK_TOKEN_URL="${LOADTEST_KEYCLOAK_TOKEN_URL:-${HOST}/auth/realms/crm/protocol/openid-connect/token}"
export LOADTEST_PASSWORD="${LOADTEST_PASSWORD:-Kam123456789!}"

seconds="${DURATION%s}"
target_rps="$(awk -v u="${USERS}" -v r="${RPS_PER_USER}" 'BEGIN { printf "%d", u * r }')"
min_rps="$(awk -v t="${target_rps}" 'BEGIN { printf "%d", t * 0.9 }')"
# Сделок с запасом 30% на один проход (каждая сделка переводится ровно один раз).
deal_count="$(awk -v t="${target_rps}" -v s="${seconds}" 'BEGIN { printf "%d", t * s * 1.3 }')"
min_requests="$(awk -v t="${target_rps}" -v s="${seconds}" 'BEGIN { printf "%d", t * s * 0.5 }')"

mkdir -p loadtest/results
rc=0

# Демо-КАМ появляется в БД при первом входе (just-in-time provisioning) — без него provision.py
# не найдёт владельца сделок. Делаем один вход тем же password-grant, что и Locust.
echo "== первый вход демо-КАМа (JIT-провижининг пользователя)"
token="$(curl -fsS \
  -d grant_type=password \
  -d client_id="${LOADTEST_CLIENT_ID:-crm-bff}" \
  -d client_secret="${LOADTEST_CLIENT_SECRET:-crm-bff-secret}" \
  -d username="${LOADTEST_USERNAME:-kam.ivanov}" \
  --data-urlencode password="${LOADTEST_PASSWORD}" \
  "${LOADTEST_KEYCLOAK_TOKEN_URL}" \
  | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")" \
  || { echo "не удалось получить токен Keycloak" >&2; exit 1; }
curl -fsS -o /dev/null -H "Authorization: Bearer ${token}" "${HOST}/api/me" || { echo "GET /api/me не прошёл — пользователь не создан" >&2; exit 1; }

# Пока не принята действующая политика ПДн, мутирующие ручки отвечают 403 (CRM-1105) — иначе весь прогон
# состоял бы из отказов. Принимаем согласие тем же способом, что и UI (версия и хэш — из /api/me/policy).
policy="$(curl -fsS -H "Authorization: Bearer ${token}" "${HOST}/api/me/policy")"
consent_body="$(printf '%s' "${policy}" | python -c "
import json, sys
p = json.load(sys.stdin)
print(json.dumps({'policy_version': p['version'], 'policy_text_hash': p.get('text_hash') or '0' * 64}))")"
curl -fsS -o /dev/null -X POST -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
  -d "${consent_body}" "${HOST}/api/me/consent" \
  || echo "::warning::принять согласие не удалось (возможно, уже принято) — продолжаем"

echo "== провижининг: ${deal_count} сделок"
python loadtest/provision.py --count "${deal_count}" --comment-pool-size 100 || exit 1

for scenario in transition comment; do
  echo "== ${scenario}: ${USERS} пользователей × ${RPS_PER_USER} RPS, ${DURATION}"
  locust -f "loadtest/locustfile_${scenario}.py" --headless \
    -u "${USERS}" -r "${USERS}" -t "${DURATION}" --host "${HOST}" \
    --csv "loadtest/results/${scenario}" --html "loadtest/results/${scenario}.html" || rc=1
  python loadtest/assert_slo.py "loadtest/results/${scenario}_stats.csv" \
    --p95-ms "${P95_MS}" --max-fail-pct 1 --min-rps "${min_rps}" --min-requests "${min_requests}" || rc=1
done

exit "${rc}"
