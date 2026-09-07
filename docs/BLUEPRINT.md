# Metacritic Games Intelligence Service — Technical Blueprint

**Версия:** 1.0
**Дата:** 2026-09-05
**Статус:** DESIGN — ожидает подтверждения заказчика, реализация не начата
**Автор:** архитектурная проработка по ТЗ

---

## 1. Executive Summary

### Что строим

Сервис, который автономно поддерживает собственную реплику каталога видеоигр Metacritic, обогащает её AI-аналитикой отзывов и данными YouTube, и отдаёт всё это через веб-каталог с поиском, фильтрацией и страницей real-time мониторинга.

Четыре независимых контура:

| Контур | Что делает | Деградирует до |
|---|---|---|
| **Ingest** | Ежечасно забирает новые игры с Metacritic (New Releases → browse-страницы), нормализует, upsert'ит в PostgreSQL | Если Metacritic лежит — run завершается `partial`, курсор не двигается, данные в БД целы |
| **Enrich** | Отзывы критиков/пользователей → AI-резюме; YouTube Let's Play → транскрипт → AI-резюме; похожие игры | Каждый шаг — отдельная job; падение любого не влияет на основные данные игры |
| **Serve** | REST API + Next.js фронтенд: список, карточка, похожие, мониторинг | Читает только БД, полностью независим от воркеров |
| **Observe** | Журнал runs/jobs/events, SSE-поток, ручной запуск | Работает даже когда воркеры мертвы (покажет это) |

### Ключевые архитектурные решения (с обоснованием — детали в §4 и §35-сравнениях)

1. **Metacritic забираем через его собственный внутренний JSON API** (`backend.metacritic.com`), а не парсингом HTML. Это подтверждено экспериментально (§2) и радикально меняет надёжность проекта: вместо хрупких CSS-селекторов по Tailwind-классам мы получаем типизированный JSON. **Риск явно обозначен** — API неофициальный, версионирования нет (§26).
2. **HTML-парсер сохраняем как второй адаптер за тем же интерфейсом** — на случай, если внутренний API закроют. JSON-LD `VideoGame` на страницах игр даёт устойчивый fallback.
3. **Дедупликация — на уровне БД, а не приложения.** Таблица `crawl_items` с `UNIQUE (crawl_date, game_slug)` + `INSERT ... ON CONFLICT DO NOTHING RETURNING id` — атомарный «захват» игры на день. Ни Redis-лок, ни логика в коде не нужны для корректности; они нужны только для эффективности.
4. **Похожие игры — гибрид:** детерминированный metadata-скор (жанры/платформы/разработчик/франшиза/эпоха/близость оценок) + опциональный embedding-скор поверх «similarity document». Embeddings считаем **локально** (BGE-small через ONNX) — ноль внешних зависимостей и ноль стоимости.
5. **YouTube-транскрипты — самая ненадёжная часть проекта, и это проверено эмпирически.** Классический путь через `timedtext` сейчас возвращает **HTTP 200 с нулевым телом** (§2.2). Проектируем каскад из 4 провайдеров с явной пометкой `transcript_source` в БД и graceful-degradation до `metadata_only`.
6. **SSE, не WebSocket** для мониторинга — поток односторонний, `EventSource` умеет reconnect из коробки, не требует протокольного апгрейда в прокси.
7. **Никаких микросервисов.** Один Python-монорепозиторий, 6 контейнеров, PostgreSQL + Redis. Всё, что можно сделать в БД — делается в БД.

---

## 2. Findings from Research

> Всё ниже — результат фактических HTTP-запросов, выполненных 2026-09-05. Каждое утверждение либо подтверждено ответом сервера, либо помечено `NEEDS VALIDATION`.

### 2.1 Metacritic

#### 2.1.1 Сайт — Nuxt 3 SPA под Cloudflare, HTML непригоден для стабильного парсинга

- `https://www.metacritic.com/game/` отдаёт **969 KB** HTML, SSR-рендер Nuxt.
- **`__NEXT_DATA__` нет, JSON-LD на хабе нет.** Есть `window.__NUXT__` c devalue-сериализованным payload (плоский массив со ссылками по индексам) — парсить его напрямую крайне неудобно.
- CSS-классы — сгенерированные Tailwind-утилиты (`shrink-0 grow-0 p-4 rounded-lg shadow-[0_0.125rem_1rem_rgba(0,0,0,0.08)]`). Селекторы по ним сломаются при первом же редизайне.
- Единственные полустабильные якоря в HTML — атрибуты `data-testid`: `product-card`, `product-card-content`, `filter-results`, `product-image`, `product-score`, `global-score-header`, `global-score-review-count`.
- На **страницах отдельных игр** есть ровно один блок `application/ld+json` со схемой `VideoGame` (name, datePublished, description, …) — это пригодный fallback.

**Bot-detection на `www`:** UA `python-requests/2.31` → **HTTP 403**. UA обычного браузера или дефолтный `curl/*` → 200. То есть простейший фильтр по User-Agent есть, но никакой JS-challenge для этих страниц не требуется.

#### 2.1.2 Внутренний JSON API — главная находка

Из конфигурации Nuxt в HTML извлекаются:

```
apiBaseHost : backend.metacritic.com
apiKey      : 1MOZgmNFxvmljaQR1X9KAij9Mo4xAY3u
fastlyApiUrl: https://www.metacritic.com/a/img
```

Проверено экспериментально:

| Проверка | Результат |
|---|---|
| Запрос **без** `apiKey` | **HTTP 200** — ключ не валидируется |
| Запрос с **неверным** `apiKey` | **HTTP 200** — ключ не валидируется |
| Дефолтный UA `curl/*` на `backend.metacritic.com` | **HTTP 200** — UA не проверяется |
| 20 параллельных запросов (10 потоков) деталей игр | 20×200 за **2.26 с**, ни одного 429 |
| Заголовки ответа | `Server: cloudflare`, `CF-Cache-Status: HIT/EXPIRED`, есть `Age` |

**Вывод:** это публично доступный, кэшируемый Cloudflare'ом read-only API. Rate limiting на наблюдаемых объёмах не проявился. Тем не менее — см. §26, риск R1.

##### Карта эндпоинтов (все подтверждены 200 + разобранной структурой)

| Назначение | Endpoint |
|---|---|
| **Листинг / поиск / New Releases** | `GET /finder/metacritic/web` |
| Словарь фильтров | `GET /finder/metacritic/filters/games/web` |
| **Полная страница игры** (всё в одном вызове) | `GET /composer/metacritic/pages/games/{slug}/web` |
| Только product-блок | `GET /games/metacritic/{slug}/web` |
| **Отзывы критиков (по платформе)** | `GET /reviews/metacritic/critic/games/{slug}/platform/{platformSlug}/web` |
| **Отзывы пользователей (по платформе)** | `GET /reviews/metacritic/user/games/{slug}/platform/{platformSlug}/web` |
| **Агрегат Metascore по платформе** | `GET /reviews/metacritic/critic/games/{slug}/platform/{platformSlug}/stats/web` |
| **Агрегат Userscore по платформе** | `GET /reviews/metacritic/user/games/{slug}/platform/{platformSlug}/stats/web` |

##### «New Releases» — точный запрос

