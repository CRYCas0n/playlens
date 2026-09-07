# FINAL SPEC — Playlens (Game Intelligence Service)

**Версия:** 1.0 · **Дата:** 2026-09-06
**Статус:** каноническая спецификация. При расхождении **побеждает этот документ и ADR**;
`BLUEPRINT.md`, `PRODUCT_BLUEPRINT.md`, `RELEASE0_VALIDATION.md` и `design/` остаются
историей решений и первичными данными.
**Как читать:** карта документов — `DOCUMENT_MAP.md`; разбор расхождений — `CONTRADICTIONS.md`;
обоснования — `adr/`; незакрытое — `OPEN_QUESTIONS.md`.

---

## 1. Product definition

**Что это.** Сервис, который автономно поддерживает собственную реплику каталога игр
Metacritic, сжимает корпуса рецензий критиков и отзывов игроков в проверяемые резюме,
считает похожесть внутри собственной базы и отдаёт всё это через веб-интерфейс
с операционным мониторингом.

**Зачем это.** Не каталог игр и не зеркало Metacritic, а **decision assistant**:

> Metacritic отвечает на вопрос «какая у игры оценка». Мы отвечаем на вопрос
> «стоит ли мне играть в неё на моём железе» — и показываем, на чём основан ответ.

Три ставки, каждая опирается на проверенный факт, а не на гипотезу:

1. **Platform-aware.** У Cyberpunk 2077 разброс 29 пунктов Metascore и 3.5 балла Userscore
   между платформами; `?platform=` у источника игнорируется, показывается lead-платформа.
   Release 0 §5.3 показал, что различаются не только числа, но и **состав претензий**:
   на PC спорят о дизайне, на PS4 — о работоспособности.
2. **Critics vs players как объект.** Оба числа есть у всех; **разрыв как информация** —
   ни у кого.
3. **Сжатие корпуса.** 40 140 отзывов у Cyberpunk PC. Человек их не прочитает; агрегаторы
   их не суммаризируют.

**Главный UX-принцип.** `Verdict first, evidence second.` Вердикт — над сгибом, всегда;
всё остальное — доказательства, которые читают, только если вердикт заинтересовал.

**Главный принцип честности.** Мы интерпретируем источник, а не подменяем его. На каждом
блоке — что это, из скольких источников и ссылка на первоисточник.

**North Star.** Завершённые решения в неделю — сессии, где пользователь открыл страницу игры,
провёл ≥20 секунд и совершил осмысленное действие. Время на сайте для этого продукта —
**инвертированная** метрика: рост времени означает, что ответ непонятен.

---

## 2. User journeys

| # | Journey | Вход | Что должно произойти | Где реализовано |
|---|---|---|---|---|
| **J-1** | «Стоит ли это моего времени» (**основной**) | Поиск / прямая ссылка | 0–3 с: обложка, название, студия, платформы, жанр. 3–8 с: две шкалы с тир-словами и числом рецензий. 8–15 с: производная строка вердикта. 15–30 с: три сильных стороны и две оговорки | `/games/{slug}` §15 |
| **J-2** | «Покажи что-нибудь стоящее» | Главная | Spotlight как **работающий вердикт**, три рельса с заявленным правилом отбора, сетка недавно обновлённых | `/` |
| **J-3** | «Сузить выбор» | Каталог | Мультивыбор платформ с counts, диапазон оценки, окно релиза, 5 сортировок включая **biggest critic–player gap**, мгновенная фильтрация на месте, пагинация | `/games` |
| **J-4** | «Найти конкретную игру» | Поиск в топбаре | Нечувствительность к регистру и пунктуации; `nier automata` находит `NieR: Automata`; пустой результат называет запрос и предлагает выход | `/games?q=` |
| **J-5** | «Что дальше» | Блок похожих | 4–12 игр **только из нашей базы**, каждая кликабельна, объяснение — только если оно реально вычислено | §13 |
| **J-6** | «Как оно ощущается» | Блок Let's Play | Spoiler-free впечатление или только ссылка, или секции нет вовсе | §14 |
| **J-7** | «Всё ли собирается» (оператор) | `/admin/monitoring` | Светофор, KPI за 24 ч, текущая задача и стадия, воркеры, очереди, история, лента проблем, `Run now` | §17 |

Инварианты навигации: поиск доступен с любой страницы за одно действие; от любой точки
входа до любой страницы игры ≤2 клика; со страницы игры выход на первоисточник за 1 клик;
похожие игры замыкают петлю discovery.

---

## 3. MVP scope

Всё обязательное по ТЗ входит. Оба бонуса реализованы; YouTube выключен по умолчанию.

| Блок | Состав |
|---|---|
| **Ingest** | Часовой цикл: New Releases → browse с курсором, дневной сброс, максимум 20 новых игр за прогон, дедупликация констрейнтом, sitemap-сверка раз в неделю, seed-обход командой |
| **Данные игры** | Название, URL Metacritic, обложка, описание, разработчик, издатель, жанры, франшиза, ESRB, дата релиза, ссылка на видео |
| **Платформы** | Нормализованный справочник + `game_platforms` с Metascore и Userscore **по каждой платформе** |
| **Отзывы** | Критики и игроки раздельно, per-platform, с дедупликацией и инкрементальной докачкой |
| **Снапшоты** | Неизменяемые версионированные корпуса с фильтром качества и стабильными `evidence_ref` |
| **AI** | Резюме критиков и игроков (evidence-driven, 0–5 пунктов на секцию), объяснение gap'а, машинная валидация claims, версионирование |
| **Вердикт** | Производный, арифметический, platform-aware |
| **Похожие** | Объяснимый гибрид, только из своей базы, порог уверенности, честное пустое состояние |
| **Web** | Home, Catalog (поиск/фильтры/сортировки/пагинация), Game Details, About, Admin Monitoring; состояния loading / empty / pending / error; responsive 1440/1280/768/390 |
| **API** | REST + OpenAPI 3.1 |
| **Фон** | Очередь в PostgreSQL, планировщик, воркеры, retry/backoff, аренда, reaper, dead-letter |
| **Мониторинг** | Все показатели ТЗ + продуктовые метрики качества данных; SSE с polling-fallback; `Run now` |
| **Bonus 1** | YouTube: гейт, поиск, ранжирование, каскад транскриптов, spoiler-free резюме — **выключено по умолчанию** |
| **Инфраструктура** | Docker Compose: postgres, migrate, api, worker, scheduler; `.env.example`; миграции; структурные логи; тесты |

---

## 4. Out of scope

| Не входит | Почему |
|---|---|
| Аккаунты, персонализация, избранное | Нет требования ТЗ; тянет auth, PII и модерацию |
| Собственная числовая оценка («GameScore») | Нечем обосновать, конкурирует с Metascore, необъяснима. Продукт объясняет чужие числа, а не производит своё |
| Лента всех отзывов | Возврат к проблеме, которую решаем |
| Сортировка/фильтрация отзывов | Функциональность первоисточника |
| Полнотекстовый поиск по отзывам | Возвращает пользователя к чтению отзывов |
| Событийная лента «Userscore 8.1 → 6.4» | Требует снапшотов оценок во времени — OQ-N10 |
| Тренды, сравнение двух игр, NL-поиск, календарь релизов | Release 5+ по продуктовому плану |
| Полный обход 177 882 игр | ~370 суток при лимите ТЗ; ADR-015 |
| Нейросетевые эмбеддинги и pgvector | ADR-011: аспектный профиль даёт тот же сигнал бесплатно |
| Встроенный видеоплеер | ADR-019 §4: сторонний JS, куки, consent при нулевой выгоде |
| Kubernetes, Kafka, микросервисы, event bus | §63 задания: не добавлять без необходимости |

---

## 5. Functional requirements

Сквозная нумерация используется в `REQUIREMENTS_TRACEABILITY.md`.

### FR-1 Crawler

| ID | Требование |
|---|---|
| FR-1.1 | Сервис запускается примерно раз в час (`CRAWL_INTERVAL_CRON`, дефолт `7 * * * *`) |
| FR-1.2 | Первый источник — New Releases: `finder?componentName=new-releases-carousel&sortBy=-releaseDate&metaScoreMin=1&mcoTypeId=13&limit=20` |
| FR-1.3 | Второй источник — browse New: `finder?sortBy=-releaseDate&metaScoreMin=1&productType=games&offset=(page-1)*size` |
| FR-1.4 | Прогон может продолжать со следующей страницы; курсор в `crawl_days.browse_page` переживает рестарт |
| FR-1.5 | В начале нового календарного дня (`CRAWL_TIMEZONE`) цикл начинается заново |
| FR-1.6 | **За один прогон — максимум 20 игр**, ещё не обработанных сегодня |
| FR-1.7 | Существующая игра обновляется, дубль не создаётся |
| FR-1.8 | Собираются: title, cover, platforms, Metascore, Userscore, developer, description, video link |
| FR-1.9 | Оценки хранятся **отдельно по каждой платформе** |
| FR-1.10 | Sitemap-сверка раз в неделю компенсирует недетерминированность пагинации |
| FR-1.11 | Seed-обход по `sortBy=-metaScore` доступен командой CLI |

