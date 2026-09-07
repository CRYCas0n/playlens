# План реализации

**Дата:** 2026-09-06 · **Спецификация:** [`FINAL_SPEC.md`](FINAL_SPEC.md)

Принцип: **каждая фаза оставляет проект в проверяемом состоянии.** После каждой —
тесты, lint, миграции, осмотр данных, обновление traceability. Не Big Bang.

Порядок отличается от предложенного в задании в одном месте: **домен и правила подачи
(Phase 2) идут до провайдеров**, потому что `ScoreValue`, `tier`, `verdict` и валидатор
claims — то, что используют все остальные слои, и то, что проверяется чистыми тестами
без единой зависимости.

---

## Phase 0 — Каркас проекта — ✅ выполнена

| | |
|---|---|
| **Goal** | Репозиторий, конфигурация, логирование, CI-скрипты, `.env.example`, каркас приложения |
| **Depends on** | — |
| **Files** | `backend/pyproject.toml`, `app/config.py`, `app/logging.py`, `app/main.py`, `app/domain/errors.py`, `.env.example`, `Makefile`, `.gitignore` |
| **DB** | — |
| **API** | `GET /api/v1/health` |
| **Frontend** | — |
| **Tests** | конфиг падает при отсутствии обязательной переменной; секреты маскируются в `repr` и в логах; `/health` отдаёт 200 |
| **DoD** | `pytest` зелёный, `ruff check` зелёный, приложение стартует |
| **Risks** | — |

## Phase 1 — Домен и правила подачи — ✅ выполнена

| | |
|---|---|
| **Goal** | Чистое ядро: оценки, тиры, вердикт, нормализация текста, ошибки, идентичность |
| **Depends on** | Phase 0 |
| **Files** | `app/domain/{scores,tiers,verdict,enums,errors}.py`, `app/normalizers/{text,ids}.py` |
| **DB** | — |
| **Tests** | `ScoreValue`: `0/2` → `unavailable`, `0.5/6382` → `valid`, `null` → `unavailable`, `low_sample`; `tier` на границах 49/50, 69/70, 84/85; `norm(8.9) == 89`, `norm(None) is None`; `verdict_line` — полная таблица включая границы 6/7/8; `platform_line` при разрыве 9/10/11; `title_norm` на юникоде, диакритике и пунктуации |
| **DoD** | ≥60 unit-тестов зелёные; ни одного импорта вне stdlib и pydantic |
| **Risks** | — |

## Phase 2 — База данных и репозитории — ✅ выполнена

| | |
|---|---|
| **Goal** | Полная схема §8, миграции, репозитории с гарантиями §10 |
| **Depends on** | Phase 1 |
| **Files** | `app/db/{base,session,models,seed}.py`, `migrations/versions/0001_init.py`, `app/repositories/*` |
| **DB** | вся схема §8 |
| **Tests (integration)** | upgrade → downgrade → upgrade; **сверка списка уникальных индексов и CHECK со схемой**; конкурентный `claim_game` из 8 потоков → ровно одна строка; две вставки активного прогона → UniqueViolation; upsert игры дважды → 0 новых строк; батч отзывов дважды → `inserted=0, updated=0`; изменённый текст → `updated=1`; reaper возвращает просроченную аренду; после 3 попыток → `failed` |
| **DoD** | миграции идемпотентны; все гарантии §10 подтверждены тестами |
| **Risks** | расхождение SQLite/PostgreSQL — минимизировано ADR-018, закрывается OQ-V1 |

## Phase 3 — Очередь, воркер, планировщик — ✅ выполнена

| | |
|---|---|
| **Goal** | Фоновая инфраструктура §7 без внешнего брокера |
| **Depends on** | Phase 2 |
| **Files** | `app/queue/{jobs,worker,scheduler,retry,ratelimit}.py`, `app/services/monitoring_service.py` (часть) |
| **DB** | `jobs`, `job_events`, `worker_heartbeats`, `rate_limits` |
| **Tests** | два воркера не захватывают одну задачу; идемпотентный ключ отсекает дубль; retry с backoff; исчерпание попыток → `dead`; истёкшая аренда → возврат в очередь; token bucket соблюдает rps; планировщик не ставит два тика на один слот |
| **DoD** | воркер выполняет задачу end-to-end; события пишутся; глубина очереди видна |
| **Risks** | R-9 — покрывается тестами на конкурентность |

## Phase 4 — Metacritic provider — ✅ выполнена