Раздел New Releases на `https://www.metacritic.com/game/` соответствует **буквально** такому вызову (проверено: первые 20 slug'ов в JSON совпали 1:1 и в том же порядке с 20 карточками в HTML):

```
GET https://backend.metacritic.com/finder/metacritic/web
      ?componentName=new-releases-carousel
      &componentType=ProductList
      &sortBy=-releaseDate
      &metaScoreMin=1          <-- только игры, у которых УЖЕ есть Metascore
      &mcoTypeId=13            <-- 13 = game-title
      &offset=0&limit=20
      &apiKey={KEY}
```

`totalResults` = **18 524**.

> **Расхождение с ТЗ №1.** ТЗ подразумевает, что New Releases «обновляется каждый день». Фактически это стабильный список, отсортированный по `releaseDate` с фильтром `metaScoreMin=1`. Он меняется только когда выходит новая игра с оценкой — а это единицы в день. Значит «каждый новый день выбор начинается заново» приведёт к тому, что **18 из 20 игр будут теми же, что вчера**. Механизм дедупликации — не оптимизация, а обязательное требование, иначе сервис будет каждый день гонять LLM по одним и тем же играм. Решение: дневной «захват» в `crawl_items` + дифференциальное обновление (§7, §18).

Соседние вкладки того же карусельного компонента (обнаружены в payload): `Top Critics' Picks` (`sortBy=-metaScore`), `Most Popular`.

##### Страница SEE ALL / New

`https://www.metacritic.com/browse/game/all/all/all-time/new/` — пагинация через `?page=N` (проверено: `page=2` и `page=500` возвращают 200 и разные наборы). Грид — 24 карточки на страницу. Backend-эквивалент:

```
GET /finder/metacritic/web?sortBy=-releaseDate&productType=games&offset={(page-1)*24}&limit=24
```
`totalResults` = **177 882**.

> **Расхождение с ТЗ №2 — критичное.** Порядок выдачи `finder` при равных `releaseDate` **недетерминирован**. Два идентичных запроса, выполненных подряд при cache MISS, вернули наборы, различающиеся **12 slug'ами из 24**. При `CF-Cache-Status: HIT` результат стабилен (8 из 8 запросов идентичны), но TTL кэша конечен.
>
> Причина, вероятно, — отсутствие вторичного tie-breaker'а в поисковом бэкенде (ElasticSearch-подобном) при большом числе игр с одинаковой датой релиза.
>
> **Следствия для дизайна:**
> 1. Offset-пагинация **может и пропускать, и дублировать** записи. Наивный «курсор страницы» не даёт гарантии полноты.
> 2. Дубликаты обязаны быть бесплатными → `ON CONFLICT DO NOTHING`.
> 3. Нужен независимый источник сверки → **sitemap** (ниже).

##### Sitemap как reconciliation-источник

`https://www.metacritic.com/games.xml` → sitemapindex, **296 шардов**, `https://www.metacritic.com/games/{1..296}.xml`, по **1000 URL** в шарде ≈ 296 000 страниц игр. `<changefreq>weekly</changefreq>`, **`<lastmod>` отсутствует**. Это полный, детерминированный перечень slug'ов — идеальный источник для еженедельной сверки «что мы пропустили» (§7.6).

##### Структура `composer/.../pages/games/{slug}/web`

Один вызов возвращает 9 компонентов: `product`, `related-news`, `related-carousel`, `where-to-buy`, `critic-score-summary`, `critic-reviews`, `user-score-summary`, `user-reviews`, `game-stats`.

Подтверждённый маппинг на требования ТЗ §3:

| Требование ТЗ | Путь в JSON | Комментарий |
|---|---|---|
| название | `product.title` | |
| URL Metacritic | `"/game/" + product.slug + "/"` | |
| обложка | `product.images[typeName=mainImage].bucketPath` | см. ниже про URL |
| описание | `product.description` | 367–948 симв. на проверенных играх |
| developer | `product.production.companies[typeName='Developer'].name` | издатель — там же с другим `typeName` |
| ссылка на видео | `product.video.embedUrl` / `manifestUrl` | **JW Player**, не YouTube; часто `null` |
| платформы | `product.platforms[]` — `{id, name, slug, releaseDate, relatedGameId, isLeadPlatform}` | |
| Metascore по платформе | `product.platforms[].criticScoreSummary.score` | **есть в том же вызове** |
| Userscore по платформе | ❌ нет — отдельный вызов `/user/.../platform/{slug}/stats/web` | |
| жанры | `product.genres[].name` | |
| франшиза | `product.gameTaxonomy.franchises[]`, `.family`, `.title`, `.game` | 4-уровневая таксономия |
| ESRB | `product.rating` | |

Проверка per-platform оценок на Cyberpunk 2077 — расхождения реальные и значимые:

| Платформа | Metascore | Userscore | n(user) |
|---|---:|---:|---:|
| pc | 86 | 7.3 | 40 140 |
| xbox-one | 61 | 5.1 | 4 384 |
| playstation-4 | 57 | 3.8 | 10 876 |
| xbox-series-x | 87 | 7.7 | 641 |
| playstation-5 | 75 | 8.0 | 1 529 |

**Это доказывает, что нормализованная модель `game_platforms` (ТЗ §3) обязательна, а не «хорошо бы».**

> **Расхождение с ТЗ №3.** Query-параметр `?platform=` **игнорируется** и композером, и HTML-страницей игры (проверено на 5 платформах Elden Ring — везде вернулся lead-platform PlayStation 5). Единственный путь к per-platform данным — эндпоинты `/platform/{slug}/stats/web` и `/platform/{slug}/web`. Соответственно, полный сбор игры с N платформами = `1 + 2N` HTTP-запросов минимум.

##### Изображения

`product.images[].bucketPath` = `/provider/6/12/6-1-824956-52.jpg`.

| URL-форма | Результат |
|---|---|
| `https://www.metacritic.com/a/img/catalog{bucketPath}` | **200, image/jpeg, 142 KB — оригинал** |
| `.../a/img/resize/{валидный hash}/catalog{bucketPath}?width=96&height=144` | 200, 3 KB |
| `.../a/img/resize/deadbeef/catalog{bucketPath}?...` | **403** |

Ресайз подписан HMAC (ключ `fastlyApiKey` присутствует в конфиге, но **воспроизводить чужую подпись мы не будем — это выход за рамки допустимого использования**). Решение: храним **оригинальный неподписанный URL**, ресайз делаем на своей стороне через `next/image` (§16). Это дополнительный аргумент в пользу Next.js.

##### Отзывы

```
GET /reviews/metacritic/{critic|user}/games/{slug}/platform/{platform}/web
    ?offset=0&limit=50&filterBySentiment={all|positive|neutral|negative}&sort={date|score}
```

- `limit` до **500** работает (проверено 50/100/200/500).
- `offset` за пределами `totalResults` → **HTTP 500** (`Cannot read properties of undefined (reading 'total')`). Обязателен clamp на клиенте.
- **User-отзыв имеет стабильный `id` (UUID)** → идеальный natural key для дедупа. Плюс `author`, `score` (0–10), `date`, `quote`, `spoiler`, `version` (epoch ms — версия текста!), `thumbsUp/Down` (наблюдались `null`).
- **Critic-отзыв `id` НЕ имеет.** Поля: `quote`, `score` (0–100), `url` (**часто пустая строка**), `date`, `author` (**часто пустая строка**), `publicationName`, `publicationSlug`, `platform`. → нужен составной ключ (§9).
- `filterBySentiment` работает корректно (positive 9136 / negative 4705 / neutral 1437 для CP2077 PC).
- **`sort=date` НЕ строго монотонна.** Первые 5 записей: `2026-09-04, 2026-08-30, 2026-08-29, 2026-07-15, 2026-08-25`. → инкрементальная докачка «до первого известного отзыва» ненадёжна; нужен допуск в K подряд идущих страниц без новых записей (§9.3).
- `reviewCount` в `stats` (4384 для xbox-one) > `totalResults` в списке (1790) — **в агрегат входят оценки без текста**. Это не баг, это надо учесть в UI и в промптах.

##### `related-carousel` — не является «похожими играми»

`links.self.href` раскрывает реализацию: `finder/...?sortBy=-metaScore&genres=Action+RPG&limit=24`. Это просто «топ по метаскору в том же жанре». Для Elden Ring выдаёт Hades II, Diablo, Mass Effect 3. **Собственный алгоритм похожести (ТЗ §6) действительно необходим.**

#### 2.1.3 robots.txt и правовой контур

```
User-agent: *
Disallow: /search      Disallow: /signup     Disallow: /login
Disallow: /user        Disallow: /jl/        Disallow: /8264/   Disallow: /7336/
```
Плюс полный `Disallow: /` для 14 именованных ботов (AhrefsBot, CCBot, **GPTBot**, **OAI-SearchBot**, SemrushBot, DataForSeoBot, …).

**Наши целевые пути `/game/*` и `/browse/game/*` НЕ запрещены.** `Crawl-delay` не задан.

Что это значит практически:
- Формального запрета robots.txt на наши пути нет.
- Но политика сайта явно недружелюбна к AI-скрейперам, а `backend.metacritic.com` вообще не покрыт robots.txt (это другой хост).
- **Metacritic ToS (Fandom) запрещает автоматизированный сбор данных.** Проект — учебный/тестовый; в README обязана быть явная дисклеймер-секция, а в `.env` — консервативные дефолты (§21, §26/R1).

**Обязательные меры вежливости, зашитые в конфиг:** идентифицируемый User-Agent с контактом, ≤2 rps на хост, ≤2 параллельных соединения, экспоненциальный backoff, уважение `Retry-After`, ночное окно не требуется, кэширование ответов на диск в dev.

#### 2.1.4 Прочие наблюдения

- Описания в ответе `finder` **не соответствуют играм** (у «Water Margin Heroes» описание баскетбольной игры). Описание берём **только** из `composer`/`product`, никогда из листинга.
- `finder` возвращает `criticScoreSummary` и `userScore` прямо в листинге — это дешёвый сигнал «изменилось ли что-то», без захода на страницу игры.
- Платформы в фильтрах — числовые `id` вида `1500000128`; человекочитаемые `slug`/`name` приходят в `product.platforms[]`. Справочник платформ наполняем инкрементально из встреченных игр + сидим известными.

### 2.2 YouTube

#### 2.2.1 Поиск

**Официальный YouTube Data API v3** (подтверждено актуальными источниками, сент. 2026):
- Дефолтная квота — **10 000 units/сутки** на проект.
- `search.list` = **100 units**. → **не более ~100 поисков в сутки.**
- `videos.list` = **1 unit** и принимает до **50 id за вызов** — то есть метаданные почти бесплатны.

Это жёсткий бюджет, определяющий дизайн (§12): **один `search.list` на игру за всё время жизни**, результат кэшируется навсегда; повторный поиск — только по явному запросу или по расписанию раз в N дней для игр без найденного видео.

**Скрейпинг выдачи** (проверено с этой машины): `GET https://www.youtube.com/results?search_query=...&sp=EgIQAQ%3D%3D` → 200, 1.2 MB, внутри `ytInitialData` с 20 объектами `videoRenderer`. Извлекаются: `videoId`, `title`, `ownerText` (канал), `lengthText`, `viewCountText`, `publishedTimeText`, `badges`.

Проблемы скрейпинга, зафиксированные фактически:
- Ответ **локализован по IP**: `viewCountText` пришёл как `"1 450 892 просмотра"`, `publishedTimeText` — `"4 года назад"`. Парсинг чисел становится locale-зависимым. Лечится `&hl=en&gl=US` + `Accept-Language`, но это не гарантия.
- Длительность — строка `"2:01:42"`, а не ISO-8601.
- Нет `liveBroadcastContent`, `defaultAudioLanguage`, `likeCount`, `caption`-флага.
- С датацентровых IP YouTube регулярно отдаёт consent-стену/капчу.

**Вывод:** API — первичный источник, скрейпинг — аварийный fallback при исчерпании квоты, с явной пометкой `source=scrape` и понижением доверия к рангу.

#### 2.2.2 Транскрипты — проверено, «бесплатно и без ограничений» больше не работает

Эмпирический тест (residential IP, реальный браузерный UA), видео `hPn9tHDzcv4` (jacksepticeye, Elden Ring Full Game):

1. `GET /watch?v=...` → 200, 1.39 MB. В HTML **есть** `captionTracks` с дорожкой `en` / `kind: asr`.
2. `baseUrl` дорожки содержит валидную серверную подпись: `signature=F127C272…`, `expire=1788656160`, `sparams=…`.
3. **Запрос по этому `baseUrl` → HTTP 200, длина тела = 0 байт.**
4. То же с `&fmt=json3`, `&fmt=srv3`, `&fmt=vtt`, `&c=WEB`, `&potc=1` → **200, 0 байт во всех случаях.**
5. InnerTube `/youtubei/v1/player` с клиентом `ANDROID` → **HTTP 400**.

**Заключение:** YouTube требует proof-of-origin token (`pot`), выдаваемый BotGuard'ом плеера. Без него `timedtext` возвращает пустоту молча — **не ошибку, а пустой 200**, что особенно коварно (наивный код решит, что «субтитров нет»).

Это подтверждается состоянием экосистемы: `yt-dlp` ведёт отдельный PO Token Guide, есть issue «Some subtitles require POT now», существуют внешние провайдеры токенов (`bgutil-ytdlp-pot-provider`, HTTP-server и script-режимы). Библиотека `youtube-transcript-api` в облаке массово получает `RequestBlocked`/`IpBlocked` — YouTube блокирует диапазоны AWS/GCP/Azure/DO.

**Официальный `captions.download` из Data API v3 неприменим:** он требует OAuth **владельца канала**. Для чужих роликов — нет. Единственное, что даёт официальный API — булев флаг `contentDetails.caption` («субтитры существуют»), стоимостью 1 unit в составе `videos.list`.

**Отсюда — каскад провайдеров (детали в §12.4):**

| Уровень | Провайдер | Надёжность | Стоимость | Замечание |
|---|---|---|---|---|
| T0 | Кэш в БД | 100% | 0 | |
| T1 | `yt-dlp --write-auto-subs --skip-download` + PO-token sidecar (`bgutil`) + residential-прокси | средняя, деградирует со временем | инфраструктура прокси | Требует sidecar-контейнер |
| T2 | Хостируемый transcript API (Supadata / TranscriptAPI / аналог) за интерфейсом `TranscriptProvider` | высокая | ~$1.6–5.7 за 1000 транскриптов | Единственный по-настоящему production-grade вариант |
| T3 | ASR: `yt-dlp` аудио → `faster-whisper` локально (или Whisper API ~$0.006/мин) | высокая, но медленно | CPU/GPU время; 3 ч видео ≈ 180 мин аудио | Выключен по умолчанию, лимит `YT_ASR_MAX_DURATION_S` |
| T4 | `metadata_only` — резюме по title + description + главам | всегда | ~0 | Явно помечается в UI как «по описанию, без транскрипта» |

**Ничего из T1–T3 не может быть обещано как «работает всегда».** Это фиксируется в БД полем `transcript_source` и на UI.

`NEEDS VALIDATION` — работоспособность T1 с PO-token sidecar в конкретной инфраструктуре заказчика. Способ проверки: поднять `bgutil-ytdlp-pot-provider` в docker-compose, прогнать 20 роликов, замерить success rate; критерий приёмки ≥70%.

### 2.3 LLM

Провайдер по умолчанию — Anthropic Claude. Актуальные модели и цены ($/1M токенов):

| Модель | Model ID | Контекст | Input | Output |
|---|---|---|---|---|
| Claude Opus 5 | `claude-opus-5` | 1M | $5.00 | $25.00 |
| Claude Sonnet 5 | `claude-sonnet-5` | 1M | $2.00 | $10.00 |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K | $1.00 | $5.00 |

Релевантные для нас возможности API:
- **Structured outputs** — `output_config: {format: {...}}` с JSON-схемой. Убирает весь класс ошибок «модель вернула не тот JSON».
- **Prompt caching** — `cache_control: {"type":"ephemeral"}`. Системный промпт + рубрика аспектов идентичны между вызовами → кэшируются, чтение кэша ~0.1× цены. При тысячах игр это существенно.
- **Message Batches API** — асинхронно, **−50% стоимости**. Идеально для первичного бэкфилла каталога.
- **Adaptive thinking** (`thinking: {"type":"adaptive"}`) + `output_config.effort` — для суммаризации отзывов достаточно `effort: "low"`/`"medium"`.
- Embeddings у Anthropic нет → см. §11.

---

## 3. Recommended Architecture

```text
                         ┌───────────────────────────────────────────┐
                         │            EXTERNAL WORLD                 │
                         │  backend.metacritic.com   www.metacritic  │
                         │  YouTube Data API v3      Transcript SaaS │
                         │  Anthropic API                            │
                         └───────────────────────────────────────────┘
                                            ▲
                    ── всё общение только через адаптеры ──
                                            │
   ┌────────────────────────────────────────┼─────────────────────────────────┐
   │  ADAPTER LAYER (app/adapters)          │                                 │
   │  MetacriticClient   YouTubeClient   TranscriptProvider   LLMClient       │
   │  ├ JsonApiSource    ├ DataApiSource ├ YtDlpProvider      ├ AnthropicProv │
   │  └ HtmlSource(fb)   └ ScrapeSource  ├ HostedApiProvider  └ (Null/Mock)   │
   │                                     ├ AsrProvider                        │
   │  ┌──────────────────────────────┐   └ MetadataOnlyProvider               │
   │  │ shared: RateLimiter (Redis   │                                        │
   │  │ token bucket per host),      │   EmbeddingProvider                    │
   │  │ RetryPolicy, CircuitBreaker, │   ├ LocalOnnxProvider (default)        │
   │  │ HttpCache, Instrumentation   │   └ RemoteApiProvider                  │
   │  └──────────────────────────────┘                                        │
   └────────────────────────────────────────┬─────────────────────────────────┘
                                            │
   ┌────────────────────────────────────────┼─────────────────────────────────┐
   │  SERVICE LAYER (app/services) — вся бизнес-логика, БЕЗ HTTP              │
   │  CrawlOrchestrator  GameSyncService  ReviewSyncService                   │
   │  SummaryService     SimilarityService  LetsPlayService  MonitoringService│
   └────────────────────────────────────────┬─────────────────────────────────┘
                                            │
   ┌────────────────────────────────────────┼─────────────────────────────────┐
   │  REPOSITORY LAYER (app/repositories) — SQLAlchemy 2.0, только upsert/    │
   │  запросы. Единственное место, знающее про таблицы.                       │
   └────────────────────────────────────────┬─────────────────────────────────┘
                                            ▼
        ┌───────────────────────────────────────────────────────────────┐
        │   PostgreSQL 16  (+ pg_trgm, unaccent, pgvector, pgcrypto)    │
        │   ─ source of truth: данные, курсоры, состояния job'ов        │
        └───────────────────────────────────────────────────────────────┘
                     ▲                                    ▲
      ┌──────────────┘                                    └──────────────┐
      │                                                                  │
┌─────┴───────────────────────────────┐        ┌──────────────────────────┴────┐
│  ASYNC SIDE                         │        │  SYNC SIDE                    │
│                                     │        │                               │
│  ┌───────────┐   Celery Beat        │        │  ┌─────────────────────────┐  │
│  │ scheduler │───hourly tick───┐    │        │  │ backend (FastAPI)       │  │
│  └───────────┘                 │    │        │  │  /api/v1/games ...      │  │
│                                ▼    │        │  │  /api/v1/monitoring/... │  │
│  ┌────────────────────────────────┐ │        │  │  POST /admin/crawl/run  │  │
│  │        Redis                   │◄┼────────┼──┤  GET  /monitoring/stream│  │
│  │  • broker (Celery queues)      │ │  SSE   │  └──────────┬──────────────┘  │
│  │  • distributed locks           │ │  ◄─────┼─────────────┘                 │
│  │  • rate-limit token buckets    │ │ pubsub │             │                 │
│  │  • pub/sub  events:*           │ │        │             ▼                 │
│  └────────────────┬───────────────┘ │        │  ┌─────────────────────────┐  │
│                   │                 │        │  │ frontend (Next.js 15)   │  │
│  ┌────────────────▼───────────────┐ │        │  │  /games /games/[slug]   │  │
│  │  worker  (Celery, 3 очереди)   │ │        │  │  /monitoring            │  │
│  │  crawl │ enrich │ ai           │ │        │  └─────────────────────────┘  │
│  └────────────────────────────────┘ │        └───────────────────────────────┘
└─────────────────────────────────────┘
```

### Поток данных одного часового цикла

```text
 Beat (crontab: minute=7, каждый час)
    │
    ▼
 crawl.hourly_tick                       ← единственная задача, которую ставит Beat
    │  1. Redis SETNX crawl:lock:{date}   (+ DB partial-unique guard)
    │  2. open/attach crawl_run
    │  3. phase = new_releases | browse
    │  4. fetch listing → claim в crawl_items (ON CONFLICT DO NOTHING)
    │  5. поставить N задач game.sync
    │  6. сдвинуть курсор, закрыть run, отпустить лок
    ▼
 game.sync(slug, crawl_item_id)          ← идемпотентна, retryable
    │  composer → normalize → upsert games/game_platforms/genres/companies
    │  per-platform stats (2N запросов)
    │  fingerprint изменился? ──нет──► крутим только «дешёвые» downstream
    │           │да
    │           ▼
    ├──► reviews.sync(game_id)           ← отдельная очередь, свои ретраи
    ├──► similarity.recompute(game_id)
    └──► (условно) letsplay.discover(game_id)
                    │
 reviews.sync ──► ai.summarize(game_id, audience)   ← очередь `ai`, rate-limited
 letsplay.discover ──► letsplay.transcript ──► ai.summarize_letsplay
```

Все стрелки — **асинхронные постановки в очередь**, не вызовы. Падение любой ветви не откатывает предыдущие: каждая пишет свой результат в своей транзакции.

---

## 4. Technology Stack

| Layer | Technology | Why |
|---|---|---|
| Язык backend | **Python 3.12** | Весь проект — data/scraping/AI. Один язык на API и воркеры. |
| Web framework | **FastAPI 0.115+** | Pydantic v2 как единый слой валидации для *входящих* HTTP-запросов **и** для *исходящих* JSON от Metacritic — одна модель, одна проверка. Автогенерация OpenAPI закрывает требование §19 бесплатно. Нативный async для SSE. |
| ORM / миграции | **SQLAlchemy 2.0 + Alembic** | Нужны PostgreSQL-специфичные конструкции: `INSERT ... ON CONFLICT`, `array_agg`, GIN/HNSW-индексы, partial unique. SQLAlchemy Core даёт их без ухода в сырой SQL. |
| БД | **PostgreSQL 16** + `pg_trgm`, `unaccent`, `pgvector`, `pgcrypto` | Одна БД закрывает: реляционку, полнотекст (`tsvector`), нечёткий поиск (trigram), векторный поиск (pgvector), advisory locks. Не нужны ни Elastic, ни отдельная vector DB. |
| Очередь | **Celery 5.4 + Redis** | Из коробки: экспоненциальный backoff, `acks_late` + `visibility_timeout` (переживает смерть воркера), `chain`/`group`/`chord` для графа задач, **Celery Beat** для часового расписания, отдельные очереди с разной concurrency. Альтернативы разобраны в §35. |
| Кэш / брокер / локи / pub-sub | **Redis 7** | Один сервис на 4 роли. |
| Frontend | **Next.js 15 (App Router) + React 19 + TypeScript** | (а) SSR/ISR для каталога — быстрый первый рендер и нормальный SEO; (б) серверный fetch к backend снимает вопрос CORS; (в) **`next/image` решает проблему обложек** — Metacritic отдаёт неподписанные оригиналы по 140 KB, а подписывать их CDN-ресайз мы не вправе; Next ресайзит и кэширует у себя. |
| UI-kit | **Tailwind CSS 4 + shadcn/ui + lucide-react** | Готовые доступные примитивы (Dialog, Select, Skeleton), но без тяжёлого дизайн-фреймворка. Требование ТЗ §27 «современный game catalog» достижимо. |
| Data fetching (клиент) | **TanStack Query v5** | Только там, где нужен клиентский стейт: фильтры списка и polling-fallback мониторинга. |
| HTTP-клиент | **httpx** (sync в воркерах, async в API) | HTTP/2, таймауты по фазам, транспортные хуки для инструментирования. |
| HTML-fallback парсер | **selectolax** (+ `extruct` для JSON-LD) | В 5–10 раз быстрее BeautifulSoup; нужен только в fallback-ветке. |
| Browser fallback | **Playwright** — опционально, выключен | Не нужен: JSON API отдаёт всё без JS (доказано). Оставлен как `BrowserSource` за тем же интерфейсом на случай появления JS-challenge. |
| LLM | **Anthropic SDK** (`anthropic`) за интерфейсом `LLMClient` | Structured outputs, prompt caching, Batch API (−50%). Провайдер подменяем через `LLM_PROVIDER`. |
| Embeddings | **fastembed** (ONNX Runtime, `BAAI/bge-small-en-v1.5`, 384-dim) | Локально, на CPU, ~130 MB модель, **$0 за вектор**, нет внешней зависимости и нет утечки данных. Подменяем на Voyage/OpenAI через `EMBEDDING_PROVIDER`. |
| YouTube | **google-api-python-client** (Data API v3) + `yt-dlp` + hosted API | См. §12. |
| Валидация/конфиг | **pydantic-settings** | Все настройки — типизированные, с дефолтами, fail-fast при старте. |
| Логи | **structlog** → JSON в stdout | Контекстные поля (`crawl_run_id`, `job_id`, `game_id`) через contextvars. |
| Метрики | **prometheus-client**, `/metrics` | Опционально; архитектура готова, стек Prometheus/Grafana не входит в MVP. |
| Тесты | **pytest, pytest-asyncio, testcontainers, respx, Playwright Test** | §19. |
| Качество | **ruff** (lint+format), **mypy --strict** на `services/`, `pre-commit` | |
| Контейнеризация | **Docker Compose v2**, multi-stage Dockerfile, `uv` для установки зависимостей | |

---

## 5. Domain Model

### Сущности и связи

```text
                       ┌──────────────┐
                       │  franchises  │
                       └──────┬───────┘
                              │ 0..1
                              ▼
  ┌──────────┐  N:M  ┌────────────────┐  1:N   ┌──────────────────┐  N:1  ┌───────────┐
  │  genres  │◄─────►│     games      │◄──────►│  game_platforms  │◄─────►│ platforms │
  └──────────┘       └───────┬────────┘        └────────┬─────────┘       └───────────┘
                             │                          │ 1:N
       ┌─────────────────────┼──────────────┐           ▼
       │ N:M                 │ 1:N          │      ┌──────────┐
       ▼                     ▼              │      │ reviews  │  (critic | user)
  ┌───────────┐     ┌──────────────────┐    │      └──────────┘
  │ companies │     │ review_summaries │    │
  │ (dev/pub) │     │ (critic | user)  │    │
  └───────────┘     └──────────────────┘    │
                                            │
       ┌────────────────────────────────────┼──────────────────────┐
       ▼                                    ▼                      ▼
 ┌──────────────┐                  ┌─────────────────┐    ┌─────────────────┐
 │ similar_games│ (self N:M)       │ youtube_videos  │    │ game_embeddings │
 └──────────────┘                  └────────┬────────┘    └─────────────────┘
                                            │ 1:1
                                   ┌────────▼────────────┐
                                   │ youtube_transcripts │
                                   └────────┬────────────┘
                                            │ 1:1
                                   ┌────────▼────────────┐
                                   │ youtube_summaries   │
                                   └─────────────────────┘

  ОПЕРАЦИОННЫЙ КОНТУР (не связан FK с доменом, кроме game_id):
  crawl_runs 1:N crawl_items      jobs 1:N job_events      api_budgets
  crawl_days (одна строка на дату — курсор/фаза)
```

### Ключевые решения модели

**Идентичность игры.** Natural key — `mc_slug` (`/game/{slug}/`). Дополнительно храним `mc_title_id` (`product.id`, напр. `1300501979`) как долговременный суррогат: slug может измениться при переименовании, id — нет. При upsert: сначала пытаемся найти по `mc_title_id`, если нет — по `mc_slug`. Если найдено по id, а slug другой — **обновляем slug и логируем `game.slug_changed` на WARNING**.

**Платформа — не строка.** `game_platforms` — ассоциативная сущность с собственными атрибутами: `metascore`, `metascore_count`, `metascore_pos/neu/neg`, `userscore`, `userscore_count`, `user_pos/neu/neg`, `sentiment`, `release_date` (у каждой платформы своя!), `mc_related_game_id`, `is_lead`. Отзывы висят на `game_platforms`, а не на `games`, потому что API их так и отдаёт.

**Денормализованные роллапы в `games`.** `best_metascore`, `best_userscore`, `platform_ids int[]`, `genre_ids int[]`. Это осознанный компромисс: страница списка (§7 ТЗ) требует фильтр по платформе + сортировку по рейтингу + пагинацию. Без роллапов это `JOIN game_platforms` + `DISTINCT` + сортировка по агрегату — медленно и с плохими планами на 200k строк. Роллапы пересчитываются **в той же транзакции**, что и запись `game_platforms` (в `GamePersistence.upsert_game`), поэтому рассинхрон невозможен. «Основной рейтинг» = метрика lead-платформы, «лучший» = максимум по платформам; в API отдаём оба, пользователь выбирает базу сортировки (ТЗ §7).

**Компании.** `companies` + `game_companies(game_id, company_id, role)` где `role ∈ {developer, publisher}`. Одна компания может быть и тем и другим (Capcom для Onimusha — проверено).

---

## 6. Database Schema

> PostgreSQL 16. Все таймстемпы — `timestamptz`. Все таблицы имеют `created_at`/`updated_at` (кроме append-only логов — только `created_at`).

### 6.0 Расширения и типы

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector

CREATE TYPE crawl_phase        AS ENUM ('new_releases','browse','exhausted');
CREATE TYPE crawl_item_status  AS ENUM ('pending','processing','done','failed','skipped');
CREATE TYPE run_status         AS ENUM ('running','succeeded','partial','failed','cancelled');
CREATE TYPE run_trigger        AS ENUM ('schedule','manual','backfill');
CREATE TYPE review_kind        AS ENUM ('critic','user');
CREATE TYPE summary_audience   AS ENUM ('critic','user','letsplay');
CREATE TYPE summary_status     AS ENUM ('fresh','stale','failed','skipped_no_data');
CREATE TYPE job_status         AS ENUM ('queued','running','succeeded','failed','retrying','dead','skipped');
CREATE TYPE transcript_source  AS ENUM ('ytdlp','hosted_api','asr','metadata_only');
CREATE TYPE transcript_status  AS ENUM ('pending','available','unavailable','failed');
CREATE TYPE similarity_method  AS ENUM ('metadata','embedding','hybrid');
```

### 6.1 Справочники

```sql
CREATE TABLE platforms (
  id            serial PRIMARY KEY,
  mc_platform_id bigint UNIQUE,                 -- 1500000128
  slug          text NOT NULL UNIQUE,           -- 'playstation-5'
  name          text NOT NULL,                  -- 'PlayStation 5'
  family        text,                           -- 'playstation' | 'xbox' | 'nintendo' | 'pc' | 'mobile'
  generation    smallint,
  sort_order    smallint NOT NULL DEFAULT 100,
  is_active     boolean  NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE genres (
  id    serial PRIMARY KEY,
  slug  text NOT NULL UNIQUE,                   -- 'action-rpg'
  name  text NOT NULL,                          -- 'Action RPG'
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE companies (
  id            bigserial PRIMARY KEY,
  mc_company_id bigint UNIQUE,                  -- 4000000330
  slug          text NOT NULL UNIQUE,           -- 'from-software'
  name          text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX companies_name_trgm ON companies USING gin (name gin_trgm_ops);

CREATE TABLE franchises (
  id            bigserial PRIMARY KEY,
  mc_franchise_id bigint UNIQUE,
  slug          text NOT NULL UNIQUE,
  name          text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
```

### 6.2 `games` — ядро

```sql
CREATE TABLE games (
  id                bigserial PRIMARY KEY,

  -- идентичность
  mc_slug           text   NOT NULL UNIQUE,
  mc_title_id       bigint UNIQUE,                       -- product.id
  mc_family_id      bigint,                              -- gameTaxonomy.family.id
  mc_url            text   NOT NULL,

  -- контент
  title             text   NOT NULL,
  title_norm        text   NOT NULL,                     -- lower(unaccent(title)) без пунктуации
  description       text,
  cover_url         text,                                -- ОРИГИНАЛ, неподписанный
  card_url          text,
  video_embed_url   text,                                -- JW Player
  video_manifest_url text,
  video_title       text,
  video_duration_s  integer,

  release_date      date,
  premiere_year     smallint,
  esrb_rating       text,
  must_play         boolean NOT NULL DEFAULT false,
  franchise_id      bigint REFERENCES franchises(id) ON DELETE SET NULL,

  -- денормализованные роллапы (в одной tx с game_platforms)
  lead_platform_id  integer REFERENCES platforms(id),
  lead_metascore    smallint,
  lead_userscore    numeric(3,1),
  best_metascore    smallint,
  best_metascore_count integer NOT NULL DEFAULT 0,
  best_userscore    numeric(3,1),
  best_userscore_count integer NOT NULL DEFAULT 0,
  platform_ids      integer[] NOT NULL DEFAULT '{}',
  genre_ids         integer[] NOT NULL DEFAULT '{}',

  -- служебное
  source_fingerprint text,                               -- sha256 нормализованного product-payload
  detail_synced_at        timestamptz,
  reviews_synced_at       timestamptz,
  summaries_synced_at     timestamptz,
  youtube_synced_at       timestamptz,
  similarity_synced_at    timestamptz,
  first_seen_at     timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT games_metascore_range CHECK (best_metascore  IS NULL OR best_metascore  BETWEEN 0 AND 100),
  CONSTRAINT games_userscore_range CHECK (best_userscore  IS NULL OR best_userscore  BETWEEN 0 AND 10)
);
```

**Индексы `games` — под конкретные запросы ТЗ:**

```sql
-- Поиск по названию (ТЗ §7 Search) — нечёткий + префиксный
CREATE INDEX games_title_trgm       ON games USING gin (title_norm gin_trgm_ops);
-- Полнотекстовый — для многословных запросов
CREATE INDEX games_title_fts        ON games USING gin (to_tsvector('simple', title_norm));

-- Фильтр по платформам (ТЗ §7 Filter) — массив, оператор &&
CREATE INDEX games_platform_ids_gin ON games USING gin (platform_ids);
CREATE INDEX games_genre_ids_gin    ON games USING gin (genre_ids);

-- Сортировка по рейтингу (ТЗ §7 Sort) — обе базы, NULLS LAST
CREATE INDEX games_best_meta_desc   ON games (best_metascore DESC NULLS LAST, id DESC);
CREATE INDEX games_best_user_desc   ON games (best_userscore DESC NULLS LAST, id DESC);
CREATE INDEX games_release_desc     ON games (release_date   DESC NULLS LAST, id DESC);
CREATE INDEX games_updated_desc     ON games (updated_at     DESC, id DESC);

-- Планировщик enrich-задач: «кому пора обновить summary/youtube/similarity»
CREATE INDEX games_needs_reviews    ON games (reviews_synced_at NULLS FIRST)
                                    WHERE best_metascore IS NOT NULL;
CREATE INDEX games_needs_similarity ON games (similarity_synced_at NULLS FIRST);
```

> **Почему `id DESC` вторым ключом во всех сортировках:** это tie-breaker для keyset-пагинации. Без него `LIMIT/OFFSET` при равных метаскорах даёт нестабильный порядок между страницами (та же болезнь, что у Metacritic — §2.1.2).

### 6.3 Связки и платформы

```sql
CREATE TABLE game_genres (
  game_id  bigint NOT NULL REFERENCES games(id)  ON DELETE CASCADE,
  genre_id integer NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
  PRIMARY KEY (game_id, genre_id)
);
CREATE INDEX game_genres_genre ON game_genres (genre_id, game_id);

CREATE TABLE game_companies (
  game_id    bigint NOT NULL REFERENCES games(id)     ON DELETE CASCADE,
  company_id bigint NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  role       text   NOT NULL CHECK (role IN ('developer','publisher')),
  PRIMARY KEY (game_id, company_id, role)
);
CREATE INDEX game_companies_company ON game_companies (company_id, role);

CREATE TABLE game_platforms (
  id                  bigserial PRIMARY KEY,
  game_id             bigint  NOT NULL REFERENCES games(id)     ON DELETE CASCADE,
  platform_id         integer NOT NULL REFERENCES platforms(id) ON DELETE RESTRICT,
  mc_related_game_id  bigint,                     -- product.platforms[].relatedGameId
  is_lead             boolean NOT NULL DEFAULT false,
  release_date        date,

  metascore           smallint,
  metascore_count     integer NOT NULL DEFAULT 0,
  metascore_positive  integer NOT NULL DEFAULT 0,
  metascore_neutral   integer NOT NULL DEFAULT 0,
  metascore_negative  integer NOT NULL DEFAULT 0,
  critic_sentiment    text,

  userscore           numeric(3,1),
  userscore_count     integer NOT NULL DEFAULT 0,   -- включает оценки БЕЗ текста
  userscore_positive  integer NOT NULL DEFAULT 0,
  userscore_neutral   integer NOT NULL DEFAULT 0,
  userscore_negative  integer NOT NULL DEFAULT 0,
  user_sentiment      text,

  critic_reviews_total integer NOT NULL DEFAULT 0,  -- totalResults списка (с текстом)
  user_reviews_total   integer NOT NULL DEFAULT 0,
  critic_reviews_synced_at timestamptz,
  user_reviews_synced_at   timestamptz,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT game_platforms_uniq UNIQUE (game_id, platform_id),
  CONSTRAINT gp_meta_range CHECK (metascore IS NULL OR metascore BETWEEN 0 AND 100),
  CONSTRAINT gp_user_range CHECK (userscore IS NULL OR userscore BETWEEN 0 AND 10)
);

-- ровно одна lead-платформа на игру
CREATE UNIQUE INDEX game_platforms_one_lead ON game_platforms (game_id) WHERE is_lead;

-- фильтр «платформа + сортировка по рейтингу» (если понадобится обход роллапов)
CREATE INDEX gp_platform_meta ON game_platforms (platform_id, metascore DESC NULLS LAST);
CREATE INDEX gp_platform_user ON game_platforms (platform_id, userscore DESC NULLS LAST);
CREATE INDEX gp_game          ON game_platforms (game_id);
```

### 6.4 `reviews`

```sql
CREATE TABLE reviews (
  id                bigserial PRIMARY KEY,
  game_platform_id  bigint  NOT NULL REFERENCES game_platforms(id) ON DELETE CASCADE,
  game_id           bigint  NOT NULL REFERENCES games(id)          ON DELETE CASCADE, -- денорм. для быстрых выборок
  kind              review_kind NOT NULL,

  source_review_id  text,          -- UUID для user-отзывов; NULL для critic
  dedupe_key        text NOT NULL, -- см. ниже

  author            text,          -- user: ник; critic: часто ''
  publication_name  text,          -- critic
  publication_slug  text,          -- critic
  score             numeric(5,2),  -- critic 0..100, user 0..10 (в исходной шкале)
  score_max         smallint NOT NULL,  -- 100 или 10
  score_normalized  numeric(5,2) GENERATED ALWAYS AS (score * 100.0 / NULLIF(score_max,0)) STORED,
  body              text,
  body_hash         text NOT NULL, -- sha256(нормализованный body)
  url               text,          -- часто '' у критиков -> NULL
  published_on      date,
  is_spoiler        boolean NOT NULL DEFAULT false,
  source_version    bigint,        -- user: поле `version` (epoch ms) — детект правок
  thumbs_up         integer,
  thumbs_down       integer,

  first_seen_at     timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT reviews_dedupe_uniq UNIQUE (game_platform_id, kind, dedupe_key)
);

CREATE INDEX reviews_game_kind_date ON reviews (game_id, kind, published_on DESC NULLS LAST, id DESC);
CREATE INDEX reviews_gp_kind_seen   ON reviews (game_platform_id, kind, first_seen_at DESC);
CREATE INDEX reviews_score          ON reviews (game_id, kind, score_normalized DESC NULLS LAST);
```

**`dedupe_key`** формируется в нормализаторе, а не в БД:
- `kind = 'user'`  → `source_review_id` (UUID из API — гарантированно уникален).
- `kind = 'critic'` → `sha256(publication_slug || '|' || coalesce(published_on::text,'') || '|' || body_hash)`.
  Проверенное обоснование: у критиков нет `id`, `url` и `author` часто пусты, но связка «издание + дата + текст» уникальна на практике. Если издание переиздаст рецензию с правкой текста — появится новая запись; это приемлемо и лучше, чем потерять отзыв.

**Обновление вместо вставки:** при конфликте по `dedupe_key` делаем `DO UPDATE SET body, score, thumbs_*, url, updated_at` только если `body_hash` или `source_version` изменились — иначе `DO NOTHING` (не трогаем `updated_at`, чтобы «новизна» отзывов для AI считалась честно).

### 6.5 AI-резюме

```sql
CREATE TABLE review_summaries (
  id             bigserial PRIMARY KEY,
  game_id        bigint NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  audience       summary_audience NOT NULL,     -- 'critic' | 'user' | 'letsplay'

  positive       jsonb NOT NULL DEFAULT '[]',   -- [{aspect, claim, evidence[], strength}]
  negative       jsonb NOT NULL DEFAULT '[]',
  overall        text,
  aspects        jsonb NOT NULL DEFAULT '{}',   -- {gameplay: 'positive'|'mixed'|'negative'|'absent', ...}
  confidence     numeric(3,2),                  -- 0..1, самооценка модели

  -- воспроизводимость и защита от лишних вызовов
  input_fingerprint text NOT NULL,              -- sha256(sorted review ids + prompt_version + model + params)
  reviews_used   integer NOT NULL DEFAULT 0,
  reviews_total  integer NOT NULL DEFAULT 0,
  sampling_note  text,                          -- 'full' | 'stratified-300' | 'map-reduce-4-chunks'
  source_review_max_seen_at timestamptz,        -- водяной знак: до какого first_seen_at учтены отзывы

  llm_provider   text NOT NULL,
  llm_model      text NOT NULL,
  prompt_version text NOT NULL,                 -- 'critic-v3'
  tokens_in      integer, tokens_out integer,
  cost_usd       numeric(10,6),
  latency_ms     integer,

  status         summary_status NOT NULL DEFAULT 'fresh',
  error_message  text,
  version        integer NOT NULL DEFAULT 1,    -- инкремент при каждой успешной перегенерации
  is_current     boolean NOT NULL DEFAULT true,

  generated_at   timestamptz NOT NULL DEFAULT now(),
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- ровно одно актуальное резюме на (игра, аудитория)
CREATE UNIQUE INDEX review_summaries_current ON review_summaries (game_id, audience) WHERE is_current;
-- быстрый ответ «этот вход уже суммаризирован»
CREATE UNIQUE INDEX review_summaries_fp ON review_summaries (game_id, audience, input_fingerprint);
CREATE INDEX review_summaries_history ON review_summaries (game_id, audience, version DESC);
```

История сохраняется: новая версия вставляется, старая получает `is_current = false` в той же транзакции. Требование ТЗ «версионирование summary» закрыто.

### 6.6 Похожие игры и эмбеддинги

```sql
CREATE TABLE game_embeddings (
  game_id      bigint PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
  embedding    vector(384) NOT NULL,
  model        text NOT NULL,          -- 'bge-small-en-v1.5'
  source_hash  text NOT NULL,          -- sha256 similarity-документа
  doc_length   integer,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX game_embeddings_hnsw ON game_embeddings
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE TABLE similar_games (
  game_id         bigint NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  similar_game_id bigint NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  rank            smallint NOT NULL,          -- 1..N
  score           numeric(6,5) NOT NULL,      -- 0..1
  method          similarity_method NOT NULL,
  components      jsonb NOT NULL DEFAULT '{}',-- {genre:0.8, platform:0.5, dev:1.0, embed:0.72, ...}
  computed_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (game_id, similar_game_id),
  CONSTRAINT similar_not_self CHECK (game_id <> similar_game_id)
);
CREATE INDEX similar_games_lookup ON similar_games (game_id, rank);
CREATE INDEX similar_games_reverse ON similar_games (similar_game_id);
```

### 6.7 YouTube

```sql
CREATE TABLE youtube_videos (
  id              bigserial PRIMARY KEY,
  game_id         bigint NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  video_id        text   NOT NULL,                  -- 11-символьный YouTube id
  url             text   NOT NULL,
  title           text   NOT NULL,
  channel_id      text,
  channel_title   text,
  description     text,
  duration_s      integer,
  view_count      bigint,
  like_count      bigint,
  comment_count   bigint,
  published_at    timestamptz,
  has_captions    boolean,                          -- contentDetails.caption
  default_audio_language text,
  live_broadcast  text,                             -- 'none' | 'live' | 'upcoming'
  thumbnail_url   text,

  rank_score      numeric(6,5),
  rank_components jsonb NOT NULL DEFAULT '{}',
  rejected_reason text,                             -- если отсеян хард-фильтром
  is_selected     boolean NOT NULL DEFAULT false,   -- выбранный Let's Play
  discovery_source text NOT NULL DEFAULT 'data_api',-- 'data_api' | 'scrape'

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT youtube_videos_uniq UNIQUE (game_id, video_id)
);
CREATE UNIQUE INDEX youtube_one_selected ON youtube_videos (game_id) WHERE is_selected;
CREATE INDEX youtube_videos_rank ON youtube_videos (game_id, rank_score DESC NULLS LAST);

CREATE TABLE youtube_transcripts (
  id            bigserial PRIMARY KEY,
  video_id      bigint NOT NULL UNIQUE REFERENCES youtube_videos(id) ON DELETE CASCADE,
  status        transcript_status NOT NULL DEFAULT 'pending',
  source        transcript_source,
  language      text,
  is_auto_generated boolean,
  text          text,
  char_count    integer,
  token_estimate integer,
  segments      jsonb,                 -- опционально: [{start, dur, text}]
  attempts      jsonb NOT NULL DEFAULT '[]',  -- [{provider, ts, ok, error}]
  error_message text,
  fetched_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
```

Let's Play-резюме хранится в **той же** `review_summaries` с `audience = 'letsplay'` — та же структура (positive/negative/overall), тот же механизм версионирования и fingerprint'а. Отдельная таблица `youtube_summaries` из ТЗ не нужна: это была бы копия схемы. Ссылка на видео берётся из `youtube_videos WHERE is_selected`.

### 6.8 Операционный контур

```sql
-- одна строка на календарный день: фаза и курсор
CREATE TABLE crawl_days (
  crawl_date            date PRIMARY KEY,
  phase                 crawl_phase NOT NULL DEFAULT 'new_releases',
  new_releases_done     boolean NOT NULL DEFAULT false,
  new_releases_seen     integer NOT NULL DEFAULT 0,
  browse_page           integer NOT NULL DEFAULT 1,   -- следующая страница к обработке
  browse_offset         integer NOT NULL DEFAULT 0,
  browse_pages_done     integer NOT NULL DEFAULT 0,
  browse_exhausted      boolean NOT NULL DEFAULT false,
  release_date_watermark date,                        -- min(releaseDate) последней обработанной страницы
  games_claimed         integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE crawl_runs (
  id             bigserial PRIMARY KEY,
  crawl_date     date NOT NULL REFERENCES crawl_days(crawl_date),
  trigger        run_trigger NOT NULL,
  triggered_by   text,                        -- 'beat' | 'admin:<token-label>'
  status         run_status NOT NULL DEFAULT 'running',
  phase_at_start crawl_phase NOT NULL,
  worker_id      text,                        -- hostname:pid

  pages_fetched      integer NOT NULL DEFAULT 0,
  games_discovered   integer NOT NULL DEFAULT 0,
  games_claimed      integer NOT NULL DEFAULT 0,
  games_skipped_dupe integer NOT NULL DEFAULT 0,
  games_succeeded    integer NOT NULL DEFAULT 0,
  games_failed       integer NOT NULL DEFAULT 0,
  jobs_enqueued      integer NOT NULL DEFAULT 0,

  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  duration_ms  integer,
  error_message text,
  error_class   text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ГАРАНТИЯ УРОВНЯ БД: не более одного активного run'а
CREATE UNIQUE INDEX crawl_runs_single_active ON crawl_runs ((status)) WHERE status = 'running';
CREATE INDEX crawl_runs_history ON crawl_runs (started_at DESC);
CREATE INDEX crawl_runs_by_date ON crawl_runs (crawl_date, started_at DESC);

-- СЕРДЦЕ ДЕДУПЛИКАЦИИ
CREATE TABLE crawl_items (
  id            bigserial PRIMARY KEY,
  crawl_date    date NOT NULL,
  game_slug     text NOT NULL,
  game_id       bigint REFERENCES games(id) ON DELETE SET NULL,
  crawl_run_id  bigint REFERENCES crawl_runs(id) ON DELETE SET NULL,
  source        text NOT NULL,             -- 'new_releases' | 'browse:page=12' | 'manual' | 'sitemap'
  status        crawl_item_status NOT NULL DEFAULT 'pending',
  attempts      smallint NOT NULL DEFAULT 0,
  lease_expires_at timestamptz,            -- аренда обработки
  changed       boolean,                   -- изменился ли fingerprint (для дифф-апдейта)
  error_message text,
  claimed_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,

  CONSTRAINT crawl_items_daily_uniq UNIQUE (crawl_date, game_slug)   -- ★ ключевой констрейнт
);
CREATE INDEX crawl_items_by_status ON crawl_items (crawl_date, status);
CREATE INDEX crawl_items_lease     ON crawl_items (lease_expires_at)
                                   WHERE status = 'processing';
CREATE INDEX crawl_items_by_run    ON crawl_items (crawl_run_id, status);

-- собственный реестр задач (не полагаемся на бэкенд результатов Celery)
CREATE TABLE jobs (
  id             bigserial PRIMARY KEY,
  celery_task_id text UNIQUE,
  job_type       text NOT NULL,            -- 'game.sync' | 'reviews.sync' | 'ai.summarize' | ...
  idempotency_key text NOT NULL,           -- напр. 'game.sync:2026-09-05:elden-ring'
  status         job_status NOT NULL DEFAULT 'queued',
  queue          text NOT NULL,
  priority       smallint NOT NULL DEFAULT 5,

  game_id        bigint REFERENCES games(id) ON DELETE CASCADE,
  crawl_run_id   bigint REFERENCES crawl_runs(id) ON DELETE SET NULL,
  parent_job_id  bigint REFERENCES jobs(id) ON DELETE SET NULL,

  payload        jsonb NOT NULL DEFAULT '{}',
  result         jsonb,
  attempts       smallint NOT NULL DEFAULT 0,
  max_attempts   smallint NOT NULL DEFAULT 5,
  next_retry_at  timestamptz,
  error_class    text,
  error_message  text,
  worker_id      text,

  queued_at   timestamptz NOT NULL DEFAULT now(),
  started_at  timestamptz,
  finished_at timestamptz,
  duration_ms integer,
  CONSTRAINT jobs_idem_uniq UNIQUE (idempotency_key)
);
CREATE INDEX jobs_active   ON jobs (status, queued_at) WHERE status IN ('queued','running','retrying');
CREATE INDEX jobs_by_game  ON jobs (game_id, job_type, finished_at DESC);
CREATE INDEX jobs_recent   ON jobs (queued_at DESC);
CREATE INDEX jobs_failures ON jobs (finished_at DESC) WHERE status IN ('failed','dead');

-- append-only лента для SSE и разбора инцидентов
CREATE TABLE job_events (
  id           bigserial PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT now(),
  level        text NOT NULL DEFAULT 'info',   -- debug|info|warning|error
  event        text NOT NULL,                  -- 'run.started' | 'game.synced' | 'llm.failed' ...
  message      text,
  crawl_run_id bigint,
  job_id       bigint,
  game_id      bigint,
  worker_id    text,
  data         jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX job_events_ts    ON job_events (ts DESC);
CREATE INDEX job_events_run   ON job_events (crawl_run_id, ts DESC);
CREATE INDEX job_events_level ON job_events (level, ts DESC) WHERE level IN ('warning','error');

-- учёт квот внешних API
CREATE TABLE api_budgets (
  provider   text NOT NULL,          -- 'youtube'
  usage_date date NOT NULL,
  units_used integer NOT NULL DEFAULT 0,
  units_limit integer NOT NULL,
  calls      jsonb NOT NULL DEFAULT '{}',   -- {'search.list': 12, 'videos.list': 40}
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, usage_date)
);
```

**Ретенция:** `job_events` — партиционировать по месяцам или чистить cron-задачей старше `EVENTS_RETENTION_DAYS` (30). `jobs` — старше 90 дней. `crawl_items` — старше 180 дней.

### 6.9 Сводка «какой индекс какое требование ТЗ закрывает»

| Требование ТЗ §13 | Индекс(ы) |
|---|---|
| поиск по названию | `games_title_trgm` (GIN trigram) + `games_title_fts` (GIN tsvector) |
| фильтрация платформ | `games_platform_ids_gin` (GIN на `int[]`, оператор `&&`) |
| сортировка по рейтингам | `games_best_meta_desc`, `games_best_user_desc` (+ `id DESC` tie-break) |
| поиск похожих игр | `game_embeddings_hnsw` (HNSW cosine) + `similar_games_lookup` |
| история crawler runs | `crawl_runs_history`, `crawl_runs_by_date`, `job_events_ts` |
| дедупликация | `crawl_items_daily_uniq` (UNIQUE), `reviews_dedupe_uniq`, `games.mc_slug` UNIQUE |
| единственность активного run'а | `crawl_runs_single_active` (partial UNIQUE) |

---

## 7. Crawler Algorithm

### 7.1 Принципы

1. **БД — источник истины.** Redis-лок нужен для эффективности; корректность обеспечивают уникальные констрейнты. Полная потеря Redis не приводит к дублям.
2. **Захват игры на день = одна вставка в `crawl_items`.** Атомарно, работает при любом числе воркеров.
3. **Один tick = ограниченный объём работы.** `CRAWL_MAX_GAMES_PER_RUN` (по умолчанию 40). Tick не должен «догонять» весь каталог.
4. **Tick только *ставит задачи*.** Он не делает тяжёлую работу сам. Средняя длительность tick'а — единицы секунд.
5. **Дата фиксируется на старте run'а** и не меняется внутри него.

### 7.2 Псевдокод `crawl.hourly_tick`

```python
def hourly_tick(trigger: RunTrigger, triggered_by: str) -> RunResult:
    today = date.today(tz=CRAWL_TIMEZONE)          # шаг 1: дата зафиксирована

    # шаг 2: распределённый лок (эффективность, не корректность)
    lock = redis.lock(f"crawl:lock:{today}", timeout=CRAWL_LOCK_TTL_S, blocking=False)
    if not lock.acquire():
        emit("run.skipped", reason="lock_held")
        return RunResult(skipped=True, reason="another run in progress")

    watchdog = start_lock_renewal(lock, every=30)   # переживает долгий tick

    try:
        with db.begin() as tx:                      # шаг 3: состояние дня
            day = tx.upsert_crawl_day(today)        # INSERT .. ON CONFLICT DO NOTHING RETURNING
            try:
                run = tx.insert_crawl_run(          # шаг 4: гарантия БД — один активный run
                    crawl_date=today, trigger=trigger,
                    triggered_by=triggered_by, phase_at_start=day.phase,
                    worker_id=WORKER_ID, status='running')
            except UniqueViolation:                 # индекс crawl_runs_single_active
                emit("run.skipped", reason="active_run_exists")
                return RunResult(skipped=True, reason="active run exists")

        emit("run.started", crawl_run_id=run.id, phase=day.phase)
        budget = CRAWL_MAX_GAMES_PER_RUN
        claimed: list[CrawlItem] = []

        # ─────────── ФАЗА A: New Releases ───────────
        if not day.new_releases_done:
            try:
                items = metacritic.new_releases(limit=NEW_RELEASES_LIMIT)   # 20
            except MetacriticUnavailable as e:
                finish_run(run, status='partial', error=e)   # курсор НЕ двигаем
                return RunResult(partial=True)

            emit("phase.new_releases", discovered=len(items))
            selected = items[:NEW_RELEASES_LIMIT]      # «первые 20»; если меньше — берём сколько есть
            claimed += claim_games(selected, today, run, source="new_releases")

            mark_day(day, new_releases_done=True,
                     new_releases_seen=len(selected), phase='browse')
            budget -= len(claimed)

        # ─────────── ФАЗА B: browse / SEE ALL → New ───────────
        while budget > 0 and not day.browse_exhausted:
            if date.today(tz=CRAWL_TIMEZONE) != today:      # дата сменилась внутри job'а
                emit("run.date_rolled"); break              # завершаем чисто

            page = day.browse_page
            try:
                items, total = metacritic.browse_new(page=page, page_size=BROWSE_PAGE_SIZE)
            except MetacriticUnavailable as e:
                finish_run(run, status='partial', error=e); return RunResult(partial=True)

            if not items:
                mark_day(day, browse_exhausted=True, phase='exhausted'); break

            fresh = claim_games(items, today, run, source=f"browse:page={page}")
            claimed += fresh
            budget  -= len(fresh)

            # курсор двигаем ВСЕГДА — даже если все 24 оказались дублями
            mark_day(day,
                     browse_page=page + 1,
                     browse_offset=page * BROWSE_PAGE_SIZE,
                     browse_pages_done=day.browse_pages_done + 1,
                     release_date_watermark=min(i.release_date for i in items if i.release_date))
            emit("phase.browse", page=page, discovered=len(items), claimed=len(fresh))

            if page * BROWSE_PAGE_SIZE >= min(total, BROWSE_MAX_OFFSET):
                mark_day(day, browse_exhausted=True, phase='exhausted'); break

        # ─────────── постановка задач ───────────
        for item in claimed:
            enqueue("game.sync",
                    idempotency_key=f"game.sync:{today}:{item.game_slug}",
                    args={"slug": item.game_slug, "crawl_item_id": item.id,
                          "crawl_run_id": run.id})

        finish_run(run, status='succeeded', games_claimed=len(claimed))
        return RunResult(claimed=len(claimed))

    except Exception as e:
        finish_run(run, status='failed', error=e); raise
    finally:
        watchdog.stop(); lock.release()
```

### 7.3 `claim_games` — атомарная дедупликация

```sql
INSERT INTO crawl_items (crawl_date, game_slug, source, crawl_run_id,
                         status, lease_expires_at)
VALUES (:crawl_date, :slug, :source, :run_id, 'processing', now() + :lease_ttl)
ON CONFLICT (crawl_date, game_slug) DO NOTHING
RETURNING id, game_slug;
```

- Вернулась строка → игра **захвачена нами** на сегодня → обрабатываем.
- Ничего не вернулось → сегодня её уже кто-то взял (этот же run, прошлый час, другой воркер) → `games_skipped_dupe += 1`.

Ответы на все вопросы дедупликации из ТЗ:

| Вопрос ТЗ | Ответ |
|---|---|
| какие идентификаторы использовать | `mc_slug` (в `crawl_items`), `mc_title_id` (в `games`) |
| как определять уникальную игру | UNIQUE `games.mc_slug` + UNIQUE `games.mc_title_id`; поиск сначала по id, затем по slug |
| как хранить дату/время обработки | `crawl_items.claimed_at/finished_at`, `games.detail_synced_at` и семейство `*_synced_at` |
| как определить, обрабатывалась ли сегодня | наличие строки в `crawl_items (crawl_date, game_slug)` |
| как переходить к следующей странице | `crawl_days.browse_page`, инкремент в транзакции вместе с записью курсора |
| как переживать перезапуск | всё состояние в PostgreSQL; после рестарта tick читает `crawl_days` и продолжает |
| как не потерять progress | курсор двигается **после** успешного `claim`; при недоступности Metacritic курсор не двигается вообще |
| как избежать race conditions | `ON CONFLICT DO NOTHING` + partial UNIQUE на активный run + Redis-лок |
| несколько worker instances | все три механизма работают на N воркерах без изменений |

### 7.4 Разбор сценариев из ТЗ §17

| Сценарий | Поведение |
|---|---|
| **В New Releases меньше 20** | `items[:20]` вернёт сколько есть; `new_releases_done = true`; остаток бюджета сразу тратится на фазу browse в этом же tick'е. |
| **Игра уже есть в БД** | Захватывается (сегодня ещё не обрабатывалась) → `game.sync` делает UPSERT. Если `source_fingerprint` не изменился — тяжёлые downstream-задачи не ставятся (§18). |
| **Игра обработана вчера** | Строки за сегодня нет → захватывается заново; дифференциальное обновление решает, что реально делать. |
| **Игра обработана сегодня** | `ON CONFLICT DO NOTHING` → 0 строк → пропуск, счётчик `games_skipped_dupe`. |
| **Crawler упал после 7 из 20** | 7 items = `done`; 13 = `processing` с истёкшей арендой. Reaper (каждые 5 мин) возвращает их в `pending` и переставляет `game.sync`. Run = `failed`; следующий tick создаёт новый run той же даты, `new_releases_done` уже `true` → идёт в browse, а зависшие 13 подхватывает reaper. |
| **Страница изменилась (схема)** | Pydantic-валидация падает → `SchemaDriftError` → run = `partial`, курсор не двигается, событие `error`, метрика `metacritic_schema_drift_total`, авто-переключение на `HtmlSource` (§8.5). |
| **Появилась новая игра** | Попадёт в New Releases (если есть метаскор) или в первые страницы browse (`sortBy=-releaseDate`). |
| **Сервер перезапустился** | Celery `acks_late=True` + `visibility_timeout` вернёт незавершённые задачи. Состояние crawler'а — в `crawl_days`. Redis-лок истечёт по TTL. |
| **Два запуска одновременно** | Первый берёт Redis-лок. Если Redis недоступен и «лок взяли» оба — `crawl_runs_single_active` даст `UniqueViolation` второму. Если бы и это обошли — `crawl_items` не даст обработать игру дважды. **Три независимых барьера.** |
| **Дата сменилась во время job'а** | Проверка на границе каждой browse-страницы → run завершается `succeeded` с тем, что успел; новый день начнётся со следующего tick'а. |

### 7.5 Ручной запуск (ТЗ §10)

`POST /api/v1/admin/crawl/run` **не выполняет** обход синхронно:
1. Проверяет `crawl_runs WHERE status='running'` → если есть, **409 Conflict** с `{active_run_id, started_at, phase}`. Это и есть «нельзя допустить два конфликтующих crawler job».
2. Ставит `crawl.hourly_tick(trigger='manual', triggered_by=...)` в очередь `crawl` с приоритетом 9.
3. Возвращает **202 Accepted** + `job_id` + `run_id` + ссылку на SSE-поток.

### 7.6 Weekly reconciliation (страховка от нестабильной пагинации)

Из-за недетерминированного порядка `finder` (§2.1.2) offset-обход **не гарантирует полноты**. Раз в неделю `crawl.reconcile_sitemap` (вс 03:00):
1. Скачивает 296 шардов `games/{n}.xml` (≈296 000 URL), извлекает slug'и.
2. `SELECT mc_slug FROM games` → diff.
3. Первые `RECONCILE_MAX_NEW` (5000) отсутствующих slug'ов кладёт в `crawl_items` с `source='sitemap'`.

Это делает каталог со временем полным, независимо от капризов листинга.

---

## 8. Metacritic Scraping Strategy

### 8.1 Структура слоя

```text
MetacriticClient                       ← единственный публичный фасад
 ├── source: MetacriticSource          ← Protocol
 │    ├── JsonApiSource      (default) ← backend.metacritic.com
 │    ├── HtmlSource         (fallback)← www + JSON-LD + data-testid
 │    └── BrowserSource      (off)     ← Playwright, если появится JS-challenge
 ├── HttpTransport
 │    ├── RateLimiter        ← Redis token-bucket, ключ = host
 │    ├── RetryPolicy        ← tenacity: 5 попыток, 1s→32s, full jitter
 │    ├── CircuitBreaker     ← 5 ошибок подряд → open 5 мин → half-open
 │    ├── ResponseCache      ← файловый кэш (dev/тесты)
 │    └── Instrumentation    ← latency/status/bytes → metrics + job_events
 └── parsers/  (чистые функции: dict → Pydantic DTO)
```

Бизнес-логика видит только `MetacriticClient` и DTO; она не знает ни про HTTP, ни про JSON, ни про HTML (Rule 4 ТЗ §33).

### 8.2 Как получаем каждое требуемое поле

| Поле ТЗ | Запрос | Путь в ответе | Нормализация |
|---|---|---|---|
| список New Releases | `finder ?componentName=new-releases-carousel&sortBy=-releaseDate&metaScoreMin=1&mcoTypeId=13&offset=0&limit=20` | `data.items[]` | → `ListingItemDTO{slug,title,release_date,metascore,userscore,image}` |
| список browse/New | `finder ?sortBy=-releaseDate&productType=games&offset={(p-1)*24}&limit=24` | `data.items[]`, `data.totalResults`, `links.next` | то же |
| детали игры | `composer/metacritic/pages/games/{slug}/web` | `components[componentName='product'].data.item` | `GameDetailDTO` |
| название | ↑ | `.title` | trim; `title_norm = lower(unaccent(strip_punct(title)))` |
| URL Metacritic | — | вычисляем | `https://www.metacritic.com/game/{slug}/` |
| обложка | ↑ | `.images[typeName='mainImage'].bucketPath` | `https://www.metacritic.com/a/img/catalog{bucketPath}` — **неподписанный оригинал** |
| карточное изображение | ↑ | `.images[typeName='cardImage'].bucketPath` | то же |
| описание | ↑ | `.description` | нормализация переводов строк; **никогда не из `finder`** (§2.1.4) |
| developer | ↑ | `.production.companies[typeName='Developer']` | → `game_companies role='developer'` |
| publisher | ↑ | `.production.companies[typeName<>'Developer']` | → `role='publisher'` |
| ссылка на видео | ↑ | `.video.embedUrl` / `.video.manifestUrl` | оба nullable; JW Player iframe |
| платформы | ↑ | `.platforms[]` | `{mc_platform_id, slug, name, release_date, mc_related_game_id, is_lead}` |
| **Metascore по платформе** | ↑ | `.platforms[].criticScoreSummary.score` | **уже здесь**, доп. запрос не нужен |
| Metascore детально | `reviews/critic/games/{slug}/platform/{p}/stats/web` | `data.item` | `score, reviewCount, positive/neutral/negativeCount, sentiment` |
| **Userscore по платформе** | `reviews/user/games/{slug}/platform/{p}/stats/web` | `data.item` | то же, шкала 0–10 |
| жанры | composer | `.genres[].name` | slugify → `genres`/`game_genres` |
| франшиза | composer | `.gameTaxonomy.franchises[0]`, `.family` | → `franchises` |
| ESRB | composer | `.rating` | |
| дата релиза | composer | `.releaseDate` + `.platforms[].releaseDate` | у каждой платформы своя |
| critic reviews | `reviews/critic/games/{slug}/platform/{p}/web?offset&limit&filterBySentiment=all&sort=date` | `data.items[]`, `data.totalResults` | §9 |
| user reviews | `reviews/user/.../web?...` | то же | §9 |

**Стоимость полного сбора одной игры:** `1 (composer) + 2×N (stats на платформу) + R (страницы отзывов)`. Игра с 4 платформами и 3 страницами отзывов ≈ 12 запросов ≈ 6 с при лимите 2 rps. 40 игр за tick ≈ 4 минуты — комфортно укладывается в час.

### 8.3 Валидация и защита от дрейфа схемы

Каждый ответ проходит Pydantic-модель с `ConfigDict(extra='ignore')`:

```python
class McProduct(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)
    id: int
    slug: str
    title: str
    description: str | None = None
    platforms: list[McPlatform] = []
    genres: list[McNamed] = []
    production: McProduction | None = None
    video: McVideo | None = None
    images: list[McImage] = []
    release_date: date | None = Field(None, alias="releaseDate")
    rating: str | None = None
    must_play: bool = Field(False, alias="mustPlay")
    game_taxonomy: McTaxonomy | None = Field(None, alias="gameTaxonomy")
```

- `extra='ignore'` → **добавление** полей на стороне Metacritic ничего не ломает.
- Отсутствие **обязательного** поля (`id`, `slug`, `title`) → `SchemaDriftError`, а не тихий `None`.
- Все прочие поля — `Optional` с дефолтами: реальность показала, что `video`, `rating`, `description`, `criticScoreSummary.score` регулярно `null`.
- При `SchemaDriftError`: событие `error`, инкремент `metacritic_schema_drift_total`; при превышении `SCHEMA_DRIFT_THRESHOLD` за окно — авто-переключение на `HtmlSource` (флаг в Redis, TTL 1 ч) + алерт.

### 8.4 Rate limiting и вежливость

Redis token-bucket, ключ `ratelimit:{host}`:
- `METACRITIC_RPS = 2.0`, `METACRITIC_BURST = 4`, `METACRITIC_MAX_CONCURRENCY = 2` (Redis-семафор).
- User-Agent: `MetacriticGamesService/1.0 (+{CONTACT_URL}; educational project)` — идентифицируем себя честно.
- Уважаем `Retry-After`; backoff `min(2^attempt, 32) + jitter`, максимум 5 попыток.
- В dev/тестах — файловый кэш ответов, чтобы не долбить сайт при разработке.

### 8.5 HTML fallback (`HtmlSource`)

Активируется вручную (`METACRITIC_SOURCE=html`) или автоматически при дрейфе схемы. Порядок извлечения — от устойчивого к хрупкому:

1. **JSON-LD `VideoGame`** со страницы игры (`extruct`) → `name`, `datePublished`, `description`, `aggregateRating`. Подтверждён на `/game/elden-ring/`.
2. **OpenGraph-мета** → `og:title`, `og:description`, `og:url`.
3. **`data-testid`-якоря** → `product-score`, `global-score-header`, `global-score-review-count`, `product-card-content` (ссылка `/game/{slug}/`), `filter-results` (грид browse).
4. **Nuxt-payload** (`window.__NUXT__`, devalue) — самый полный и самый хрупкий; только последним, в изолированном парсере со своими фикстурными тестами.

**CSS-селекторы по Tailwind-классам не используются нигде** — зафиксировано отдельным ADR.

### 8.6 Пайплайн (разделение по ТЗ §16)

```text
 SourceDiscovery      → list[ListingItemDTO]     (finder / browse / sitemap)
        ↓
 GameParser           → GameDetailDTO            (composer → Pydantic)
        ↓
 GameNormalizer       → NormalizedGame           (slug'и, шкалы, URL, title_norm, fingerprint)
        ↓
 GamePersistence      → game_id                  (ОДНА транзакция: games + platforms +
        ↓                                         genres + companies + роллапы)
 ReviewSync           → n_new                    (отдельная транзакция, батчами)
        ↓
 EnrichmentDispatcher → enqueue(ai, youtube, similarity)
```

`GameParser` и `GameNormalizer` — чистые функции, полностью покрываются unit-тестами на фикстурах без сети (§19).

---

## 9. Review Processing

### 9.1 Стратегия сбора

Отзывы привязаны к **паре (игра, платформа)** — так их отдаёт API. Для игры с 4 платформами это 8 «потоков» (critic+user × 4). Чтобы не взорвать объём — **бюджет по платформам**:
- всегда lead-платформа;
- плюс платформы с `metascore_count >= REVIEWS_MIN_PLATFORM_CRITICS` (3) или `userscore_count >= 50`;
- максимум `REVIEWS_MAX_PLATFORMS` (4), отсортированных по сумме отзывов.

### 9.2 Первичная синхронизация

```
critic: страницы по 100, до min(totalResults, REVIEWS_CRITIC_CAP=200)
user:   страницы по 100, до min(totalResults, REVIEWS_USER_CAP=500), sort=date
```
`offset` всегда клампится (`offset < totalResults`) — иначе API отдаёт **HTTP 500** (§2.1.2).

### 9.3 Инкрементальная синхронизация

Наивное «идём по `sort=date`, пока не встретим известный отзыв» **не работает**: проверено, что `sort=date` не строго монотонна. Алгоритм с допуском:

```python
consecutive_stale_pages = 0
for page in range(REVIEWS_INCREMENTAL_MAX_PAGES):     # 5
    batch = client.reviews(slug, platform, kind, offset=page*100, limit=100, sort='date')
    if not batch: break
    r = repo.upsert_reviews(batch)                     # ON CONFLICT по dedupe_key
    if r.inserted == 0 and r.updated == 0:
        consecutive_stale_pages += 1
        if consecutive_stale_pages >= REVIEWS_STALE_PAGE_TOLERANCE:   # 2
            break
    else:
        consecutive_stale_pages = 0
```

Плюс раз в `REVIEWS_FULL_RESYNC_DAYS` (30) — полная пересинхронизация игры, чтобы подчистить пропуски.

### 9.4 Дедупликация и обновление

```sql
INSERT INTO reviews (...) VALUES (...), (...)
ON CONFLICT (game_platform_id, kind, dedupe_key) DO UPDATE
SET body = EXCLUDED.body,
    body_hash = EXCLUDED.body_hash,
    score = EXCLUDED.score,
    thumbs_up = EXCLUDED.thumbs_up,
    thumbs_down = EXCLUDED.thumbs_down,
    url = COALESCE(NULLIF(EXCLUDED.url, ''), reviews.url),
    source_version = EXCLUDED.source_version,
    updated_at = now()
WHERE reviews.body_hash      IS DISTINCT FROM EXCLUDED.body_hash
   OR reviews.source_version IS DISTINCT FROM EXCLUDED.source_version
RETURNING id, (xmax = 0) AS inserted;
```

`xmax = 0` различает вставку и обновление — так мы честно считаем «сколько отзывов реально новые», а это вход для решения о перегенерации AI-резюме. Условие в `WHERE` предотвращает `updated_at`-шум: **повторный запуск не создаёт дублей и не трогает неизменившиеся строки** (Rule 6 ТЗ §33).

### 9.5 Нормализация

- Убираем `\r`, схлопываем 3+ переводов строк в 2, `strip()`.
- `body_hash = sha256(lower(collapse_whitespace(body)))` — устойчив к косметическим правкам.
- Пустые `url`/`author` у критиков → `NULL`, не `''`.
- **Шкалы не приводим при хранении** (критики 0–100, юзеры 0–10): храним `score` + `score_max`, а `score_normalized` считает БД как generated column. Исходные данные не теряются, сравнение возможно.

### 9.6 Отдача в API

`GET /games/{id}/reviews` — фильтры `kind`, `platform`, `sentiment`, `sort`, keyset-пагинация. Тексты отдаём как есть (это цитаты с указанием источника и ссылкой), но в UI список свёрнут по умолчанию: основной контент карточки — AI-резюме.

---

## 10. AI Architecture

### 10.1 Абстракция провайдера

```python
class LLMClient(Protocol):
    def complete_structured(
        self, *, prompt: RenderedPrompt, schema: type[BaseModel],
        max_tokens: int, effort: Literal["low","medium","high"] = "low",
    ) -> LLMResult[BaseModel]: ...
    def estimate_tokens(self, prompt: RenderedPrompt) -> int: ...

@dataclass
class LLMResult(Generic[T]):
    value: T; model: str
    tokens_in: int; tokens_out: int
    cost_usd: Decimal; latency_ms: int; cache_read_tokens: int
```

Реализации: `AnthropicLLMClient` (default), `NullLLMClient` (тесты / `LLM_ENABLED=false`), `RecordingLLMClient` (запись фикстур). `SummaryService` знает только протокол — **смена провайдера = одна строка в DI-контейнере**.

### 10.2 Structured output

```python
Aspect = Literal["gameplay","graphics","story","mechanics","performance",
                 "sound_music","innovation","replayability","content_amount",
                 "difficulty","ui_ux","price_value","bugs","multiplayer","other"]

class Point(BaseModel):
    aspect: Aspect
    claim: str          = Field(max_length=240)
    evidence: list[str] = Field(max_length=3)
    strength: Literal["strong","moderate","weak"]

class SummaryOut(BaseModel):
    positive: list[Point] = Field(max_length=6)
    negative: list[Point] = Field(max_length=6)
    overall: str          = Field(max_length=700)
    aspect_verdicts: dict[Aspect, Literal["positive","mixed","negative","absent"]]
    confidence: float     = Field(ge=0, le=1)
```

Схема отдаётся модели через `output_config: {format: {...}}` — это убирает весь класс ошибок «модель вернула не тот JSON». Фиксированный enum аспектов даёт три выигрыша: (1) резюме сопоставимы между играми, (2) `aspect_verdicts` — готовый признак для similarity (§11.2), (3) UI рендерит стабильные бейджи. Аспекты покрывают ровно то, что перечислено в ТЗ (gameplay, graphics, story, mechanics, performance, sound/music, innovation, replayability) + практически значимые добавки.

### 10.3 Промпты и версионирование

Промпты — файлы `app/ai/prompts/{critic_v3,user_v3,letsplay_v2}.jinja`; версия зашита в имя и пишется в `review_summaries.prompt_version`. **Изменение промпта = новая версия = новый `input_fingerprint` = легитимная перегенерация.**

Компоновка под prompt caching:
```
[system, cache_control: ephemeral]  роль + рубрика аспектов + правила + JSON-схема   ← идентично между играми
[user]                              контекст игры (название, жанр, платформы, оценки)
[user]                              отзывы (нумерованные, с оценкой и датой)
```
Стабильный префикс → кэш-хиты по ~0.1× цены. При тысячах игр это основная статья экономии.

### 10.4 Когда вызывать LLM (и когда — нет)

```python
def should_regenerate(game_id, audience) -> Decision:
    if reviews_total == 0:
        return SKIP(status='skipped_no_data')            # ТЗ: обработка отсутствия отзывов

    fp = fingerprint(sorted(review_ids), prompt_version, model, params)
    if exists(review_summaries WHERE game_id, audience, input_fingerprint = fp):
        return SKIP(reason='identical_input')            # ★ жёсткая гарантия

    if current is None:                    return GENERATE('first')
    new = count(reviews WHERE first_seen_at > current.source_review_max_seen_at)
    if new >= AI_MIN_NEW_REVIEWS:          return GENERATE('new_reviews')      # 10
    if new / reviews_total >= AI_MIN_NEW_RATIO:  return GENERATE('new_ratio')  # 0.20
    if current.status == 'failed' and retry_window_passed:
                                            return GENERATE('retry_after_failure')
    if age(current) > AI_MAX_SUMMARY_AGE_DAYS and new > 0:
                                            return GENERATE('staleness')       # 90
    return SKIP('not_enough_change')
```

`input_fingerprint` — главный предохранитель: даже при ошибке вышестоящей логики повторный вызов с тем же входом **физически не дойдёт до LLM** (уникальный индекс `review_summaries_fp`).

### 10.5 Слишком много отзывов

Elden Ring: 24 372 оценки и 6 593 текстовых отзыва только на PS5. Целиком отправлять не нужно и не стоит.

**Стратифицированная выборка** (`sampling_note='stratified-N'`):
1. Бюджет `AI_MAX_INPUT_TOKENS = 60_000`.
2. Квоты по тональности **пропорционально реальному распределению** (`userscore_positive/neutral/negative`), но с полом 15% на каждую непустую страту — чтобы меньшинство не исчезло.
3. Внутри страты ранг = `0.4·length + 0.3·recency + 0.2·helpfulness(thumbs) + 0.1·score_extremity`; отсекаем `len(body) < 80` («10/10 огонь») и дубликаты по `body_hash`.
4. Критиков берём **всех** до 200 — их цитаты короткие и это самый ценный сигнал.

**Map-reduce** (`sampling_note='map-reduce-K-chunks'`) — если и после выборки не влезаем (редкость при 1M-контексте, но нужно для дешёвых моделей): чанки по `AI_CHUNK_TOKENS` (15k) → map (`effort=low`) → reduce. Токены и стоимость суммируются по всем вызовам.

### 10.6 Контроль стоимости

- Модель по умолчанию — `LLM_MODEL=claude-opus-5` (`$5/$25` за 1M). Снижение стоимости — сменой одной переменной окружения на `claude-sonnet-5` (`$2/$10`) или `claude-haiku-4-5` (`$1/$5`). Это решение владельца сервиса, а не кода.
- `effort: "low"` — суммаризация не требует глубокого рассуждения.
- **Prompt caching** на системном блоке (§10.3).
- **Batch API** (`AI_USE_BATCH=true`) для первичного бэкфилла — **−50%**; латентность до 24 ч для фонового обогащения не важна.
- Жёсткие лимиты: `AI_DAILY_COST_LIMIT_USD` (10.0), `AI_MAX_CALLS_PER_HOUR` (100), учёт в `api_budgets`. При превышении задачи не отменяются, а откладываются (`countdown`), событие `warning`.
- Каждый вызов пишет `tokens_in/out`, `cost_usd`, `latency_ms` → отчёт по стоимости доступен одним SQL-запросом.

### 10.7 Отказоустойчивость AI-слоя

| Ситуация | Поведение |
|---|---|
| 429 / 529 от LLM | Celery autoretry, backoff 60→600 с, `max_attempts=6`. Данные игры не тронуты. |
| Невалидный JSON | Structured output почти исключает; если всё же — 1 «repair»-ретрай, затем `status='failed'`. |
| Таймаут | Streaming, таймаут 300 с, ретрай. |
| Провайдер лежит | Circuit breaker open 5 мин. `ai.*` копятся в очереди. **Игры, отзывы, YouTube работают.** |
| Ретраи исчерпаны | `status='failed'` + `error_message`. **Предыдущее успешное резюме остаётся `is_current` и продолжает отдаваться в API.** UI показывает бейдж «обновляется». |
| `LLM_ENABLED=false` | `NullLLMClient` → `skipped_no_data`; сервис полностью функционален без AI. |

Это ровно Сценарий 1 из ТЗ §30: `Game=SUCCESS, Reviews=SUCCESS, AI Summary=RETRY`.

---

## 11. Similar Games Algorithm

### 11.1 Сравнение вариантов

| | **A. Metadata** | **B. Embeddings** | **C. Hybrid (выбран)** |
|---|---|---|---|
| Как работает | Взвешенная схожесть по жанрам/платформам/разработчику/франшизе/эпохе/оценкам | Косинус между векторами описаний и AI-резюме | A как жёсткий приор + B как семантический уточнитель |
| Плюсы | $0, детерминизм, объяснимость, работает сразу | Ловит «похожа по духу», не зависит от полноты метаданных | Обе сильные стороны; деградирует до A |
| Минусы | В базе ~30k игр «Action» — жанр сам по себе не различает | Холодный старт; у инди описания слабые/отсутствуют; сходство текстов ≠ сходство игр; стоимость векторов | Сложнее реализовать и объяснить |
| Как выглядит провал | «все Action RPG» | случайная выдача | — |

**Почему hybrid.** Metacritic даёт очень сильные *жёсткие* сигналы, которые эмбеддинг размывает: одна франшиза, один разработчик, одна платформенная семья — это факты, а не «похожесть текстов». Одновременно по одним метаданным нельзя отличить souls-like от action-RPG про фермерство. Плюс у значительной доли свежих инди описание — маркетинговый шум, и embedding-only на них деградирует. Hybrid с явными весами объясним (`components jsonb` показывает вклад каждого фактора) и **работает даже когда эмбеддингов ещё нет** — на старте система едет на metadata-скоре.

### 11.2 Формула

```
S(a,b) = w_meta·M(a,b) + w_embed·E(a,b)      если есть оба вектора
       = M(a,b)                               иначе

M(a,b) = 0.30·Jaccard(genres)
       + 0.20·max(franchise_match, family_match)     -- 1.0 при одной франшизе
       + 0.15·company_overlap(developer >> publisher)-- dev 1.0, pub 0.5
       + 0.12·Jaccard(platforms)
       + 0.10·aspect_similarity                      -- косинус по aspect_verdicts из AI-резюме
       + 0.08·score_proximity                        -- 1 − |meta_a − meta_b| / 100
       + 0.05·era_proximity                          -- exp(−|Δyears| / 6)

E(a,b) = (1 + cosine(v_a, v_b)) / 2                  -- нормируем в [0,1]

w_meta = 0.6, w_embed = 0.4                          -- конфигурируемо
```

Множители-штрафы:
- `×0.35` если у обеих `best_metascore IS NULL` и `user_reviews_total < 5` — не рекомендуем пустышки.
- `×0.5` если это DLC/переиздание той же `mc_family_id` (`Elden Ring` ↔ `Shadow of the Erdtree`) — показывать стоит, но не занимать ими весь топ.
- Хард-фильтр `similar_game_id <> game_id`.

### 11.3 Similarity document

```
{title}
Genres: {genres}. Developer: {devs}. Publisher: {pubs}. Franchise: {franchise}.
Platforms: {platforms}. Released: {year}. ESRB: {rating}.
{description[:1200]}
Critics liked: {critic_summary.positive[:3].claim}
Critics disliked: {critic_summary.negative[:3].claim}
Players liked: {user_summary.positive[:3].claim}
Players disliked: {user_summary.negative[:3].claim}
```

`source_hash = sha256(doc)`; вектор пересчитывается только при изменении хэша. Модель: `BAAI/bge-small-en-v1.5` через `fastembed` (ONNX, CPU) → 384-dim, ~15 мс на документ, **$0**. За интерфейсом `EmbeddingProvider` — заменяемо на Voyage/OpenAI через `EMBEDDING_PROVIDER`.

### 11.4 Вычисление и хранение

Полная матрица `N²` невозможна (177k игр → 3·10¹⁰ пар). Две фазы:

**Фаза 1 — кандидаты (SQL, дёшево).** До 400 кандидатов объединением:
```sql
SELECT id FROM games WHERE genre_ids && :genre_ids AND id <> :id
  ORDER BY best_metascore DESC NULLS LAST LIMIT 200
UNION
SELECT g.id FROM games g JOIN game_companies gc ON gc.game_id = g.id
  WHERE gc.company_id = ANY(:dev_ids) AND g.id <> :id LIMIT 100
UNION
SELECT game_id FROM game_embeddings WHERE game_id <> :id
  ORDER BY embedding <=> :vec LIMIT 100          -- HNSW
```

**Фаза 2 — точный скоринг** кандидатов, top-`SIMILAR_TOP_N` (12) пишется в `similar_games` через `DELETE + INSERT` в одной транзакции (идемпотентно).

**Когда пересчитывать:** после `game.sync` с изменившимся fingerprint'ом; после генерации AI-резюме; и — важно — **симметрия**: новая игра меняет топ у соседей, поэтому суточная `similarity.refresh_stale` пересчитывает игры с `similarity_synced_at < now() - 7 days` батчами по 500. Мгновенная симметрия не нужна и стоила бы слишком дорого — это документированный компромисс.

**Отдача:** `GET /games/{id}/similar` читает готовые строки по индексу `(game_id, rank)` с `JOIN games` — один запрос, никаких N+1. `components` позволяет UI показать «почему похожа» («тот же разработчик, схожие жанры»).

---

## 12. YouTube Pipeline

```text
 Game (title, release_date, platforms)
   │
   ▼ [0] Гейт: стоит ли тратить квоту?
   │      best_metascore IS NOT NULL AND (metascore >= YT_MIN_SCORE OR user_reviews_total >= 50)
   │      AND youtube_synced_at IS NULL
   ▼
 [1] SEARCH  — 100 units, ОДИН раз на игру
   │   search.list(q='"{title}" let\'s play', type=video, videoDuration=long,
   │               order=relevance, maxResults=25, relevanceLanguage=en, regionCode=US)
   │   videoDuration=long (>20 мин) — БЕСПЛАТНО отсекает Shorts и трейлеры
   ▼
 [2] HYDRATE — 1 unit на все 25
   │   videos.list(part=snippet,contentDetails,statistics,status, id=<25 ids>)
   │   → duration ISO-8601, viewCount, likeCount, liveBroadcastContent,
   │     caption(bool), defaultAudioLanguage, embeddable
   ▼
 [3] HARD FILTER   (reject → rejected_reason; строка всё равно пишется в БД)
   ▼
 [4] RANK          (взвешенный скор)
   ▼
 [5] SELECT best → is_selected = true
   ▼
 [6] TRANSCRIPT    (каскад T1 → T4)
   ▼
 [7] NORMALIZE     ([Music], повторы ASR, таймкоды)
   ▼
 [8] CHUNK / временнóй sampling
   ▼
 [9] LLM → review_summaries(audience='letsplay')
```

### 12.1 Хард-фильтры

| Правило | Причина |
|---|---|
| `liveBroadcastContent != 'none'` | стримы/анонсы — нет стабильного транскрипта |
| `duration_s < YT_MIN_DURATION_S` (600) | трейлеры, тизеры, Shorts, короткие ревью |
| `duration_s > YT_MAX_DURATION_S` (25 200 = 7 ч) | компиляции; транскрипт неподъёмный |
| title матчит `TRAILER_RX` | `trailer, teaser, announcement, reveal, launch trailer, cinematic` |
| title матчит `NON_LP_RX` | `review, tier list, news, ost, soundtrack, speedrun, all cutscenes, the movie, tips, guide, how to, best settings, benchmark, fps test, mods, ranking, iceberg` |
| `title_token_overlap(video, game) < 0.6` | не про эту игру (нормализация: lower, убрать пунктуацию, римские цифры, подзаголовки) |
| `defaultAudioLanguage` задан и не в `YT_ALLOWED_LANGS` (`en`) | LLM-резюме на непонятном языке |
| `embeddable = false` **и** `caption = false` | нечего показать и нечего расшифровать |

**«Не выбрать trailer вместо Let's Play»** — три независимых барьера: `videoDuration=long` в самом запросе, минимальная длительность 10 мин, regex по названию. Трейлер не проходит ни один из трёх.

### 12.2 Ранжирование

```
score = 0.32 · popularity        # log10(views+1) / log10(max_views_in_set+1)
      + 0.28 · title_relevance   # 0.6·token_overlap + 0.4·lp_keyword_score
      + 0.14 · format_fit        # колокол по длительности, пик 30 мин – 3 ч
      + 0.12 · engagement        # min(1, (likes/views) / 0.05)
      + 0.08 · recency_fit       # exp(−|upload − release| / 120 дней); 0 если раньше релиза
      + 0.06 · series_signal     # 1.0 если ≥3 ролика того же канала в кандидатах
      − penalties
```

`lp_keyword_score`: `+1.0` за `let's play|lets play|playthrough|full playthrough`; `+0.7` за `part 1|episode 1|ep 1|blind|first playthrough`; `+0.4` за `gameplay|walkthrough|full game|first look`.

Штрафы: `−0.25` за `no commentary` (**нет комментария → нечего суммаризировать — ключевой нюанс именно нашей задачи**), `−0.15` за `#shorts`, `−0.10` за компиляции `all endings|all bosses`, `−0.20` если `caption == false`.

Тай-брейк: предпочитаем «part 1 / episode 1» — начало прохождения даёт представление об игре в целом, а не о поздней главе.

**«Самый популярный ≠ первый результат»:** вес просмотров всего 0.32 из 1.0, и он логарифмический. Ролик с 7M просмотров, но `no commentary` и слабым совпадением названия проиграет 150k-ролику с явным «Let's Play … Part 1».

### 12.3 Бюджет квоты

`api_budgets` учитывает units посуточно; `YOUTUBE_DAILY_UNIT_BUDGET = 9500` (запас 500 от лимита 10 000). Перед `search.list` проверяется остаток. При нехватке:
1. Задача переносится (`countdown` до 00:05 UTC следующих суток), событие `warning`.
2. Если `YOUTUBE_ALLOW_SCRAPE_FALLBACK=true` — `ScrapeSource` с `discovery_source='scrape'` и пониженным доверием к рангу. **По умолчанию `false`** — это ToS-серая зона (§21).

Практический потолок: ~94 игры в сутки (94 × 101 = 9494 units). С гейтом [0] этого достаточно для 20–40 новых игр в час.

### 12.4 Каскад получения транскрипта

```python
PROVIDERS = [YtDlpProvider, HostedApiProvider, AsrProvider, MetadataOnlyProvider]

for p in PROVIDERS:
    if not p.enabled: continue
    try:
        t = p.fetch(video)
        if t and t.char_count >= YT_MIN_TRANSCRIPT_CHARS:      # 2000
            save(status='available', source=p.name); return t
        record_attempt(p, ok=False, error='empty_or_too_short')  # ← пустой ответ ≠ успех
    except ProviderError as e:
        record_attempt(p, ok=False, error=str(e))
save(status='unavailable')
```

**Критично (см. §2.2.2): HTTP 200 с нулевым телом обязан трактоваться как ОШИБКА, а не как «субтитров нет».** Именно на этом ломаются наивные реализации. Все попытки пишутся в `youtube_transcripts.attempts jsonb` — без этого отладку каскада не провести.

Провайдеры:
- **`YtDlpProvider`** — `yt-dlp --write-auto-subs --sub-langs en.* --skip-download --sub-format json3`, с `--proxy $YT_PROXY_URL` и sidecar `bgutil-pot-provider`. Отдельный контейнер, чтобы частые обновления `yt-dlp` не требовали пересборки воркера.
- **`HostedApiProvider`** — REST к `YT_TRANSCRIPT_API_URL` с `YT_TRANSCRIPT_API_KEY`. Единственный по-настоящему production-grade вариант; порядок цен — $1.6–5.7 за 1000 транскриптов.
- **`AsrProvider`** — `yt-dlp -f bestaudio` → `faster-whisper` (`small`, int8, CPU). Лимит `YT_ASR_MAX_DURATION_S = 3600`. **Выключен по умолчанию**: 3 ч видео ≈ 10 мин CPU и заметный трафик.
- **`MetadataOnlyProvider`** — всегда доступен: title + description + главы из description. Помечает `source='metadata_only'`; LLM получает явную инструкцию «это НЕ транскрипт, оценивай осторожно», в UI — дисклеймер.

### 12.5 Нормализация и чанкинг

- Удаляем `[Music]`, `[Applause]`, `[__]`, повторы одной фразы подряд (типичный артефакт ASR), таймкоды.
- Схлопываем сегменты в абзацы ~800 символов.
- Если превышает `AI_MAX_INPUT_TOKENS` — **временнóй sampling, а не обрезка**: равномерные `K` окон по всей длительности (начало, середина, конец — обязательно). Так резюме отражает всю игру, а не только первый час.

### 12.6 Отдельный промпт для Let's Play

Принципиально отличается от промпта по отзывам: блогер не оценивает, а *переживает*. `letsplay_v2` просит извлечь: реальный игровой цикл, темп и структуру, что вызывало восторг/фрустрацию у играющего, техническое состояние, доступность для новичка, объём показанного контента, и **явно отметить, что это опыт одного человека**. Результат — тот же `SummaryOut` с `audience='letsplay'`.

---

## 13. Background Jobs

### 13.1 Архитектура

**Celery 5.4 + Redis broker + Celery Beat.** Три очереди с разной параллельностью:

| Очередь | Задачи | Concurrency | Обоснование |
|---|---|---|---|
| `crawl` | `crawl.hourly_tick`, `crawl.reap_stale_items`, `crawl.reconcile_sitemap`, `maintenance.cleanup` | 1 | сериализация обхода; лок и так один |
| `enrich` | `game.sync`, `reviews.sync`, `similarity.*`, `letsplay.*` | 4 | IO-bound, ограничено rate-limiter'ом внешних API |
| `ai` | `ai.summarize`, `ai.summarize_letsplay`, `embedding.compute` | 2 | дорого; отдельная очередь = отдельный «кран» |

```python
task_acks_late = True                 # подтверждение ПОСЛЕ выполнения → смерть воркера = возврат в очередь
worker_prefetch_multiplier = 1        # не забирать пачку задач в память
broker_transport_options = {"visibility_timeout": 3600}
task_reject_on_worker_lost = True
task_time_limit = 900; task_soft_time_limit = 840
result_backend = None                 # результаты в своей таблице `jobs`, не в Redis
```

> **Почему собственная таблица `jobs`, а не Celery result backend.** Мониторинг (ТЗ §10) должен показывать историю, связи «run → job → game», причины ошибок и переживать перезапуск Redis. Redis result backend с TTL этого не даёт. `jobs` + `job_events` — durable-журнал в источнике истины.

### 13.2 Реестр задач

| Задача | Очередь | Retryable | Idempotent | Триггер | Идемпотентный ключ |
|---|---|---|---|---|---|
| `crawl.hourly_tick` | crawl | нет (следующий tick через час) | да (3 барьера) | Beat `minute=7` / manual | `crawl.tick:{date}:{hour}` |
| `crawl.reap_stale_items` | crawl | да | да | Beat каждые 5 мин | `reap:{date}:{slot}` |
| `crawl.reconcile_sitemap` | crawl | да (3) | да | Beat вс 03:00 | `reconcile:{isoweek}` |
| `game.sync` | enrich | **да** (5, 2ˣ) | **да** (upsert) | из tick'а / reaper | `game.sync:{date}:{slug}` |
| `reviews.sync` | enrich | да (5) | да | из `game.sync` | `reviews.sync:{game_id}:{date}` |
| `ai.summarize` | ai | да (6, 60→600 с) | да (fingerprint) | из `reviews.sync` | `ai.sum:{game_id}:{audience}:{fp8}` |
| `similarity.recompute` | enrich | да (3) | да (DELETE+INSERT в tx) | из `game.sync` / `ai.summarize` | `sim:{game_id}:{fp8}` |
| `similarity.refresh_stale` | enrich | да | да | Beat 04:00 | `sim.stale:{date}` |
| `embedding.compute` | ai | да (3) | да (source_hash) | из `game.sync` / `ai.summarize` | `emb:{game_id}:{hash8}` |
| `letsplay.discover` | enrich | да (3) | да (UNIQUE video_id) | из `game.sync` при прохождении гейта | `yt.disc:{game_id}` |
| `letsplay.transcript` | enrich | да (4) | да | из `discover` | `yt.tr:{video_id}` |
| `ai.summarize_letsplay` | ai | да (6) | да | из `transcript` | `ai.lp:{video_id}:{fp8}` |
| `maintenance.cleanup` | crawl | да | да | Beat 05:00 | `cleanup:{date}` |

**Синхронно в HTTP-запросе не выполняется ничего**, кроме чтения из БД (Rule 5 ТЗ §33). `POST /admin/crawl/run` только ставит задачу и отвечает 202.

### 13.3 State machine задачи

```text
                    ┌──────────┐
   enqueue ────────►│  queued  │
                    └────┬─────┘
        worker picks up  │
                    ┌────▼─────┐    success    ┌────────────┐
                    │ running  ├──────────────►│ succeeded  │
                    └────┬─────┘               └────────────┘
   retryable error       │       ┌──────────────┐
                    ┌────▼──────►│  retrying    │──── backoff ──┐
                    │            └──────────────┘                │
                    │                    ▲                        │
                    │                    └────────────────────────┘
                    │ attempts >= max        (возврат в queued)
                    ▼
              ┌──────────┐  ручной / плановый повтор  ┌────────────┐
              │  failed  │◄──────────────────────────►│    dead    │
              └──────────┘                            └────────────┘
                    ▲
  предусловие не выполнено (нет отзывов, квота, вход не изменился)
                    │
              ┌─────┴────┐
              │ skipped  │   ← НЕ ошибка. Отдельный счётчик в мониторинге.
              └──────────┘
```

Разделение `failed` (можно повторить руками) / `dead` (ретраи исчерпаны, нужен разбор) плюс явный `skipped` — то, без чего дашборд врёт: 500 пропусков из-за «вход не изменился» это здоровая система, а не 500 ошибок.

### 13.4 Классификация ошибок

```python
class RetryableError(Exception): ...          # 429, 5xx, таймаут, сеть, circuit open
class PermanentError(Exception): ...          # 404, невалидный slug, ошибка валидации после N
class SchemaDriftError(PermanentError): ...   # структура источника изменилась → алерт
class BudgetExhausted(RetryableError): ...    # квота → retry с countdown до следующих суток
```

Celery: `autoretry_for=(RetryableError,)`, `retry_backoff=True`, `retry_backoff_max=600`, `retry_jitter=True`. `PermanentError` → сразу `failed`, без ретраев.

### 13.5 Reaper — восстановление после падения воркера

```sql
UPDATE crawl_items
   SET status = 'pending', lease_expires_at = NULL,
       attempts = attempts + 1, error_message = 'lease expired'
 WHERE status = 'processing' AND lease_expires_at < now() AND attempts < 3
RETURNING id, game_slug, crawl_date;
-- attempts >= 3  →  status = 'failed'
```

Возвращённые строки переставляются как `game.sync`. Идемпотентный ключ включает дату, поэтому дублирующей задачи не возникнет; а если Celery всё же доставит задачу дважды — `jobs.idempotency_key` UNIQUE отсечёт вторую.

---

## 14. Monitoring Architecture

### 14.1 Выбор транспорта: SSE

| | Polling | **SSE (выбран)** | WebSocket |
|---|---|---|---|
| Направление | клиент→сервер | сервер→клиент | двунаправленно |
| Reconnect | вручную | **встроен в `EventSource`** (+ `Last-Event-ID`) | вручную/библиотека |
| Инфраструктура | ничего | обычный HTTP-ответ | протокольный апгрейд в прокси |
| Нагрузка | N клиентов × 1 rps впустую | push только при событии | push |
| Совместимость с FastAPI | тривиально | `StreamingResponse`, тривиально | нужен `websockets`, свой lifecycle |

Наш трафик **односторонний**: сервер сообщает о событиях, клиент ничего не шлёт (ручной запуск — обычный `POST`). WebSocket дал бы неиспользуемую половину и лишнюю сложность в прокси. Polling при 1-секундном интервале и 5 открытых вкладках — 5 rps впустую и задержка до секунды.

**Реализация:**
```
worker  ──► redis.publish("events:global", json)        ← при каждом emit()
            redis.publish(f"events:run:{run_id}", json)
                     │
FastAPI  GET /api/v1/monitoring/stream
            1. отдаёт полный снапшот события `snapshot`
            2. подписывается на канал(ы) Redis
            3. ретранслирует как SSE (`event: <type>\ndata: <json>\nid: <event_id>\n\n`)
            4. heartbeat `: ping` каждые 15 с (против таймаутов прокси)
            5. при reconnect с `Last-Event-ID` — доотдаёт пропущенное из job_events
```

**Fallback:** если `EventSource` не открылся за 5 с, фронт переключается на polling `GET /monitoring/status` раз в 3 с (TanStack Query `refetchInterval`). Требование ТЗ «мониторинг не должен требовать перезагрузки страницы» выполняется в обоих режимах.

### 14.2 Что показываем (полное покрытие ТЗ §10)

| Требование ТЗ | Источник |
|---|---|
| текущий статус сервиса | health-агрегат: БД, Redis, Beat alive (heartbeat-ключ), внешние API (circuit state) |
| worker status | `SELECT DISTINCT worker_id, max(ts)` из `job_events` + Celery `inspect ping` (кэш 10 с) |
| текущая операция | `jobs WHERE status='running'` + последнее событие активного `crawl_run` |
| количество обработанных игр | `crawl_runs.games_succeeded + games_failed` за текущий run и за сегодня |
| количество успешных | `games_succeeded` |
| количество ошибок | `games_failed` + `jobs WHERE status IN ('failed','dead')` |
| количество пропущенных | `games_skipped_dupe` + `jobs WHERE status='skipped'` |
| текущая страница Metacritic | `crawl_days.browse_page`, `phase`, `browse_pages_done` |
| время последнего запуска | `max(crawl_runs.started_at)` |
| время следующего запуска | вычисляется из Beat-расписания (`crontab minute=7`) → следующий час :07 |
| длительность обработки | `crawl_runs.duration_ms`, p50/p95 по `jobs.duration_ms` за 24 ч |
| AI jobs | `jobs WHERE job_type LIKE 'ai.%'` — очередь/в работе/успешно/ошибки, сумма `cost_usd` за сутки |
| YouTube jobs | `jobs WHERE job_type LIKE 'letsplay.%'` + остаток квоты из `api_budgets` |
| последние ошибки | `job_events WHERE level IN ('warning','error') ORDER BY ts DESC LIMIT 50` |
| history последних запусков | `crawl_runs ORDER BY started_at DESC LIMIT 20` |
| кнопка «Запустить сейчас» | `POST /admin/crawl/run` → 202 или 409 |

### 14.3 Типы событий SSE

| `event:` | Когда | Полезная нагрузка |
|---|---|---|
| `snapshot` | при подключении | весь объект `MonitoringStatus` |
| `run.started` / `run.finished` | старт/финиш crawl-run | `{run_id, status, counters, duration_ms}` |
| `run.progress` | после каждой страницы | `{run_id, phase, page, discovered, claimed}` |
| `job.started` / `job.finished` | смена статуса job'а | `{job_id, job_type, game_id, status, duration_ms}` |
| `game.synced` | игра обновлена | `{game_id, slug, title, changed}` |
| `error` | `level='error'` | `{message, error_class, job_id, game_id}` |
| `budget` | изменение остатка квоты | `{provider, units_used, units_limit}` |
| `heartbeat` | каждые 15 с | `{ts, workers_alive}` |

Все события **сначала пишутся в `job_events`** (durable), затем публикуются в Redis. Порядок именно такой: если Redis упадёт, история не потеряется, а после reconnect клиент доберёт по `Last-Event-ID`.

### 14.4 Метрики (Prometheus, опционально)

`crawl_runs_total{status}`, `crawl_run_duration_seconds`, `games_processed_total{result}`,
`jobs_total{job_type,status}`, `job_duration_seconds{job_type}`,
`external_request_duration_seconds{host,endpoint}`, `external_request_total{host,status}`,
`llm_tokens_total{direction,model}`, `llm_cost_usd_total`,
`youtube_quota_units_used`, `metacritic_schema_drift_total`,
`queue_depth{queue}` (из `LLEN` Redis).

Стек Prometheus/Grafana **не входит в MVP**, но `/metrics` есть — добавить его позже это правка `docker-compose`, а не кода.

---

## 15. API Specification

**База:** `/api/v1`. Формат — JSON. Ошибки — RFC 7807 `application/problem+json`. OpenAPI 3.1 генерируется FastAPI автоматически: `/api/v1/openapi.json`, Swagger UI `/api/v1/docs`.

**Пагинация:** два режима.
- *Keyset* (по умолчанию, стабильный): `?limit=&cursor=` → `{items, next_cursor, has_more}`. Курсор — base64 от `(sort_value, id)`. Без него на 200k строках `OFFSET 100000` деградирует, а при равных метаскорах страницы «плывут».
- *Offset* (для UI-пагинатора с номерами страниц): `?limit=&offset=` → дополнительно `total`. `total` считается через `COUNT(*) OVER ()` в том же запросе; при `offset > OFFSET_HARD_LIMIT` (10 000) — `400`.

### 15.1 Каталог

| Метод | URL | Параметры | Ответ | Коды |
|---|---|---|---|---|
| `GET` | `/games` | `q` (поиск по названию, ≥2 симв.), `platform` (slug, **повторяемый** → OR), `genre` (повторяемый), `min_score`, `max_score`, `score_base=metascore\|userscore` (по чему фильтровать/сортировать), `year_from`, `year_to`, `has_summary` (bool), `sort=metascore\|userscore\|release_date\|updated_at\|title\|relevance`, `order=asc\|desc`, `limit` (1..100, деф. 24), `cursor`/`offset` | `{items: GameListItem[], total?, next_cursor?, has_more, facets?}` | 200, 400, 422 |
| `GET` | `/games/{id_or_slug}` | `include=platforms,summaries,similar,youtube,reviews_preview` (деф. всё, кроме `reviews_preview`) | `GameDetail` | 200, 404 |
| `GET` | `/games/{id}/reviews` | `kind=critic\|user` (обяз.), `platform`, `sentiment=positive\|neutral\|negative`, `sort=date\|score\|helpful`, `limit`, `cursor` | `{items: Review[], total, next_cursor}` | 200, 404, 422 |
| `GET` | `/games/{id}/similar` | `limit` (1..24, деф. 12) | `{items: SimilarGame[]}` | 200, 404 |
| `GET` | `/games/{id}/youtube` | — | `{video: YoutubeVideo\|null, transcript_status, summary: Summary\|null}` | 200, 404 |
| `GET` | `/games/{id}/summaries` | `audience=critic\|user\|letsplay` | `{items: Summary[]}` | 200, 404 |
| `GET` | `/platforms` | `only_with_games=true` | `{items: Platform[]}` (с `games_count`) | 200 |
| `GET` | `/genres` | `only_with_games=true` | `{items: Genre[]}` | 200 |
| `GET` | `/search/suggest` | `q` (≥2), `limit` (деф. 8) | `{items: [{id, slug, title, cover_url, best_metascore}]}` | 200 |
| `GET` | `/stats` | — | `{games_total, games_with_summaries, reviews_total, last_crawl_at, coverage: {...}}` | 200 |

**Поиск (`q`)** — двухступенчатый, в одном SQL:
```sql
WHERE (title_norm ILIKE :prefix || '%'                       -- префикс, самый релевантный
       OR title_norm % :q                                     -- trigram similarity
       OR to_tsvector('simple', title_norm) @@ plainto_tsquery('simple', :q))
ORDER BY (title_norm ILIKE :prefix || '%') DESC,
         similarity(title_norm, :q) DESC,
         best_metascore DESC NULLS LAST, id DESC
```
При `sort=relevance` порядок именно такой; при других `sort` релевантность только фильтрует.

### 15.2 Мониторинг

| Метод | URL | Параметры | Ответ |
|---|---|---|---|
| `GET` | `/monitoring/status` | — | `MonitoringStatus` (см. ниже) |
| `GET` | `/monitoring/runs` | `limit` (деф. 20), `cursor`, `status`, `date_from`, `date_to` | `{items: CrawlRun[], next_cursor}` |
| `GET` | `/monitoring/runs/{id}` | — | `CrawlRun` + `items_breakdown` + `jobs_breakdown` |
| `GET` | `/monitoring/jobs` | `status`, `job_type`, `game_id`, `crawl_run_id`, `limit`, `cursor` | `{items: Job[], next_cursor}` |
| `GET` | `/monitoring/events` | `level`, `crawl_run_id`, `game_id`, `since` (ts), `limit` (деф. 100) | `{items: Event[]}` |
| `GET` | `/monitoring/stream` | `run_id` (опц.) | `text/event-stream` |
| `GET` | `/health` | — | `{status: ok\|degraded\|down, checks: {db, redis, beat, metacritic, llm, youtube}}` |
| `GET` | `/metrics` | — | Prometheus text format |

`MonitoringStatus`:
```jsonc
{
  "service": { "status": "ok", "version": "1.0.0", "uptime_s": 84213 },
  "scheduler": { "alive": true, "last_beat_at": "...", "next_run_at": "2026-09-05T21:07:00Z" },
  "workers": [ { "worker_id": "worker-1:34", "queues": ["enrich","ai"], "last_seen_at": "...", "active_jobs": 3 } ],
  "current_run": { "id": 412, "status": "running", "phase": "browse", "started_at": "...",
                   "elapsed_ms": 18400, "current_operation": "game.sync elden-ring-tarnished-edition",
                   "browse_page": 37,
                   "counters": { "discovered": 240, "claimed": 38, "succeeded": 31,
                                 "failed": 2, "skipped_dupe": 202 } },
  "today": { "crawl_date": "2026-09-05", "phase": "browse", "browse_page": 37,
             "runs": 6, "games_processed": 214, "succeeded": 205, "failed": 4, "skipped": 5 },
  "jobs": { "queued": {"crawl":0,"enrich":12,"ai":4},
            "running": 5, "failed_24h": 7, "dead": 1,
            "p50_duration_ms": 820, "p95_duration_ms": 5400 },
  "ai":  { "calls_24h": 63, "tokens_in_24h": 2140000, "tokens_out_24h": 88000,
           "cost_usd_24h": 1.84, "daily_limit_usd": 10.0, "failures_24h": 1 },
  "youtube": { "searches_24h": 41, "quota_units_used": 4183, "quota_limit": 9500,
               "transcripts_ok_24h": 28, "transcripts_failed_24h": 13,
               "by_source": {"ytdlp": 9, "hosted_api": 19, "metadata_only": 13} },
  "external": { "metacritic": {"circuit":"closed","p95_ms":410,"error_rate_1h":0.004},
                "llm": {"circuit":"closed","p95_ms":6100},
                "youtube": {"circuit":"closed","p95_ms":320} },
  "recent_errors": [ { "ts":"...", "level":"error", "event":"llm.failed",
                       "message":"...", "game_id": 8123, "job_id": 90211 } ]
}
```

### 15.3 Админ

| Метод | URL | Тело | Ответ | Коды |
|---|---|---|---|---|
| `POST` | `/admin/crawl/run` | `{"force": false, "max_games": 40}` | `{run_id, job_id, status:"accepted", stream_url}` | **202**, **409** (активный run), 401, 429 |
| `POST` | `/admin/games/{id}/resync` | `{"parts": ["detail","reviews","summary","youtube","similar"]}` | `{job_ids: []}` | 202, 401, 404 |
| `POST` | `/admin/jobs/{id}/retry` | — | `{job_id}` | 202, 401, 404, 409 |
| `POST` | `/admin/crawl/runs/{id}/cancel` | — | `{status:"cancelling"}` | 202, 401, 404 |

Все `/admin/*` требуют заголовок `X-Admin-Token`. Rate limit 10 req/min на токен.

### 15.4 Формат ошибок (RFC 7807)

```json
{
  "type": "https://api.example.com/errors/crawl-already-running",
  "title": "Crawl already running",
  "status": 409,
  "detail": "Run 412 started at 2026-09-05T20:07:03Z is still active.",
  "instance": "/api/v1/admin/crawl/run",
  "run_id": 412
}
```

Стандартные коды: `400` неверные параметры, `401` нет/неверный админ-токен, `404` нет ресурса, `409` конфликт состояния, `422` ошибка валидации Pydantic (с полем `errors[]`), `429` rate limit (+`Retry-After`), `500` внутренняя, `503` БД/Redis недоступны.

---

## 16. Frontend Structure

### 16.1 Маршруты

```text
app/
  layout.tsx                  header (лого, поиск, ссылка Monitoring), theme provider, footer
  page.tsx                    → redirect /games
  games/
    page.tsx                  ★ Список игр (Server Component, SSR + streaming)
      loading.tsx             skeleton-грид 12 карточек
      error.tsx               error boundary + retry
    [slug]/
      page.tsx                ★ Карточка игры (Server Component)
      loading.tsx             skeleton карточки
      not-found.tsx           404
  monitoring/
    page.tsx                  ★ Мониторинг (Client Component, SSE)
```

### 16.2 Страница `/games`

**Данные:** серверный fetch к `GET /api/v1/games` с параметрами из `searchParams` → фильтры живут в URL (шарится, работает «назад», SSR отдаёт готовый HTML).

**Layout:**
```text
┌──────────────────────────────────────────────────────────────────┐
│  ⌘ Metacritic Catalog          [ 🔍 поиск по названию…      ]     │
├──────────────────────────────────────────────────────────────────┤
│ ┌── Filters (sticky, ≥lg — колонка; <lg — Sheet «Фильтры») ──┐   │
│ │ Платформы  ☑PS5 ☐PS4 ☑PC ☐XSX ☐Switch 2 …                 │   │
│ │ Жанры      [multi-select]                                  │   │
│ │ Оценка     [◼─────◼] 0…100                                 │   │
│ │ Год        [2015]—[2026]                                   │   │
│ │ ☐ Только с AI-резюме                                       │   │
│ └────────────────────────────────────────────────────────────┘   │
│  Сортировка: [ Metascore ▾ ] [ Userscore ] [ Дата ] [ Название ]  │
│  База рейтинга: ( • Metascore  ○ Userscore )   ← выбор пользователя│
├──────────────────────────────────────────────────────────────────┤
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐             │
│  │ [cover]  │ │ [cover]  │ │ [cover]  │ │ [cover]  │  grid:       │
│  │      ⟨92⟩│ │      ⟨85⟩│ │      ⟨78⟩│ │      ⟨—⟩ │  2/3/4/6 col │
│  │ Название │ │ …        │ │ …        │ │ …        │  (sm/md/lg/xl)│
│  │ PS5 · PC │ │          │ │          │ │          │             │
│  │ Action…  │ │          │ │          │ │          │             │
│  │ ↻ 2 ч наз│ │          │ │          │ │          │             │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘             │
│                     ‹ 1 2 3 … 47 ›                               │
└──────────────────────────────────────────────────────────────────┘
```

Карточка (`GameCard`) содержит всё требуемое ТЗ §7: обложка (`next/image`, `sizes`, blur-placeholder), название, **основной рейтинг** — бейдж с цветовой шкалой Metacritic (зелёный ≥75, жёлтый 50–74, красный <50, серый `tbd`), список платформ (первые 3 + «+N»), краткая информация (жанр + разработчик), дата добавления/обновления (`updated_at`, относительное время).

**Про выбор рейтинга (ТЗ §7).** Даём пользователю переключатель «база рейтинга»: Metascore / Userscore. Он одновременно управляет и полем сортировки, и тем, какое число крупно показано на карточке. Дефолт — Metascore (это «фирменная» метрика Metacritic и она заполнена чаще). Оба значения показываются в карточке игры.

**Состояния:**
- *Loading* — `loading.tsx` со skeleton-гридом (Next streaming, не спиннер).
- *Empty* — иллюстрация + «Ничего не найдено» + список активных фильтров с кнопкой «Сбросить».
- *Error* — `error.tsx`: «Не удалось загрузить каталог» + `reset()`.
- *Partial* — если API вернул данные, но `stats.last_crawl_at` старше 6 ч, показываем ненавязчивый баннер «Данные могли устареть».

### 16.3 Страница `/games/[slug]`

```text
┌──────────────────────────────────────────────────────────────────┐
│  ‹ Назад к каталогу                                              │
│  ┌────────────┐   НАЗВАНИЕ ИГРЫ                        ⟨ 92 ⟩    │
│  │            │   Разработчик · Издатель · 2026        Metascore │
│  │   COVER    │   Action RPG · M                       ⟨ 8.4 ⟩   │
│  │            │   [PS5] [PC] [XSX] [Switch 2]          Userscore │
│  └────────────┘   ↗ Открыть на Metacritic                        │
├──────────────────────────────────────────────────────────────────┤
│  Оценки по платформам                                            │
│  ┌───────────────┬───────────┬───────────┬────────┬────────┐     │
│  │ Платформа     │ Metascore │ Userscore │ Крит.  │ Игроки │     │
│  │ PlayStation 5 │    92 ●   │   8.4 ●   │   86   │ 24 372 │     │
│  │ PC            │    89 ●   │   7.9 ●   │   41   │ 12 004 │     │
│  └───────────────┴───────────┴───────────┴────────┴────────┘     │
├──────────────────────────────────────────────────────────────────┤
│  Описание                     │  Видео (JW Player iframe)        │
├──────────────────────────────────────────────────────────────────┤
│  ★ Что говорят критики                    [ AI · обновлено 2 ч ] │
│  ┌── Нравится ──────────────┐ ┌── Не нравится ─────────────────┐ │
│  │ ● Gameplay  «…»          │ │ ● Performance  «…»             │ │
│  │ ● Story     «…»          │ │ ● Bugs         «…»             │ │
│  └──────────────────────────┘ └────────────────────────────────┘ │
│  Итог: …                                                          │
│  ▸ Показать 86 отзывов критиков                                   │
├──────────────────────────────────────────────────────────────────┤
│  ★ Что говорят игроки                     [ AI · обновлено 2 ч ] │
│  (та же структура)                                                │
│  ▸ Показать 500 отзывов игроков                                   │
├──────────────────────────────────────────────────────────────────┤
│  ▶ Let's Play                                                     │
│  ┌─────────┐  «Rise Tarnished! | Elden Ring Pt. 1»               │
│  │[thumb]  │  Marz · 918 763 просмотра · 2:02:27                  │
│  └─────────┘  ↗ Открыть на YouTube                                │
│  ★ Что видно из прохождения                [ AI · по транскрипту ]│
│  …                                                                │
├──────────────────────────────────────────────────────────────────┤
│  Похожие игры                                                     │
│  ┌────┐┌────┐┌────┐┌────┐┌────┐┌────┐   ← горизонтальная карусель │
│  │    ││    ││    ││    ││    ││    │     каждая → /games/{slug}   │
│  └────┘└────┘└────┘└────┘└────┘└────┘                             │
└──────────────────────────────────────────────────────────────────┘
```

Все элементы ТЗ §8 присутствуют. Похожие игры — `<Link href={`/games/${slug}`}>`, полноценная навигация (не JS-обработчик), работает средний клик и «открыть в новой вкладке».

**Деградация:** каждый AI-блок рендерится по отдельности. Нет резюме критиков → показываем «Резюме готовится» / «Недостаточно отзывов». Нет Let's Play → секция скрыта целиком. Нет похожих → «Пока не подобрали». Отсутствие любого блока **не ломает страницу**.

**Дисклеймер AI:** под каждым AI-блоком — мелким шрифтом «Сгенерировано моделью на основе N отзывов, обновлено {дата}». Для `metadata_only`-резюме Let's Play — дополнительно «по описанию ролика, без транскрипта».

### 16.4 Страница `/monitoring`

Client Component. При монтировании: `GET /monitoring/status` (мгновенный снапшот) → открывает `EventSource('/api/v1/monitoring/stream')`.

```text
┌──────────────────────────────────────────────────────────────────┐
│  Мониторинг            ● Онлайн   Следующий запуск: 21:07 (32 м) │
│                                    [ ⟳ Запустить обработку сейчас ]│
├───────────────┬───────────────┬───────────────┬──────────────────┤
│ Обработано    │ Успешно       │ Ошибок        │ Пропущено        │
│    214        │   205         │     4         │    5             │
├───────────────┴───────────────┴───────────────┴──────────────────┤
│  Текущий запуск #412   ● running · 00:18   фаза: browse · стр. 37│
│  ▸ game.sync elden-ring-tarnished-edition                        │
│  ████████████████░░░░░░░░  31 / 38                                │
├──────────────────────────────────────────────────────────────────┤
│  Воркеры              │  Очереди           │  Внешние сервисы    │
│  worker-1  ● 3 задачи │  crawl    0        │  Metacritic ● 410ms │
│  worker-2  ● 2 задачи │  enrich  12        │  LLM        ● 6.1s  │
│                       │  ai       4        │  YouTube    ● 320ms │
├──────────────────────────────────────────────────────────────────┤
│  AI: 63 вызова · $1.84 / $10.00   │  YouTube: 4183 / 9500 units  │
│  ████░░░░░░░░░░░░░░░░  18%        │  ████████░░░░░░░░  44%       │
├──────────────────────────────────────────────────────────────────┤
│  Последние ошибки                     │  История запусков        │
│  20:41 ⚠ llm.rate_limited  game 8123  │  #412 running   00:18    │
│  20:12 ✕ yt.transcript_unavailable    │  #411 ✓ 00:42   38 игр   │
│  …                                     │  #410 ✓ 00:39   40 игр   │
└──────────────────────────────────────────────────────────────────┘
```

Кнопка «Запустить обработку сейчас»:
- `disabled` пока `current_run.status === 'running'` — визуальное дублирование серверной защиты.
- При `409` — toast «Обработка уже идёт (запуск #412)», без падения UI.
- При `202` — оптимистично показываем «Запускается…», ждём события `run.started` из SSE.

Индикатор соединения: `● Онлайн` (SSE открыт) / `◐ Переподключение` / `○ Опрос` (fallback на polling).

### 16.5 Общие решения фронтенда

- **Server Components по умолчанию**, `'use client'` только там, где нужен стейт: фильтры, SSE-панель, карусель.
- **`next/image`** для всех обложек: Metacritic отдаёт неподписанные оригиналы по ~140 KB (§2.1.2), Next ресайзит и кэширует → трафик падает на порядок. `remotePatterns` разрешает только `www.metacritic.com`.
- **Кэширование:** список — `revalidate: 60`; карточка — `revalidate: 300` + `generateStaticParams` для топ-500 игр; мониторинг — `cache: 'no-store'`.
- **Тёмная тема** через `next-themes` + CSS-переменные (`prefers-color-scheme` по умолчанию).
- **Доступность:** видимый фокус, `aria-live` для счётчиков мониторинга, контраст цветовых бейджей ≥4.5:1 (проверяется в CI через axe), полная клавиатурная навигация.
- **Responsive:** грид 2/3/4/6 колонок; фильтры уезжают в `Sheet` на мобильном; таблица платформ горизонтально скроллится в своём контейнере.

---

## 17. Docker Architecture

### 17.1 Сервисы

```yaml
services:
  postgres:    # pgvector/pgvector:pg16 — образ с уже собранным pgvector
  redis:       # redis:7-alpine, appendonly yes
  migrate:     # тот же образ, что backend; alembic upgrade head; restart: "no"
  backend:     # FastAPI + uvicorn, порт 8000
  worker:      # celery -A app.worker worker -Q crawl,enrich,ai
  scheduler:   # celery -A app.worker beat
  frontend:    # Next.js standalone, порт 3000
  ytdlp:       # (опц., profile: youtube) bgutil PO-token provider sidecar
```

**Почему `worker` и `scheduler` — разные контейнеры.** Beat обязан быть в единственном экземпляре, иначе одно расписание породит N тиков. Воркеров хочется масштабировать (`--scale worker=3`). Объединение сделало бы масштабирование воркеров невозможным без дублирования расписания.

**Почему `worker` один контейнер на три очереди (а не три контейнера).** Для MVP объёмы малы, а `-Q crawl,enrich,ai` с общим пулом проще эксплуатировать. Разделение — одна строка в compose, когда понадобится изоляция «дорогих» AI-задач: `worker-ai` с `-Q ai -c 2`, `worker-crawl` с `-Q crawl -c 1`.

**Почему отдельный `migrate`.** Миграции не должны выполняться в `entrypoint` backend'а: при `--scale backend=3` три реплики стартуют гонку на `alembic upgrade`. Отдельный one-shot контейнер с `depends_on: postgres: condition: service_healthy`, а backend и worker ждут `migrate: condition: service_completed_successfully`.

### 17.2 Порядок старта

```text
postgres (healthcheck: pg_isready)
   │
redis (healthcheck: redis-cli ping)
   │
   └──► migrate (alembic upgrade head + seed платформ) ──[completed_successfully]──┐
                                                                                   │
                            ┌──────────────────────────────────────────────────────┤
                            ▼                    ▼                    ▼            ▼
                        backend             worker              scheduler      frontend
                     (healthcheck:      (healthcheck:        (healthcheck:   (depends_on
                      GET /health)       celery inspect       файл-heartbeat  backend healthy)
                                          ping)                моложе 120 с)
```

### 17.3 Dockerfiles

**`docker/backend.Dockerfile`** — multi-stage:
```dockerfile
FROM python:3.12-slim AS base
    ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
    RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates && rm -rf /var/lib/apt/lists/*
    COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

FROM base AS deps
    WORKDIR /app
    COPY pyproject.toml uv.lock ./
    RUN uv sync --frozen --no-dev            # слой кэшируется, пока не менялись зависимости

FROM base AS runtime
    WORKDIR /app
    COPY --from=deps /app/.venv /app/.venv
    ENV PATH="/app/.venv/bin:$PATH"
    COPY app ./app
    COPY alembic.ini ./
    RUN useradd -m -u 10001 appuser && chown -R appuser /app
    USER appuser                             # не root
    HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
      CMD curl -fsS http://localhost:8000/api/v1/health || exit 1
    CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
```

Воркер использует **тот же образ** с другим `command` — общий код, один build, никакого дрейфа версий.

**`docker/frontend.Dockerfile`** — `node:22-alpine`, три стадии (deps → build → runner), `output: 'standalone'` в `next.config.js`, `USER node`.

### 17.4 Volumes, сети, ресурсы

```yaml
volumes:
  pgdata:        # /var/lib/postgresql/data
  redisdata:     # /data (appendonly)
  modelcache:    # /home/appuser/.cache/fastembed — иначе модель качается при каждом старте
  httpcache:     # /app/.cache/http — dev-кэш ответов Metacritic
networks:
  internal:      # postgres, redis — БЕЗ публикации портов наружу в prod-override
  public:        # backend, frontend
```

В `docker-compose.yml` (dev) порты 5432/6379 публикуются для удобства отладки; в `docker-compose.prod.yml` — не публикуются, а `backend` за reverse-proxy.

Лимиты: `postgres` 1 CPU / 1 GB, `worker` 2 CPU / 2 GB (эмбеддинги), `backend` 1 CPU / 512 MB, `frontend` 1 CPU / 512 MB.

### 17.5 Запуск

```bash
cp .env.example .env      # заполнить ANTHROPIC_API_KEY, YOUTUBE_API_KEY, ADMIN_TOKEN
docker compose up -d      # → http://localhost:3000
docker compose --profile youtube up -d          # + PO-token sidecar
docker compose --profile seed run --rm seed     # опц.: бэкфилл первых 200 игр
```

Никаких ручных шагов, кроме заполнения `.env`. Миграции и сид справочника платформ выполняются автоматически.

---

## 18. Environment Variables (`.env.example`)

```dotenv
# ─────────────── Core ───────────────
APP_ENV=development                     # development | production
APP_NAME=metacritic-service
LOG_LEVEL=INFO                          # DEBUG | INFO | WARNING | ERROR
LOG_FORMAT=json                         # json | console
TZ=UTC

# ─────────────── Database ───────────────
POSTGRES_USER=mc
POSTGRES_PASSWORD=change-me
POSTGRES_DB=metacritic
DATABASE_URL=postgresql+psycopg://mc:change-me@postgres:5432/metacritic
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=10
DB_ECHO=false
DB_STATEMENT_TIMEOUT_MS=15000

# ─────────────── Redis ───────────────
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
CACHE_TTL_S=300

# ─────────────── API / CORS / Security ───────────────
API_PREFIX=/api/v1
CORS_ALLOW_ORIGINS=http://localhost:3000
ADMIN_TOKEN=change-me-long-random-string     # заголовок X-Admin-Token
ADMIN_RATE_LIMIT_PER_MIN=10
PUBLIC_RATE_LIMIT_PER_MIN=120
TRUSTED_HOSTS=localhost,127.0.0.1
CONTACT_URL=https://example.com/contact      # уходит в User-Agent наших запросов

# ─────────────── Metacritic ───────────────
METACRITIC_SOURCE=json_api                   # json_api | html | browser
METACRITIC_BASE_URL=https://www.metacritic.com
METACRITIC_API_BASE_URL=https://backend.metacritic.com
METACRITIC_API_KEY=1MOZgmNFxvmljaQR1X9KAij9Mo4xAY3u   # публичный ключ фронтенда; не валидируется
METACRITIC_USER_AGENT=MetacriticGamesService/1.0 (+${CONTACT_URL}; educational project)
METACRITIC_RPS=2.0
METACRITIC_BURST=4
METACRITIC_MAX_CONCURRENCY=2
REQUEST_TIMEOUT_S=20
REQUEST_CONNECT_TIMEOUT_S=5
REQUEST_MAX_RETRIES=5
REQUEST_BACKOFF_BASE_S=1.0
REQUEST_BACKOFF_MAX_S=32.0
CIRCUIT_FAIL_THRESHOLD=5
CIRCUIT_RESET_TIMEOUT_S=300
HTTP_CACHE_ENABLED=false                     # true в dev — не долбить сайт при разработке
SCHEMA_DRIFT_THRESHOLD=10                    # ошибок валидации за час до авто-фолбэка на html

# ─────────────── Crawler ───────────────
CRAWL_ENABLED=true
CRAWL_INTERVAL_CRON=7 * * * *                # каждый час в :07
CRAWL_TIMEZONE=UTC                           # граница «нового дня»
CRAWL_LOCK_TTL_S=3600
CRAWL_MAX_GAMES_PER_RUN=40
NEW_RELEASES_LIMIT=20
BROWSE_PAGE_SIZE=24
BROWSE_MAX_OFFSET=100000                     # глубже пагинация теряет смысл
CRAWL_ITEM_LEASE_TTL_S=1800
CRAWL_ITEM_MAX_ATTEMPTS=3
RECONCILE_ENABLED=true
RECONCILE_CRON=0 3 * * 0                     # вс 03:00
RECONCILE_MAX_NEW=5000

# ─────────────── Reviews ───────────────
REVIEWS_ENABLED=true
REVIEWS_PAGE_SIZE=100
REVIEWS_CRITIC_CAP=200
REVIEWS_USER_CAP=500
REVIEWS_MAX_PLATFORMS=4
REVIEWS_MIN_PLATFORM_CRITICS=3
REVIEWS_INCREMENTAL_MAX_PAGES=5
REVIEWS_STALE_PAGE_TOLERANCE=2               # sort=date немонотонна — нужен допуск
REVIEWS_FULL_RESYNC_DAYS=30

# ─────────────── LLM ───────────────
LLM_ENABLED=true
LLM_PROVIDER=anthropic                       # anthropic | null
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-opus-5                      # claude-sonnet-5 / claude-haiku-4-5 — дешевле
LLM_EFFORT=low                               # low | medium | high
LLM_MAX_OUTPUT_TOKENS=4000
LLM_TIMEOUT_S=300
LLM_MAX_RETRIES=6
AI_MAX_INPUT_TOKENS=60000
AI_CHUNK_TOKENS=15000
AI_MIN_NEW_REVIEWS=10
AI_MIN_NEW_RATIO=0.20
AI_MAX_SUMMARY_AGE_DAYS=90
AI_DAILY_COST_LIMIT_USD=10.0
AI_MAX_CALLS_PER_HOUR=100
AI_USE_BATCH=false                           # true для бэкфилла: −50% стоимости
AI_PROMPT_CACHE=true

# ─────────────── Embeddings / Similarity ───────────────
EMBEDDING_ENABLED=true
EMBEDDING_PROVIDER=local                     # local | voyage | openai
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
EMBEDDING_DIM=384
EMBEDDING_API_KEY=
SIMILARITY_TOP_N=12
SIMILARITY_CANDIDATE_LIMIT=400
SIMILARITY_W_META=0.6
SIMILARITY_W_EMBED=0.4
SIMILARITY_REFRESH_STALE_DAYS=7
SIMILARITY_REFRESH_CRON=0 4 * * *

# ─────────────── YouTube ───────────────
YOUTUBE_ENABLED=true
YOUTUBE_API_KEY=
YOUTUBE_DAILY_UNIT_BUDGET=9500               # официальный лимит 10000
YOUTUBE_SEARCH_MAX_RESULTS=25
YOUTUBE_ALLOW_SCRAPE_FALLBACK=false          # ToS-серая зона; включать осознанно
YT_MIN_SCORE=60                              # гейт: не тратим квоту на низкие оценки
YT_MIN_DURATION_S=600
YT_MAX_DURATION_S=25200
YT_ALLOWED_LANGS=en
YT_MIN_TRANSCRIPT_CHARS=2000

YT_TRANSCRIPT_PROVIDERS=ytdlp,hosted_api,metadata_only   # порядок каскада; asr — по желанию
YT_PROXY_URL=                                # residential-прокси для yt-dlp
YT_POT_PROVIDER_URL=http://ytdlp:4416        # bgutil PO-token sidecar
YT_TRANSCRIPT_API_URL=
YT_TRANSCRIPT_API_KEY=
YT_ASR_ENABLED=false
YT_ASR_MODEL=small
YT_ASR_MAX_DURATION_S=3600

# ─────────────── Celery / Workers ───────────────
CELERY_WORKER_CONCURRENCY=4
CELERY_QUEUES=crawl,enrich,ai
CELERY_TASK_TIME_LIMIT_S=900
CELERY_TASK_SOFT_TIME_LIMIT_S=840
CELERY_VISIBILITY_TIMEOUT_S=3600

# ─────────────── Monitoring ───────────────
SSE_HEARTBEAT_S=15
SSE_MAX_CLIENTS=50
EVENTS_RETENTION_DAYS=30
JOBS_RETENTION_DAYS=90
CRAWL_ITEMS_RETENTION_DAYS=180
METRICS_ENABLED=true

# ─────────────── Frontend (build+runtime) ───────────────
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1     # для браузера
API_INTERNAL_BASE_URL=http://backend:8000/api/v1          # для SSR внутри сети
NEXT_PUBLIC_SSE_URL=http://localhost:8000/api/v1/monitoring/stream
NEXT_PUBLIC_APP_NAME=Metacritic Catalog
```

Все переменные читаются через `pydantic-settings` с типами и валидацией. **Отсутствие обязательной переменной валит приложение на старте**, а не в рантайме при первом запросе. Секреты (`LLM_API_KEY`, `YOUTUBE_API_KEY`, `ADMIN_TOKEN`, `POSTGRES_PASSWORD`) не имеют дефолтов и не логируются — `Settings.__repr__` их маскирует.

---

## 19. Testing Strategy

### 19.1 Пирамида

```text
        ╱ E2E (Playwright) ╲                ~12 сценариев, ~3 мин
       ╱───────────────────╲
      ╱  Integration        ╲               ~60 тестов, ~90 с (testcontainers)
     ╱  (Postgres/Redis/API) ╲
    ╱─────────────────────────╲
   ╱        Unit               ╲            ~250 тестов, <10 с
  ╱──────────────────────────────╲
```

**Железное правило: ни один тест не ходит в реальный Metacritic / YouTube / LLM.** Всё через фикстуры и `respx`. Отдельная опциональная сюита `@pytest.mark.contract` (запускается вручную и по расписанию, а не в PR-CI) бьёт по реальным API и проверяет, что схема не изменилась — это ранний детектор дрейфа.

### 19.2 Unit-тесты

| Модуль | Что проверяем |
|---|---|
| `parsers/metacritic` | Реальные JSON-фикстуры (`elden-ring.json`, `cyberpunk-2077.json`, `orbitals.json`, `bioeden.json`) → корректные DTO. Отдельно: `video=null`, `rating=null`, `description=null`, `criticScoreSummary.score=null`, платформа без оценок. |
| `parsers/schema_drift` | Убрали обязательное поле → `SchemaDriftError`. Добавили неизвестное → парсится нормально. |
| `normalizers` | slugify, `title_norm` (юникод, диакритика, пунктуация), сборка URL обложки, вычисление `source_fingerprint` (стабилен при перестановке ключей!). |
| `dedupe` | `dedupe_key` критика стабилен при изменении регистра/пробелов; различается при разном тексте; user-ключ = UUID. |
| `crawl_algorithm` | Табличные тесты по всем 11 сценариям §7.4 — с фейковым репозиторием и фейковыми часами (`freezegun`). |
| `similarity` | Известные пары: две части одной франшизы → score > 0.7; Action RPG vs Puzzle → < 0.2; штраф за DLC применён; результат детерминирован. |
| `youtube_ranking` | **Ключевая сюита.** Набор из 25 реальных карточек (зафиксированных как фикстура) → трейлер никогда не побеждает; `no commentary` проигрывает; live отсеян; ролик другой игры отсеян; при одинаковой релевантности выигрывает более просматриваемый. |
| `ai/sampling` | Стратификация соблюдает пропорции и пол 15%; бюджет токенов не превышен; короткие отзывы отброшены; выборка детерминирована при фиксированном seed. |
| `ai/should_regenerate` | Все ветви решения; одинаковый fingerprint → SKIP. |
| `transcript_cascade` | Пустой 200 от провайдера → попытка следующего, **не** «субтитров нет»; все провайдеры упали → `unavailable`; `attempts` заполнен. |
| `budget` | Квота YouTube и лимит стоимости LLM корректно блокируют и переносят задачи. |

### 19.3 Интеграционные тесты (`testcontainers`: Postgres 16 + Redis 7)

- **Миграции:** `alembic upgrade head` → `downgrade base` → `upgrade head` на чистой БД.
- **Дедупликация:** два конкурентных `claim_games` из разных сессий на одну игру → ровно одна строка. Проверяется реальным параллелизмом (`ThreadPoolExecutor`), а не моком.
- **Один активный run:** две одновременные вставки `crawl_runs(status='running')` → `UniqueViolation` у второй.
- **Upsert игры:** повторный `game.sync` с тем же payload → 0 новых строк, `updated_at` не изменился; с изменённым метаскором → обновились и `game_platforms`, и роллапы в `games`.
- **Upsert отзывов:** батч из 100, повтор → `inserted=0, updated=0`; изменённый текст → `updated=1`.
- **Reaper:** строка с истёкшим lease возвращается в `pending`; после 3 попыток → `failed`.
- **API:** `httpx.AsyncClient` + `ASGITransport` — все эндпоинты §15: пагинация (keyset и offset), фильтры (мульти-платформа = OR), сортировки, 404, 409 на повторный `/admin/crawl/run`, 401 без токена.
- **SSE:** подписка → `emit()` в другом соединении → событие получено; heartbeat приходит; reconnect с `Last-Event-ID` доотдаёт пропущенное.
- **Celery:** `task_always_eager=False` + реальный воркер в потоке; проверка `acks_late` (kill воркера → задача вернулась), идемпотентности (двойная постановка → одна `jobs`-строка).
- **pgvector:** вставка 1000 случайных векторов, HNSW-поиск возвращает ожидаемых соседей.

### 19.4 E2E (Playwright, против `docker compose` со seeded-БД)

| Сценарий | Шаги |
|---|---|
| Основной флоу ТЗ §25 | `/games` → поиск «elden» → фильтр PS5 → сортировка по Userscore → открыть игру → проверить все блоки → кликнуть похожую → оказаться на её странице |
| Пагинация | стр. 2 → «назад» → фильтры сохранились в URL |
| Empty state | поиск «zzzzqqq» → «Ничего не найдено» + «Сбросить фильтры» работает |
| Error state | API отдаёт 500 (route-mock) → показан error-boundary с кнопкой повтора |
| Мониторинг | открыть `/monitoring` → снапшот отрендерился → сервер шлёт SSE-событие → счётчик изменился **без перезагрузки** |
| Ручной запуск | нажать «Запустить сейчас» → 202 → появился активный run → повторный клик даёт toast 409 |
| Responsive | 375 px: фильтры в Sheet, грид 2 колонки, нет горизонтального скролла у `body` |
| Доступность | axe-core на `/games` и `/games/[slug]` — 0 нарушений уровня serious/critical |

### 19.5 Фикстуры

`tests/fixtures/metacritic/` — реальные ответы, снятые при исследовании (обезличивать нечего, это публичные данные):
`finder_new_releases.json`, `finder_browse_p1.json`, `composer_elden_ring.json`, `composer_cyberpunk.json`, `composer_orbitals.json` (одна платформа), `composer_bioeden.json` (нет видео, нет ESRB), `reviews_critic_p1.json`, `reviews_user_p1.json`, `stats_user_xbox_one.json`, `game_page.html` (для HTML-fallback), `browse_page.html`, `games_sitemap_shard.xml`.

`tests/fixtures/youtube/` — `search_elden_ring.json` (25 кандидатов, включая 3 трейлера, 1 live, 2 shorts, 1 no-commentary), `videos_list.json`, `transcript_json3.json`, `transcript_empty_200.txt` (**воспроизводит найденный баг пустого ответа**).

`tests/fixtures/llm/` — записанные `SummaryOut`-ответы для `RecordingLLMClient`.

### 19.6 CI (GitHub Actions)

```
lint      : ruff check, ruff format --check, mypy --strict app/services app/domain
unit      : pytest -m "not integration and not e2e and not contract"   (<30 c)
integration: pytest -m integration  (testcontainers)                    (<3 мин)
build     : docker build backend + frontend
e2e       : docker compose up -d && seed && playwright test            (<5 мин)
contract  : (nightly, не блокирует PR) pytest -m contract → алерт при дрейфе схемы
```

Порог покрытия: ≥85% на `app/services`, `app/parsers`, `app/domain`; адаптеры покрываются интеграционными.

---

## 19-A. Existing Game Update — дифференциальная синхронизация (ТЗ §18)

Игра, уже присутствующая в БД, ежедневно захватывается заново (§7.3), но это **не значит**, что мы каждый раз проделываем всю работу. `game.sync` работает так:

```python
def game_sync(slug, crawl_item_id, crawl_run_id):
    detail = metacritic.game_detail(slug)                 # 1 запрос
    normalized = normalize(detail)
    fp_new = fingerprint(normalized)                      # sha256 по нормализованному подмножеству

    existing = repo.find_game(mc_title_id=normalized.mc_title_id) \
            or repo.find_game(mc_slug=slug)

    if existing and existing.mc_slug != slug:
        log.warning("game.slug_changed", old=existing.mc_slug, new=slug)
        # slug обновляем, id — источник истины

    changed = existing is None or existing.source_fingerprint != fp_new

    with db.begin():                                      # ОДНА транзакция
        game_id = repo.upsert_game(normalized)            # UPDATE только изменившихся колонок
        repo.upsert_platforms(game_id, normalized.platforms)   # добавляет НОВЫЕ платформы
        repo.upsert_genres(game_id, ...)
        repo.upsert_companies(game_id, ...)
        repo.refresh_rollups(game_id)                     # best_*, platform_ids, genre_ids
        repo.mark_crawl_item(crawl_item_id, status='done', changed=changed)

    # ── дифференциальный диспетчер downstream ──
    scores_moved = any(p.metascore != old.metascore or p.userscore != old.userscore
                       for p, old in zip_platforms(normalized, existing))
    reviews_stale = (existing is None
                     or existing.reviews_synced_at < now() - REVIEWS_MIN_INTERVAL   # 24 ч
                     or scores_moved)                      # оценка сдвинулась ⇒ появились отзывы

    if reviews_stale:
        enqueue("reviews.sync", game_id)                   # → внутри решит про ai.summarize
    if changed:
        enqueue("embedding.compute", game_id)
        enqueue("similarity.recompute", game_id)
    if youtube_gate_passed(game_id) and existing.youtube_synced_at is None:
        enqueue("letsplay.discover", game_id)
```

Что это даёт по каждому пункту ТЗ §18:

| Требование ТЗ §18 | Как выполняется |
|---|---|
| не создавать duplicate | поиск по `mc_title_id` → `mc_slug`; UNIQUE на обоих |
| обновлять изменившиеся данные | `upsert_game` пишет только реально отличающиеся колонки (сравнение в Python, один `UPDATE ... SET` с изменёнными полями) |
| обновлять scores | `upsert_platforms` обновляет `metascore`/`userscore` и счётчики по каждой платформе |
| добавлять новые platforms | `ON CONFLICT (game_id, platform_id)` — новая платформа вставляется, существующая обновляется; исчезнувшая **не удаляется** (история важнее) |
| добавлять новые reviews | `reviews.sync` с инкрементальной стратегией §9.3 |
| пересчитывать summaries при необходимости | `should_regenerate` (§10.4) — жёсткий фильтр по `input_fingerprint` |
| обновлять YouTube при необходимости | только если ранее не искали или `YT_RESEARCH_AFTER_DAYS` прошло и видео не найдено — квота дорогая |

**Ключевой эффект:** типичный «повторный» день для уже известной игры стоит **1 HTTP-запрос** и 0 вызовов LLM. Именно это делает требование «каждый час, каждый день заново» экономически выполнимым.

---

## 20. Error Handling

### 20.1 Матрица отказов

| Что упало | Что происходит | Итог для данных | Восстановление |
|---|---|---|---|
| **Metacritic 5xx / таймаут** | retry ×5 с backoff; при исчерпании run = `partial`, **курсор не двигается** | Всё, что успели, сохранено | Следующий часовой tick повторит ту же страницу |
| **Metacritic 429** | уважаем `Retry-After`, глобально снижаем RPS вдвое на 10 мин | — | Автоматически |
| **Metacritic полностью недоступен** | circuit breaker open 5 мин; `game.sync` мгновенно падает в retry без сетевых попыток | БД не тронута; API отдаёт прежние данные | half-open проба через 5 мин |
| **Изменилась схема** | `SchemaDriftError` → `PermanentError`, событие `error`, метрика; при превышении порога — авто-переключение на `HtmlSource` | Игра помечена `failed`, остальные обрабатываются | Ручная правка парсера; nightly contract-тест ловит заранее |
| **LLM 429/529/таймаут** | retry ×6, 60→600 с | **Game=SUCCESS, Reviews=SUCCESS, Summary=RETRY** (Сценарий 1 ТЗ) | Автоматически |
| **LLM недоступен долго** | circuit open; `ai.*` копятся в очереди `ai` | Прежнее `is_current`-резюме продолжает отдаваться | Автоматически при восстановлении |
| **Reviews не загрузились** | `reviews.sync` retry ×5 → `failed`; `game.sync` уже `succeeded` | **Game=SUCCESS, Reviews=FAILED/RETRY** (Сценарий 2 ТЗ) | Reaper/следующий день |
| **YouTube quota exhausted** | `BudgetExhausted` → retry с `countdown` до 00:05 UTC | **Данные игры не считаются failed** (Сценарий 3 ТЗ) | Автоматически на следующие сутки |
| **YouTube недоступен** | circuit open; `letsplay.*` retry | Игра/отзывы/резюме не затронуты | Автоматически |
| **Транскрипт недоступен** | каскад провайдеров → `metadata_only` → `unavailable` | Ссылка на ролик всё равно сохранена и показана | Периодический повтор для `unavailable` старше 14 дней |
| **Worker crash** | `acks_late` → задача возвращается в очередь через `visibility_timeout`; `crawl_items` с истёкшим lease реанимируются reaper'ом | Незавершённая работа переигрывается **идемпотентно** (Сценарий 4 ТЗ) | ≤5 мин |
| **App restart** | Beat поднимается, читает `crawl_days`, продолжает с курсора | Ничего не потеряно | Автоматически |
| **DB restart** | Пул пересоздаёт соединения; текущие задачи падают в retry; API отдаёт 503 на `/health` | Транзакции атомарны — частично записанных игр не бывает | Автоматически |
| **Redis restart** | Celery переподключается; **очередь теряется** (AOF снижает риск); лок теряется | `crawl_runs_single_active` в БД не даст запуститься второму run'у; незакрытые run'ы «протухают» по `RUN_STALE_TIMEOUT` и помечаются `failed` | ≤1 час (следующий tick) |
| **Два tick'а одновременно** | Redis-лок → DB unique → `crawl_items` unique | Дублей нет ни при каком раскладе | — |

### 20.2 Границы транзакций

| Операция | Транзакция | Обоснование |
|---|---|---|
| Upsert игры + платформы + жанры + компании + роллапы | **одна** | Иначе возможна игра без платформ или роллапы, не соответствующие `game_platforms` |
| Батч отзывов (100 шт.) | одна на батч | Батчи независимы; частичный успех допустим и полезен |
| Запись `review_summaries` + снятие `is_current` со старой | **одна** | Иначе два «текущих» резюме или ни одного |
| `similar_games`: DELETE + INSERT | **одна** | Иначе окно, когда похожих нет вовсе |
| `crawl_days` курсор | отдельная, **после** успешного claim | Курсор не должен двигаться, если claim не удался |
| `crawl_items` claim | автокоммит одиночного INSERT | Захват обязан быть виден другим воркерам немедленно |
| `job_events` | автокоммит | Append-only, не должен откатываться вместе с бизнес-транзакцией |

**Правило:** `job_events` и метрики пишутся **вне** бизнес-транзакции — иначе откат бизнес-логики стирает и запись о том, что она провалилась.

### 20.3 Гарантии: БД vs приложение

| Гарантия | Кем обеспечивается |
|---|---|
| Игра уникальна | **БД** — `UNIQUE(mc_slug)`, `UNIQUE(mc_title_id)` |
| Игра обрабатывается ≤1 раза в день | **БД** — `UNIQUE(crawl_date, game_slug)` |
| Отзыв не дублируется | **БД** — `UNIQUE(game_platform_id, kind, dedupe_key)` |
| Активен ≤1 crawl-run | **БД** — partial `UNIQUE ... WHERE status='running'` |
| Ровно одна lead-платформа | **БД** — partial `UNIQUE(game_id) WHERE is_lead` |
| Ровно одно текущее резюме | **БД** — partial `UNIQUE(game_id, audience) WHERE is_current` |
| Задача не выполнится дважды | **БД** — `UNIQUE(jobs.idempotency_key)` |
| Роллапы согласованы с `game_platforms` | **приложение** — одна транзакция (можно было бы триггером; выбран явный код ради тестируемости) |
| LLM не вызывается зря | **БД** — `UNIQUE(game_id, audience, input_fingerprint)` + **приложение** — `should_regenerate` |
| Rate limit к внешним API | **приложение** — Redis token-bucket |
| Порядок обработки страниц | **приложение** — курсор в `crawl_days` |

Осознанный принцип: **всё, что можно сделать констрейнтом, делается констрейнтом.** Логика в приложении может содержать баг; `UNIQUE` — нет.

---

## 21. Security

### 21.1 Модель угроз (для учебного/внутреннего сервиса)

| # | Угроза | Вектор | Меры |
|---|---|---|---|
| T1 | Неавторизованный запуск обхода (DoS на Metacritic от нашего имени, расход LLM-бюджета) | `POST /admin/crawl/run` | Заголовок `X-Admin-Token` (≥32 байта энтропии), сравнение через `secrets.compare_digest`; rate limit 10/мин; `409` при активном run'е; лимит `CRAWL_MAX_GAMES_PER_RUN` не обходится параметром запроса |
| T2 | SQL-инъекция | `q`, `platform`, `sort`, `cursor` | Только параметризованные запросы SQLAlchemy; `sort`/`order` — `Literal`-enum в Pydantic (не подстановка строки); поле сортировки маппится через словарь-белый-список, а не форматируется в SQL |
| T3 | XSS | Описания игр и **тексты отзывов пользователей** — сторонний контент | React экранирует по умолчанию; `dangerouslySetInnerHTML` **запрещён** правилом ESLint; на бэкенде дополнительно `bleach`-очистка при сохранении описаний; CSP `default-src 'self'` |
| T4 | **SSRF** | `cover_url`, `video_embed_url`, `url` отзыва, YouTube URL — всё приходит извне | **Ключевая мера:** сервер никогда не выполняет HTTP-запрос по URL из данных. Изображения проксирует `next/image` с `remotePatterns: [{hostname: 'www.metacritic.com'}]` — жёсткий whitelist. Видео — `<iframe>` с `sandbox` и whitelist `cdn.jwplayer.com`. Ссылки отзывов — только `<a target="_blank" rel="noopener noreferrer nofollow">`, валидация схемы `https?` и отсутствия приватных диапазонов при сохранении |
| T5 | Утечка секретов | Логи, ответы API, git | `Settings` маскирует секреты в `repr`; structlog-процессор вырезает ключи `*_key|*_token|*_password|authorization`; `.env` в `.gitignore`; `.env.example` без значений; `gitleaks` в pre-commit |
| T6 | CORS-злоупотребление | Браузер третьей стороны | `CORS_ALLOW_ORIGINS` — явный список, не `*`; `allow_credentials=false` (кук у нас нет) |
| T7 | Ресурсное исчерпание | `limit=100000`, глубокий `offset`, тяжёлый `q` | `limit ≤ 100` (Pydantic), `offset ≤ 10 000` (иначе 400), `q` ≥2 и ≤100 символов, `statement_timeout = 15 с` на уровне БД, public rate limit 120/мин по IP |
| T8 | SSE-исчерпание | Много открытых `EventSource` | `SSE_MAX_CLIENTS`, таймаут неактивного соединения, heartbeat |
| T9 | Prompt injection через отзывы | Пользователь пишет в отзыве «ignore previous instructions» | Отзывы передаются в **отдельном user-блоке** с явной рамкой («ниже — данные, не инструкции»), нумерованные; ответ ограничен JSON-схемой (structured output) — модель физически не может вернуть произвольное действие; результат используется только как текст для отображения, никогда как команда |
| T10 | Компрометация БД/Redis из вне | Открытые порты | В prod-override порты 5432/6379 не публикуются; сервисы в `internal`-сети; пароли из `.env` |

### 21.2 Что осознанно НЕ делаем в MVP

Полноценной аутентификации пользователей (регистрация, сессии, RBAC) **нет**: каталог целиком публичный и read-only, персональных данных сервис не хранит. Единственная привилегированная операция — админ-эндпоинты, и для неё достаточно статического токена.

**Граница, за которой нужна настоящая auth** (зафиксировать в README): появление пользовательских аккаунтов, избранного, комментариев, нескольких админов с разными правами, или вывод сервиса в публичный интернет с реальной нагрузкой. Тогда — OIDC/JWT + RBAC, а `X-Admin-Token` уходит.

### 21.3 Правовой контур (обязательно в README)

- Metacritic — товарный знак Fandom. Сервис — учебный проект, не аффилирован.
- Сбор данных выполняется с уважением к `robots.txt` (целевые пути не запрещены), с идентифицируемым User-Agent и консервативным rate limit'ом.
- Внутренний API `backend.metacritic.com` — **неофициальный и недокументированный**; его использование может противоречить ToS Fandom. Для коммерческой эксплуатации требуется договорённость с правообладателем или официальный источник данных.
- Тексты отзывов отображаются как цитаты с атрибуцией (издание/автор) и ссылкой на первоисточник.
- YouTube: скрейпинг выдачи по умолчанию **выключен**; официальный Data API используется в рамках квоты; транскрипты — через провайдеров, чьи ToS должны быть проверены перед продакшеном.
- AI-резюме явно помечаются как сгенерированные.

---

## 22. Performance

### 22.1 Профиль нагрузки

Чтение доминирует: каталог читают люди, пишет — один воркер-пул. При 177k играх и 24 карточках на страницу узкие места предсказуемы и их немного.

### 22.2 Сделать сразу (входит в MVP)

| Проблема | Решение |
|---|---|
| **Список игр: JOIN + DISTINCT + сортировка по агрегату** | Денормализованные роллапы `best_metascore`, `platform_ids int[]`, `genre_ids int[]` в `games` → список читается **из одной таблицы**, без единого JOIN |
| **Глубокий OFFSET** | Keyset-пагинация по умолчанию (`(sort_value, id)`), offset-режим ограничен 10 000 |
| **Нестабильный порядок при равных оценках** | Вторичный ключ `id DESC` во всех индексах сортировки |
| **N+1 на списке** | Список не делает подзапросов вовсе: платформы/жанры приходят как `int[]` и резолвятся из кэшированного справочника в приложении (справочники — десятки строк, кэш в памяти процесса с TTL) |
| **N+1 на карточке** | Ровно 4 запроса: game (+роллапы), `game_platforms` c `JOIN platforms`, `review_summaries WHERE is_current`, `similar_games JOIN games`. Плюс 1 на YouTube. Никаких ORM-lazy-load: везде явные `selectinload`/прямые `SELECT` |
| **Батчевые вставки отзывов** | `execute_values`-стиль: один `INSERT ... VALUES (...), (...) ON CONFLICT ...` на 100 отзывов вместо 100 запросов |
| **Повторные вызовы LLM** | `input_fingerprint` UNIQUE — самая дорогая операция защищена на уровне БД |
| **Повторные эмбеддинги** | `source_hash` — пересчёт только при изменении документа |
| **Квота YouTube** | `search.list` один раз на игру за всё время + гейт по качеству игры |
| **Трафик обложек** | `next/image` (AVIF/WebP, нужные размеры) вместо 140 KB оригиналов |
| **Concurrency краулера** | Redis token-bucket 2 rps + семафор 2 — защищает и нас, и источник |
| **Кэш ответов API** | `Cache-Control: public, max-age=60, stale-while-revalidate=300` на `/games`, `max-age=300` на `/games/{id}`; Next ISR поверх |

### 22.3 Отложить (делать по факту измерений)

- Redis-кэш горячих ответов API (сначала посмотреть, хватает ли ISR + `Cache-Control`).
- Materialized view для фасетов (счётчики игр по платформам/жанрам) — пока считается по GIN-индексам приемлемо.
- Партиционирование `reviews` по `game_id` — оправдано после ~50M строк.
- Партиционирование `job_events` по месяцам — после ~10M строк.
- Batch API для регулярной (не бэкфилл) суммаризации.
- CDN перед фронтендом.

### 22.4 Понадобится при масштабировании

- Реплика PostgreSQL для чтения; API ходит в реплику, воркеры — в primary.
- Разделение воркеров по очередям в отдельные контейнеры с независимым автоскейлом.
- Вынос эмбеддингов в отдельный сервис с GPU (если перейдём на крупную модель).
- Elasticsearch/OpenSearch — **только** если понадобится поиск по текстам отзывов; для поиска по названию `pg_trgm` достаточен и на 1M строк.
- Шардирование `reviews` — при десятках миллионов игр (нереалистично).

### 22.5 Целевые показатели (SLO для MVP)

| Операция | Цель p95 |
|---|---|
| `GET /games` (24 карточки, с фильтрами) | < 120 мс |
| `GET /games/{id}` (полная карточка) | < 150 мс |
| `GET /search/suggest` | < 60 мс |
| `GET /monitoring/status` | < 100 мс |
| `crawl.hourly_tick` (только постановка задач) | < 10 с |
| `game.sync` одной игры | < 8 с |
| Полный цикл обогащения игры (без ASR) | < 3 мин |

---

## 23. Project Folder Structure

```text
metacritic-service/
├── README.md                          обзор, quickstart, дисклеймер о ToS
├── docker-compose.yml                 dev
├── docker-compose.prod.yml            override: без публикации портов БД, ресурсы
├── .env.example
├── Makefile                           up, down, test, lint, migrate, seed, logs
├── docs/
│   ├── BLUEPRINT.md                   ← этот документ
│   ├── API.md                         сгенерированный OpenAPI + примеры
│   ├── RUNBOOK.md                     что делать, когда что-то сломалось
│   └── adr/
│       ├── 0001-use-internal-json-api.md
│       ├── 0002-no-tailwind-css-selectors.md
│       ├── 0003-celery-over-arq.md
│       ├── 0004-sse-over-websocket.md
│       ├── 0005-hybrid-similarity.md
│       └── 0006-transcript-provider-cascade.md
│
├── backend/
│   ├── pyproject.toml                 зависимости, ruff, mypy, pytest
│   ├── uv.lock
│   ├── alembic.ini
│   ├── migrations/versions/
│   └── app/
│       ├── main.py                    FastAPI app, lifespan, middleware
│       ├── config.py                  pydantic-settings — ЕДИНСТВЕННОЕ место чтения env
│       ├── container.py               DI: сборка клиентов/сервисов
│       ├── logging.py                 structlog + contextvars
│       │
│       ├── domain/                    ЧИСТЫЙ слой: типы, енумы, DTO, доменные ошибки
│       │   ├── models.py
│       │   ├── enums.py
│       │   └── errors.py              RetryableError / PermanentError / SchemaDriftError / ...
│       │
│       ├── adapters/                  ВСЁ общение с внешним миром
│       │   ├── http/
│       │   │   ├── transport.py       httpx + таймауты + инструментирование
│       │   │   ├── rate_limiter.py    Redis token-bucket
│       │   │   ├── retry.py           tenacity-политики
│       │   │   └── circuit.py         circuit breaker
│       │   ├── metacritic/
│       │   │   ├── client.py          MetacriticClient (фасад)
│       │   │   ├── source_json.py     JsonApiSource
│       │   │   ├── source_html.py     HtmlSource (JSON-LD + data-testid)
│       │   │   ├── source_browser.py  BrowserSource (Playwright, off)
│       │   │   ├── schemas.py         Pydantic-модели ОТВЕТОВ Metacritic
│       │   │   └── endpoints.py       константы URL и параметров
│       │   ├── youtube/
│       │   │   ├── client.py
│       │   │   ├── source_dataapi.py
│       │   │   ├── source_scrape.py
│       │   │   ├── ranking.py         ★ алгоритм §12.2 — чистые функции
│       │   │   └── transcripts/
│       │   │       ├── base.py        TranscriptProvider Protocol
│       │   │       ├── ytdlp.py
│       │   │       ├── hosted_api.py
│       │   │       ├── asr.py
│       │   │       └── metadata_only.py
│       │   ├── llm/
│       │   │   ├── base.py            LLMClient Protocol
│       │   │   ├── anthropic_client.py
│       │   │   ├── null_client.py
│       │   │   └── pricing.py         таблица цен → cost_usd
│       │   └── embeddings/
│       │       ├── base.py
│       │       ├── local_onnx.py      fastembed
│       │       └── remote_api.py
│       │
│       ├── parsers/                   ЧИСТЫЕ функции dict/html → DTO
│       │   ├── game.py
│       │   ├── listing.py
│       │   ├── reviews.py
│       │   └── jsonld.py
│       │
│       ├── normalizers/               ЧИСТЫЕ функции DTO → доменная модель
│       │   ├── game.py                slug, title_norm, URL обложки, fingerprint
│       │   ├── review.py              dedupe_key, body_hash, шкалы
│       │   └── text.py
│       │
│       ├── repositories/              ЕДИНСТВЕННОЕ место, знающее про таблицы
│       │   ├── base.py
│       │   ├── games.py               upsert_game, refresh_rollups, search
│       │   ├── platforms.py
│       │   ├── reviews.py             batch upsert с RETURNING xmax=0
│       │   ├── summaries.py
│       │   ├── similarity.py
│       │   ├── youtube.py
│       │   ├── crawl.py               claim_games, курсоры, runs, reaper
│       │   └── jobs.py                jobs + job_events
│       │
│       ├── services/                  БИЗНЕС-ЛОГИКА (без HTTP и без SQL)
│       │   ├── crawl_orchestrator.py  ★ алгоритм §7.2
│       │   ├── game_sync.py           ★ дифференциальное обновление §19-A
│       │   ├── review_sync.py
│       │   ├── summary_service.py     ★ should_regenerate, sampling, map-reduce
│       │   ├── similarity_service.py  ★ гибридный скоринг §11.2
│       │   ├── letsplay_service.py    ★ пайплайн §12
│       │   ├── monitoring_service.py
│       │   └── budget_service.py      квоты YouTube и стоимость LLM
│       │
│       ├── ai/
│       │   ├── prompts/
│       │   │   ├── critic_v3.jinja
│       │   │   ├── user_v3.jinja
│       │   │   └── letsplay_v2.jinja
│       │   ├── schemas.py             SummaryOut, Point, Aspect
│       │   ├── chunking.py
│       │   └── sampling.py            стратифицированная выборка
│       │
│       ├── api/
│       │   ├── deps.py                get_db, get_settings, require_admin
│       │   ├── errors.py              RFC7807 handlers
│       │   ├── pagination.py          keyset + offset
│       │   ├── schemas/               Pydantic ЗАПРОСОВ/ОТВЕТОВ нашего API
│       │   └── routers/
│       │       ├── games.py
│       │       ├── reviews.py
│       │       ├── similar.py
│       │       ├── youtube.py
│       │       ├── platforms.py
│       │       ├── search.py
│       │       ├── monitoring.py      + SSE stream
│       │       ├── admin.py
│       │       └── health.py
│       │
│       ├── worker/
│       │   ├── celery_app.py          конфиг, очереди, роутинг
│       │   ├── beat_schedule.py
│       │   ├── base_task.py           ★ обёртка: jobs-запись, idempotency, эмит событий
│       │   └── tasks/
│       │       ├── crawl.py
│       │       ├── games.py
│       │       ├── reviews.py
│       │       ├── ai.py
│       │       ├── similarity.py
│       │       ├── youtube.py
│       │       └── maintenance.py
│       │
│       └── db/
│           ├── session.py
│           ├── models.py              SQLAlchemy ORM
│           └── seed.py                справочник платформ
│
├── frontend/
│   ├── package.json  next.config.js  tailwind.config.ts  tsconfig.json
│   ├── app/
│   │   ├── layout.tsx  page.tsx  globals.css
│   │   ├── games/page.tsx  loading.tsx  error.tsx
│   │   ├── games/[slug]/page.tsx  loading.tsx  not-found.tsx
│   │   └── monitoring/page.tsx
│   ├── components/
│   │   ├── ui/                        shadcn primitives
│   │   ├── game/                      GameCard, GameGrid, ScoreBadge, PlatformChips,
│   │   │                              PlatformScoreTable, SummaryPanel, SimilarCarousel,
│   │   │                              LetsPlayPanel, ReviewList, VideoEmbed
│   │   ├── filters/                   SearchInput, PlatformFilter, GenreFilter,
│   │   │                              ScoreRange, SortSelect, ScoreBaseToggle, FilterSheet
│   │   ├── monitoring/                StatusHeader, StatCards, RunProgress, WorkerList,
│   │   │                              QueueStats, BudgetBars, ErrorFeed, RunHistory,
│   │   │                              TriggerButton
│   │   └── common/                    Skeletons, EmptyState, ErrorState, Pagination
│   ├── lib/
│   │   ├── api.ts                     типизированный клиент (типы из OpenAPI)
│   │   ├── types.ts                   сгенерировано openapi-typescript
│   │   ├── sse.ts                     useEventSource + polling fallback
│   │   └── format.ts
│   └── e2e/                           Playwright
│
├── docker/
│   ├── backend.Dockerfile
│   ├── frontend.Dockerfile
│   ├── ytdlp.Dockerfile               PO-token sidecar
│   └── postgres/init.sql              CREATE EXTENSION ...
│
└── tests/
    ├── conftest.py
    ├── fixtures/{metacritic,youtube,llm}/
    ├── unit/
    ├── integration/
    └── contract/                      реальные API, только вручную/nightly
```

**Почему `parsers` и `normalizers` вынесены из `adapters`.** Это чистые функции без IO — самая ценная для тестирования часть системы. Отдельные пакеты делают невозможным случайный импорт `httpx` в парсер и позволяют покрыть их сотнями быстрых тестов на фикстурах.

---

## 24. Implementation Roadmap

Оценки — в человеко-днях одного senior-разработчика. Фазы 1–6 обязательны (Must Have); 7–12 — по ТЗ, но допускают параллелизацию.

### Phase 0 — Research ✅ ВЫПОЛНЕНО
**Результат:** этот документ; подтверждённая карта API Metacritic; зафиксированные ограничения YouTube; JSON-фикстуры реальных ответов.
**DoD:** заказчик подтвердил архитектуру и открытые вопросы §28.

### Phase 1 — Foundation · 1.5 дн
**Делаем:** монорепо, `pyproject.toml`, `config.py` (pydantic-settings), `logging.py` (structlog + contextvars), DI-контейнер, каркас FastAPI (`/health`), Celery app с 3 очередями, `docker-compose.yml` (postgres+redis+backend+worker+scheduler), Makefile, pre-commit (ruff, mypy, gitleaks), CI-пайплайн lint+unit.
**Файлы:** `app/{config,logging,container,main}.py`, `app/worker/celery_app.py`, `docker/*`, `.env.example`, `.github/workflows/ci.yml`.
**Зависимости:** fastapi, uvicorn, pydantic-settings, structlog, celery, redis, httpx.
**Тесты:** конфиг падает при отсутствии обязательной переменной; `/health` возвращает 200; Celery видит очереди.
**DoD:** `docker compose up` поднимает всё; `/health` зелёный; CI зелёный.

### Phase 2 — Database · 2 дн
**Делаем:** полную схему §6 в SQLAlchemy; Alembic-миграции; все индексы и констрейнты; сид платформ; репозитории `games`, `platforms`, `crawl` (в т.ч. `claim_games`, курсоры, reaper); контейнер `migrate`.
**Файлы:** `app/db/models.py`, `migrations/versions/0001_init.py`, `app/repositories/*`, `app/db/seed.py`.
**Зависимости:** sqlalchemy, alembic, psycopg, pgvector.
**Тесты (integration, testcontainers):** upgrade/downgrade/upgrade; **конкурентный `claim_games` из 8 потоков → ровно 1 строка**; две вставки активного run'а → UniqueViolation; reaper возвращает протухший lease.
**DoD:** миграции идемпотентны; тест конкурентной дедупликации зелёный.

### Phase 3 — Metacritic Crawler · 3 дн
**Делаем:** `HttpTransport` (rate limiter, retry, circuit breaker), `JsonApiSource`, Pydantic-схемы ответов, парсеры, нормализаторы, `GameSyncService` (дифференциальное обновление), `CrawlOrchestrator` (§7.2), задачи `crawl.hourly_tick`, `game.sync`, `crawl.reap_stale_items`, Beat-расписание, `base_task` (запись в `jobs`, идемпотентность, эмит событий).
**Файлы:** `app/adapters/http/*`, `app/adapters/metacritic/*`, `app/parsers/{game,listing}.py`, `app/normalizers/game.py`, `app/services/{crawl_orchestrator,game_sync}.py`, `app/worker/tasks/{crawl,games}.py`.
**Тесты:** unit на всех 6 JSON-фикстурах + все 11 сценариев §7.4 с фейковыми часами; integration — полный tick против замоканного API (`respx`) с реальной БД.
**DoD:** `make crawl-once` наполняет БД 20 играми New Releases с платформами, жанрами, разработчиками и оценками; повторный запуск в тот же день добавляет 0 строк; смена даты — обрабатывает заново, но без изменений в `games`.

### Phase 4 — Reviews · 1.5 дн
**Делаем:** эндпоинты отзывов и per-platform stats; `ReviewSyncService` (первичная + инкрементальная с допуском); батчевый upsert с `RETURNING xmax=0`; задача `reviews.sync`.
**Файлы:** `app/parsers/reviews.py`, `app/normalizers/review.py`, `app/repositories/reviews.py`, `app/services/review_sync.py`, `app/worker/tasks/reviews.py`.
**Тесты:** dedupe_key стабилен/различающ; повторный батч → 0 вставок и 0 обновлений; изменённый текст → 1 обновление; `offset > total` не вызывает 500 (клампится).
**DoD:** для Elden Ring загружено ≥200 отзывов критиков и ≥500 пользовательских; повторный запуск не создаёт дублей.

### Phase 5 — Backend API · 2 дн
**Делаем:** все эндпоинты §15.1; keyset+offset пагинация; поиск (trigram+FTS+префикс); фильтры/сортировки; RFC7807; OpenAPI; кэш-заголовки; rate limiting; `require_admin`.
**Файлы:** `app/api/**`.
**Тесты (integration):** пагинация обоих типов; мульти-платформенный фильтр = OR; все сортировки стабильны (проверка на равных метаскорах); 404/400/422/401; `EXPLAIN` подтверждает использование целевых индексов.
**DoD:** Swagger UI полон; p95 `/games` < 120 мс на 50k сидированных играх.

### Phase 6 — Frontend · 3 дн
**Делаем:** Next.js каркас, `/games` со всеми фильтрами и состояниями, `/games/[slug]` со всеми блоками, `GameCard`, `ScoreBadge`, `SimilarCarousel`, `next/image`, тёмная тема, responsive, типы из OpenAPI.
**Файлы:** `frontend/**`.
**Тесты:** Playwright — основной флоу §19.4, empty/error, responsive 375 px, axe без serious/critical.
**DoD:** на 375/768/1440 px нет горизонтального скролла; Lighthouse Performance ≥90 на `/games`; все элементы ТЗ §7 и §8 присутствуют.

### Phase 7 — AI · 2.5 дн
**Делаем:** `LLMClient` + `AnthropicLLMClient` + `NullLLMClient`; structured output; промпты v3; `SummaryService` (`should_regenerate`, стратифицированная выборка, map-reduce); учёт стоимости и дневной лимит; задача `ai.summarize`; отображение резюме в UI.
**Файлы:** `app/adapters/llm/*`, `app/ai/**`, `app/services/summary_service.py`, `app/worker/tasks/ai.py`, `frontend/components/game/SummaryPanel.tsx`.
**Тесты:** все ветви `should_regenerate`; одинаковый fingerprint → 0 вызовов (проверяется счётчиком на моке); выборка соблюдает бюджет и пропорции; недоступный LLM не ломает `game.sync`.
**DoD:** для 20 игр есть оба резюме; повторный прогон делает 0 вызовов LLM; `cost_usd` заполнен; при `LLM_ENABLED=false` сервис полностью работоспособен.

### Phase 8 — Similarity · 1.5 дн
**Делаем:** `EmbeddingProvider` (local ONNX), similarity-документ, metadata-скор, гибрид, двухфазный отбор кандидатов, `similar_games`, задачи `embedding.compute` / `similarity.recompute` / `similarity.refresh_stale`, карусель в UI.
**Файлы:** `app/adapters/embeddings/*`, `app/services/similarity_service.py`, `app/repositories/similarity.py`, `frontend/components/game/SimilarCarousel.tsx`.
**Тесты:** известные пары (франшиза → высоко, разные жанры → низко); детерминизм; работа **без** эмбеддингов (только metadata); HNSW-поиск.
**DoD:** для каждой игры с ≥1 жанром есть ≥5 похожих; клик ведёт на карточку; ручная проверка топ-10 игр даёт осмысленный результат.

### Phase 9 — YouTube · 3 дн
**Делаем:** `YouTubeClient` (Data API + scrape fallback), гейт, `ranking.py`, `TranscriptProvider`-каскад, sidecar `bgutil`, нормализацию/чанкинг, промпт `letsplay_v2`, задачи, учёт квоты, панель в UI.
**Файлы:** `app/adapters/youtube/**`, `app/services/letsplay_service.py`, `app/worker/tasks/youtube.py`, `docker/ytdlp.Dockerfile`, `frontend/components/game/LetsPlayPanel.tsx`.
**Тесты:** ранжирование на фикстуре из 25 кандидатов (трейлер/live/shorts/no-commentary отсеиваются); **пустой 200 → следующий провайдер**; исчерпание квоты → перенос, а не ошибка; `metadata_only` помечается в UI.
**DoD:** для 10 игр найден релевантный Let's Play; ≥1 транскрипт получен и суммаризирован; недоступность YouTube не влияет на остальные данные.

### Phase 10 — Monitoring · 2 дн
**Делаем:** `MonitoringService`, все эндпоинты §15.2, SSE + Redis pub/sub + `Last-Event-ID`, `/metrics`, страницу `/monitoring`, кнопку ручного запуска с обработкой 409, polling-fallback.
**Файлы:** `app/services/monitoring_service.py`, `app/api/routers/{monitoring,admin}.py`, `frontend/app/monitoring/page.tsx`, `frontend/lib/sse.ts`, `frontend/components/monitoring/**`.
**Тесты:** SSE доставляет событие; heartbeat; reconnect доотдаёт; 409 при активном run'е; E2E «нажал → счётчик обновился без перезагрузки».
**DoD:** все 14 пунктов ТЗ §10 видны на странице; ручной запуск работает; два одновременных клика не создают два run'а.

### Phase 11 — Testing & Hardening · 2 дн
**Делаем:** добор покрытия до порогов; contract-тесты; нагрузочный прогон (k6, 50 rps на `/games`); RUNBOOK; ADR; профилирование и добавление недостающих индексов по `EXPLAIN ANALYZE`.
**DoD:** покрытие ≥85% на `services/parsers/domain`; CI < 10 мин; SLO §22.5 достигнуты; RUNBOOK описывает 8 инцидентов из §20.1.

### Phase 12 — Docker & Deployment · 1 дн
**Делаем:** multi-stage образы, healthchecks, `depends_on` условия, prod-override, `maintenance.cleanup`, README с quickstart и дисклеймером, скрипт бэкапа БД.
**DoD:** `cp .env.example .env && docker compose up -d` на чистой машине даёт работающий сервис за одну команду; `docker compose down && up` не теряет данные; образы < 500 MB (backend) и < 250 MB (frontend).

**Итого: ~25 человеко-дней.** Критический путь: 1 → 2 → 3 → 4 → 5 → 6. Фазы 7/8/9 могут вестись параллельно после Phase 5.

---

## 25. MVP vs Bonus

### Must Have (обязательное по ТЗ)
1. Часовой сбор игр: New Releases (20) → browse-страницы, с посуточным сбросом.
2. Надёжная дедупликация на уровне БД + курсор, переживающий рестарт.
3. Полная информация об игре: название, URL, обложка, описание, developer, видео.
4. Нормализованные платформы с Metascore и Userscore **по каждой платформе**.
5. Отзывы критиков и пользователей с дедупликацией.
6. AI-резюме критиков и пользователей (positive/negative/overall) с версионированием.
7. Похожие игры **только из своей БД**, кликабельные.
8. Веб-интерфейс: список (поиск, фильтр по платформе, сортировка по выбранному рейтингу, пагинация, loading/empty/error, responsive) и карточка со всеми полями.
9. REST API.
10. Фоновая обработка: scheduler + queue + workers.
11. Надёжность: retry, backoff, timeout, rate limit, idempotency, graceful degradation.
12. PostgreSQL со спроектированной схемой и индексами.
13. `docker compose up` запускает всё.
14. Структурированные логи с корреляцией.
15. Тесты: unit + integration + E2E.

### Bonus 1 — YouTube Let's Play
Поиск, ранжирование (не «первый результат»), транскрипт с каскадом провайдеров, AI-резюме прохождения, ссылка на ролик в карточке. **Риск оговорён явно** (§2.2.2): транскрипт может быть недоступен, ссылка и метаданные будут всегда.

### Bonus 2 — Monitoring + ручной запуск
Страница `/monitoring` со всеми 14 показателями, real-time через SSE, история запусков, лента ошибок, кнопка «Запустить сейчас» с защитой от конфликта.

### Nice to Have (за рамками ТЗ, не делаем в MVP)
- Weekly reconciliation по sitemap (**решено включить в MVP** — без него каталог не будет полным из-за нестабильной пагинации).
- Prometheus/Grafana дашборды.
- Поиск по текстам отзывов.
- Сравнение двух игр бок о бок.
- Экспорт CSV/JSON.
- Уведомления в Telegram/Slack о падениях.
- Пользовательские аккаунты и избранное.
- Многоязычные резюме.
- Тренды оценок во времени (потребует снапшотов `game_platforms`).

---

## 26. Risks

| # | Риск | Вероятн. | Влияние | Митигация |
|---|---|---|---|---|
| **R1** | **Внутренний API `backend.metacritic.com` закроют или начнут требовать подпись** | Средняя | **Критическое** — основной источник | (1) `HtmlSource` реализован и покрыт тестами с первого дня, переключение — одна переменная окружения; (2) nightly contract-тесты дают предупреждение раньше пользователей; (3) авто-фолбэк по порогу `SCHEMA_DRIFT_THRESHOLD`; (4) sitemap как независимый источник перечня игр |
| **R2** | Anti-bot: Cloudflare-challenge, IP-бан | Средняя | Высокое | 2 rps + 2 параллельных соединения, честный UA с контактом, уважение `Retry-After`, circuit breaker; `BrowserSource` (Playwright) готов как адаптер; при необходимости — прокси-пул через одну переменную |
| **R3** | **Изменение HTML/JSON-структуры Metacritic** | **Высокая** (уже сменили платформу на Nuxt) | Среднее | `extra='ignore'` терпит добавления; обязательные поля явно объявлены; парсеры изолированы и покрыты фикстурами; **никаких Tailwind-CSS-селекторов**; nightly contract-тест |
| **R4** | **Недетерминированный порядок пагинации `finder`** (подтверждено: 12/24 расхождения) | **Подтверждён** | Среднее — пропуск игр | `ON CONFLICT DO NOTHING` делает дубли бесплатными; курсор монотонный; **weekly sitemap reconciliation закрывает пропуски** |
| **R5** | **Транскрипт YouTube недоступен** (подтверждено: пустой 200 у `timedtext`) | **Подтверждён, высокая** | Среднее — деградация Bonus 1 | Каскад из 4 провайдеров; `metadata_only` как гарантированный последний уровень; ссылка на ролик и метаданные сохраняются всегда; статус явно виден в БД и UI |
| **R6** | Исчерпание квоты YouTube (100 поисков/сутки) | **Высокая** при росте | Среднее | Гейт по качеству игры; один `search.list` на игру за всё время; `videos.list` батчами по 50 (1 unit); бюджет в `api_budgets`; перенос задач вместо ошибки; опциональный scrape-fallback |
| **R7** | Стоимость LLM выходит из-под контроля | Средняя | Среднее | `input_fingerprint` UNIQUE (физически блокирует повтор); стратифицированная выборка; prompt caching; дневной лимит `AI_DAILY_COST_LIMIT_USD`; Batch API −50% для бэкфилла; модель меняется одной переменной |
| **R8** | Дублирующиеся job'ы / двойной запуск краулера | Низкая | Высокое | Три независимых барьера (§7.4); `jobs.idempotency_key` UNIQUE |
| **R9** | Падение воркера в середине обработки | Средняя | Низкое | `acks_late` + `visibility_timeout`; lease + reaper; все операции идемпотентны |
| **R10** | Race conditions при N воркерах | Средняя | Высокое | Дедуп и уникальность — на уровне БД, а не приложения; интеграционный тест с реальным параллелизмом в CI |
| **R11** | Потеря Redis (очередь + локи) | Низкая | Среднее | AOF включён; корректность не зависит от Redis; протухшие `running`-run'ы закрываются по таймауту; следующий tick через час |
| **R12** | Разрастание БД (177k игр × отзывы) | Средняя | Среднее | Кэпы на отзывы (`REVIEWS_*_CAP`); ретенция `job_events`/`jobs`/`crawl_items`; план партиционирования готов |
| **R13** | Юридические/ToS-претензии | Низкая (учебный проект) | Высокое | Явный дисклеймер в README; вежливый краулинг; scrape-fallback YouTube выключен по умолчанию; цитаты с атрибуцией и ссылкой; AI-контент помечен |
| **R14** | Некачественные AI-резюме (галлюцинации) | Средняя | Среднее | Structured output с фиксированными аспектами; обязательные `evidence` (цитаты из отзывов); `confidence`; версионирование позволяет откатить промпт; дисклеймер в UI |
| **R15** | Плохая релевантность «похожих игр» | Средняя | Среднее | Гибрид с объяснимыми компонентами (`components jsonb`); веса вынесены в конфиг; ручная проверка топ-50 в Phase 8 как критерий приёмки |
| **R16** | Ошибочный выбор Let's Play (трейлер/не та игра) | Средняя | Низкое | Три независимых барьера против трейлеров; порог совпадения названия 0.6; фикстурная тест-сюита на 25 кандидатов; `rejected_reason` сохраняется для разбора |

---

## 27. Assignment Compliance Matrix

| № | Требование ТЗ | Реализация | Компонент | Статус |
|---|---|---|---|---|
| 1 | Сбор игр 1 раз в час | Celery Beat `7 * * * *` → `crawl.hourly_tick` | scheduler, worker | ✅ спроектировано |
| 1 | Первые 20 игр из New Releases | `finder?componentName=new-releases-carousel&metaScoreMin=1&limit=20` (подтверждено 1:1 с сайтом) | `JsonApiSource` | ✅ |
| 1 | Каждый новый день выбор заново | `crawl_days` per-date; `crawl_items` scoped по `crawl_date` | `CrawlOrchestrator` | ✅ |
| 2 | Далее страницы browse `all-time/new` | фаза `browse`, `sortBy=-releaseDate`, курсор `browse_page` | `CrawlOrchestrator` | ✅ |
| 2 | Последовательная обработка страниц | монотонный курсор в `crawl_days` | repo `crawl` | ✅ |
| — | **Дедупликация: идентификаторы** | `mc_slug` + `mc_title_id` | `games` | ✅ |
| — | **уникальная игра** | UNIQUE на обоих | БД | ✅ |
| — | **дата/время обработки** | `crawl_items.claimed_at/finished_at`, `games.*_synced_at` | БД | ✅ |
| — | **обрабатывалась ли сегодня** | `UNIQUE(crawl_date, game_slug)` + `ON CONFLICT DO NOTHING` | БД | ✅ |
| — | **переход к следующей странице** | `crawl_days.browse_page` | БД | ✅ |
| — | **переживание перезапуска** | всё состояние в PostgreSQL | БД | ✅ |
| — | **не потерять progress** | курсор двигается после claim; при сбое источника не двигается | `CrawlOrchestrator` | ✅ |
| — | **race conditions** | 3 барьера: Redis-лок → partial UNIQUE run → UNIQUE crawl_items | Redis + БД | ✅ |
| — | **несколько worker instances** | те же 3 барьера, изменений не требуется | — | ✅ |
| 3 | название, URL, обложка, описание, developer, видео | `composer/product` (см. таблицу §8.2) | `MetacriticClient` | ✅ |
| 3 | Платформы — нормализованная модель | `platforms` + `game_platforms` (не текстовое поле) | БД | ✅ |
| 3 | Metascore и Userscore на каждую платформу | `platforms[].criticScoreSummary.score` + `/platform/{p}/stats/web` | `MetacriticClient` | ✅ подтверждено на CP2077 |
| 4 | Critic reviews: reviewer, publication, score, текст, URL, дата, тип | `reviews` (author, publication_name/slug, score, body, url, published_on, kind) | БД | ✅ (`author`/`url` часто пусты у источника — nullable) |
| 4 | User reviews: identifier, score, текст, URL, дата | `reviews` + `source_review_id` (UUID) | БД | ✅ |
| 4 | Не сохранять одинаковые отзывы повторно | `UNIQUE(game_platform_id, kind, dedupe_key)` + `WHERE hash IS DISTINCT FROM` | БД | ✅ |
| 5 | Summary критиков: positive/negative/overall | `review_summaries audience='critic'` | `SummaryService` | ✅ |
| 5 | Summary пользователей | `audience='user'` | `SummaryService` | ✅ |
| 5 | Когда обновлять | `should_regenerate` §10.4 | `SummaryService` | ✅ |
| 5 | Какие отзывы считать новыми | `first_seen_at > source_review_max_seen_at` | БД | ✅ |
| 5 | Не вызывать LLM без необходимости | `UNIQUE(game_id, audience, input_fingerprint)` | БД | ✅ |
| 5 | Версионирование summary | `version`, `is_current`, история строк | БД | ✅ |
| 5 | Восстановление после ошибки AI | retry ×6; прежнее `is_current` сохраняется | worker | ✅ |
| 5 | Timestamp последнего успешного анализа | `generated_at`, `games.summaries_synced_at` | БД | ✅ |
| 6 | Похожие игры — только из своей БД | кандидаты выбираются `SELECT ... FROM games` | `SimilarityService` | ✅ |
| 6 | Рассмотреть A/B/C и выбрать | §11.1 — выбран Hybrid с обоснованием | — | ✅ |
| 6 | Эффективное хранение/вычисление | `similar_games` предвычислена; двухфазный отбор кандидатов | БД | ✅ |
| 6 | Отображение в карточке, кликабельные | `SimilarCarousel` → `<Link href="/games/{slug}">` | frontend | ✅ |
| 7 | Список: название, cover, рейтинг, платформы, краткая инфо, дата | `GameCard` | frontend | ✅ |
| 7 | Search по названию | `GET /games?q=` — trigram + FTS + префикс | API + БД | ✅ |
| 7 | Filter по платформе | `?platform=` (повторяемый) → GIN на `platform_ids` | API + БД | ✅ |
| 7 | Sort по рейтингу | `?sort=metascore\|userscore` + переключатель базы в UI | API + frontend | ✅ |
| 7 | Выбор рейтинга пользователем | `ScoreBaseToggle` (Metascore / Userscore) | frontend | ✅ |
| 7 | Pagination / loading / empty / error / responsive | keyset+offset; `loading.tsx`; `EmptyState`; `error.tsx`; Tailwind breakpoints | API + frontend | ✅ |
| 8 | Карточка: все поля + оба резюме + Let's Play + похожие | §16.3 | frontend | ✅ |
| 8 | Похожие кликабельные | `<Link>` | frontend | ✅ |
| 9 | YouTube: поиск → релевантность → популярность → лучший | §12.1–12.2 | `LetsPlayService` | ✅ |
| 9 | Transcript + перевод в текст | каскад из 4 провайдеров, включая ASR | `TranscriptProvider` | ⚠️ частично — см. R5 |
| 9 | AI-заключение + сохранение ссылки | `review_summaries audience='letsplay'` + `youtube_videos.is_selected` | БД | ✅ |
| 9 | «Популярный» ≠ первый результат | вес просмотров 0.32 из 1.0, логарифмический | `ranking.py` | ✅ |
| 9 | Не выбрать trailer | `videoDuration=long` + мин. 10 мин + regex — 3 барьера | `ranking.py` | ✅ |
| 10 | Мониторинг — все 14 показателей | §14.2 (таблица построчного соответствия) | API + frontend | ✅ |
| 10 | Кнопка «Запустить сейчас» | `POST /admin/crawl/run` → 202/409 | API | ✅ |
| 10 | Нельзя два конфликтующих crawler job | 3 барьера | Redis + БД | ✅ |
| 11 | Архитектура фоновой обработки | Celery + Redis + Beat, 3 очереди | worker | ✅ |
| 11 | Разделение job'ов; sync/async/retryable/idempotent | §13.2 (таблица) | worker | ✅ |
| 12 | retry / backoff / timeout / rate limit / circuit breaker | `app/adapters/http/*` | adapters | ✅ |
| 12 | idempotency / transaction boundaries / partial failures | §20.2, §20.3 | БД + services | ✅ |
| 12 | Отказ Metacritic/YouTube/LLM не ломает сервис | §20.1 (матрица) | — | ✅ |
| 13 | PostgreSQL, полная схема с типами/PK/FK/UNIQUE/индексами | §6 | БД | ✅ |
| 13 | Индексы: поиск, платформы, рейтинги, похожие, история | §6.9 (сводка) | БД | ✅ |
| 14 | Production-ready структура | §3, §23 | — | ✅ |
| 15 | Обоснованный выбор стека | §4, Приложение A | — | ✅ |
| 16 | Разделённая scraping-архитектура | §8.1, §8.6 | adapters | ✅ |
| 16 | Устойчивые selectors / structured data | JSON API → JSON-LD → data-testid; **Tailwind-селекторы запрещены** | `HtmlSource` | ✅ |
| 17 | Daily crawl algorithm + 11 сценариев | §7.2, §7.4 | `CrawlOrchestrator` | ✅ |
| 18 | Differential update | §19-A | `GameSyncService` | ✅ |
| 19 | Полный API contract | §15 | API | ✅ |
| 20 | Real-time без перезагрузки | SSE + polling fallback, обоснование в §14.1 | API + frontend | ✅ |
| 21 | Security: admin, CORS, secrets, SSRF, rate limit, SQLi, XSS | §21 (T1–T10) | — | ✅ |
| 22 | Docker: compose, Dockerfiles, env, volumes, healthchecks, порядок, миграции | §17 | docker | ✅ |
| 23 | Полный `.env.example` | §18 | — | ✅ |
| 24 | Structured logs с корреляцией | structlog + contextvars (`crawl_run_id`, `job_id`, `game_id`, `worker_id`) | `app/logging.py` | ✅ |
| 25 | Unit / integration / E2E, моки внешних сервисов | §19 | tests | ✅ |
| 26 | Observability | §14.4 — метрики есть, тяжёлый стек опционален | — | ✅ |
| 27 | UX/UI современного каталога | §16 | frontend | ✅ |
| 28 | Performance: что сразу / отложить / при масштабировании | §22.2–22.4 | — | ✅ |
| 29 | Data consistency: гарантии БД vs приложения | §20.2, §20.3 | — | ✅ |
| 30 | 4 сценария partial success + state machine | §20.1, §13.3 | — | ✅ |
| 31 | Самостоятельная проверка 16 пунктов | §2 — все проверены эмпирически | — | ✅ |
| 33 | Rules 1–8 | Rule 1 §4; Rule 2 §2.1.2 + R1; Rule 3 §2.2.2 + R5; Rule 4 §8.1/§10.1/§12.4; Rule 5 §13.2; Rule 6 §9.4/§13.2; Rule 7 §7.1; Rule 8 §20.1 | — | ✅ |
| 35 | Сравнение ≥2 вариантов по 8 осям | Приложение A | — | ✅ |

**Единственный пункт со статусом «частично» — получение YouTube-транскрипта (Bonus 1).** Это не проектный недочёт, а измеренное внешнее ограничение (§2.2.2), для которого спроектирована деградация вместо отказа.

---

## 28. Questions / Unknowns

Только то, что действительно требует вашего решения или проверки в вашей инфраструктуре. Всё, что можно было выяснить исследованием, выяснено в §2.

### Требуют решения заказчика

| # | Вопрос | Почему важно | Рекомендация по умолчанию |
|---|---|---|---|
| **Q1** | **Допустимо ли использовать внутренний API `backend.metacritic.com`?** Он публичен, ключ не валидируется, robots.txt его не покрывает — но это недокументированный интерфейс, и ToS Fandom запрещает автоматический сбор. | Определяет основной источник данных. При запрете — переходим на `HtmlSource`, что увеличивает Phase 3 на ~2 дня и снижает надёжность. | Использовать для учебного проекта с дисклеймером; `HtmlSource` держать готовым |
| **Q2** | **Бюджет на YouTube-транскрипты.** Бесплатные пути (§2.2.2) ненадёжны: `timedtext` отдаёт пустоту, cloud-IP блокируются. Надёжен только платный hosted API (~$1.6–5.7 / 1000) либо ASR (CPU-время). | Определяет реальный процент игр с Let's Play-резюме. Без бюджета — ждать 30–60% успеха вместо 95%. | Начать с `ytdlp + metadata_only` (бесплатно); заложить $20–50 на hosted API, если качество критично |
| **Q3** | **Бюджет на LLM.** При `claude-opus-5` и ~40 играх/сутки с 2 резюме: ориентировочно $1.5–4/сутки. `claude-sonnet-5` — примерно в 2.5 раза дешевле, `claude-haiku-4-5` — в 5. | Прямая статья расходов. | Дефолт `claude-opus-5`; при жёстком бюджете — `claude-sonnet-5` и `AI_USE_BATCH=true` для бэкфилла |
| **Q4** | **Глубина каталога.** 177 882 игры в `finder`, ~296 000 в sitemap. При 40 играх/час это ~185 суток на полный обход. Нужен ли весь каталог или достаточно игр с Metascore (18 524 — обход за ~19 суток)? | Определяет `CRAWL_MAX_GAMES_PER_RUN`, объём БД и сроки. | Ограничиться играми с Metascore (`metaScoreMin=1` и в browse-фазе) — остальные 90% каталога это шлак-приложения без оценок и отзывов, на них нечего суммаризировать |
| **Q5** | **Часовой пояс «нового дня».** UTC или локальная зона? Влияет на момент сброса дневного цикла. | Наблюдаемое поведение сервиса. | `CRAWL_TIMEZONE=UTC` |
| **Q6** | **Полнота отзывов.** Хранить все (для Elden Ring — десятки тысяч) или ограничиться кэпами (200 критиков / 500 пользователей на платформу)? | Объём БД и стоимость синхронизации. | Кэпы; они уже дают репрезентативную выборку для AI и для UI |
| **Q7** | **Целевая среда деплоя.** Одна VPS с docker compose или что-то большее (k8s, managed Postgres)? | Влияет на prod-override, бэкапы, секреты. | Одна VPS + docker compose; managed Postgres опционально |

### Требуют проверки в вашей инфраструктуре (`NEEDS VALIDATION`)

| # | Что проверить | Как | Критерий приёмки |
|---|---|---|---|
| **V1** | **Доступность `backend.metacritic.com` с сервера деплоя.** Проверено с residential-IP; с датацентрового Cloudflare может вести себя иначе. | `curl -s -o /dev/null -w "%{http_code}" "https://backend.metacritic.com/finder/metacritic/web?limit=1&productType=games"` с целевой машины, 50 раз за 5 минут | 50/50 → HTTP 200 без challenge |
| **V2** | **Работоспособность `yt-dlp` + PO-token sidecar.** §2.2.2 показал, что без `pot` субтитры пусты. Эффективность `bgutil` в вашей сети не проверена. | Поднять `bgutil-ytdlp-pot-provider`, прогнать 20 разных роликов, посчитать success rate | ≥70% — путь T1 годен; <70% — нужен hosted API (Q2) |
| **V3** | **Нужны ли residential-прокси для YouTube.** С датацентрового IP YouTube отдаёт consent-стену/капчу. | Тот же прогон V2 с прямого IP сервера | Если <30% — закладывать прокси в бюджет |
| **V4** | **Реальный rate limit Metacritic.** Наблюдали 20 параллельных запросов без 429, но не тестировали длительную нагрузку. | Контролируемый прогон 2 rps в течение 30 минут с мониторингом кодов ответа | 0 ответов 429/403 → `METACRITIC_RPS=2.0` безопасен |
| **V5** | **Стабильность `apiKey`.** Ключ `1MOZ…` зашит во фронтенд Metacritic и сейчас не валидируется. Он может смениться при их деплое. | Nightly contract-тест, извлекающий ключ из HTML и сравнивающий с конфигом | При расхождении — авто-обновление ключа из HTML + алерт |
| **V6** | **Качество автоматических субтитров** для игровых Let's Play (много звукоподражаний, шума, перекрикивания). | 10 транскриптов → ручная оценка пригодности для суммаризации | Если >50% нечитаемы — понизить приоритет Bonus 1 |

---

# Приложение A — Архитектурные развилки (требование ТЗ §35)

Для каждой оси рассмотрено минимум два разумных варианта, указаны trade-offs и сделан выбор.

### A.1 FastAPI (Python) vs NestJS (TypeScript)

| | FastAPI + Python | NestJS + TypeScript |
|---|---|---|
| Парсинг/скрейпинг | httpx, selectolax, extruct — зрелые | cheerio, got — тоже неплохо |
| Валидация внешнего JSON | **Pydantic v2 — та же модель для входящих и исходящих данных**, Rust-ядро | zod/class-validator — работает, но DTO-слоя два |
| AI-экосистема | anthropic SDK, tiktoken, fastembed/ONNX, numpy — родная территория | SDK есть, эмбеддинги локально — боль |
| Очередь | Celery (зрелая, но синхронная) / arq | **BullMQ — отличная, типизированная** |
| Единый язык с фронтендом | нет | **да** |
| Скорость самого рантайма | ниже | выше |
| Наш профиль нагрузки | IO-bound, 40 игр/час — рантайм не узкое место | то же |

**Выбор: FastAPI.** Решающие аргументы: (1) весь проект — про данные и AI, а не про высокочастотный API; (2) Pydantic позволяет описать **схему ответов Metacritic** и **схему нашего API** одним инструментом, что напрямую решает задачу защиты от дрейфа схемы (§8.3); (3) локальные эмбеддинги на CPU в Node — маргинальный сценарий, в Python — стандарт. Единый язык с фронтендом — реальная потеря, но её компенсирует генерация TS-типов из OpenAPI.

### A.2 Next.js vs React SPA (Vite) vs Vue/Nuxt

| | Next.js 15 | React SPA (Vite) | Nuxt 3 |
|---|---|---|---|
| Первый рендер каталога | **SSR/ISR — HTML сразу** | пустой div → загрузка JS → fetch | SSR |
| SEO | **есть** | нет | есть |
| CORS | **не нужен** (серверный fetch) | нужен | не нужен |
| **Оптимизация обложек** | **`next/image` — ключевое** (Metacritic отдаёт неподписанные оригиналы ~140 KB, а их CDN-ресайз подписан HMAC и нам недоступен) | нужен свой прокси/сервис | `nuxt/image` |
| Контейнеров | +1 Node | 0 (статика в nginx) | +1 Node |
| Сложность | средняя | **минимальная** | средняя |

**Выбор: Next.js.** Аргумент, перевешивающий лишний контейнер, — обложки. Мы **обязаны** отдавать пользователю уменьшенные изображения, а воспользоваться ресайз-CDN Metacritic не можем (§2.1.2: неверная подпись → 403, а подделывать подпись мы не будем). Писать собственный image-proxy — это фактически переизобрести `next/image`. Плюс SSR даёт быстрый каталог без «мигания» скелетонов. Nuxt эквивалентен по возможностям, но React-экосистема (shadcn/ui, TanStack) для нашей задачи богаче.

### A.3 Celery vs RQ vs arq vs Dramatiq

| | **Celery 5** | RQ | arq | Dramatiq |
|---|---|---|---|---|
| Планировщик | **Beat — встроен** | rq-scheduler (сторонний) | встроен | APScheduler |
| Retry/backoff | **встроены, гибкие** | базовые | базовые | хорошие |
| Переживание смерти воркера | **`acks_late` + `visibility_timeout`** | слабо | средне | средне |
| Графы задач | **chain/group/chord** | нет | нет | pipelines |
| Async-воркеры | нет (prefork/gevent) | нет | **да, asyncio** | нет |
| Зрелость / поиск решений | **максимальная** | высокая | низкая | средняя |
| Вес | тяжёлый | лёгкий | **очень лёгкий** | средний |

**Выбор: Celery.** Наши задачи — IO-bound, и arq с asyncio дал бы бóльшую плотность конкурентности. Но реальная нагрузка (40 игр/час, ~12 HTTP-запросов на игру при лимите 2 rps) **упирается в rate limiter, а не в воркер** — выигрыш asyncio здесь нулевой. Взамен Celery даёт то, что нам действительно нужно и что пришлось бы писать руками: `acks_late` (Сценарий 4 ТЗ §30), Beat, гибкие ретраи, изоляцию очередей. RQ отпадает из-за слабого поведения при падении воркера — а это прямое требование ТЗ. Решение зафиксировано в ADR-0003.

### A.4 HTTP-скрейпинг vs Playwright/browserless

| | **Чистый HTTP** | Playwright |
|---|---|---|
| Нужен ли JS для наших данных | **нет — проверено**: JSON API отдаёт всё, HTML SSR-рендерится | — |
| Скорость | 20 игр за 2.26 с | секунды на страницу |
| Ресурсы | десятки МБ | сотни МБ + браузер в образе |
| Устойчивость к anti-bot | ниже | выше |

**Выбор: чистый HTTP, с Playwright как готовым отключённым адаптером.** Эмпирика однозначна: `curl` с дефолтным UA получает 200 и от `backend.metacritic.com`, и от `www.metacritic.com`. Тащить браузер в образ ради гипотетического будущего — нарушение Rule 1 («не переусложняй»). Но интерфейс `MetacriticSource` спроектирован так, что `BrowserSource` добавляется без изменения бизнес-логики.

### A.5 REST vs GraphQL

| | **REST + OpenAPI** | GraphQL |
|---|---|---|
| Число потребителей | 1 (наш фронт) | 1 |
| Число ресурсов | ~12 эндпоинтов | — |
| Over/under-fetching | решается параметром `include` | решается схемой |
| Кэширование | **HTTP-кэш, ISR, CDN — из коробки** | нужен persisted queries |
| Риск дорогих запросов | нет | N+1 и глубокие запросы требуют dataloader + depth limit |
| Генерация типов | **openapi-typescript** | codegen |

**Выбор: REST.** GraphQL решает проблему множества разнородных клиентов — у нас один клиент и стабильный набор экранов. Зато HTTP-кэширование для публичного каталога (`Cache-Control` + ISR + CDN) — это прямая выгода, которую GraphQL усложняет.

### A.6 Polling vs SSE vs WebSocket

Подробная таблица — §14.1. Кратко: **SSE**, потому что поток односторонний, `EventSource` умеет reconnect и `Last-Event-ID` из коробки, а WebSocket добавил бы неиспользуемый обратный канал и апгрейд протокола в прокси. Polling оставлен как автоматический fallback.

### A.7 Metadata vs Embeddings vs Hybrid similarity

Подробная таблица — §11.1. Кратко: **Hybrid**, потому что Metacritic даёт сильные *жёсткие* сигналы (франшиза, разработчик, платформенная семья), которые эмбеддинг размывает, а по одним метаданным 30 000 игр жанра «Action» неразличимы. Гибрид объясним, деградирует до metadata-скора и не имеет холодного старта.

### A.8 YouTube Data API vs альтернативные способы

| | **Data API v3** | Скрейпинг выдачи | Hosted API третьей стороны |
|---|---|---|---|
| Легальность | **официально** | серая зона, против ToS | зависит от провайдера |
| Метаданные | **чистые: ISO-длительность, числовые views, `liveBroadcastContent`, `caption`, язык** | локализованные строки, нет части полей | зависит |
| Квота | 100 поисков/сутки | «безлимит», но баны | по тарифу |
| С датацентрового IP | работает | капчи/consent | работает |
| Транскрипты | **невозможны для чужих видео** (`captions.download` требует OAuth владельца) | `timedtext` пуст без `pot` (проверено) | **работают** |

**Выбор: Data API для поиска и метаданных** (единственный легальный и качественный источник ранжирующих признаков), **скрейпинг — выключенный fallback** только при исчерпании квоты и только по явному включению, **транскрипты — каскад** (§12.4), потому что ни один единичный способ не даёт приемлемой надёжности.

---

# Приложение B — Реестр ADR

| ADR | Решение | Ключевое обоснование |
|---|---|---|
| 0001 | Использовать внутренний JSON API Metacritic как первичный источник | Типизированный JSON вместо Tailwind-селекторов; 1 вызов вместо парсинга 970 KB HTML; подтверждено экспериментально |
| 0002 | Запрет CSS-селекторов по Tailwind-классам | Классы генерируются и меняются при любом редизайне; допустимые якоря — JSON-LD, OpenGraph, `data-testid` |
| 0003 | Celery вместо arq/RQ | `acks_late` + Beat + гибкие ретраи; выигрыш asyncio нулевой, т.к. упираемся в rate limiter |
| 0004 | SSE вместо WebSocket | Односторонний поток; встроенный reconnect и `Last-Event-ID`; нет апгрейда протокола в прокси |
| 0005 | Гибридная модель похожести | Жёсткие сигналы (франшиза/разработчик) + семантика; нет холодного старта; объяснимо |
| 0006 | Каскад провайдеров транскриптов | Ни один способ не надёжен; `metadata_only` гарантирует непустой результат; статус явно виден в БД и UI |
| 0007 | Дедупликация констрейнтом БД, а не логикой приложения | Логика может содержать баг, `UNIQUE` — нет; работает при любом числе воркеров |
| 0008 | Денормализованные роллапы в `games` | Список из одной таблицы без JOIN; согласованность гарантируется единой транзакцией |

---

**Конец документа.** Реализация не начата — ожидается подтверждение архитектуры и ответы на §28.
