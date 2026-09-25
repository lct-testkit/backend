# Сборка образа api и worker.
#
# Никаких системных пакетов: asyncpg и cryptography поставляются готовыми
# manylinux-колёсами, а libpq не нужен, потому что psycopg не используется.
# Благодаря этому сборка не зависит от зеркал Debian, а рантайм не требует
# доступа в интернет — условие развёртывания в закрытом контуре.
#
# Воспроизводимость и цепочка поставки:
#   * базовый образ закреплён по digest (обновляет dependabot);
#   * зависимости ставятся ТОЛЬКО из requirements.lock с проверкой хэшей
#     (`pip install --require-hashes`), транзитивные версии не «плавают»;
#   * multi-stage: в рантайм-образ едет готовый venv, без pip/setuptools/wheel
#     (это и есть основной источник CVE, которые раньше приходилось глушить
#     в .trivyignore).
#
# Обновление lock-файлов: tools/lock.sh

# ---- builder -----------------------------------------------------------------------------------------------
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=15

WORKDIR /build

# venv без pip: рантайму он не нужен, а ставить в него будем внешним pip.
RUN python -m venv --without-pip /opt/venv

# Отдельный слой зависимостей: пересобирается только при изменении lock-файла.
COPY requirements.lock ./
RUN pip --python /opt/venv/bin/python install --require-hashes -r requirements.lock

# ---- runtime -----------------------------------------------------------------------------------------------
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS runtime

LABEL org.opencontainers.image.source="https://github.com/lct-testkit/backend" \
      org.opencontainers.image.title="rtk-crm-api" \
      org.opencontainers.image.description="CRM ИТ Школы Ростелекома — API и worker"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}"

# В рантайме pip/setuptools/wheel не нужны: меньше поверхность атаки и
# меньше CVE в скане образа.
RUN python -m pip uninstall -y pip setuptools wheel

# Непривилегированный пользователь: контейнер не должен работать под root.
RUN useradd --create-home --uid 10001 crm

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv/app
COPY --chown=crm:crm alembic.ini ./
COPY --chown=crm:crm migrations ./migrations
COPY --chown=crm:crm app ./app
COPY --chown=crm:crm --chmod=0755 deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
USER crm

EXPOSE 8000

# Проверка через стандартную библиотеку, чтобы не тянуть curl из apt.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