### FR-2 Review analysis

| ID | Требование |
|---|---|
| FR-2.1 | Отзывы критиков и игроков собираются и хранятся раздельно, привязаны к паре (игра, платформа) |
| FR-2.2 | Одинаковые отзывы не сохраняются повторно |
| FR-2.3 | Для каждой стороны формируется краткое резюме |
| FR-2.4 | Выделяются сильные стороны |
| FR-2.5 | Выделяются проблемы — **только при наличии evidence**; пустая секция легитимна |
| FR-2.6 | Резюме обновляется при повторном обходе **только при содержательном изменении входа** |
| FR-2.7 | Каждое существенное утверждение имеет ссылки на конкретные отзывы снапшота |
| FR-2.8 | Резюме версионируются; предыдущее валидное остаётся текущим при провале нового |
| FR-2.9 | Ниже порога корпуса резюме не генерируется; состояние явное |

### FR-3 Web interface

| ID | Требование |
|---|---|
| FR-3.1 | Discovery/Home: список игр с краткой информацией |
| FR-3.2 | Catalog: поиск по названию |
| FR-3.3 | Catalog: фильтрация по платформе |
| FR-3.4 | Catalog: сортировка по рейтингу с явным выбором базы (Metascore / Player score) |
| FR-3.5 | Catalog: пагинация; состояние отражено в URL |
| FR-3.6 | Game Details: title, cover, developer, platforms, scores, description, video, critic summary, user summary, similar games |
| FR-3.7 | Similar games — только из нашей БД; клик открывает страницу игры |
| FR-3.8 | Состояния loading / empty / pending / unavailable / error реализованы отдельно и различимы |
| FR-3.9 | Responsive: 1440 / 1280 / 768 / 390 без горизонтального скролла страницы |

### FR-4 Bonus 1 — YouTube

| ID | Требование |
|---|---|
| FR-4.1 | Поиск Let's Play по игре |
| FR-4.2 | Выбор наиболее популярного **релевантного** ролика (популярность ≠ первый результат) |
| FR-4.3 | Трейлер не может быть выбран |
| FR-4.4 | Получение транскрипта каскадом провайдеров с fallback |
| FR-4.5 | Пустой HTTP 200 трактуется как ошибка провайдера, а не как отсутствие субтитров |
| FR-4.6 | Spoiler-free резюме и ссылка на ролик |
| FR-4.7 | Отсутствие транскрипта не ломает систему; отсутствие ролика удаляет секцию |

### FR-5 Bonus 2 — Monitoring

| ID | Требование |
|---|---|
| FR-5.1 | Статус краулера и воркеров |
| FR-5.2 | Текущая задача и стадия |
| FR-5.3 | Количество обработанных / успешных / неуспешных / пропущенных |
| FR-5.4 | Ошибки с типом и затронутой игрой |
| FR-5.5 | Глубина очередей |
| FR-5.6 | Время последнего и следующего запуска, длительность |
| FR-5.7 | История запусков |
| FR-5.8 | Кнопка `Run now` с защитой от параллельного запуска |
| FR-5.9 | Обновление без перезагрузки страницы |

---

## 6. Non-functional requirements

| ID | Требование | Значение |
|---|---|---|
| NFR-1 | p95 `GET /games` (24 карточки с фильтрами) | < 120 мс |
| NFR-2 | p95 `GET /games/{slug}` | < 150 мс |
| NFR-3 | p95 `GET /monitoring/status` | < 100 мс |
| NFR-4 | Длительность часового tick'а (только постановка задач) | < 10 с |
| NFR-5 | Синхронной работы в HTTP-запросе нет, кроме чтения из БД | — |
| NFR-6 | Отказ любого внешнего сервиса не делает сервис недоступным | §23 |
| NFR-7 | Повторный запуск любой задачи не создаёт неконсистентных данных | §10, ADR-006 |
| NFR-8 | Rate limit к источнику | ≤2 rps, ≤2 параллельных соединения |
| NFR-9 | Все секреты — из окружения; в логах маскируются | ADR-019 T5 |
| NFR-10 | Каждая запись лога позволяет ответить «что случилось с игрой X в прогоне Y» | §21 |
| NFR-11 | Контраст текста ≥4.5:1, фокус виден, смысл не передаётся только цветом | design/UX_SPEC §8 |
| NFR-12 | Развёртывание одной командой после заполнения `.env` | §20 |

---

## 7. Architecture

```
                     ВНЕШНИЙ МИР
      backend.metacritic.com  |  www.metacritic.com
      YouTube Data API        |  Transcript providers
      Anthropic API
                     |
        ---- только через адаптеры ----
                     |
+--------------------------------------------------------------+
| ADAPTERS  app/adapters/                                       |
|   MetacriticProvider   -> JsonApiSource | HtmlSource          |
|   LLMProvider          -> AnthropicProvider | Null | Fixture  |
|   YouTubeProvider      -> DataApiSource | ScrapeSource(off)   |
|   TranscriptProvider   -> ytdlp | hosted | asr | metadata_only|
|   EmbeddingProvider    -> Null (точка расширения)             |
|   общее: HttpTransport, RateLimiter(DB), RetryPolicy,         |
|          CircuitBreaker, ResponseCache, Instrumentation       |
+--------------------------------------------------------------+
                     |
+--------------------------------------------------------------+
| PARSERS + NORMALIZERS  (чистые функции, без IO)               |
|   dict -> DTO -> доменная модель; fingerprint, dedupe_key     |
+--------------------------------------------------------------+
                     |
+--------------------------------------------------------------+
| DOMAIN  app/domain/   ScoreValue, tier, verdict, enums, errors|
+--------------------------------------------------------------+
                     |
+--------------------------------------------------------------+
| SERVICES  app/services/   бизнес-логика, без HTTP и без SQL   |
|   CrawlOrchestrator  GameSyncService   ReviewSyncService      |
|   SnapshotService    SummaryService    SimilarityService      |
|   LetsPlayService    MonitoringService BudgetService          |
+--------------------------------------------------------------+
                     |
+--------------------------------------------------------------+
| REPOSITORIES  app/repositories/  единственное место про SQL   |
+--------------------------------------------------------------+
                     |
              PostgreSQL 16  (SQLite в dev/test)
                     |
        +------------+-------------+
        |                          |
+---------------+        +--------------------+     +-----------+
| worker        |        | api (FastAPI)      |     | scheduler |
| claim/execute |        | REST + SSR HTML    |     | cron-цикл |
| jobs          |        | SSE из job_events  |     |           |
+---------------+        +--------------------+     +-----------+
```

**Четыре контура и их деградация:**

| Контур | Что делает | Деградирует до |
|---|---|---|
| Ingest | Часовой обход, нормализация, upsert | Источник лежит → прогон `partial`, курсор не двигается, данные целы |
| Enrich | Отзывы → снапшот → резюме; похожие; YouTube | Каждый шаг — своя задача; падение любого не влияет на данные игры |
| Serve | REST + HTML | Читает только БД; полностью независим от воркеров |
| Observe | `crawl_runs`, `jobs`, `job_events`, SSE, `Run now` | Работает, даже когда воркеры мертвы — и показывает это |

**Правила слоёв (проверяются архитектурным тестом):**
`app/services/**` не импортирует `httpx`, `sqlalchemy` и не содержит строк `backend.metacritic`.
`app/parsers/**` и `app/normalizers/**` не импортируют ничего из `app/repositories` и `httpx`.
`app/repositories/**` — единственное место, знающее про таблицы.

---

## 8. Data model

Портируемое подмножество PostgreSQL 16 / SQLite (ADR-018): без нативных ENUM, без массивов,
без pgvector. Все таймстемпы — timezone-aware UTC.

### 8.1 Справочники

```
platforms(id PK, mc_platform_id UNIQUE, slug UNIQUE, name, code, family, sort_order, is_active,
          created_at, updated_at)
genres(id PK, slug UNIQUE, name, created_at)
companies(id PK, mc_company_id UNIQUE, slug UNIQUE, name, created_at, updated_at)
franchises(id PK, mc_franchise_id UNIQUE, slug UNIQUE, name, created_at)
```

### 8.2 Ядро

