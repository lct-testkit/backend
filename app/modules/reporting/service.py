"""Сервисный слой отчётности и дашбордов (new_spec §4.13, §7.9).

`ReportJobService.create` реализует буквально раздел 4.13: «Синхронно отдаём
только "лёгкие" отчёты (< 1 000 строк, < 2 с). Тяжёлые: `POST /reports` →
`report_job` в очередь → поллинг статуса → готовый файл в S3». Решение
принимается по `REPORT_ESTIMATORS` (см. `reporting.builders`) — если для
вида отчёта нет оценщика, он структурно лёгкий (агрегат на несколько
десятков строк) и генерируется сразу же, в рамках текущей транзакции
запроса.

`generate()` не перехватывает исключения сама: обработка ошибок специфична
контексту вызова.
  * `create()` (sync-путь) — исключение долетает до обычного обработчика
    ошибок FastAPI (422/500), вся транзакция запроса откатывается вместе со
    строкой `report_jobs` — от невалидных параметров не остаётся осиротевшей
    `queued`-записи.
  * `reporting.tasks.sweep_report_jobs` (async-путь, воркер) — там нет
    HTTP-слоя, которому можно вернуть ошибку, поэтому сама задача ловит
    исключение и переводит job в `failed` через `mark_failed`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import threading
import uuid
from typing import Any

from sqlalchemy import Select, any_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.config import get_settings
from app.core.errors import (
    AppError,
    ErrorCode,
    FieldError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from app.core.ids import uuid7
from app.core.queue import enqueue_after_commit
from app.core.security import Principal
from app.core.storage import ensure_bucket, generate_presigned_get, upload_object_bytes
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.files.models import Attachment, AttachmentCategory, File, FileStatus
from app.modules.reporting.builders import (
    FILE_ONLY_REPORTS,
    REPORT_BUILDERS,
    REPORT_ESTIMATORS,
    ReportDataset,
    resolve_user_timezone_name,
)
from app.modules.reporting.models import (
    Dashboard,
    DashboardWidget,
    ReportFormat,
    ReportJob,
    ReportJobStatus,
    ReportTemplate,
)
from app.modules.reporting.rendering import (
    CONTENT_TYPES,
    FILE_EXTENSIONS,
    render_report,
    report_file_name,
)
from app.modules.reporting.rendering_xls import XLS_MAX_ROWS, ReportTooLargeError
from app.modules.reporting.schemas import ReportColumnOut, ReportTemplateOut
from app.modules.reporting.spec import REPORT_SPECS, derived_data_categories, validate_params

#: Раздел 4.13: «< 1 000 строк, < 2 с» — граница между sync- и async-путём.
SYNC_ROW_THRESHOLD = 1000

#: Предпросмотр числа строк зовётся формой запуска при каждой правке параметров (с задержкой):
#: потолок выше, чем у синхронных отчётов (30/мин), но скрипт в цикле всё равно упрётся в 429.
PREVIEW_RATE_LIMIT_PER_MIN = 120

# new_spec §4.13: «каждая генерация — событие REPORT_EXPORTED в аудите с указанием количества
# строк и категорий данных». Код отчёта, которого нет в словаре, трактуется как содержащий ПДн
# (безопасный отказ, а не молчаливое «пусто») — см. `assert` в `reporting.spec`, который не даёт
# такому случаю произойти молча для зарегистрированных builder'ов.
#
# B1-2/B1-12: производный от `ReportSpec.data_categories` (`reporting.spec`) — раньше это был
# самостоятельный словарь без `deal_register` (он уходил в «не классифицировано»); теперь
# источник один, расхождения между каталогом вида отчёта и аудитом структурно невозможны.
REPORT_DATA_CATEGORIES: dict[str, list[str]] = derived_data_categories()


def _reject_null_fields(data: dict[str, Any]) -> None:
    """Все поля дашборда и виджета в БД NOT NULL, а схемы PATCH допускают `null` (он означает
    «не менять» только для не переданного поля). Переданный `null` доходил до UPDATE и давал
    ошибку БД, то есть 500; теперь — 422 с перечнем полей."""
    nulls = [key for key, value in data.items() if value is None]
    if nulls:
        raise ValidationError(
            "Это поле нельзя очистить",
            [FieldError(field=key, reason="Нельзя оставить пустым") for key in nulls],
        )


def _report_data_categories(template_code: str) -> list[str]:
    return REPORT_DATA_CATEGORIES.get(
        template_code, ["не классифицировано — считать содержащим ПДн"]
    )


# matplotlib (pyplot держит глобальное состояние), openpyxl и reportlab не рассчитаны на
# параллельные сборки из разных потоков. Цикл событий они не блокируют (`to_thread`), но между
# собой идут по одной.
_RENDER_LOCK = threading.Lock()


def _render_serialized(
    dataset: ReportDataset,
    *,
    format: str,
    template_code: str,
    job_id: uuid.UUID | None = None,
    params: dict[str, Any] | None = None,
    tz_name: str | None = None,
) -> bytes:
    with _RENDER_LOCK:
        return render_report(
            dataset,
            format=format,
            template_code=template_code,
            job_id=job_id,
            params=params,
            tz_name=tz_name,
        )


def report_template_out(template: ReportTemplate) -> ReportTemplateOut:
    """B1-2/B1-3: `columns`/`params`/`date_fields`/`default_date_field` не хранятся в БД — они
    вычисляются из `REPORT_SPECS` по `query_def.kind` при каждом ответе, тем же приёмом, что уже
    используют производные `CHART_KINDS`/`REPORT_DATA_CATEGORIES` этого модуля. Код без описания
    в `REPORT_SPECS` (не бывает для активного шаблона — см. `assert` в `reporting.spec`, но
    запись в БД теоретически может опередить код при откате миграции/деплоя) отдаёт пустые
    списки, а не падает 500."""
    kind = template.query_def.get("kind", template.code)
    spec = REPORT_SPECS.get(kind)
    if spec is None:
        columns: list[ReportColumnOut] = []
        params: list[str] = []
        date_fields: list[str] = []
        default_date_field = None
    else:
        columns = [
            ReportColumnOut(key=c.key, title=c.title, default=c.default) for c in spec.columns
        ]
        allowed = set(spec.common_filters) | set(spec.params) | {"limit"}
        if spec.columns:
            allowed.add("columns")
        if spec.date_fields:
            allowed.add("date_field")
        params = sorted(allowed)
        date_fields = list(spec.date_fields)
        default_date_field = spec.default_date_field
    return ReportTemplateOut(
        id=template.id,
        code=template.code,
        name=template.name,
        description=template.description,
        allowed_roles=template.allowed_roles,
        default_params=template.default_params,
        output_formats=template.output_formats,  # type: ignore[arg-type]
        is_active=template.is_active,
        columns=columns,
        params=params,
        date_fields=date_fields,
        default_date_field=default_date_field,
    )


class ReportTemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def list_query(self, principal: Principal) -> Select[tuple[ReportTemplate]]:
        stmt = select(ReportTemplate).where(ReportTemplate.is_active.is_(True))
        if not principal.is_admin:
            # Пустой allowed_roles = доступен всем с правом report:read (тот
            # же принцип, что `workflow_transitions.allowed_roles` уже
            # использует: пустой список — не «никому», а «без ограничения»).
            stmt = stmt.where(
                or_(
                    func.cardinality(ReportTemplate.allowed_roles) == 0,
                    # `:role = ANY(allowed_roles)`: то же, что `ARRAY.any()`, но без обращения
                    # к relationship-компаратору, который mypy подставляет вместо ARRAY.
                    literal(principal.role) == any_(ReportTemplate.allowed_roles),
                )
            )
        return stmt.order_by(ReportTemplate.code)

    async def get_or_404(self, code: str) -> ReportTemplate:
        template = await self._session.scalar(
            select(ReportTemplate).where(ReportTemplate.code == code)
        )
        if template is None:
            raise NotFoundError("Шаблон отчёта", code)
        return template


class ReportJobService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def get_or_404(self, job_id: uuid.UUID) -> ReportJob:
        job = await self._session.get(ReportJob, job_id)
        if job is None:
            raise NotFoundError("Задача отчёта", job_id)
        return job

    def ensure_read_access(self, job: ReportJob, principal: Principal) -> None:
        # Раздел 4.13: отчёт уже отражает скоуп того, кто его запросил
        # (`deal_scope_clause` применён в момент генерации) — второй,
        # более широкой роли на чтение чужого готового отчёта не нужно,
        # HEAD строит свой собственный отчёт того же вида при необходимости.
        if job.requested_by != principal.user_id and not principal.is_admin:
            raise ForbiddenError(
                "Отчёт доступен только тому, кто его запросил, либо администратору"
            )

    def list_query(
        self, principal: Principal, *, template_code: str | None = None
    ) -> Select[tuple[ReportJob]]:
        stmt = select(ReportJob)
        if not principal.is_admin:
            stmt = stmt.where(ReportJob.requested_by == principal.user_id)
        if template_code:
            stmt = stmt.where(ReportJob.template_code == template_code)
        return stmt.order_by(ReportJob.created_at.desc(), ReportJob.id.desc())

    def _ensure_role_allowed(self, template: ReportTemplate, principal: Principal) -> None:
        if principal.is_admin:
            return
        if template.allowed_roles and principal.role not in template.allowed_roles:
            raise ForbiddenError(f"Отчёт {template.code!r} недоступен вашей роли")

    async def create(self, principal: Principal, payload: Any) -> ReportJob:
        template = await ReportTemplateService(self._session).get_or_404(payload.template_code)
        if not template.is_active:
            raise NotFoundError("Шаблон отчёта", payload.template_code)
        self._ensure_role_allowed(template, principal)
        if payload.format not in template.output_formats:
            raise ValidationError(
                f"Формат {payload.format!r} недоступен для отчёта {template.code!r}",
                [
                    FieldError(
                        field="format",
                        reason=f"Допустимо: {', '.join(template.output_formats)}",
                    )
                ],
            )
        kind = template.query_def.get("kind", template.code)
        if kind not in REPORT_BUILDERS:
            raise AppError(ErrorCode.VALIDATION, f"Неизвестный вид отчёта: {kind!r}")

        # B8-11: личный лимит (`reports_max_per_user`, раньше делил имя с `reports_max_concurrent`
        # — одновременными рендерами воркера) не должен вытеснять отчёты остальных сотрудников.
        settings = get_settings()
        limit = settings.reports_max_per_user
        unfinished = await self._session.scalar(
            select(func.count(ReportJob.id)).where(
                ReportJob.requested_by == principal.user_id,
                ReportJob.status.in_(
                    [ReportJobStatus.QUEUED.value, ReportJobStatus.PROCESSING.value]
                ),
            )
        )
        unfinished = unfinished or 0
        if unfinished >= limit:
            raise AppError(
                ErrorCode.REPORTS_LIMIT_EXCEEDED,
                f"Не завершено отчётов: {unfinished} (лимит {limit}). Дождитесь готовности "
                "запущенных и повторите",
                headers={"Retry-After": "10"},
                extra={"limit": limit, "unfinished": unfinished},
            )

        # Общая очередь на всех: без потолка один сотрудник не вытесняет остальных лично, но
        # сто пользователей по десять тяжёлых отчётов разом всё равно забили бы воркер надолго.
        queued_total = await self._session.scalar(
            select(func.count(ReportJob.id)).where(ReportJob.status == ReportJobStatus.QUEUED.value)
        )
        if (queued_total or 0) >= settings.reports_queue_max:
            raise AppError(
                ErrorCode.REPORTS_LIMIT_EXCEEDED,
                f"Очередь отчётов переполнена: {queued_total} из {settings.reports_queue_max}. "
                "Повторите позже",
                headers={"Retry-After": "30"},
                extra={"limit": settings.reports_queue_max, "queued": queued_total},
            )

        params = {**template.default_params, **(payload.params or {})}
        # B1-2: строгая проверка ДО создания записи `report_jobs` — так некорректный запрос
        # (неизвестный параметр, недопустимая колонка, `date_field` вне каталога) не оставляет
        # осиротевшее задание: вся транзакция откатывается обычным обработчиком ошибок FastAPI.
        # Код без описания (гипотетический рассинхрон сида и `REPORT_SPECS`) не проверяем — это
        # уже отловлено выше через `kind not in REPORT_BUILDERS`, а `REPORT_SPECS` описывает
        # ровно то же множество кодов (`assert` в `reporting.spec`).
        spec = REPORT_SPECS.get(kind)
        if spec is not None:
            validate_params(spec, params)

        job = ReportJob(
            template_code=template.code,
            params=params,
            format=payload.format,
            requested_by=principal.user_id,
            status=ReportJobStatus.QUEUED.value,
        )
        self._session.add(job)
        await self._session.flush()

        estimator = REPORT_ESTIMATORS.get(kind)
        is_light = True
        if estimator is not None:
            row_estimate = await estimator(self._session, principal, params)
            is_light = row_estimate <= SYNC_ROW_THRESHOLD
            # B1-4: лимит xls (65 535 строк, BIFF8) проверяется заранее по оценке — тот же приём,
            # что уже разделяет sync/async по `SYNC_ROW_THRESHOLD`. Невалидный запрос не доходит
            # до рендера (который не раньше бы, а глубже внутри `render_xls`, поднял бы то же
            # исключение — `ReportTooLargeError` ниже перехватывает именно этот случай для
            # видов без оценщика, у которых фактическое число строк оценке не подвергалось).
            if payload.format == ReportFormat.XLS.value and row_estimate > XLS_MAX_ROWS:
                raise ValidationError(
                    f"Формат xls вмещает не более {XLS_MAX_ROWS} строк: выберите xlsx или "
                    "сузьте период",
                    [
                        FieldError(
                            field="format",
                            reason=f"Оценка строк ({row_estimate}) превышает лимит xls",
                        )
                    ],
                )

        if is_light:
            # Лёгкий отчёт строится прямо в запросе и в лимит выше не попадает: без своего
            # ограничения один пользователь скриптом (цикл `POST /reports` без параметров)
            # занимал бы `_RENDER_LOCK` на всех репликах подряд.
            await rate_limit.enforce(
                str(principal.user_id), "report:sync", limit=30, window_seconds=60
            )
            try:
                await self.generate(job, principal)
            except ReportTooLargeError as exc:
                # Оценка (если она вообще была) могла устареть между `estimator(...)` выше и
                # самим рендером, либо вид отчёта без оценщика на практике дал больше строк,
                # чем предполагалось структурно — 422, а не 500 неперехваченного исключения.
                raise ValidationError(
                    str(exc), [FieldError(field="format", reason=str(exc))]
                ) from exc
        elif settings.reports_enqueue:
            # B8-11: не ждать минутного тика `sweep_report_jobs` — поставить задание в arq
            # сразу после коммита (до коммита воркер не нашёл бы строку `queued` в БД).
            enqueue_after_commit(
                self._session, "render_report_job", str(job.id), job_id=f"report:{job.id}"
            )
        # Иначе (REPORTS_ENQUEUE=false) остаётся `queued` — заберёт cron-добор как раньше.
        return job

    async def preview(self, principal: Principal, payload: Any) -> tuple[int, bool]:
        """Сколько строк даст отчёт с этими параметрами — без задания, файла и аудита.

        Форма запуска показывает число до нажатия «Запустить», чтобы пустой отчёт был виден
        сразу. Проверки те же, что у `create` (шаблон, роль, параметры); лёгкий отчёт
        считается тем же построителем, тяжёлый (оценка выше `SYNC_ROW_THRESHOLD`) — оценкой.
        """
        template = await ReportTemplateService(self._session).get_or_404(payload.template_code)
        if not template.is_active:
            raise NotFoundError("Шаблон отчёта", payload.template_code)
        self._ensure_role_allowed(template, principal)
        kind = template.query_def.get("kind", template.code)
        if kind not in REPORT_BUILDERS:
            raise AppError(ErrorCode.VALIDATION, f"Неизвестный вид отчёта: {kind!r}")
        params = {**template.default_params, **(payload.params or {})}
        spec = REPORT_SPECS.get(kind)
        if spec is not None:
            validate_params(spec, params)
        await rate_limit.enforce(
            str(principal.user_id),
            "report:preview",
            limit=PREVIEW_RATE_LIMIT_PER_MIN,
            window_seconds=60,
        )
        estimator = REPORT_ESTIMATORS.get(kind)
        if estimator is not None:
            estimate = await estimator(self._session, principal, params)
            if estimate > SYNC_ROW_THRESHOLD:
                return estimate, False
        dataset = await REPORT_BUILDERS[kind](self._session, principal, params)
        total = dataset.total_rows if dataset.total_rows is not None else len(dataset.rows)
        return total, True

    async def generate(self, job: ReportJob, principal: Principal) -> None:
        template = await ReportTemplateService(self._session).get_or_404(job.template_code)
        kind = template.query_def.get("kind", template.code)
        builder = REPORT_BUILDERS[kind]

        job.status = ReportJobStatus.PROCESSING.value
        await self._session.flush()

        dataset = await builder(self._session, principal, job.params)
        # B1-12: пояс нужен не только конверту json (B1-5) — он же идёт в `result_meta.timezone`
        # для ЛЮБОГО формата (снимок момента генерации), поэтому резолвится всегда, а не только
        # при `format == json`, как было в черновике B1-4/B1-5.
        tz_name = await resolve_user_timezone_name(self._session, principal)
        # Рендер (openpyxl, reportlab, matplotlib) — секунды CPU: в потоке, а не в цикле событий,
        # иначе на это время замирали бы все остальные запросы воркера.
        content = await asyncio.to_thread(
            _render_serialized,
            dataset,
            format=job.format,
            template_code=kind,
            job_id=job.id,
            params=job.params,
            tz_name=tz_name,
        )

        settings = get_settings()
        bucket = settings.s3_bucket_reports
        await ensure_bucket(bucket)
        file_id = uuid7()
        # B1-4: расширение файла через `FILE_EXTENSIONS`, не напрямую `job.format` — у всех
        # сегодняшних форматов они совпадают буквально, но явный словарь (`reporting.rendering`)
        # не даёт этому молча разойтись, если когда-нибудь появится формат с другим расширением.
        extension = FILE_EXTENSIONS[job.format]
        storage_key = f"{job.id}/{file_id}.{extension}"
        await upload_object_bytes(
            bucket=bucket,
            key=storage_key,
            body=content,
            content_type=CONTENT_TYPES[job.format],
        )

        file = File(
            id=file_id,
            storage_key=storage_key,
            bucket=bucket,
            # B1-6: `{название}_{ГГГГ-ММ-ДД}.{расширение}`, не просто `{dataset.title}.{extension}`
            # — повторная выгрузка одного и того же вида отчёта в другой день давала то же имя
            # файла, браузер различал копии только припиской `(1)`/`(2)`.
            original_filename=report_file_name(dataset.title, dataset.generated_at, extension),
            mime_type=CONTENT_TYPES[job.format],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            status=FileStatus.READY.value,
            uploaded_by=job.requested_by,
            refcount=1,
        )
        self._session.add(file)
        # Отдельный flush перед Attachment: File/Attachment не связаны ORM
        # relationship (полиморфная связь по entity_type/entity_id) — тот же
        # найденный вживую в спринте 5 баг гонки flush-порядка, см.
        # imports.service._write_error_report.
        await self._session.flush()
        self._session.add(
            Attachment(
                file_id=file.id,
                entity_type="report_job",
                entity_id=job.id,
                category=AttachmentCategory.REPORT.value,
                uploaded_by=job.requested_by,
            )
        )

        job.status = ReportJobStatus.COMPLETED.value
        # Генерация идёт одной транзакцией: промежуточных значений клиент не увидит,
        # только 0 до готовности и 100 после.
        job.progress_pct = 100
        job.file_id = file.id
        job.row_count = len(dataset.rows)
        job.finished_at = dt.datetime.now(dt.UTC)
        job.expires_at = job.finished_at + dt.timedelta(days=settings.reports_retention_days)
        # B1-12: снимок момента генерации — то, что не восстановить заново по одному `params`,
        # если данные успели измениться (заголовки колонок — в порядке файла; `total_rows`/
        # `truncated` — B1-6/B1-7, `None` у builder'ов без оценки означает «усечения не было»;
        # `data_as_of` сериализован строкой — JSONB-колонки этой кодовой базы пишутся обычным
        # `json.dumps` без кастомного энкодера, см. `crm.service._json_safe`).
        job.result_meta = {
            "title": dataset.title,
            "columns": list(dataset.columns),
            "total_rows": (
                dataset.total_rows if dataset.total_rows is not None else len(dataset.rows)
            ),
            "truncated": dataset.truncated,
            "data_as_of": (
                dataset.data_as_of.isoformat() if dataset.data_as_of is not None else None
            ),
            "timezone": tz_name,
        }
        await self._session.flush()

        await self._audit.record(
            AuditAction.REPORT_EXPORTED,
            entity_type="report_job",
            entity_id=job.id,
            changes={
                "template_code": {"old": None, "new": job.template_code},
                "format": {"old": None, "new": job.format},
                "row_count": {"old": None, "new": job.row_count},
                "data_categories": {
                    "old": None,
                    "new": _report_data_categories(job.template_code),
                },
            },
        )

    async def mark_failed(self, job: ReportJob, error: str) -> None:
        """`error` попадает в `ReportJobOut.error`, то есть к клиенту: только обобщённый текст, без
        текста исключения (в нём бывают SQL, пути и данные). Подробности воркер пишет в лог по
        коду ошибки, который здесь входит в сообщение."""
        job.status = ReportJobStatus.FAILED.value
        job.error = error[:2000]
        job.finished_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.REPORT_FAILED,
            entity_type="report_job",
            entity_id=job.id,
            changes={"error": {"old": None, "new": error[:500]}},
        )

    async def get_data(self, job: ReportJob, principal: Principal) -> ReportDataset:
        """П2 / backend-issues.md #19: результат в JSON, а не в файле.

        Не читает `job.file_id`/`job.status` нарочно: строит датасет заново
        по `job.template_code`/`job.params`, тем же builder'ом, что и
        `generate()` для xlsx/pdf — тогда `/data` отдаёт свежие данные (не
        снимок на момент, когда файл когда-то генерировался) и работает даже
        для задания, чей файл ещё строится (`queued`/`processing`) или уже
        удалён ретеншеном. Никакого файла в S3 и `REPORT_EXPORTED` — это
        чтение, не выгрузка (docstring `ReportDataOut`).
        """
        self.ensure_read_access(job, principal)
        template = await ReportTemplateService(self._session).get_or_404(job.template_code)
        kind = template.query_def.get("kind", template.code)
        if kind in FILE_ONLY_REPORTS:
            # Отчёт с ПДн целиком (например, выгрузка для LMS: СНИЛС, паспорт, адрес). У `/data`
            # нет записи `REPORT_EXPORTED`, значит, отдать его здесь — вынести ПДн мимо аудита.
            raise ForbiddenError(
                "Отчёт содержит персональные данные и доступен только файлом: скачайте его"
            )
        builder = REPORT_BUILDERS[kind]
        return await builder(self._session, principal, job.params)

    async def download(self, job: ReportJob, principal: Principal) -> tuple[str, dt.datetime]:
        self.ensure_read_access(job, principal)
        # Роль могла измениться после запуска: отчёт шаблона, закрытого для нынешней роли,
        # не выдаётся и автору (`allowed_roles` проверялась только при создании).
        template = await self._session.scalar(
            select(ReportTemplate).where(ReportTemplate.code == job.template_code)
        )
        if template is not None:
            self._ensure_role_allowed(template, principal)
        if job.status == ReportJobStatus.FAILED.value:
            raise AppError(
                ErrorCode.REPORT_NOT_READY,
                "Отчёт не сформирован из-за ошибки: запустите его заново",
            )
        if job.status != ReportJobStatus.COMPLETED.value:
            raise AppError(ErrorCode.REPORT_NOT_READY)
        # Ретеншен (`expire_report_files`) удаляет файл, а запись оставляет `completed` с
        # `expires_at` — по нему же клиент показывает «срок хранения истёк».
        file = await self._session.get(File, job.file_id) if job.file_id is not None else None
        expired = job.expires_at is not None and job.expires_at < dt.datetime.now(dt.UTC)
        if file is None or file.status != FileStatus.READY.value or expired:
            raise AppError(
                ErrorCode.REPORT_EXPIRED,
                "Срок хранения результата отчёта истёк: запустите отчёт заново",
            )

        settings = get_settings()
        ttl_seconds = settings.reports_link_ttl_minutes * 60
        url = await generate_presigned_get(
            bucket=file.bucket,
            key=file.storage_key,
            expires_seconds=ttl_seconds,
            filename=file.original_filename,
        )
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl_seconds)
        # Выдача ссылки на отчёт — выгрузка данных (часто с ПДн): пишется в аудит, как и скачивание
        # обычного файла. Отдельного действия `REPORT_DOWNLOADED` в перечне нет — используется
        # `FILE_DOWNLOADED` с сущностью `report_job`.
        await self._audit.record(
            AuditAction.FILE_DOWNLOADED,
            entity_type="report_job",
            entity_id=job.id,
            changes={
                "file_id": {"old": None, "new": str(file.id)},
                "template_code": {"old": None, "new": job.template_code},
                "format": {"old": None, "new": job.format},
            },
        )
        return url, expires_at

    async def expire_for_user(self, user_id: uuid.UUID) -> int:
        """B4-5/B4-6: смена ограничений доступа (`AccessRulesService.replace`) помечает
        готовые отчёты этого пользователя истёкшими — тот же механизм, что фоновая
        `reporting.tasks.expire_report_files`, но безусловно (не по `expires_at`) и
        только для него: отчёт, собранный ДО ограничения, мог включать строки, которые
        пользователь больше не должен видеть. Очередь `queued` трогать не нужно — воркер
        читает `access_restricted` из БД в момент запуска (`reporting.tasks`)."""
        from app.modules.files.service import delete_object_if_unreferenced

        now = dt.datetime.now(dt.UTC)
        jobs = (
            (
                await self._session.execute(
                    select(ReportJob).where(
                        ReportJob.requested_by == user_id,
                        ReportJob.status == ReportJobStatus.COMPLETED.value,
                        ReportJob.file_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        expired = 0
        for job in jobs:
            file = await self._session.get(File, job.file_id)
            if file is not None and file.status == FileStatus.READY.value:
                file.status = FileStatus.DELETED.value
                file.deleted_at = now
                file.refcount = max(0, file.refcount - 1)
                await self._session.flush()
                await delete_object_if_unreferenced(
                    self._session,
                    bucket=file.bucket,
                    key=file.storage_key,
                    excluding_file_id=file.id,
                )
            job.file_id = None
            job.expires_at = now
            expired += 1
        if jobs:
            await self._session.flush()
        return expired


# =============================================================================
# Дашборды
# =============================================================================


class DashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(self, principal: Principal) -> Select[tuple[Dashboard]]:
        stmt = select(Dashboard)
        if not principal.is_admin:
            stmt = stmt.where(
                or_(Dashboard.owner_id == principal.user_id, Dashboard.is_shared.is_(True))
            )
        return stmt.order_by(Dashboard.created_at.desc(), Dashboard.id.desc())

    async def get_or_404(self, dashboard_id: uuid.UUID) -> Dashboard:
        dashboard = await self._session.get(Dashboard, dashboard_id)
        if dashboard is None:
            raise NotFoundError("Дашборд", dashboard_id)
        return dashboard

    def ensure_read_access(self, dashboard: Dashboard, principal: Principal) -> None:
        if (
            dashboard.owner_id != principal.user_id
            and not dashboard.is_shared
            and not principal.is_admin
        ):
            raise ForbiddenError("Дашборд недоступен")

    def ensure_write_access(self, dashboard: Dashboard, principal: Principal) -> None:
        if dashboard.owner_id != principal.user_id and not principal.is_admin:
            raise ForbiddenError("Изменять дашборд может только владелец или администратор")

    async def create(self, principal: Principal, payload: Any) -> Dashboard:
        dashboard = Dashboard(
            name=payload.name,
            owner_id=principal.user_id,
            is_shared=payload.is_shared,
            layout=payload.layout or {},
        )
        self._session.add(dashboard)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_CREATED,
            entity_type="dashboard",
            entity_id=dashboard.id,
            changes={"name": {"old": None, "new": dashboard.name}},
        )
        return dashboard

    async def update(
        self, dashboard: Dashboard, payload: Any, *, expected_version: int
    ) -> Dashboard:
        if dashboard.version != expected_version:
            raise VersionConflictError(dashboard.version, {"name": dashboard.name})
        data = payload.model_dump(exclude_unset=True)
        _reject_null_fields(data)
        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            old = getattr(dashboard, key)
            if old == value:
                continue
            changes[key] = {"old": old, "new": value}
            setattr(dashboard, key, value)
        if not changes:
            return dashboard
        dashboard.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_UPDATED,
            entity_type="dashboard",
            entity_id=dashboard.id,
            changes=changes,
        )
        return dashboard

    async def delete(self, dashboard: Dashboard) -> None:
        dashboard_id = dashboard.id
        await self._session.delete(dashboard)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_DELETED, entity_type="dashboard", entity_id=dashboard_id
        )


class DashboardWidgetService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def get_or_404(self, widget_id: uuid.UUID) -> DashboardWidget:
        widget = await self._session.get(DashboardWidget, widget_id)
        if widget is None:
            raise NotFoundError("Виджет дашборда", widget_id)
        return widget

    def list_query(self, dashboard_id: uuid.UUID) -> Select[tuple[DashboardWidget]]:
        return (
            select(DashboardWidget)
            .where(DashboardWidget.dashboard_id == dashboard_id)
            .order_by(DashboardWidget.created_at)
        )

    async def add(self, dashboard: Dashboard, payload: Any) -> DashboardWidget:
        widget = DashboardWidget(
            dashboard_id=dashboard.id,
            widget_type=payload.widget_type,
            config=payload.config,
            position=payload.position or {},
        )
        self._session.add(widget)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_WIDGET_ADDED,
            entity_type="dashboard",
            entity_id=dashboard.id,
            changes={
                "widget_id": {"old": None, "new": str(widget.id)},
                "widget_type": {"old": None, "new": widget.widget_type},
            },
        )
        return widget

    async def update(self, widget: DashboardWidget, payload: Any) -> DashboardWidget:
        data = payload.model_dump(exclude_unset=True)
        _reject_null_fields(data)
        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            old = getattr(widget, key)
            if old == value:
                continue
            changes[key] = {"old": old, "new": value}
            setattr(widget, key, value)
        if not changes:
            return widget
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_WIDGET_UPDATED,
            entity_type="dashboard",
            entity_id=widget.dashboard_id,
            changes={"widget_id": {"old": None, "new": str(widget.id)}, **changes},
        )
        return widget

    async def remove(self, widget: DashboardWidget) -> None:
        dashboard_id = widget.dashboard_id
        widget_id = widget.id
        await self._session.delete(widget)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_WIDGET_REMOVED,
            entity_type="dashboard",
            entity_id=dashboard_id,
            changes={"widget_id": {"old": str(widget_id), "new": None}},
        )
