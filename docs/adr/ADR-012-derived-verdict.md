# ADR-012 — Вердикт вычисляется, а не генерируется

**Статус:** принято · 2026-09-06
**Связанные противоречия:** C-11, C-13

## Context

Страница игры обязана дать ответ за 20–30 секунд. Главный носитель ответа — одна фраза
над сгибом.

Два источника расходятся:

- `design/README.md §3 п.5` (**non-negotiable**): «The verdict sentence is **derived, not
  generated**. `verdictLine()` composes it from the two scores. It can never contradict the
  numbers and needs no AI disclaimer. **Do not replace it with model output.**»
- `PRODUCT_BLUEPRINT.md §6.4, §16, DEFINITION OF MVP п.6`: «Platform-aware вердикт» —
  killer feature, отнесена к AI-функциям.

При этом сам продуктовый блюпринт (§16) пишет про эту фичу: риск галлюцинаций «**низкий**…
числовая часть — арифметика, не генерация», а §14 отдельно требует «никогда не императив»,
«всегда атрибуция», «всегда объём выборки».

## Decision

**Вердикт — чистая детерминированная функция от оценок. LLM в ней не участвует.**

```python
def verdict_line(critic: ScoreValue, user: ScoreValue) -> VerdictLine:
    m, u = critic.normalized, user.normalized
    if m is None and u is None:
        return "Not enough reviews yet to say anything useful."
    if m is None:
        return f"{PLAYER_COPY[tier(u)]}. No critic score yet."
    if u is None:
        return f"{CRITIC_COPY[tier(m)]}. No player score yet."
    delta = m - u
    if abs(delta) < AGREEMENT_THRESHOLD:          # 7 пунктов
        return f"{CRITIC_COPY[tier(m)]}, and players agree."
    if delta > 0:
        return f"{CRITIC_COPY[tier(m)]} — but players rate it {delta} points lower."
    return f"{CRITIC_COPY[tier(m)]} — and players rate it {abs(delta)} points higher."
```

Строки — из `design/COPY.md §3`, дословно. Порог согласия — 7 пунктов, из
`design/prototype/assets/app.js`. Обе константы живут в одном месте на весь продукт.

### Platform-awareness — тоже арифметика

Вторая строка появляется, когда у пользователя выбрана платформа (ADR-010) и она заметно
отличается от лучшей:

```python
def platform_line(selected: PlatformScores, best: PlatformScores) -> str | None:
    if selected is None or best is None or selected.platform_id == best.platform_id:
        return None
    d = best.metascore.normalized - selected.metascore.normalized
    if d < PLATFORM_GAP_MIN_POINTS:               # 10
        return None
    return (f"On {selected.platform_name} critics rate this {d} points lower "
            f"than on {best.platform_name}.")
```

Это ровно та killer feature, которую описывает продукт («на PS4 — 57, на PC — 86»),
и она не может соврать, потому что не является утверждением о мире — только о наших числах.

### Что остаётся за AI

| Элемент | Кто пишет | Дисклеймер |
|---|---|---|
| Строка вердикта | арифметика | не нужен |
| Платформенная строка | арифметика | не нужен |
| Consensus-note (интерпретация gap'а в словах) | шаблон по величине gap'а (`COPY.md §3`) | не нужен |
| Signal chips (3 strength + 2 watch-out) | **берутся дословно** из списков резюме, ничего нового не генерируется | наследуют provenance резюме |
| Consensus-параграф резюме | LLM | `[AI summary] Compressed from N reviews` |
| Most praised / Most criticised | LLM с evidence | то же |
| Объяснение gap'а | LLM с evidence обеих сторон | то же |
| Let's Play impression | LLM по транскрипту | `[YouTube] [AI summary] Written from the video, not from reviews` |

Signal chips — важная деталь дизайна (`UX_SPEC.md §2`): «Signals are phrases lifted from the
summaries, **never newly generated** — no new claim enters the page at the point where the
user is least equipped to check it.» Соблюдается буквально: чипы — это первые N элементов
списков `positive`/`negative` уже провалидированного резюме.

## Alternatives considered

| Вариант | Почему не выбран |
|---|---|
| Вердикт пишет модель по резюме обеих сторон | Может противоречить числам прямо над собой — худший дефект доверия. Требует дисклеймера в самом заметном месте страницы. Стоит денег на каждой игре |
| Вердикт — модель, но с post-проверкой соответствия числам | Проверка «текст не противоречит числам» на естественном языке ненадёжна; проще вычислить текст, чем проверять его |
| Вообще без вердикта, только числа | Продукт становится тем, чем является источник: «какая оценка» вместо «стоит ли». Теряется весь смысл |
| Шаблон, но с 20 вариациями формулировок для «живости» | Вариативность здесь — минус: пользователь, сравнивающий две игры, должен видеть одинаково устроенные фразы |

## Consequences

- Вердикт доступен **всегда**, даже когда резюме нет, ниже порога или отвергнуто валидатором —
  он не зависит от LLM и от корпуса.
- Стоимость вердикта — ноль вызовов и ноль токенов на игру.
- Тестируется таблично: матрица (tier критиков × tier игроков × наличие/отсутствие × знак gap'а),
  включая границы 6/7/8 пунктов и 84/85, 69/70, 49/50 по тирам.
- Меньше «нюанса», чем у модельной фразы, — это цена, зафиксированная и в
  `design/REVIEW.md §3` как осознанный trade-off.

## Risks

| Риск | Митигация |
|---|---|
| Фраза звучит механически | Копирайт взят из дизайна, который его специально прорабатывал; тон «informed, plain, unexcited» — заявленный тон продукта |
| Порог 7 пунктов подобран | Он из дизайна и согласуется с порогом gap-блока; вынесен в конфиг; калибровка — OQ-N2 |
| Пользователь решит, что вердикт тоже AI | Дисклеймеры стоят **только** там, где действительно работала модель; `/about` объясняет разницу |