```
games(
  id PK,
  mc_slug UNIQUE NOT NULL, mc_title_id UNIQUE, mc_family_id, mc_url NOT NULL,
  title NOT NULL, title_norm NOT NULL, description, cover_url, card_url,
  video_url, video_title, video_duration_s,
  release_date, premiere_year, esrb_rating, must_play, franchise_id FK,
  -- роллапы, пересчитываются в одной транзакции с game_platforms
  lead_platform_id FK, lead_metascore, lead_metascore_count,
  lead_userscore, lead_userscore_count,
  best_metascore, best_metascore_count, best_userscore, best_userscore_count,
  platform_count, critic_reviews_total, user_reviews_total,
  -- служебное
  source_fingerprint,
  detail_synced_at, reviews_synced_at, summaries_synced_at,
  youtube_searched_at, similarity_synced_at,
  first_seen_at, created_at, updated_at,
  CHECK (best_metascore IS NULL OR best_metascore BETWEEN 0 AND 100),
  CHECK (best_userscore IS NULL OR best_userscore BETWEEN 0 AND 10)
)

game_genres(game_id FK, genre_id FK, PK(game_id, genre_id))
game_companies(game_id FK, company_id FK, role CHECK IN ('developer','publisher'),
               PK(game_id, company_id, role))

game_platforms(
  id PK, game_id FK, platform_id FK, mc_related_game_id, is_lead, release_date,
  metascore_raw, metascore_count, metascore_positive, metascore_neutral, metascore_negative,
  critic_sentiment,
  userscore_raw, userscore_count, userscore_positive, userscore_neutral, userscore_negative,
  user_sentiment,
  critic_reviews_with_text, user_reviews_with_text,
  critic_reviews_synced_at, user_reviews_synced_at,
  created_at, updated_at,
  UNIQUE(game_id, platform_id),
  CHECK (metascore_raw IS NULL OR metascore_raw BETWEEN 0 AND 100),
  CHECK (userscore_raw IS NULL OR userscore_raw BETWEEN 0 AND 10)
)
UNIQUE INDEX game_platforms_one_lead ON game_platforms(game_id) WHERE is_lead
```

> `*_raw` — как пришло от источника. Статус (`valid` / `unavailable`) **вычисляется на чтении**
> функцией домена (ADR-002), поэтому изменение порога не требует миграции данных.

### 8.3 Отзывы и снапшоты

```
reviews(
  id PK, game_platform_id FK, game_id FK, kind CHECK IN ('critic','user'),
  source_review_id, dedupe_key NOT NULL,
  author, publication_name, publication_slug,
  score, score_max NOT NULL, score_normalized GENERATED STORED,
  body, body_hash NOT NULL, char_count, language_hint,
  url, published_on, is_spoiler, source_version, thumbs_up, thumbs_down,
  first_seen_at, created_at, updated_at,
  UNIQUE(game_platform_id, kind, dedupe_key)
)

review_snapshots(
  id PK, game_id FK, game_platform_id FK, kind,
  content_hash NOT NULL, ordering_version, sampling_version,
  review_count, candidate_count, rejected JSON,
  date_min, date_max, built_at,
  UNIQUE(game_platform_id, kind, content_hash)
)

snapshot_reviews(
  snapshot_id FK, evidence_ref, review_id FK, position,
  PK(snapshot_id, evidence_ref), UNIQUE(snapshot_id, review_id)
)
```

`evidence_ref` (`C00…`, `U00…`) присваивается один раз и **никогда не меняется** — ADR-007.

### 8.4 Резюме и claims

```
summaries(
  id PK, game_id FK, game_platform_id FK, snapshot_id FK,
  audience CHECK IN ('critic','user','letsplay'),
  heading, overall, aspect_verdicts JSON,
  confidence, status CHECK IN ('fresh','skipped_no_data','rejected','failed'),
  input_fingerprint NOT NULL,
  reviews_used, reviews_candidates,
  llm_provider, llm_model, prompt_version, params_version,
  tokens_in, tokens_out, cost_usd, latency_ms,
  error_message, version, is_current,
  generated_at, created_at,
  UNIQUE(game_platform_id, audience, input_fingerprint)
)
UNIQUE INDEX summaries_current ON summaries(game_platform_id, audience) WHERE is_current

summary_claims(
  id PK, summary_id FK, side CHECK IN ('positive','negative'),
  aspect, claim, claim_type CHECK IN ('descriptive','comparative','temporal'),
  evidence_refs JSON, strength, position,
  validation CHECK IN ('accepted','rejected_missing_ref','rejected_low_support',
                       'rejected_vague','rejected_temporal_unsupported',
                       'rejected_quote_not_found'),
  validation_detail
)

gap_explanations(
  id PK, game_platform_id FK, critic_snapshot_id FK, user_snapshot_id FK,
  gap_points, explanation, evidence_refs JSON, status, is_current,
  llm_model, prompt_version, generated_at,
  UNIQUE(game_platform_id, critic_snapshot_id, user_snapshot_id)
)
```

### 8.5 Похожие игры

```
similar_games(
  game_id FK, similar_game_id FK, rank, score, method,
  components JSON,        -- {metadata: .., aspect: .., lexical: .., penalties: ..}
  reason,                 -- NULL, если объяснить нечем. НИКОГДА не выдумывается
  computed_at,
  PK(game_id, similar_game_id), CHECK(game_id <> similar_game_id)
)
```

### 8.6 YouTube

```
youtube_videos(
  id PK, game_id FK, video_id, url, title, channel_id, channel_title, description,
  duration_s, view_count, like_count, published_at, has_captions,
  default_audio_language, live_broadcast, thumbnail_url,
  rank_score, rank_components JSON, rejected_reason, is_selected,
  discovery_source, created_at, updated_at,
  UNIQUE(game_id, video_id)
)
UNIQUE INDEX youtube_one_selected ON youtube_videos(game_id) WHERE is_selected

youtube_transcripts(
  id PK, video_id FK UNIQUE, status, source, language, is_auto_generated,
  text, char_count, attempts JSON, error_message, fetched_at, created_at, updated_at
)
```

Резюме Let's Play живёт в той же `summaries` с `audience='letsplay'`.

### 8.7 Операционный контур

```
crawl_days(crawl_date PK, phase, new_releases_done, new_releases_seen,
           browse_page, browse_offset, browse_pages_done, browse_exhausted,
           release_date_watermark, games_claimed, created_at, updated_at)

crawl_runs(id PK, crawl_date FK, trigger, triggered_by, status, phase_at_start,
           worker_id, source_params JSON,
           pages_fetched, games_discovered, games_claimed, games_skipped_dupe,
           games_succeeded, games_failed, jobs_enqueued,
           started_at, finished_at, duration_ms, error_message, error_class, created_at)
UNIQUE INDEX crawl_runs_single_active ON crawl_runs(status) WHERE status = 'running'

crawl_items(id PK, crawl_date, game_slug, game_id FK, crawl_run_id FK, source, status,
            attempts, lease_expires_at, changed, error_message, claimed_at, finished_at,
            UNIQUE(crawl_date, game_slug))

jobs(id PK, job_type, idempotency_key UNIQUE, status, queue, priority,
     game_id FK, crawl_run_id FK, parent_job_id FK,
     payload JSON, result JSON,
     attempts, max_attempts, next_retry_at, lease_expires_at, worker_id,
     error_class, error_message,
     queued_at, started_at, finished_at, duration_ms)

job_events(id PK, ts, level, event, stage, message,
           crawl_run_id, job_id, game_id, worker_id, data JSON)

worker_heartbeats(worker_id PK, queues, started_at, heartbeat_at, active_jobs, version)

api_budgets(provider, usage_date, units_used, units_limit, calls JSON, updated_at,
            PK(provider, usage_date))

rate_limits(bucket_key PK, tokens, updated_at)
```

### 8.8 Индексы под конкретные запросы

| Запрос | Индекс |
|---|---|
| поиск по названию | `games(title_norm)`; в PG дополнительно GIN `pg_trgm` |
| фильтр по платформе | `game_platforms(platform_id, game_id)` |
| фильтр по жанру | `game_genres(genre_id, game_id)` |
| сортировка по Metascore | `games(best_metascore DESC NULLS LAST, id DESC)` |
| сортировка по Userscore | `games(best_userscore DESC NULLS LAST, id DESC)` |
| сортировка по дате | `games(release_date DESC NULLS LAST, id DESC)` |
| «недавно обновлённые» | `games(updated_at DESC, id DESC)` |
| планировщик enrich | `games(reviews_synced_at)`, `games(similarity_synced_at)` |
| отзывы игры | `reviews(game_id, kind, published_on DESC, id DESC)` |
| похожие | `similar_games(game_id, rank)` |
| очередь | `jobs(status, queue, priority DESC, queued_at)`, `jobs(lease_expires_at) WHERE status='running'` |
| лента событий / SSE | PK `job_events(id)`, `job_events(crawl_run_id, id)` |
| история прогонов | `crawl_runs(started_at DESC)` |
| дедупликация дня | `crawl_items(crawl_date, status)` |

`id DESC` вторым ключом во всех сортировках — tie-breaker для стабильной keyset-пагинации.
Без него страницы «плывут» при равных оценках — та же болезнь, что у источника.

---

## 9. API contracts

База `/api/v1`. JSON. Ошибки — RFC 7807 `application/problem+json`.
OpenAPI 3.1 генерируется автоматически: `/api/v1/openapi.json`, Swagger UI `/api/v1/docs`.

### 9.1 Каталог

