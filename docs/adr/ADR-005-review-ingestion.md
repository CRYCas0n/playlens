# ADR-005 — Сбор отзывов: адаптивная пагинация, дедупликация, бюджет платформ

**Статус:** принято · 2026-09-06
**Отменяет:** `BLUEPRINT.md §2.1.2` в части «`limit` до 500 работает» и §9.2 в части «страницы по 100»
**Связанные противоречия:** C-01, C-04

## Context

- `BLUEPRINT.md §2.1.2` утверждал, что `limit` до 500 работает для обоих типов отзывов.
- `RELEASE0_VALIDATION.md §5.6` перепроверил: **critic-эндпоинт отдаёт ровно 10 записей при
  любом `limit`**; `offset`-пагинация работает и даёт различные записи (4 страницы = 40 без
  дублей). Для user-отзывов `limit` работает.
- `offset` за пределами `totalResults` → **HTTP 500** (`Cannot read properties of undefined`).
- У critic-отзыва **нет `id`**; `url` и `author` часто пустые строки.
- У user-отзыва есть стабильный UUID и поле `version` (epoch ms — версия текста).
- `sort=date` **не строго монотонна** (первые записи: 2026-09-04, 2026-08-30, 2026-08-29,
  2026-07-15, 2026-08-25).
- `reviewCount` в stats больше `totalResults` списка (4384 против 1790) — в агрегат входят
  оценки **без текста**.
- Отзывы привязаны к паре (игра, платформа); `?platform=` у composer игнорируется.

## Decision

### 1. Пагинатор не доверяет ни `limit`, ни константе

```python
def paginate(fetch, *, requested_limit, cap, total_hint=None):
    offset, step, seen = 0, requested_limit, 0
    while seen < cap:
        if total_hint is not None and offset >= total_hint:   # clamp: иначе HTTP 500
            break
        page = fetch(offset=offset, limit=requested_limit)
        if not page.items:
            break
        step = len(page.items)          # <-- фактический размер страницы
        yield page
        seen += step
        offset += step
        if total_hint is None:
            total_hint = page.total_results
```

Шаг берётся из **фактической длины первой страницы**, а не из запрошенного `limit`.
Это корректно и для критиков (10), и для пользователей (100), и останется корректным,
если Metacritic завтра изменит поведение в любую сторону. `offset` всегда клампится
по `totalResults` — прямая защита от известного HTTP 500.

### 2. Ключ дедупликации

| Тип | `dedupe_key` | Обоснование |
|---|---|---|
| user | `source_review_id` (UUID из API) | Гарантированно уникален источником |
| critic | `sha256(publication_slug + '|' + published_on + '|' + body_hash)` | `id` нет; `url` и `author` часто пусты; связка «издание + дата + текст» уникальна на практике |

`UNIQUE(game_platform_id, kind, dedupe_key)`. Обновление только при реальном изменении:

```sql
ON CONFLICT (game_platform_id, kind, dedupe_key) DO UPDATE
SET body = EXCLUDED.body, body_hash = EXCLUDED.body_hash, score = EXCLUDED.score,
    thumbs_up = EXCLUDED.thumbs_up, thumbs_down = EXCLUDED.thumbs_down,
    url = COALESCE(NULLIF(EXCLUDED.url, ''), reviews.url),
    source_version = EXCLUDED.source_version, updated_at = now()
WHERE reviews.body_hash      IS DISTINCT FROM EXCLUDED.body_hash
   OR reviews.source_version IS DISTINCT FROM EXCLUDED.source_version
```

Условие в `WHERE` — не оптимизация: без него `updated_at` шумит на каждом прогоне,
и «сколько отзывов реально новых» (вход для решения о перегенерации резюме) считается неверно.

**Различение вставки и обновления без `xmax`** (портируемость, ADR-018): батч выполняется
как `INSERT ... ON CONFLICT DO NOTHING RETURNING id` (даёт число вставок), затем отдельным
`UPDATE ... WHERE ... IS DISTINCT FROM ... RETURNING id` (даёт число обновлений).
Две операции в одной транзакции; результат тот же, что у `xmax = 0`, и работает в обоих диалектах.