| | |
|---|---|
| **Goal** | Адаптер источника: транспорт, ретраи, circuit breaker, парсеры, нормализаторы |
| **Depends on** | Phase 1 |
| **Files** | `app/adapters/http/*`, `app/adapters/metacritic/{provider,source_json,source_html,schemas,endpoints}.py`, `app/parsers/*`, `app/normalizers/game.py` |
| **DB** | — |
| **Tests** | все реальные фикстуры → корректные DTO; `video=null`, `rating=null`, `criticScoreSummary.score=null`, платформа без оценок; убрали обязательное поле → `SchemaDriftError`, добавили неизвестное → ок; **адаптивная пагинация: 10 у критиков, 100 у пользователей, оба из одного кода**; clamp offset; `dedupe_key` стабилен к регистру и пробелам; `source_fingerprint` стабилен к перестановке ключей; retry/backoff; circuit breaker открывается и закрывается |
| **DoD** | парсеры покрыты на реальных данных; **ни одного сетевого вызова в тестах** |
| **Risks** | R-3 — фикстуры + contract-тесты |

## Phase 5 — Crawler — ✅ выполнена

| | |
|---|---|
| **Goal** | State machine §10, дневной цикл, дедупликация, sitemap-сверка, seed |
| **Depends on** | Phases 2, 3, 4 |
| **Files** | `app/services/{crawl_orchestrator,game_sync}.py`, `app/tasks/{crawl,games}.py`, `app/cli.py` |
| **DB** | `crawl_days`, `crawl_runs`, `crawl_items` |
| **Tests** | все 12 сценариев §10 с подменёнными часами и фейковым провайдером; страница из одних дублей двигает курсор; источник упал → `partial` и курсор на месте; дата сменилась внутри прогона; бюджет ровно 20; sitemap-сверка добавляет ≤ лимита |
| **DoD** | `python -m app.cli crawl --once` наполняет БД из фикстур; повторный запуск в тот же день добавляет 0 строк |
| **Risks** | R-4 — sitemap-сверка |

## Phase 6 — Отзывы и снапшоты — ✅ выполнена