| Метод | URL | Параметры | Ответ |
|---|---|---|---|
| `GET` | `/games` | `q`, `platform` (повторяемый → OR), `genre`, `score_band` (`any\|excellent\|good\|mixed\|unrated`, **кумулятивные**: `good` = 70+), `released` (`any\|last30\|2026\|earlier`), `sort` (`metascore\|userscore\|release_date\|gap\|title`), `order`, `limit` (1..100, деф. 24), `offset` (≤ `OFFSET_HARD_LIMIT`) | `{items: GameListItem[], total, limit, offset, has_more, facets}` |
| `GET` | `/games/{id_or_slug}` | `include` | `GameDetail` |
| `GET` | `/games/{id_or_slug}/similar` | `limit` (1..24, деф. 12) | `{items: SimilarGame[]}` |
| `GET` | `/games/{id_or_slug}/summaries` | `audience`, `platform` | `{items: Summary[]}` |
| `GET` | `/platforms` | `only_with_games` | `[{slug, name, code}]` |
| `GET` | `/genres` | `only_with_games` | `[{slug, name, count}]` |
| `GET` | `/search/suggest` | `q` (≥2), `limit` (деф. 8) | `{items: [...]}` |
| `GET` | `/stats` | — | `{games_total, games_with_metascore, games_with_summaries, last_crawl_at}` |
| `GET` | `/metrics` | — | Prometheus text format |
| `GET` | `/health` | — | `{status, version, checks}` |

**Два эндпоинта из первоначального списка не реализованы, и это решение, а не пропуск:**

* `GET /games/{slug}/reviews` — лента отзывов. Полный текст чужих отзывов сервис не
  переиздаёт (ADR-001, OQ-B3); отзывы существуют как доказательная база резюме, а не как
  контент. Эндпоинт, отдающий их постранично, — это ровно перепубликация. Цитаты
  доступны там, где они что-то значат: в `evidence` конкретного claim.
* `GET /games/{slug}/youtube` — Let's Play приходит внутри `GameDetail.lets_play`. Секция
  всегда рендерится вместе со страницей; отдельный запрос дал бы второй источник правды о
  том, есть ли видео.

`?cursor=` также не реализован: глубже `OFFSET_HARD_LIMIT` (10 000) выдаётся 400 с
объяснением, а keyset нужен именно для глубокого листания. `id DESC` вторым ключом
сортировки сохранён — он делает страницы стабильными и остаётся основой, если keyset
понадобится.

### 9.2 Ключевые схемы ответа

`ScoreValue` — единственная форма подачи оценки во всём API:

```jsonc
{
  "value": 8.9,            // нативное значение или null
  "scale_max": 10,
  "normalized": 89,        // ось 0-100 или null
  "status": "valid",       // valid | unavailable | n/a
  "tier": "excellent",     // excellent | good | mixed | poor | none
  "tier_label": "Excellent",
  "review_count": 14204,
  "low_sample": false
}
```

`Verdict`:

```jsonc
{
  "line": "Critics rate this among the year's best, and players agree.",
  "kind": "agree",                    // agree | players-lower | players-higher | one-side | none
  "delta": 4,
  "platform_line": null,              // появляется при выбранной платформе и разрыве >= 10
  "strengths": ["...", "...", "..."], // ДОСЛОВНО из summary.claims, ничего нового
  "watch_outs": ["...", "..."],
  "derived": true                     // всегда true: вердикт не генерируется моделью
}
```

`Summary`:

```jsonc
{
  "audience": "critic",
  "platform": {"code": "PS5", "name": "PlayStation 5"},
  "heading": "Critics rate this among the year's best",
  "overall": "…",
  "positive": [{"aspect": "gameplay", "claim": "…", "evidence": ["C03","C11","C27"],
                "strength": "strong"}],
  "negative": [],
  "aspect_verdicts": {"gameplay": "positive", "performance": "mixed"},
  "provenance": {
    "kind": "ai_summary",
    "reviews_used": 118,
    "reviews_candidates": 134,
    "snapshot_id": 9012,
    "model": "claude-sonnet-5",
    "prompt_version": "critic-v1",
    "generated_at": "2026-09-06T07:10:00Z",
    "source_url": "https://www.metacritic.com/game/elden-ring/critic-reviews/"
  },
  "status": "fresh"
}
```

`negative: []` — **валидное состояние**, а не ошибка (ADR-008).

`SimilarGame`:

```jsonc
{ "slug": "lies-of-p", "title": "Lies of P", "cover_url": "…",
  "metascore": { /* ScoreValue */ },
  "reason": "Same studio",        // null, если объяснить нечем
  "score": 0.71,
  "components": {"metadata": 0.68, "aspect": 0.81, "lexical": 0.42} }
```

### 9.3 Мониторинг и админ

| Метод | URL | Доступ | Ответ |
|---|---|---|---|
| `GET` | `/monitoring/status` | открыто | `MonitoringStatus` |
| `GET` | `/monitoring/runs` | открыто | история прогонов |
| `GET` | `/monitoring/runs/{id}` | открыто | прогон + разбивка по items и jobs |
| `GET` | `/monitoring/jobs` | открыто | задачи с фильтрами |
| `GET` | `/monitoring/events` | открыто | события с фильтрами |
| `GET` | `/monitoring/stream` | открыто | `text/event-stream` |
| `GET` | `/health` | открыто | `{status, checks:{db, scheduler, metacritic, llm, youtube}}` |
| `GET` | `/metrics` | открыто | Prometheus text |
| `POST` | `/admin/crawl/run` | `X-Admin-Token` | 202 `{run_id, job_id, stream_url}` / **409** |
| `POST` | `/admin/games/{id}/resync` | `X-Admin-Token` | 202 `{job_ids}` |
| `POST` | `/admin/jobs/{id}/retry` | `X-Admin-Token` | 202 |
| `POST` | `/admin/crawl/runs/{id}/cancel` | `X-Admin-Token` | 202 |

### 9.4 HTML-маршруты

`/` · `/games` · `/games/{slug}` · `/about` · `/admin/monitoring` ·
`/games/fragment` (HTML-фрагмент сетки для фильтрации без перезагрузки) ·
`/img?u=&w=` (ресайз обложек с whitelist хоста).

**HTML и JSON рендерятся из одних и тех же Pydantic-схем и одного сервисного вызова** —
контракты не могут разойтись (ADR-017).

---

## 10. Crawler state machine

Полная диаграмма — `adr/ADR-004-crawler-architecture.md`. Здесь — контракт.

### Состояния и переходы

| Состояние `crawl_items.status` | Вход | Выход |
|---|---|---|
| `pending` | reaper вернул просроченную аренду | → `processing` при постановке `game.sync` |
| `processing` | успешный захват (`ON CONFLICT DO NOTHING RETURNING`) | → `done` \| `failed` \| `pending` (аренда истекла) |
| `done` | `game.sync` завершился | терминальное |
| `failed` | `attempts >= CRAWL_ITEM_MAX_ATTEMPTS` или `PermanentError` | терминальное; ручной retry |
| `skipped` | игра уже захвачена сегодня | терминальное |

Стадии внутри обработки одной игры (видны в `job_events.stage`):
`fetching → normalized → persisted → reviews_fetching → snapshot_built → summary_pending →
summary_generated → validated → completed`, с ветками `summary_rejected`, `partial`, `failed`.

### Разбор обязательных сценариев

| Сценарий | Поведение |
|---|---|
| В New Releases меньше 20 | Берём сколько есть; `new_releases_done = true`; остаток бюджета уходит в browse в этом же прогоне |
| Игра уже есть в БД | Захватывается (сегодня ещё не обрабатывалась) → upsert; при неизменившемся fingerprint дорогие downstream не ставятся |
| Игра обработана вчера | Строки за сегодня нет → захватывается заново; дифференциальное обновление решает, что делать |
| Игра обработана сегодня | `ON CONFLICT DO NOTHING` → 0 строк → `games_skipped_dupe += 1` |
| Прогон упал после 7 из 20 | 7 = `done`, 13 = `processing` с истёкшей арендой; reaper возвращает в `pending`; прогон = `failed`; следующий tick продолжает |
| Схема источника изменилась | `SchemaDriftError` → прогон `partial`, курсор не двинут, событие `error`, метрика; при превышении порога — авто-переключение на `HtmlSource` |
| Появилась новая игра | Попадёт в New Releases (если есть Metascore) или в первые страницы browse |
| Сервер перезапустился | Аренда истекает → reaper; состояние краулера в `crawl_days` |
| Два запуска одновременно | Три барьера: блокировка строки дня → partial UNIQUE на активный прогон → UNIQUE `crawl_items` |
| Дата сменилась внутри прогона | Проверка на границе каждой страницы; прогон завершается `succeeded` с тем, что успел |
| Источник недоступен | Прогон `partial`, **курсор не двигается**; следующий tick повторит ту же страницу |
| Все 24 элемента страницы — дубли | Курсор всё равно двигается, иначе краулер застревает навсегда |

