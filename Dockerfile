# Сборка образа api и worker.
#
# Никаких системных пакетов: asyncpg и cryptography поставляются готовыми
# manylinux-колёсами, а libpq не нужен, потому что psycopg не используется.
# Благодаря этому сборка не зависит от зеркал Debian, а рантайм не требует
# доступа в интернет — условие развёртывания в закрытом контуре.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=15

WORKDIR /srv/app

COPY pyproject.toml ./
# Отдельный слой зависимостей: пересобирается только при изменении pyproject.
RUN pip install --upgrade pip setuptools wheel \
    && pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Непривилегированный пользователь: контейнер не должен работать под root.
RUN useradd --create-home --uid 10001 crm \
    && chown -R crm:crm /srv/app
USER crm

EXPOSE 8000

# Проверка через стандартную библиотеку, чтобы не тянуть curl из apt.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