### 3. Инкрементальная докачка с допуском

`sort=date` не монотонна, поэтому «идём, пока не встретим известный отзыв» неверно:

```python
stale = 0
for page in paginate(...):
    r = repo.upsert_reviews(page.items)
    if r.inserted == 0 and r.updated == 0:
        stale += 1
        if stale >= REVIEWS_STALE_PAGE_TOLERANCE:   # 2
            break
    else:
        stale = 0
```

Раз в `REVIEWS_FULL_RESYNC_DAYS` (30) — полная пересинхронизация игры.

### 4. Бюджет платформ и объёма

Игра с 5 платформами — это 10 потоков отзывов. Ограничения:

- всегда lead-платформа;
- плюс платформы с `critic_review_count >= REVIEWS_MIN_PLATFORM_CRITICS` (3)
  **или** `user_review_count >= 50`;
- максимум `REVIEWS_MAX_PLATFORMS` (3) по сумме отзывов;
- `REVIEWS_CRITIC_CAP = 200`, `REVIEWS_USER_CAP = 500` на пару (игра, платформа).

Кэпы — осознанный компромисс (`BLUEPRINT §28 Q6`): 200 рецензий критиков — это практически
весь корпус даже у Zelda (150); 500 пользовательских отзывов, отобранных стратифицированно,
репрезентативны для суммаризации и достаточны для UI, который ленты отзывов не показывает.

### 5. Что записывается всегда

`user_ratings_total` (из stats, включает оценки без текста) и `user_reviews_with_text`
(`totalResults` списка) хранятся **раздельно**. Provenance в UI обязан показывать оба:
«Compressed from 1 790 player reviews with text, of 4 384 ratings» — иначе продукт вводит
в заблуждение (`PRODUCT_BLUEPRINT §4 Journey 8`).

## Alternatives considered

| Вариант | Почему не выбран |
|---|---|
| Захардкодить `CRITIC_PAGE_SIZE = 10` | Верно сегодня, ломается молча при изменении на стороне источника. Адаптивный шаг стоит две строки |
| Собирать все отзывы без кэпов | Elden Ring PS5 — 24 375 оценок и 4 906 текстов только на одной платформе. Объём БД и время обхода растут без выигрыша для резюме |
| Дедупликация критиков по `url` | `url` часто пустая строка. По `author` — тоже часто пуст |
| Хранить только нормализованный `score` | Теряется исходная шкала. Храним `score` + `score_max`, нормализация — вычисляемое поле |
| Инкремент «до первого известного отзыва» | `sort=date` не монотонна — доказано. Пропустит новые отзывы |

## Consequences

- Стоимость сбора Zelda (150 критиков): 15 запросов вместо ожидавшегося одного. Это учтено
  в бюджете обхода и в SLO (`game.sync` < 30 с при 2 rps).
- Повторный запуск синхронизации не создаёт дублей и не трогает неизменившиеся строки
  (Rule 6 ТЗ §33) — проверяется интеграционным тестом «второй батч: inserted=0, updated=0».
- Отзывы висят на `game_platforms`, а не на `games` — так их отдаёт источник, и так они нужны
  для per-platform резюме.

## Risks

| Риск | Митигация |
|---|---|
| Издание переиздаёт рецензию с правкой текста → появится вторая запись | Приемлемо и лучше, чем потерять отзыв. Дубликаты по смыслу отсекаются фильтром корпуса (ADR-007) |
| Кэп 500 обрезает мнение по свежим отзывам | Выборка стратифицирована по тональности **и по датам**, сортировка `sort=date`; полная пересинхронизация раз в 30 дней |
| `totalResults` соврал, `offset` вышел за границу | Clamp + отдельная обработка HTTP 500 как retryable с уменьшением offset |