### Дифференциальное обновление

```
detail = provider.game_detail(slug)                 # 1 запрос
fp_new = fingerprint(normalize(detail))
existing = repo.find_by_title_id(...) or repo.find_by_slug(slug)
changed = existing is None or existing.source_fingerprint != fp_new

with tx:                                            # ОДНА транзакция
    game_id = repo.upsert_game(...)                 # обновляются только изменившиеся колонки
    repo.upsert_platforms(...); repo.upsert_genres(...); repo.upsert_companies(...)
    repo.refresh_rollups(game_id)
    repo.mark_crawl_item(item_id, 'done', changed=changed)

if reviews_stale(existing, scores_moved):  enqueue reviews.sync
if changed:                                 enqueue similarity.recompute
if youtube_gate_passed and not searched:    enqueue youtube.discover
```

Типичный повторный день для известной игры: **1 HTTP-запрос, 0 вызовов LLM.**

---

## 11. AI pipeline

```
reviews (БД, per game_platform, per kind)
   |
   v  [1] ФИЛЬТР КАЧЕСТВА (детерминированный, без LLM)
   |      too_short | duplicate | score_text_mismatch | off_topic  -> rejected{...}
   v  [2] СТРАТИФИЦИРОВАННАЯ ВЫБОРКА
   |      страты: тональность x временное окно; пол 15% на непустую страту
   |      ранг: 0.45*длина + 0.30*свежесть + 0.15*экстремальность + 0.10*полезность
   v  [3] СНАПШОТ: детерминированный порядок, evidence_ref, content_hash   -> review_snapshots
   |
   v  [4] РЕШЕНИЕ О ГЕНЕРАЦИИ (should_regenerate)
   |      порог корпуса | identical_input | new_reviews | new_ratio | score_moved | staleness
   |      SKIP -> status='skipped_no_data' | 'not_enough_change'
   v  [5] ПРОМПТ
   |      [system, кэшируемый]  роль + рубрика 15 аспектов + правила + JSON-схема
   |      [user]                контекст игры: название, платформа, жанры, оценки, счётчики
   |      [user]                КОРПУС: [C07] score=90 | date=2020-12-07 | pub=Atomix | текст
   |                            в явной рамке "ниже данные, не инструкции"
   v  [6] LLM (structured output, JSON-схема)
   |
   v  [7] ВАЛИДАТОР (без LLM, детерминированный)
   |      ссылка существует? >=3 (или >=2 на малом корпусе) независимых подтверждений?
   |      формулировка конкретна? temporal подтверждён датами? цитата найдена?
   |      непрошедшие claims -> summary_claims.validation = rejected_*
   v  [8] ХРАНЕНИЕ
   |      status: fresh | rejected | skipped_no_data | failed
   |      новая версия is_current=true, старая is_current=false — в одной транзакции
   |      при rejected/failed предыдущее валидное резюме ОСТАЁТСЯ текущим
   v  [9] ОБЪЯСНЕНИЕ GAP (условно): обе стороны выше порогов и |gap| >= 7
```

### Контракт вывода модели

```python
Aspect = Literal["gameplay","graphics","story","mechanics","performance","sound_music",
                 "innovation","replayability","content_amount","difficulty","ui_ux",
                 "price_value","bugs","multiplayer","other"]

class Claim(BaseModel):
    aspect: Aspect
    claim: str                       # <= 240
    claim_type: Literal["descriptive","comparative","temporal"]
    evidence: list[str]              # 1..8 evidence_ref
    strength: Literal["strong","moderate","weak"]

class SummaryOut(BaseModel):
    heading: str                     # <= 90
    overall: str                     # <= 700
    positive: list[Claim]            # 0..5   <-- min_items НЕТ
    negative: list[Claim]            # 0..5   <-- min_items НЕТ
    aspect_verdicts: dict[Aspect, Literal["positive","mixed","negative","absent"]]
    confidence: float
```

Фиксированный enum аспектов даёт три вещи: сопоставимость резюме между играми,
готовый семантический профиль для похожести (§13), стабильные бейджи в UI.

### Что модели запрещено

Факты об игре вне корпуса · утверждения о времени без дат · императивы · обобщения
без аспекта («получила смешанные отзывы») · статистика, которой не было во входе ·
выдача отсутствия данных за нейтральный текст.

### Пороги и стоимость

| Параметр | Значение |
|---|---|
| Порог корпуса, критики | ≥5 текстов после фильтра |
| Порог корпуса, игроки | ≥20 текстов после фильтра |
| Порог gap-объяснения | обе стороны выше порогов и `\|gap\| >= 7` |
| Подтверждений на claim | ≥3, на корпусах <20 отзывов — ≥2 |
| Бюджет входа | `AI_MAX_INPUT_TOKENS` = 60 000, при превышении — map-reduce |
| Дневной лимит стоимости | `AI_DAILY_COST_LIMIT_USD`; при превышении задачи откладываются, не падают |
| Кэш идентичного входа | `UNIQUE(game_platform_id, audience, input_fingerprint)` — физическая гарантия |

`input_fingerprint = sha256(snapshot.content_hash + prompt_version + model + params_version)`.

---

## 12. Review evidence model

Три уровня адресации, каждый неизменяем на своём горизонте:

| Уровень | Идентификатор | Стабильность |
|---|---|---|
| Отзыв в БД | `reviews.id` + `dedupe_key` | пока отзыв существует у источника |
| Отзыв в корпусе | `snapshot_reviews.evidence_ref` (`C07`) | **навсегда внутри снапшота** |
| Корпус | `review_snapshots.content_hash` | детерминирован по составу и порядку |

Claim ссылается на `evidence_ref` **своего** снапшота. Пересборка корпуса создаёт новый
снапшот; старые резюме продолжают указывать на свой. Ссылка не может молча указать
на другой отзыв — дефект, найденный Release 0 §6.1, устранён структурно.

Хранимые поля claim'а (требование задания §24):
`claim` · `claim_type` · `aspect` · `evidence_refs` · `strength` · `validation` ·
и через `summaries`: `generated_at` · `llm_model` · `prompt_version` · `snapshot_id`.

**Даты обязательны** в каждой записи корпуса — иначе temporal-claims либо не генерируются,
либо выдумываются (Release 0 §7.3).

---

## 13. Similarity architecture

```
S(a,b) = 0.62*M + 0.26*A + 0.12*L,  веса перенормируются, если компонент недоступен

M metadata  = 0.30*Jaccard(genres) + 0.20*max(franchise, family) + 0.16*company_overlap
            + 0.12*Jaccard(platforms) + 0.12*score_proximity + 0.10*era_proximity
A aspect    = косинус по 15-мерному aspect_verdicts (positive=+1, mixed=0, negative=-1)
L lexical   = сходство нормализованных токенов similarity-документа

штрафы: x0.35 — обе без Metascore и <5 отзывов; x0.5 — DLC/переиздание той же family
фильтр:  similar_game_id <> game_id;  публикуется только score >= SIMILARITY_MIN_SCORE (0.34)
```

Две фазы: SQL-отбор до 300 кандидатов (по жанрам, разработчику, франшизе, близости оценки),
затем точный скоринг в Python и запись top-12 через `DELETE + INSERT` в одной транзакции.

**Объяснение вычисляется правилом по самому сильному компоненту**, не моделью:
`Same series` · `Same studio` · `Similar genre and tone` · `Players praise both for {aspect}` ·
`Similar critic reception` · **`null`** — и тогда чип не рендерится (правило дизайна).

Пересчёт: после `game.sync` с изменившимся fingerprint'ом; после генерации резюме
(меняется аспектный профиль); суточная задача для игр с `similarity_synced_at` старше 7 дней —
новая игра меняет топ у соседей, мгновенная симметрия стоила бы слишком дорого.

Только игры из нашей БД — требование ТЗ выполняется по построению: кандидаты берутся
`SELECT ... FROM games`.

---

## 14. YouTube architecture

**Отдельный, не блокирующий конвейер. Выключен по умолчанию (`YOUTUBE_ENABLED=false`).**

```
[0] ГЕЙТ      best_metascore IS NOT NULL
              AND (best_metascore >= 60 OR user_reviews_with_text >= 50)
              AND youtube_searched_at IS NULL
[1] SEARCH    search.list — 100 units, ОДИН раз на игру за всё время
              videoDuration=long (>20 мин) отсекает Shorts и трейлеры бесплатно
[2] HYDRATE   videos.list — 1 unit на 50 id: duration, views, likes,
              liveBroadcastContent, caption, defaultAudioLanguage, embeddable
[3] ФИЛЬТР    3 барьера против трейлеров + live + язык + пересечение названия < 0.6
              отсеянные пишутся в БД с rejected_reason
[4] RANK      0.32*popularity + 0.28*title_relevance + 0.14*format_fit
              + 0.12*engagement + 0.08*recency_fit + 0.06*series_signal - penalties
              penalties: -0.25 no commentary, -0.15 shorts, -0.10 компиляции, -0.20 нет caption
[5] SELECT    is_selected = true (partial UNIQUE на game_id)
[6] TRANSCRIPT ytdlp -> hosted_api -> asr -> metadata_only
              ПУСТОЙ HTTP 200 == ОШИБКА ПРОВАЙДЕРА, не "субтитров нет"
              все попытки -> youtube_transcripts.attempts
[7] NORMALIZE [Music], повторы ASR, таймкоды; временной sampling вместо обрезки
[8] LLM       промпт letsplay: только темп, сложность, управление, техсостояние, объём;
              сюжет, персонажи, боссы, концовка — ЗАПРЕЩЕНЫ
[9] UI        транскрипт есть -> впечатление; только метаданные -> ссылка без выводов;
              ролика нет -> секция удаляется вместе с якорем
```

