# Architecture Decision Records

Каждый ADR фиксирует одно решение в формате
**Context / Decision / Alternatives considered / Why / Consequences / Risks**.

Решения этой директории **побеждают** более ранние документы (`BLUEPRINT.md`,
`PRODUCT_BLUEPRINT.md`) там, где расходятся с ними. Расхождения перечислены
в [`../CONTRADICTIONS.md`](../CONTRADICTIONS.md).

| ADR | Решение | Заменяет / уточняет |
|---|---|---|
| [001](ADR-001-metacritic-data-acquisition.md) | Источник данных Metacritic и изоляция зависимости | BLUEPRINT ADR-0001, ADR-0002 |
| [002](ADR-002-score-value-model.md) | Оценка как значение со статусом; одна ось сравнения | BLUEPRINT §6.3; PRODUCT §12/§27 |
| [003](ADR-003-job-queue.md) | Очередь в PostgreSQL вместо Celery + Redis | BLUEPRINT §4/§13, ADR-0003 |
| [004](ADR-004-crawler-architecture.md) | State machine краулера, дневной цикл, sitemap-сверка | BLUEPRINT §7 |
| [005](ADR-005-review-ingestion.md) | Адаптивная пагинация отзывов, дедупликация, бюджеты | BLUEPRINT §2.1.2/§9 |
| [006](ADR-006-deduplication-idempotency.md) | Гарантии дедупликации на уровне БД | BLUEPRINT ADR-0007 |
| [007](ADR-007-review-snapshots.md) | Неизменяемый версионированный снапшот корпуса | BLUEPRINT §10.4 |
| [008](ADR-008-evidence-driven-summaries.md) | Evidence-driven резюме и машинная валидация claims | PRODUCT §13/§15 |
| [009](ADR-009-summary-thresholds.md) | Асимметричные пороги и политика перегенерации | PRODUCT §14 |
| [010](ADR-010-platform-aware-scoring.md) | Platform-aware данные и подача | — |
| [011](ADR-011-similarity-engine.md) | Объяснимый гибрид похожести без эмбеддингов | BLUEPRINT §11, ADR-0005 |
| [012](ADR-012-derived-verdict.md) | Вердикт вычисляется, а не генерируется | design/README §3 п.5 |
| [013](ADR-013-jobs-and-events.md) | Мониторинг и SSE поверх журнала событий | BLUEPRINT §14.1, ADR-0004 |
| [014](ADR-014-seed-catalog.md) | Seed-каталог тем же конвейером | PRODUCT §5/§28 |
| [015](ADR-015-catalog-depth.md) | Глубина каталога и правило eligibility | BLUEPRINT §28 Q4 |
| [016](ADR-016-youtube-enrichment.md) | YouTube: изолированное обогащение с деградацией | BLUEPRINT §12, ADR-0006 |
| [017](ADR-017-server-rendered-frontend.md) | Server-rendered Jinja2 вместо Next.js | BLUEPRINT §4/§16, A.2 |
| [018](ADR-018-database-portability.md) | PostgreSQL в проде, SQLite в разработке и тестах | BLUEPRINT §6 |
| [019](ADR-019-security-trust-boundaries.md) | Границы доверия и модель угроз | BLUEPRINT §21 |
| [020](ADR-020-settings-must-do-something.md) | Настройка обязана что-то делать, иначе удаляется | — |
| [021](ADR-021-verification-environment.md) | Проверять окружение, а не предполагать его отсутствие | — |
| [022](ADR-022-openai-provider.md) | OpenAI как второй LLM-провайдер | ADR-008 |
