# ADR-017 — Фронтенд: server-rendered Jinja2 вместо Next.js

**Статус:** принято · 2026-09-06
**Заменяет:** `BLUEPRINT.md §4, §16, Приложение A.2`
**Связанные противоречия:** C-20

## Context

Блюпринт выбрал Next.js 15 + React 19 + Tailwind + shadcn/ui. Решающим аргументом (A.2) была
оптимизация обложек: Metacritic отдаёт неподписанные оригиналы ~140 КБ, а его CDN-ресайз
подписан HMAC и нам недоступен, поэтому «писать собственный image-proxy — это фактически
переизобрести `next/image`». Вторичные аргументы: SSR/SEO, отсутствие CORS, экосистема.

Дизайн-пакет, объявленный source of truth для фронтенда, устроен иначе:

- `design/README.md §4`: «Framework, routing, rendering strategy, state management —
  **adapt freely**. Nothing here assumes React.»
- `design/README.md §5`: «`tokens.css` — **copy this into the app almost verbatim**.»
- `design/prototype/assets/app.css` (929 строк) — обычный CSS с семантическими классами,
  каждый задокументирован в `COMPONENTS.md`.
- `design/prototype/assets/app.js` — ванильный JS; правила, которые он кодирует
  (`tier()`, `verdictLine()`, fallback обложки), объявлены **нормативными**, а реализация — нет.
- `styleguide.html` объявлен «the acceptance reference».

То есть дизайн уже поставил готовый, отревьюированный слой представления, не требующий сборки.

## Decision

**Фронтенд — server-rendered Jinja2-шаблоны внутри того же FastAPI-приложения.
CSS и клиентский JS взяты из `design/prototype/assets` практически без изменений.**

```
app/web/
  templates/
    base.html                 topbar, footer, skip-link, theme, noindex для /admin
    index.html                Home / Discovery
    catalog.html              Catalog
    game.html                 Game Details
    admin/monitoring.html     Operations
    about.html
    partials/                 game_card, score, verdict, consensus, summary, similar,
                              platform_row, empty_state, pending, skeleton, rail, pager
    fragments/                catalog_grid.html   <- отдаётся при фильтрации без перезагрузки
  static/
    tokens.css                из design/prototype/assets/tokens.css
    app.css                   из design/prototype/assets/app.css
    app.js                    ванильный: тема, рельсы, clamp, spoiler, табы, фильтры, SSE
```

### Что получаем

| | Next.js | **Jinja2 SSR (выбрано)** |
|---|---|---|
| Соответствие визуальному source of truth | порт CSS в Tailwind/CSS-modules → риск дрейфа от `styleguide.html` | **те же классы, тот же CSS-файл** |
| SSR / SEO | да | **да** |
| CORS | не нужен | **не нужен** |
| Шаг сборки | обязателен | **отсутствует** |
| Контейнеров | +1 Node | **0** |
| Языков в проекте | 2 | **1** |
| Зависимостей | ~500 МБ node_modules | **0** |
| Ресайз обложек | `next/image` | собственный `/img` endpoint |

### Ресайз обложек — `/img`

```
GET /img?u=<url>&w=<width>
```

- **Жёсткий whitelist хоста** (`www.metacritic.com`) — не regex, а точное сравнение;
  всё остальное → 400. Это строже, чем `remotePatterns` в Next, и закрывает SSRF (T4).
- Разрешённые ширины — фиксированный набор (`96, 200, 320, 480, 640`), иначе 400:
  произвольная ширина = произвольная нагрузка на CPU.
- Ответ кэшируется на диск по `sha256(url|w)` с TTL и лимитом размера кэша;
  `Cache-Control: public, max-age=604800, immutable`.
- Ресайз — Pillow, формат WebP с fallback на JPEG.
- Таймаут и лимит размера ответа источника; при ошибке — 302 на оригинал, а не 500.

Реализация — ~80 строк, покрыта тестами (whitelist, недопустимая ширина, отказ источника).

### Интерактивность без фреймворка