Квота учитывается в `api_budgets` посуточно; при нехватке задача переносится на следующие
сутки, а не падает. Ошибка любого шага не влияет на статус игры (Сценарий 3 ТЗ §30).

---

## 15. Frontend architecture

Server-rendered Jinja2 внутри того же приложения; CSS и JS — из `design/prototype/assets`
практически без изменений (ADR-017).

```
/                    Home       Spotlight + 3 рельса + сетка Recently updated + trust-футер
/games               Catalog    sidebar-фильтры (≤1023 -> bottom sheet), toolbar, чипы,
                                счётчик, сетка, пагинация
/games/{slug}        Details    hero -> verdict -> anchor nav -> scores -> what it is
                                -> critics & players -> see it played -> if you like this
/about               About      методология, источники, дисклеймер
/admin/monitoring    Ops        status -> KPI -> current job -> stage strip -> workers ||
                                queues -> run history -> problem log
/games/fragment      HTML-фрагмент сетки для мгновенной фильтрации
/img                 ресайз обложек, whitelist хоста
```

Порядок секций страницы игры и два отклонения от буквального порядка ТЗ — из дизайна
(`PAGES.md §1.1`) и обоснованы там: описание **ниже** оценок (иначе вердикт уходит за сгиб),
критики и игроки **рядом**, а не последовательно (сравнение — суть продукта).

---

## 16. UX implementation rules

Правила, нарушение которых меняет то, чем является продукт. Каждое проверяется тестом.