| | |
|---|---|
| **Goal** | Сбор отзывов, фильтр качества, стратифицированная выборка, неизменяемые снапшоты |
| **Depends on** | Phase 5 |
| **Files** | `app/services/{review_sync,snapshot_service}.py`, `app/ai/{sampling,corpus}.py`, `app/tasks/reviews.py` |
| **DB** | `reviews`, `review_snapshots`, `snapshot_reviews` |
| **Tests** | `content_hash` детерминирован и не зависит от порядка выдачи; фильтр отбрасывает короткие, дубликаты и несогласованные (на реальных примерах Release 0: RDR2 «0 + The best game I've ever played», Gollum «10 + jogo de merda»); стратификация соблюдает пол 15%; **корпус содержит даты у каждой записи**; `evidence_ref` не меняется при повторной сборке того же состава |
| **DoD** | снапшоты строятся на корпусах Release 0 и воспроизводят их состав |
| **Risks** | ложные срабатывания фильтра — метрика `rejected` в мониторинге |

## Phase 7 — AI-конвейер — ✅ выполнена

| | |
|---|---|
| **Goal** | Резюме, валидация claims, версионирование, объяснение gap'а, контроль стоимости |
| **Depends on** | Phase 6 |
| **Files** | `app/adapters/llm/*`, `app/ai/{schemas,prompts,validator}.py`, `app/services/summary_service.py`, `app/tasks/ai.py` |
| **DB** | `summaries`, `summary_claims`, `gap_explanations` |
| **Tests** | все ветви `should_regenerate`; идентичный вход → **0 вызовов** (счётчик на провайдере); **Elden Ring: 0 негативных claims, резюме валидно**; битая ссылка → claim отброшен; <3 подтверждений → отброшен; temporal без интервала дат → отброшен; вагонная формулировка → отброшена; всё отброшено → `rejected`, предыдущее остаётся текущим; недоступный LLM не ломает `game.sync`; prompt injection в тексте отзыва не проходит валидатор |
| **DoD** | конвейер отрабатывает на корпусах Release 0 с `FixtureLLMProvider`; метрика доли валидных claims пишется |
| **Risks** | R-7 — валидатор; OQ-V3 — замер на живой модели |

## Phase 8 — Похожие игры — ✅ выполнена

| | |
|---|---|
| **Goal** | Гибридный скоринг, кандидаты, объяснение, порог |
| **Depends on** | Phases 5, 7 |
| **Files** | `app/services/similarity_service.py`, `app/repositories/similarity.py`, `app/tasks/similarity.py` |
| **DB** | `similar_games` |
| **Tests** | одна франшиза → высоко; разные жанры → низко; штраф за DLC; детерминизм; работа **без** аспектного профиля; ниже порога → не публикуется; reason `null`, когда объяснить нечем; текущая игра не попадает в свой список |
| **DoD** | для каждой игры с ≥1 жанром есть либо ≥3 похожих, либо честное пустое состояние |
| **Risks** | R-8 — порог и `components` для разбора |

## Phase 9 — REST API — ✅ выполнена

| | |
|---|---|
| **Goal** | Все эндпоинты §9, пагинация, фильтры, ошибки RFC 7807, OpenAPI |
| **Depends on** | Phases 2, 8 |
| **Files** | `app/api/**` |
| **API** | §9.1–§9.3 |
| **Tests** | keyset и offset пагинация; мульти-платформенный фильтр = OR; все сортировки стабильны при равных оценках; `unavailable` не всплывает при сортировке по возрастанию; 404/400/422/401; 409 на повторный `Run now`; `ScoreValue` никогда не отдаёт `0` вместо `null` |
| **DoD** | Swagger полон; все схемы ответов покрыты тестами |
| **Risks** | — |

## Phase 10 — Фронтенд: каркас и дизайн-система — ✅ выполнена

| | |
|---|---|
| **Goal** | Базовый шаблон, ассеты дизайна, партиалы примитивов, `/img` |
| **Depends on** | Phase 9 |
| **Files** | `app/web/templates/{base,partials/*}`, `app/web/static/{tokens.css,app.css,app.js}`, `app/web/routes.py`, `app/web/images.py` |
| **Frontend** | topbar, footer, skip-link, тема, Score (chip/badge/gauge/bar), PlatformBadge, GameCard, SourceChip, EmptyState, Pending, Skeleton |
| **Tests** | `/img` отвергает чужой хост и недопустимую ширину; `null` рендерится как `—` + «Not rated»; карточка показывает максимум 2 кода + `+N`; `|safe` отсутствует в шаблонах |
| **DoD** | партиалы совпадают по классам с `COMPONENTS.md` |
| **Risks** | R-16 |

## Phase 11 — Home и Catalog — ✅ выполнена

| | |
|---|---|
| **Goal** | Discovery и каталог со всеми состояниями |
| **Depends on** | Phase 10 |
| **Files** | `templates/{index,catalog}.html`, `templates/fragments/catalog_grid.html` |
| **Tests** | пустой индекс; пустой результат поиска называет запрос и сохраняет фильтры; пустой результат фильтров — другой копирайт; фильтры отражены в URL; фрагмент возвращает ту же разметку, что и полная страница |
| **DoD** | все состояния из `states.html` воспроизведены |
| **Risks** | — |

## Phase 12 — Game Details — ✅ выполнена

| | |
|---|---|
| **Goal** | Главная страница продукта |
| **Depends on** | Phase 11 |
| **Files** | `templates/game.html` + партиалы verdict, consensus, platform table, summary, letsplay, similar |
| **Tests** | **все 28 кейсов `EDGE_CASES.md`**: длинный заголовок, 11 платформ, нет Metascore, нет Userscore, нет обеих, крошечный счётчик, нет резюме, длинное резюме, нет обложки, нет Let's Play (секция и якорь удалены), нет похожих, похожие без reason, одна платформа, нет per-platform оценок (Pending, секция не удалена), сильное расхождение, игроки выше критиков, неизвестный slug → 404 |
| **DoD** | вердикт присутствует всегда; ни один блок без данных не рендерится пустым |
| **Risks** | — |

## Phase 13 — Мониторинг и админ — ✅ выполнена

| | |
|---|---|
| **Goal** | Операционная страница, SSE, `Run now` |
| **Depends on** | Phases 3, 9 |
| **Files** | `app/api/routers/{monitoring,admin}.py`, `templates/admin/monitoring.html` |
| **Tests** | SSE доставляет записанное событие; `Last-Event-ID` доотдаёт пропущенное; heartbeat приходит; 409 при активном прогоне; без токена → 401; лимит игр не повышается параметром запроса |
| **DoD** | все показатели ТЗ Bonus 2 видны; обновление без перезагрузки |
| **Risks** | — |

## Phase 14 — YouTube (Bonus 1) — ✅ выполнена

| | |
|---|---|
| **Goal** | Полный конвейер, выключенный по умолчанию |
| **Depends on** | Phases 5, 7 |
| **Files** | `app/adapters/youtube/**`, `app/services/letsplay_service.py`, `app/tasks/youtube.py`, партиал `letsplay` |
| **Tests** | трейлер никогда не побеждает; live отсеян; `no commentary` проигрывает более релевантному; ролик другой игры отсеян; **пустой HTTP 200 → следующий провайдер, а не «нет субтитров»**; все провайдеры упали → `unavailable`; квота исчерпана → перенос, не ошибка; `metadata_only` помечен в UI |
| **DoD** | конвейер проходит на фикстурах; `YOUTUBE_ENABLED=false` не ломает ничего |
| **Risks** | R-5, OQ-B2, OQ-V5 |

## Phase 15 — Docker, документация, финальный аудит — ✅ выполнена

| | |
|---|---|
| **Goal** | Развёртывание, README, RUNBOOK, `FINAL_AUDIT.md` |
| **Depends on** | все |
| **Files** | `docker-compose.yml`, `docker-compose.prod.yml`, `docker/backend.Dockerfile`, `README.md`, `docs/RUNBOOK.md`, `docs/FINAL_AUDIT.md` |
| **Tests** | `docker compose config` валиден; полный прогон всех сюит; сверка traceability |
| **DoD** | каждое требование ТЗ имеет статус; известные ограничения перечислены честно |
| **Risks** | OQ-V1: Docker в среде разработки отсутствует — compose проверяется статически |

---

## Итог

Все 16 фаз выполнены. Порядок соблюдён: каждая заканчивалась прогоном тестов, линтера и
миграций, и следующая начиналась только после этого. Ни одна фаза не была объявлена
готовой авансом — дефекты, найденные при верификации более поздних фаз в уже принятом
коде более ранних, перечислены в `FINAL_AUDIT.md §3` (14 штук).

Отклонения от плана: **`docker-compose.prod.yml` не написан.** Продовый оверлей
отличался бы от базового только заменой `build:` на `image:`, снятием проброса порта
PostgreSQL и переносом секретов в `secrets:`. Писать его вслепую, ни разу не собрав
образ в этой среде (нет docker-демона), означало бы выдать за проверенное то, что не
запускалось ни разу. `docker-compose.yml` — рабочий и валидируется парсером; продовый
оверлей делается за 15 минут после первой успешной сборки (OQ-V2).

### Phase 16 — финальный проход и передача — ✅ выполнена

Не было в исходном плане. Появилась потому, что первая редакция `FINAL_AUDIT.md`
перечисляла шесть непроверенных пунктов, и часть из них оказалась непроверенной по
предположению, а не по факту: Chromium был установлен всё это время.

Сделано: автоматический поиск объявленных-но-нечитаемых настроек (нашёл 12), реализация
пяти из них и удаление семи с указанием причины; предохранитель `SCHEMA_DRIFT_THRESHOLD`;
round-trip миграций; счётчик SQL-запросов; SSE против настоящего uvicorn; браузерный
проход на 4 ширинах; contract-набор против живого Metacritic; `docker-compose.prod.yml`
и `docker-compose.dev.yml`; `scripts/release0.py`, `scripts/seed_demo.py`,
`scripts/browser_smoke.mjs`; `QUICKSTART.md`, `HANDOFF.md`, `HUMAN_EVALUATION.md`,
`PROJECT_STATUS.md`.

Найдено и исправлено семь дефектов, три серьёзных (`FINAL_AUDIT.md §3`).

Финальные цифры: **749 тестов** + 13 opt-in, `ruff` чисто, `make smoke` — 15 маршрутов,
браузерный проход зелёный, 84 требования (**79 `VERIFIED`**, 5 `IMPLEMENTED`).

---

## Сводка зависимостей

```
0 -> 1 -> 2 -> 3 ------\
          |            |
          +-> 4 -> 5 -> 6 -> 7 -> 8 -> 9 -> 10 -> 11 -> 12 -> 13 -> 15
                                              \-> 14 ------------/
```

Критический путь: 0 → 1 → 2 → 4 → 5 → 6 → 7 → 9 → 10 → 12.
Phase 14 (YouTube) может вестись параллельно после Phase 7 и вырезается первой,
если время закончится (`PRODUCT_BLUEPRINT §Особый вопрос 6`).