| Поведение дизайна | Реализация |
|---|---|
| Фильтрация «instant and in place, 200 мс debounce, без Apply на десктопе» | `fetch('/games/fragment?…')` → замена `innerHTML` сетки и чипов; URL обновляется `history.replaceState` (фильтры остаются в ссылке — требование `US-005`) |
| Bottom sheet фильтров ≤1023 | тот же CSS дизайна + `dialog`-семантика, focus trap, Escape |
| Anchor nav со scroll-spy | скролл-обработчик из прототипа (последняя секция, чей top прошёл 140px) — **не** `IntersectionObserver`, дизайн явно объясняет почему |
| Clamp «Read full summary» | из прототипа, `aria-expanded` |
| SpoilerShield | из прототипа |
| Rails со snap и стрелками | из прототипа; стрелки скрыты, когда нечего скроллить |
| Тема | `data-theme` на `<html>` + `localStorage` |
| Мониторинг real-time | `EventSource` → SSE; fallback на polling (ADR-013) |
| Предпочитаемая платформа | `localStorage`, влияет на фильтр каталога и подсветку строки (ADR-010) |

Прогрессивное улучшение: без JS каталог фильтруется обычной формой (GET-параметры),
страница игры и мониторинг рендерятся сервером. Это строго лучше, чем SPA-каталог.

### Честная оговорка

В среде разработки свободно 384 МБ; `npm install` для Next.js 15 (~500 МБ) невозможен,
и фронтенд на Next остался бы ненаписанным или непроверенным. Это ускорило решение.
Но аргументы выше самодостаточны: главный довод блюпринта (`next/image`) закрывается
80 строками, которые вдобавок дают более строгую защиту от SSRF, а дизайн явным текстом
приглашает не тащить React.

## Alternatives considered

| Вариант | Почему не выбран |
|---|---|
| Next.js 15 (блюпринт) | Второй язык, второй пакетный менеджер, шаг сборки, +1 контейнер, ~500 МБ зависимостей и риск дрейфа от `styleguide.html` — ради ресайза картинок и SSR, которые есть и так |
| React SPA (Vite) | Нет SSR/SEO, нужен CORS, пустой div до загрузки JS — прямо против «20-second contract» дизайна |
| Отдать статический прототип как есть | Прототип работает на мок-данных и объявлен не-production кодом; нет серверной фильтрации, пагинации и мониторинга |
| HTMX | Дал бы то же самое, но добавил бы зависимость ради ~120 строк ванильного JS, которые и так надо написать |

## Consequences

- Один Docker-образ, один процесс, одна команда запуска.
- Шаблоны и API живут рядом; риск — «логика представления просачивается в API».
  Митигация: **шаблоны рендерятся из тех же Pydantic-схем ответов**, что отдаёт REST API.
  То есть `/games/{slug}` (JSON) и `/games/{slug}` (HTML) используют один сервисный вызов
  и одну схему. Расхождение контракта невозможно по построению.
- OpenAPI по-прежнему актуален и полон — REST API существует независимо и покрыт тестами.
- E2E-тесты выполняются на уровне HTML (парсинг ответа), а не через браузер. Playwright
  и axe-проверки — отложены как validation-задача (OQ-N3), доступность обеспечена
  разметкой дизайна и проверяется структурными тестами (наличие `aria-label` у гейджей,
  `aria-current`, skip-link, `role="dialog"` у sheet).

## Risks

| Риск | Митигация |
|---|---|
| Ручной перенос вёрстки исказит дизайн | CSS не переписывается, а копируется; классы совпадают с `COMPONENTS.md`; `styleguide.html` остаётся приёмочной ссылкой |
| Богатая интерактивность потребует фреймворка позже | Все шаблоны — партиалы; переход на любой фреймворк не требует переписывать API |
| Отсутствие браузерных тестов | Структурные HTML-тесты на каждое правило `EDGE_CASES.md`; браузерная проверка — OQ-N3 |
| `/img` станет вектором нагрузки | Whitelist хоста, фиксированный набор ширин, дисковый кэш, rate limit, таймаут источника |