| # | Правило | Источник |
|---|---|---|
| U-1 | Одна ось: Userscore отображается нативно (`8.9`), сравнивается нормализованным (`89`); подпись про `×10` присутствует всегда | design §3.1, ADR-002 |
| U-2 | Четыре тира и «Not rated»; пороги в одном месте | design §3.2 |
| U-3 | Цвет — никогда единственный сигнал: у каждой оценки есть тир-слово, у каждого статуса — точка **и** слово | design §3.3 |
| U-4 | `null` — не `0`: `—` + «Not rated» + серое кольцо. Никогда `0`, никогда скрытая строка | design §3.4, ADR-002 |
| U-5 | Строка вердикта производная, а не сгенерированная; дисклеймер ей не нужен | design §3.5, ADR-012 |
| U-6 | Разрыв критики/игроки — first-class: флаг на карточке, сортировка в каталоге, блок на странице | design §3.6 |
| U-7 | AI-атрибуция живёт в provenance-футере, а не бейджем сверху; счётчик источников не опускается никогда | design §3.7 |
| U-8 | Reason похожести рендерится **только** если backend его прислал. Никогда не выдумывается | design §3.8, ADR-011 |
| U-9 | Секция без данных **удаляется** (нет Let's Play → нет секции и нет якоря). Исключение — *pending*: «скоро» ≠ «ничего» | design §3.9 |
| U-10 | Отказ секции не роняет страницу; голый код статуса не является сообщением | design §3.10 |
| U-11 | Вердикт над сгибом на 1440×900 и в первом экране-с-небольшим на 390 | design §1 |
| U-12 | Signal chips берутся **дословно** из списков резюме; новых утверждений на первом экране не появляется | design UX_SPEC §2 |
| U-13 | ≤1023: сайдбар фильтров становится bottom sheet с явным `Show results` | design RESPONSIVE §3 |
| U-14 | ≤1023: резюме складываются в колонку (критики первыми) и **никогда не становятся табами** | design RESPONSIVE §4b |
| U-15 | <768: обложка героя встаёт над заголовком; появляется sticky-бар с двумя оценками | design RESPONSIVE §4a,c |
| U-16 | Карточка показывает максимум 2 кода платформ + `+N` | design COMPONENTS |
| U-17 | Пустой результат поиска называет запрос и **не сбрасывает фильтры молча** | design EDGE_CASES 21 |
| U-18 | Empty ≠ broken: разные копирайт и иконка | design COPY §1.7 |
| U-19 | Тон: sentence case, без восклицаний и эмодзи, числа конкретны | design COPY §1 |
| U-20 | Фокус виден всегда; skip-link на каждой странице; у гейджей текстовая альтернатива | design UX_SPEC §8 |

---

## 17. Monitoring

Полное покрытие ТЗ Bonus 2 — таблица соответствия в `adr/ADR-013-jobs-and-events.md`.

Сверх ТЗ добавлены **продуктовые метрики качества данных** (`PRODUCT_BLUEPRINT §19`):
доля игр с полными данными · доля игр с резюме · средний возраст данных ·
доля отбракованных валидатором claims · расход LLM за сутки · доля отфильтрованных отзывов.

Оператору важно не «worker-3 обработал 47 записей», а «12% каталога без резюме — пользователи
видят пустые карточки».

Транспорт: SSE из `job_events` по `id > Last-Event-ID`, с heartbeat и автоматическим
fallback на polling `GET /monitoring/status` раз в 3 с. Индикатор соединения в UI:
`● Live` / `◐ Reconnecting` / `○ Polling`.

`Run now`: `POST /api/v1/admin/crawl/run` → **202** с `run_id` и ссылкой на поток,
либо **409** с `{active_run_id, started_at, phase}`. Кнопка заблокирована, пока прогон активен —
визуальное дублирование серверной защиты. Деструктивных операций нет.

---

## 18. Security

Полная модель угроз — `adr/ADR-019-security-trust-boundaries.md`. Кратко:

- **Review text — недоверенный ввод.** Prompt injection закрыт шестью независимыми мерами,
  включая проверку evidence: инъекция, подделывающая вывод, не может подделать подтверждение.
- **SSRF.** Сервер не ходит по URL из данных нигде, кроме `/img`, где: точный whitelist хоста,
  фиксированный набор ширин, запрет редиректов, отказ на приватные диапазоны, таймаут и лимит
  размера.
- **XSS.** Jinja2 с автоэкранированием; `|safe` запрещён (проверяется тестом); `bleach`
  на описаниях при сохранении; CSP `default-src 'self'`.
- **SQLi.** Только параметризованные запросы; `sort`/`order` — enum, резолвятся словарём.
- **Админ.** `X-Admin-Token`, `compare_digest`, rate limit, `409` при активном прогоне,
  лимит игр за прогон не повышается параметром запроса.
- **Секреты.** Только из окружения; маскируются в `repr` и в логах; `.env` в `.gitignore`.
- **Остаточный риск (принят осознанно):** `GET /monitoring/*` открыт — требование ТЗ;
  секретов там нет, сообщения проходят редактор; в prod рекомендован reverse-proxy auth
  на `/admin/*`.
- **Правовой контур:** дисклеймер и атрибуция на каждой странице, консервативный rate limit,
  цитаты — короткие фрагменты со ссылкой; цитата критика без рабочей ссылки не показывается;
  AI-контент помечен.

---

## 19. Testing strategy

```
        /  E2E (HTML-уровень)  \        ключевые флоу + все правила EDGE_CASES
       /------------------------\
      /  Integration (БД, API)   \      репозитории, очередь, дедупликация, эндпоинты, SSE
     /----------------------------\
    /            Unit              \     парсеры, нормализаторы, домен, скоринг,
   /--------------------------------\    выборка, валидатор, ранжирование, вердикт
```

**Железное правило: ни один тест не ходит в сеть.** Всё через фикстуры.
Отдельная опциональная сюита `@pytest.mark.contract` бьёт по реальным API и запускается
вручную или по расписанию — ранний детектор дрейфа схемы.

Ключевые обязательные тесты (полный перечень — `IMPLEMENTATION_PLAN.md`):

| Область | Что проверяется |
|---|---|
| Crawler | все 12 сценариев §10, включая «страница из одних дублей», «дата сменилась», «источник упал» |
| Дедупликация | N потоков захватывают одну игру → ровно одна строка; два активных прогона → UniqueViolation |
| Reviews | адаптивный шаг пагинации (10 и 100); clamp offset; повторный батч → 0/0; изменённый текст → 1 update |
| Snapshot | детерминированность `content_hash` при перестановке; фильтр качества; стратификация; **даты в корпусе** |
| AI | все ветви `should_regenerate`; идентичный вход → 0 вызовов; **Elden Ring: 0 негативных claims, резюме валидно**; битая ссылка → claim отброшен; temporal без дат → отброшен |
| Scores | `userScore=0` при `count<=3` → `unavailable`; `0.5` при 6382 → `valid`; сортировка не поднимает `unavailable` |
| Verdict | таблица (tier × tier × наличие × знак), границы 6/7/8 и 49/50, 69/70, 84/85 |
| Similarity | одна франшиза → высоко; разные жанры → низко; ниже порога → не публикуется; reason `null` не выдумывается |
| YouTube | трейлер никогда не побеждает; live отсеян; `no commentary` проигрывает; **пустой 200 → следующий провайдер** |
| Security | `|safe` отсутствует в шаблонах; `/img` отвергает чужой хост; админ без токена → 401; prompt injection не проходит валидатор |
| Frontend | каждое правило `EDGE_CASES.md` (28 кейсов) на уровне HTML |
| Архитектура | `services/` не импортирует httpx и sqlalchemy; нет Tailwind-селекторов; схема содержит ожидаемые констрейнты |

Фикстуры — **реальные**: `docs/research-fixtures/metacritic/` и
`docs/research-fixtures/release0/`. Мок-данные дизайна (`design/data/mock-games.json`)
используются **только** для проверки фронтенд-состояний и явно помечены как фиктивные
(их собственный `$meta.warning` это требует).

---

## 20. Deployment

```yaml
services:
  postgres:   # postgres:16-alpine, healthcheck pg_isready
  migrate:    # тот же образ, alembic upgrade head + seed платформ, restart: "no"
  api:        # uvicorn app.main:app, healthcheck GET /api/v1/health
  worker:     # python -m app.worker --queues crawl,enrich,ai
  scheduler:  # python -m app.scheduler
```

Порядок: `postgres` (healthy) → `migrate` (completed_successfully) → `api` ∥ `worker` ∥ `scheduler`.

Отдельный one-shot `migrate` — потому что при `--scale api=3` три реплики устроили бы гонку
на `alembic upgrade`.

`worker` и `scheduler` — разные контейнеры: планировщик обязан быть в единственном
экземпляре, воркеров хочется масштабировать.

Запуск:

```bash
cp .env.example .env          # заполнить ADMIN_TOKEN; при желании LLM_API_KEY, YOUTUBE_API_KEY
docker compose up -d          # -> http://localhost:8000
docker compose exec api python -m app.cli seed --limit 500   # опционально
```

`docker-compose.prod.yml` — override: порт 5432 не публикуется, ресурсные лимиты,
рекомендация reverse-proxy auth на `/admin/*`.

Локально без Docker: `DATABASE_URL=sqlite+pysqlite:///./playlens.db`, `alembic upgrade head`,
`uvicorn app.main:app`, `python -m app.worker`.

---

## 21. Observability

**Структурные логи** (JSON в stdout) с корреляцией через contextvars:
`request_id` · `crawl_run_id` · `job_id` · `game_id` · `slug` · `stage` · `status` ·
`duration_ms` · `error_code` · `worker_id`.

Каждая запись позволяет ответить: **что произошло с конкретной игрой во время конкретного
прогона.** Секреты вырезаются процессором по маске.

**Метрики** (`/metrics`, Prometheus text): `crawl_runs_total{status}` ·
`crawl_run_duration_seconds` · `games_processed_total{result}` · `jobs_total{job_type,status}` ·
`job_duration_seconds{job_type}` · `external_request_duration_seconds{host,endpoint}` ·
`external_request_total{host,status}` · `llm_tokens_total{direction,model}` ·
`llm_cost_usd_total` · `summary_claims_total{validation}` ·
`youtube_quota_units_used` · `metacritic_schema_drift_total` · `queue_depth{queue,status}`.

Стек Prometheus/Grafana в состав не входит; эндпоинт есть — подключение это правка compose,
а не кода.

**События** — `job_events`, они же лента мониторинга, они же поток SSE.

---

## 22. Performance

**Сделано сразу:**

| Проблема | Решение |
|---|---|
| Список игр требует агрегатов | Скалярные роллапы `best_*`/`lead_*` в `games`; список читается из одной таблицы |
| Фильтр по платформе/жанру | Полу-джойн `EXISTS` по индексу `(platform_id, game_id)` — без `DISTINCT` |
| Глубокий OFFSET | Keyset-пагинация по `(sort_value, id)`; offset-режим ограничен 10 000 |
| Нестабильный порядок при равных оценках | Вторичный ключ `id DESC` во всех индексах сортировки |
| N+1 на списке | Список не делает подзапросов; справочники платформ и жанров кэшируются в процессе |
| N+1 на карточке | Фиксированный набор запросов с явными `selectinload`; никакого lazy-load |
| Батчевые вставки отзывов | Один `INSERT ... VALUES (...), (...)` на батч вместо N запросов |
| Повторные вызовы LLM | `UNIQUE(input_fingerprint)` — самая дорогая операция защищена констрейнтом |
| Трафик обложек | `/img` с фиксированными ширинами, WebP и дисковым кэшем |
| Нагрузка на источник | Token bucket 2 rps + семафор 2 |
| Кэш ответов API | `Cache-Control: public, max-age=60, stale-while-revalidate=300` на списки, `max-age=300` на карточку |

**Отложено до измерений:** кэш горячих ответов, materialized view фасетов,
партиционирование `reviews` (оправдано после ~50M строк) и `job_events` (после ~10M).

**Понадобится при масштабировании:** реплика для чтения, разделение воркеров по очередям
в отдельные контейнеры, полнотекстовый движок — **только** если понадобится поиск по текстам
отзывов (для поиска по названию `pg_trgm` достаточен и на 1M строк).

---

## 23. Failure handling

| Что упало | Поведение | Итог для данных | Восстановление |
|---|---|---|---|
| Metacritic 5xx / таймаут | retry ×5 с backoff и джиттером; при исчерпании прогон `partial`, **курсор не двигается** | всё, что успели, сохранено | следующий tick повторит страницу |
| Metacritic 429 | уважаем `Retry-After`, глобально снижаем rps вдвое на 10 минут | — | автоматически |
| Metacritic недоступен | circuit breaker open 5 мин; задачи падают в retry без сетевых попыток | БД не тронута, API отдаёт прежние данные | half-open проба |
| Схема изменилась | `SchemaDriftError` (permanent) → игра `failed`, событие, метрика; при превышении порога — авто-фолбэк на `HtmlSource` | остальные игры обрабатываются | правка парсера; contract-тест предупреждает заранее |
| LLM 429/5xx/таймаут | retry ×6, 60→600 с | **Game=SUCCESS, Reviews=SUCCESS, Summary=RETRY** | автоматически |
| LLM недоступен долго | circuit open; задачи копятся в очереди `ai` | прежнее `is_current` резюме продолжает отдаваться | автоматически |
| Резюме отвергнуто валидатором | `status='rejected'`, предыдущее валидное остаётся текущим | пользователь видит прежнее корректное резюме | следующий содержательно изменившийся вход |
| Отзывы не загрузились | `reviews.sync` retry → `failed`; `game.sync` уже `succeeded` | **Game=SUCCESS, Reviews=FAILED** | reaper / следующий день |
| YouTube квота исчерпана | `BudgetExhausted` → перенос на следующие сутки | **данные игры не считаются failed** | автоматически |
| Транскрипт недоступен | каскад → `metadata_only` → `unavailable` | ссылка на ролик всё равно сохранена | периодический повтор для `unavailable` старше 14 дней |
| Воркер упал | аренда истекает → reaper возвращает задачу | работа переигрывается **идемпотентно** | ≤5 мин |
| Планировщик упал | tick пропущен; `idempotency_key` не даст дубля при возврате | ничего не потеряно | следующий час |
| Приложение перезапущено | состояние в `crawl_days` и `jobs` | ничего не потеряно | автоматически |
| БД перезапущена | пул пересоздаёт соединения; задачи падают в retry; `/health` отдаёт 503 | транзакции атомарны, частично записанных игр не бывает | автоматически |
| Два прогона одновременно | три барьера | дублей нет | — |

**Границы транзакций:**

| Операция | Транзакция | Почему |
|---|---|---|
| Игра + платформы + жанры + компании + роллапы | **одна** | иначе возможна игра без платформ или роллапы, не соответствующие данным |
| Батч отзывов | одна на батч | батчи независимы, частичный успех полезен |
| Новое резюме + снятие `is_current` со старого | **одна** | иначе два текущих или ни одного |
| `similar_games`: DELETE + INSERT | **одна** | иначе окно, когда похожих нет вовсе |
| Курсор `crawl_days` | отдельная, **после** успешного захвата | курсор не должен двигаться при неудаче |
| Захват `crawl_items` | автокоммит одиночного INSERT | захват обязан быть виден другим воркерам немедленно |
| `job_events` | автокоммит **вне** бизнес-транзакции | иначе откат бизнес-логики стирает запись о том, что она провалилась |

---

## 24. Data retention

| Данные | Срок | Механизм |
|---|---|---|
| `job_events` | `EVENTS_RETENTION_DAYS` = 30 | задача обслуживания, ежедневно |
| `jobs` (терминальные) | `JOBS_RETENTION_DAYS` = 90 | то же |
| `crawl_items` | `CRAWL_ITEMS_RETENTION_DAYS` = 180 | то же |
| `review_snapshots` без ссылающихся резюме | `SNAPSHOT_RETENTION_DAYS` = 90 | то же |
| Не-текущие версии резюме | хранятся; чистятся при превышении `SUMMARY_HISTORY_KEEP` = 10 на пару | то же |
| `reviews` | не удаляются | доказательная база продукта |
| `games`, `game_platforms` | не удаляются | исчезнувшая платформа перестаёт обновляться, но остаётся |
| Кэш `/img` | LRU по лимиту размера | при обращении |

---

## 25. External dependencies

| Зависимость | Роль | Обязательна | Что при отказе | Абстракция |
|---|---|---|---|---|
| `backend.metacritic.com` | основной источник | да | прогон `partial`, данные целы | `MetacriticSource` |
| `www.metacritic.com` | HTML-fallback, оригиналы обложек | нет | fallback недоступен → остаётся JSON API | `MetacriticSource` |
| Anthropic API | генерация резюме | **нет** | резюме `pending`; сервис полностью работоспособен | `LLMProvider` |
| YouTube Data API | Bonus 1 | **нет** | секция отсутствует | `YouTubeProvider` |
| Провайдеры транскриптов | Bonus 1 | **нет** | `metadata_only` или отсутствие секции | `TranscriptProvider` |
| PostgreSQL | состояние | да | сервис отдаёт 503 на `/health` | — |

Ни один внешний сервис не является предусловием для отдачи уже собранных данных.

---

## 26. Risks

| # | Риск | Вероятность | Влияние | Митигация |
|---|---|---|---|---|
| R-1 | Внутренний API закроют или начнут требовать подпись | средняя | **критическое** | `HtmlSource` реализован и покрыт тестами с первого дня; авто-фолбэк по порогу дрейфа; contract-тесты; sitemap как независимый перечень |
| R-2 | Anti-bot с датацентрового IP | средняя | высокое | 2 rps, честный UA, `Retry-After`, circuit breaker; `BrowserSource` — описанная точка расширения; **OQ-V2** |
| R-3 | Изменение структуры ответов | **высокая** | среднее | `extra='ignore'`; обязательные поля объявлены явно; парсеры изолированы и покрыты фикстурами; никаких Tailwind-селекторов |
| R-4 | Недетерминированная пагинация (подтверждён) | подтверждён | среднее | дубли бесплатны; курсор монотонный; weekly sitemap-сверка |
| R-5 | Транскрипт YouTube недоступен (подтверждён) | подтверждён | низкое | каскад + `metadata_only`; секция удаляется; статус виден |
| R-6 | Стоимость LLM выходит из-под контроля | средняя | среднее | `UNIQUE(input_fingerprint)`; стратифицированная выборка; дневной лимит; отложенные задачи вместо падения |
| R-7 | AI-резюме галлюцинирует | средняя | **высокое** | evidence обязательны; машинная валидация; отброшенные claims сохраняются; `rejected` не публикуется; предыдущее валидное остаётся |
| R-8 | Похожие игры не кажутся похожими | **высокая** | среднее | порог; честное пустое состояние; `components` для разбора; PV3 — **OQ-N2** |
| R-9 | Собственный код очереди содержит ошибку | средняя | высокое | малый объём, покрытый тестами на конкурентность, аренду, исчерпание попыток и идемпотентность |
| R-10 | «Работает на SQLite, ломается на PostgreSQL» | средняя | высокое | никакого сырого диалектного SQL вне двух функций; CI-джоб против `postgres:16`; **OQ-V1** |
| R-11 | Каталог мал в момент показа | высокая | высокое | seed-обход (ADR-014) — часть процедуры первого запуска |
| R-12 | Юридические/ToS-претензии | низкая | высокое | дисклеймер, атрибуция, вежливый краулинг, scrape выключен, готовность отключить; **OQ-B3** |
| R-13 | Разрастание БД | средняя | среднее | кэпы отзывов, ретенция, план партиционирования |
| R-14 | Review-bombing искажает резюме | средняя | среднее | фильтр согласованности; распределение оценок в UI; аномалия отмечается в резюме |
| R-15 | Ошибочный выбор Let's Play | средняя | низкое | три барьера, порог совпадения названия, фикстурная сюита, `rejected_reason` |
| R-16 | Отсутствие браузерных E2E | подтверждён | среднее | структурные HTML-тесты на все 28 кейсов `EDGE_CASES.md`; **OQ-N3** |

---

## 27. Open issues

Полный реестр — `OPEN_QUESTIONS.md`. Блокирующие:

- **OQ-B1** — исходного файла ТЗ нет в репозитории; требования реконструированы.
- **OQ-B2** — нет ключа YouTube Data API; Bonus 1 не измерен на живых данных.
- **OQ-B3** — правовой режим эксплуатации (внутренняя демонстрация / публичный доступ).
- **OQ-B4** — нет ключа Anthropic API; production-качество резюме не измерено.

---

## 28. Acceptance criteria

Проект считается выполненным, когда **все** пункты ниже проверены.

### Функциональные

| # | Критерий | Как проверяется |
|---|---|---|
| A-1 | Часовой прогон захватывает **не более 20** новых игр за раз и не обрабатывает игру дважды за день | интеграционный тест + `crawl_runs.games_skipped_dupe` |
| A-2 | Повторный прогон в тот же день добавляет 0 строк в `games` | интеграционный тест |
| A-3 | Смена даты запускает новый цикл; курсор browse переживает рестарт | тест с подменёнными часами |
| A-4 | Для игры сохранены title, cover, platforms, Metascore, Userscore, developer, description, video | тест на фикстуре `composer_elden_ring.json` |
| A-5 | Оценки хранятся отдельно по платформам; Cyberpunk даёт 5 разных строк | тест на фикстуре |
| A-6 | Отзывы критиков и игроков собраны раздельно, повторный батч не создаёт дублей | интеграционный тест |
| A-7 | Резюме содержит только утверждения с валидными evidence; Elden Ring даёт **0** негативных claims и остаётся валидным | тест на корпусе Release 0 |
| A-8 | `userScore: 0` при 2 оценках рендерится как «Not rated», а не как 0 | unit + HTML-тест |
| A-9 | Похожие игры берутся только из БД, кликабельны, ниже порога не показываются, reason не выдумывается | интеграционный + HTML-тест |
| A-10 | Каталог: поиск, фильтр по платформе, 5 сортировок, пагинация, состояние в URL | интеграционный тест |
| A-11 | Страница игры содержит все элементы ТЗ §8 | HTML-тест |
| A-12 | Все 28 кейсов `EDGE_CASES.md` рендерятся как предписано | HTML-тесты |
| A-13 | Мониторинг показывает все показатели ТЗ Bonus 2 и обновляется без перезагрузки | интеграционный + SSE-тест |
| A-14 | `Run now` при активном прогоне даёт 409 и не создаёт второй прогон | интеграционный тест |
| A-15 | Bonus 1: трейлер не выбирается; пустой HTTP 200 → следующий провайдер; нет ролика → нет секции | unit + HTML-тест |

### Нефункциональные

| # | Критерий | Как проверяется |
|---|---|---|
| A-16 | Отказ LLM не влияет на игру, отзывы и каталог | интеграционный тест с падающим провайдером |
| A-17 | Отказ YouTube не влияет ни на что, кроме своей секции | то же |
| A-18 | Недоступность источника оставляет курсор на месте и данные целыми | тест |
| A-19 | Конкурентный захват одной игры N потоками → ровно одна строка | тест с реальным параллелизмом |
| A-20 | Идентичный вход не приводит к вызову LLM | тест со счётчиком на провайдере |
| A-21 | `|safe` отсутствует в шаблонах; `/img` отвергает чужой хост; админ без токена → 401 | тесты безопасности |
| A-22 | `services/` не импортирует `httpx`/`sqlalchemy`; в шаблонах и парсерах нет Tailwind-селекторов | архитектурный тест |
| A-23 | Схема содержит полный ожидаемый набор уникальных индексов и CHECK | тест сверки схемы |
| A-24 | `alembic upgrade head` → `downgrade base` → `upgrade head` проходит | тест миграций |
| A-25 | `ruff check` и `mypy` проходят | CI |
| A-26 | Все требования ТЗ имеют статус в `REQUIREMENTS_TRACEABILITY.md`, ни одно не `PLANNED` без причины | ревью |
| A-27 | `docker compose up -d` после заполнения `.env` поднимает работающий сервис | ручная проверка (**OQ-V1**, Docker в среде разработки отсутствует) |
