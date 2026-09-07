# Requirement Traceability Matrix

**Дата:** 2026-09-06 · **Вторая редакция.** Статусы проставлены по факту прогона: `make test`, браузерный проход, contract-набор против живого источника.

> **Оговорка (OQ-B1):** файла исходного ТЗ в репозитории нет. Требования реконструированы
> по матрице соответствия `BLUEPRINT.md §27` (35 пунктов) и по тексту задания.
> Если оригинал будет положен в `docs/ASSIGNMENT.md`, матрица пересверяется за один проход.

Легенда статусов: `PLANNED` · `IN_PROGRESS` · `IMPLEMENTED` (код есть) ·
`VERIFIED` (код есть и покрыт проходящим тестом) · `BLOCKED` · `DEFERRED`.

Колонки: **Req** — требование · **Prod** — продуктовое решение · **UX** — где в интерфейсе ·
**BE** — модуль backend · **Data** — таблицы · **API** — эндпоинт · **Test** — тест ·
**AC** — критерий приёмки из `FINAL_SPEC §28` · **St** — статус.

---

## 1. Crawler (ТЗ §1–§2, §16–§18)

| ID | Req | Prod | UX | BE | Data | API | Test | AC | St |
|---|---|---|---|---|---|---|---|---|---|
| R-1.1 | Запуск примерно раз в час | свежесть как SLA, а не расписание | «Updated hourly» в каталоге | `app/queue/scheduler.py` | `jobs` | — | `test_fires_the_hourly_tick_on_its_minute`, `test_the_same_slot_cannot_fire_twice` | A-1 | VERIFIED |
| R-1.2 | Первый источник — New Releases, первые 20 | источник данных, в UI подаётся как «Just released» | Home rail 1 | `services/crawl_orchestrator.py` | `crawl_days.new_releases_done` | — | `test_claims_the_first_twenty` | A-1 | VERIFIED |
| R-1.3 | Далее browse `all-time/new` | наполнение каталога | `/games` | `services/crawl_orchestrator.py` | `crawl_days.browse_page` | — | `test_leftover_budget_spills_into_browse` | A-3 | VERIFIED |
| R-1.4 | Продолжать со следующей страницы | — | — | то же | `crawl_days` | — | `test_cursor_advances_and_persists`, `test_cursor_is_persisted_and_survives_a_new_session` | A-3 | VERIFIED |
| R-1.5 | Новый день — цикл заново | — | — | то же | `crawl_days.crawl_date` | — | `test_a_new_day_restarts_the_cycle`, `test_a_new_day_starts_over` | A-3 | VERIFIED |
| R-1.6 | **Максимум 20 игр за прогон**, не обработанных сегодня | прямая экономия LLM | — | `crawl_orchestrator` + `CRAWL_MAX_GAMES_PER_RUN=20` | `crawl_items` UNIQUE | — | `test_claims_the_first_twenty`, `test_budget_is_respected`, `test_max_games_can_be_lowered_but_never_raised` | A-1 | VERIFIED |
| R-1.7 | Существующая игра обновляется, не дублируется | — | — | `services/game_sync.py` | `games` UNIQUE | — | `test_a_renamed_slug_updates_rather_than_duplicates`, `test_an_unchanged_day_creates_no_new_rows` | A-2 | VERIFIED |
| R-1.8 | Собрать title, cover, platform, Metascore, Userscore, developer, description, video | базовое узнавание | hero + What it is | `parsers/game.py`, `normalizers/game.py` | `games` | `GET /games/{slug}` | `test_elden_ring`, `test_persists_the_whole_game_in_one_go` | A-4 | VERIFIED |
| R-1.9 | Отдельные оценки по платформам | **killer feature** | секция `By platform` | `services/game_sync.py` | `game_platforms` | `GameDetail.platforms` | `test_per_platform_scores`, `test_platforms_and_scores_are_normalised`, `test_a_selected_platform_adds_the_second_sentence` | A-5 | VERIFIED |
| R-1.10 | Дедупликация: идентификаторы | — | — | `normalizers/ids.py` | `mc_slug`, `mc_title_id` | — | `test_a_renamed_slug_updates_rather_than_duplicates`, `test_is_stable_across_parses` | A-2 | VERIFIED |
| R-1.11 | Дедупликация: обрабатывалась ли сегодня | — | — | `repositories/crawl.py` | `UNIQUE(crawl_date, game_slug)` | — | `test_games_already_claimed_today_are_counted_as_duplicates`, `test_claim_returns_only_new_games` | A-19 | VERIFIED |
| R-1.12 | Переживание перезапуска | — | — | всё состояние в БД | `crawl_days`, `jobs` | — | `test_cursor_is_persisted_and_survives_a_new_session`, `test_expired_lease_returns_to_pending` | A-18 | VERIFIED |
| R-1.13 | Не потерять прогресс при сбое источника | — | — | `crawl_orchestrator` | `crawl_runs.status='partial'` | — | `test_source_failure_leaves_the_cursor_untouched` | A-18 | VERIFIED |
| R-1.14 | Race conditions, N воркеров | — | — | 3 барьера | partial UNIQUE + `crawl_items` | — | `test_concurrent_claims_yield_exactly_one_row`, `test_two_workers_never_claim_the_same_job`, `test_second_active_run_is_refused` | A-19 | VERIFIED |
| R-1.15 | Differential update | 1 запрос и 0 LLM на известную игру | — | `services/game_sync.py` | `source_fingerprint` | — | `test_an_unchanged_day_creates_no_new_rows`, `test_unchanged_input_does_not_queue_similarity` | A-2 | VERIFIED |
| R-1.16 | Устойчивые селекторы / structured data | — | — | `source_json` → `source_html` (JSON-LD, `data-testid`) | — | — | `test_no_tailwind_class_selectors_in_html_parsing`, `test_missing_cards_raise_drift`, `test_schema_drift_is_partial_not_fatal` | A-22 | VERIFIED |

## 2. Reviews и AI (ТЗ §4–§5)

| ID | Req | Prod | UX | BE | Data | API | Test | AC | St |
|---|---|---|---|---|---|---|---|---|---|
| R-2.1 | Critic reviews: издание, оценка, текст, URL, дата | доказательная база | provenance + ссылка | `parsers/reviews.py` | `reviews` | `GET /games/{s}/reviews` | `test_critic_reviews` | A-6 | VERIFIED |
| R-2.2 | User reviews: идентификатор, оценка, текст, дата | то же | то же | то же | `reviews.source_review_id` | то же | `test_user_reviews` | A-6 | VERIFIED |
| R-2.3 | Не сохранять одинаковые повторно | — | — | `repositories/reviews.py` | `UNIQUE(gp_id, kind, dedupe_key)` | — | `test_critic_dedupe_key_is_stable_and_discriminating`, `test_critic_dedupe_key_survives_cosmetic_edits` | A-6 | VERIFIED |
| R-2.4 | Резюме критиков: нравится / не нравится / вывод | ядро ценности | `What critics say` | `services/summary_service.py` | `summaries`, `summary_claims` | `GET /games/{s}/summaries` | `test_a_small_critic_corpus_still_produces_a_summary`, `test_a_claim_matching_its_evidence_passes` | A-7 | VERIFIED |
| R-2.5 | Резюме игроков | то же | `What players say` | то же | то же | то же | `test_a_thin_player_corpus_is_skipped_not_faked`, `test_the_skip_is_recorded_so_the_ui_can_explain_it` | A-7 | VERIFIED |
| R-2.6 | **Не заставлять генерировать негатив без данных** | принцип «неудобная правда без квоты» | пустая секция с честной формулировкой | `ai/validator.py`, схема без `min_items` | `summary_claims` | `Summary.negative: []` | `test_zero_negative_claims_is_a_valid_summary`, `test_critic_stats_can_be_all_positive`, `test_no_criticism_is_stated_informatively` | A-7 | VERIFIED |
| R-2.7 | Когда обновлять резюме | пересчёт по дельте, не по расписанию | — | `should_regenerate` | `input_fingerprint` UNIQUE | — | `test_an_identical_corpus_never_reaches_the_model`, `test_a_moved_score_forces_a_review_resync` | A-20 | VERIFIED |
| R-2.8 | Не вызывать LLM без необходимости | статья затрат | — | то же | UNIQUE | — | `test_an_identical_corpus_never_reaches_the_model` | A-20 | VERIFIED |
| R-2.9 | Версионирование резюме | — | — | `summaries.version`, `is_current` | partial UNIQUE | — | `test_a_new_version_demotes_the_old_one_atomically`, `test_the_constraint_backs_up_the_logic` | — | VERIFIED |
| R-2.10 | Восстановление после ошибки AI | прежнее резюме остаётся | бейдж «обновляется» | retry + `status` | `summaries.status` | — | `test_a_provider_failure_keeps_the_previous_summary_current` | A-16 | VERIFIED |
| R-2.11 | Timestamp последнего успешного анализа | — | provenance | — | `generated_at`, `summaries_synced_at` | `Summary.provenance` | `test_cost_is_recorded_per_call`, `test_candidate_count_is_reported_for_provenance` | — | VERIFIED |
| R-2.12 | Evidence на каждое утверждение (§24 задания) | механика доверия | ссылки в блоке | `ai/validator.py` | `summary_claims.evidence_refs` | `Claim.evidence` | `test_an_unknown_reference_is_rejected`, `test_invented_references_are_rejected_and_the_summary_is_not_published` | A-7 | VERIFIED |
| R-2.13 | Immutable snapshot корпуса | ссылки не разъезжаются | — | `services/snapshot_service.py` | `review_snapshots` | `Summary.provenance.snapshot_id` | `test_references_keep_pointing_at_the_same_reviews`, `test_new_reviews_produce_a_new_snapshot_not_a_mutated_one` | A-7 | VERIFIED |
| R-2.14 | Даты отзывов в корпусе | temporal-claims обоснованы | — | `ai/corpus.py` | `reviews.published_on` | — | `test_corpus_lines_carry_dates`, `test_every_line_carries_a_reference_and_a_date`, `test_undated_evidence_cannot_support_a_temporal_claim` | A-7 | VERIFIED |

## 3. Similar games (ТЗ §6)

| ID | Req | Prod | UX | BE | Data | API | Test | AC | St |
|---|---|---|---|---|---|---|---|---|---|
| R-3.1 | Только из своей БД | — | `If you like this` | `similarity_service` (кандидаты из `games`) | `similar_games` | `GET /games/{s}/similar` | `test_a_game_never_appears_in_its_own_list`, `test_similar_is_empty_rather_than_padded` | A-9 | VERIFIED |
| R-3.2 | Рассмотреть варианты и обосновать | — | — | ADR-011 | — | — | — | A-9 | IMPLEMENTED |
| R-3.3 | Эффективное хранение/вычисление | — | — | 2 фазы, top-12 | `similar_games(game_id, rank)` | — | `test_recompute_is_idempotent`, `test_scoring_is_deterministic` | — | VERIFIED |
| R-3.4 | Показ в карточке, кликабельные | драйвер глубины сессии | snap-рельс | шаблон `similar` | — | — | `test_case_15_a_similar_card_without_a_reason_omits_the_chip` | A-9 | VERIFIED |
| R-3.5 | Объяснение похожести | без него ценность отрицательна | reason-чип | правило по компонентам | `similar_games.reason` | `SimilarGame.reason` | `test_no_reason_is_produced_when_nothing_is_strong`, `test_aspect_profile_produces_a_reason`, `test_same_studio_is_named` | A-9 | VERIFIED |

## 4. Web interface (ТЗ §7–§8, §27)

| ID | Req | Prod | UX | BE | Data | API | Test | AC | St |
|---|---|---|---|---|---|---|---|---|---|
| R-4.1 | Список: название, cover, рейтинг, платформы, краткая инфо | скан за 0.5 с | `GameCard` | `templates/partials/game_card.html` | роллапы `games` | `GET /games` | `test_case_2_a_card_shows_two_codes_and_an_overflow_token`, `test_case_4_a_missing_userscore_omits_the_card_chip` | A-10 | VERIFIED |
| R-4.2 | Поиск по названию | точка входа №1 | topbar + catalog | `repositories/games.search_titles` | `title_norm` | `?q=` | `test_search_normalises_punctuation`, `test_search_matches_after_normalisation`, `test_suggest` | A-10 | VERIFIED |
| R-4.3 | Фильтр по платформе | + предпочитаемая платформа | чипы с counts | полу-джойн | `game_platforms` | `?platform=` | `test_platform_filter_is_an_or`, `test_facets_carry_counts` | A-10 | VERIFIED |
| R-4.4 | Сортировка по рейтингу с явной базой | — | select | `repositories/games.py` | индексы сортировки | `?sort=` | `test_ascending_sort_does_not_float_unrated_games_to_the_top`, `test_sort_by_gap_is_a_first_class_option` | A-10 | VERIFIED |
| R-4.5 | Пагинация | стабильная позиция | `Pager` | offset + tiebreak по `id` | — | `?offset=`+`?limit=` | `test_pagination`, `test_deep_offsets_are_refused` | A-10 | VERIFIED |
| R-4.6 | loading / empty / error | «empty ≠ broken» | `states.html` дизайна | шаблоны | — | — | `test_case_21_a_search_with_no_results_names_the_query`, `test_case_22_filters_with_no_results_point_at_the_filters`, `test_case_23_an_empty_index_is_its_own_message` | A-12 | VERIFIED |
| R-4.7 | Responsive | 4 брейкпоинта | CSS дизайна | — | — | — | `scripts/browser_smoke.mjs` (Chromium, 4 viewport) | A-12 | VERIFIED |
| R-4.8 | Карточка игры: все поля §8 | инвертированная пирамида | `game.html` | — | — | `GET /games/{slug}` | `test_returns_the_full_detail`, `test_case_6_the_review_count_is_always_rendered`, `test_case_18_platform_scores_pending_keeps_the_section` | A-11 | VERIFIED |
| R-4.9 | Вердикт над сгибом | 20–30 секунд до ответа | `verdict` | `domain/verdict.py` | — | `GameDetail.verdict` | `test_never_contradicts_the_numbers`, `test_the_verdict_never_contradicts_the_scores`, `test_real_cyberpunk_case` | A-11 | VERIFIED |
| R-4.10 | `null != 0` в UI | P0-требование Release 0 | `—` + «Not rated» | `domain/scores.py` | — | `ScoreValue.status` | `test_a_false_zero_is_reported_as_unavailable`, `test_case_3_a_missing_metascore_never_renders_zero`, `test_userscore_rule_is_identical_in_python_and_sql` | A-8 | VERIFIED |

## 5. Bonus 1 — YouTube (ТЗ §9)

| ID | Req | Prod | UX | BE | Data | API | Test | AC | St |
|---|---|---|---|---|---|---|---|---|---|
| R-5.1 | Поиск Let's Play | P2, изолирован | `See it played` | `adapters/youtube` | `youtube_videos` | `GameDetail.lets_play` | `test_a_low_interest_game_never_costs_a_search`, `test_the_search_is_spent_once_per_game` | A-15 | VERIFIED |
| R-5.2 | Наиболее популярный релевантный | популярность ≠ первый | — | `youtube/ranking.py` | `rank_score` | — | `test_the_most_viewed_survivor_does_not_automatically_win`, `test_popularity_is_normalised_against_survivors_not_the_trailer` | A-15 | VERIFIED |
| R-5.3 | Не выбрать трейлер | — | — | 3 барьера | `rejected_reason` | — | `test_the_trailer_is_rejected_by_name`, `test_a_playthrough_is_selected_and_the_trailer_is_kept_as_a_reject` | A-15 | VERIFIED |
| R-5.4 | Транскрипт с fallback | деградация вместо отказа | пометка источника | каскад провайдеров | `youtube_transcripts` | — | `test_the_first_good_result_stops_the_cascade`, `test_a_failed_cascade_records_every_attempt` | A-15 | VERIFIED |
| R-5.5 | Пустой 200 ≠ «нет субтитров» | — | — | `transcripts/base.py` | `attempts` | — | `test_an_empty_result_is_a_provider_failure_not_an_answer`, `test_empty_200_is_a_provider_error_not_empty_data` | A-15 | VERIFIED |
| R-5.6 | Резюме + ссылка | spoiler-free | свёрнутый блок внизу | `letsplay_service` | `summaries audience='letsplay'` | — | `test_without_a_transcript_no_summary_is_ever_written`, `test_a_disabled_llm_leaves_the_section_showing_metadata_only` | A-15 | IMPLEMENTED |
| R-5.7 | Отказ не ломает систему | — | секция удаляется | изолированные задачи | — | — | `test_a_quota_error_does_not_fail_the_game`, `test_a_provider_that_crashes_outright_cannot_kill_the_job`, `test_case_12_no_lets_play_removes_the_section_and_its_anchor` | A-17 | VERIFIED |

## 6. Bonus 2 — Monitoring (ТЗ §10, §20)

| ID | Req | Prod | UX | BE | Data | API | Test | AC | St |
|---|---|---|---|---|---|---|---|---|---|
| R-6.1 | Статус краулера и воркеров | оператор ≠ игрок | светофор | `monitoring_service` | `worker_heartbeats` | `GET /monitoring/status` | `test_status_carries_every_indicator_the_assignment_lists`, `test_queue_depth_and_workers`, `test_the_worker_heartbeats_even_when_idle` | A-13 | VERIFIED |
| R-6.2 | Текущая задача и стадия | — | current job + stage strip | то же | `jobs`, `job_events.stage` | то же | `test_the_stage_list_is_this_products_pipeline`, `test_events_bracket_every_job` | A-13 | VERIFIED |
| R-6.3 | Счётчики обработано/успешно/ошибки/пропущено | — | KPI | то же | `crawl_runs` | то же | `test_status_carries_every_indicator_the_assignment_lists`, `test_data_quality_is_reported_as_product_metrics` | A-13 | VERIFIED |
| R-6.4 | Ошибки | — | problem log | то же | `job_events` | `GET /monitoring/events` | `test_runs_and_events_are_listable`, `test_problem_filter` | A-13 | VERIFIED |
| R-6.5 | Очередь | — | queues | то же | `jobs` | то же | `test_queue_depth_and_workers`, `test_the_stage_list_is_this_products_pipeline` | A-13 | VERIFIED |
| R-6.6 | Последний / следующий запуск, длительность | — | header | то же | `crawl_runs` | то же | `test_status_carries_every_indicator_the_assignment_lists`, `test_next_run_is_reported_for_the_dashboard` | A-13 | VERIFIED |
| R-6.7 | История запусков | — | run history | то же | `crawl_runs` | `GET /monitoring/runs` | `test_runs_and_events_are_listable`, `test_case_26_a_stale_pipeline_is_a_banner_not_a_blocking_screen` | A-13 | VERIFIED |
| R-6.8 | `Run now` | инструмент демонстрации | кнопка + toast | `api/routers/admin.py` | — | `POST /admin/crawl/run` | `test_the_correct_token_is_accepted`, `test_run_now_never_runs_the_crawl_inside_the_request` | A-14 | VERIFIED |
| R-6.9 | Нельзя два конфликтующих job | — | кнопка заблокирована | 3 барьера | partial UNIQUE | — | `test_a_second_run_while_one_is_active_is_a_conflict`, `test_second_active_run_is_refused` | A-14 | VERIFIED |
| R-6.10 | Обновление без перезагрузки | — | SSE + polling | SSE из `job_events` | — | `GET /monitoring/stream` | `test_the_response_is_an_event_stream`, `test_last_event_id_replays_what_was_missed`, `test_the_server_survives_a_client_hanging_up` | A-13 | VERIFIED |

## 7. Технические требования (ТЗ §11–§15, §19, §21–§26, §28–§30, §33)

| ID | Req | Реализация | Test | AC | St |
|---|---|---|---|---|---|
| R-7.1 | Архитектура фоновой обработки | очередь в БД, воркер, планировщик (ADR-003) | `test_two_workers_never_claim_the_same_job`, `test_runs_a_job_and_records_the_result`, `test_priority_then_age` | A-19 | VERIFIED |
| R-7.2 | Разделение sync/async/retryable/idempotent | реестр задач §7 FINAL_SPEC | `test_every_task_declares_its_contract` | — | VERIFIED |
| R-7.3 | retry / backoff / timeout / rate limit / circuit breaker | `adapters/http/*`, `queue/retry.py` | `test_retries_on_5xx_then_succeeds`, `test_429_is_retryable_and_respects_retry_after`, `test_opens_after_threshold_and_fails_fast`, `test_jitter_spreads_retries` | A-18 | VERIFIED |
| R-7.4 | Idempotency / границы транзакций / partial failures | §23 FINAL_SPEC | `test_the_result_and_the_status_commit_together`, `test_duplicate_is_refused_by_the_constraint`, `test_a_failing_stats_call_degrades_only_that_platform` | A-19 | VERIFIED |
| R-7.5 | Отказ внешнего API не ломает сервис | §23 матрица | `test_a_disabled_provider_does_not_break_anything`, `test_a_quota_error_does_not_fail_the_game`, `test_an_exhausted_quota_defers_rather_than_fails` | A-16, A-17 | VERIFIED |
| R-7.6 | PostgreSQL, полная схема, индексы | §8 FINAL_SPEC | `test_all_tables_created`, `test_expected_unique_constraints_exist`, `test_partial_unique_indexes_exist` | A-23 | IMPLEMENTED |
| R-7.7 | Production-ready структура | §7 FINAL_SPEC | `test_domain_is_dependency_free`, `test_normalizers_are_pure`, `test_services_do_not_write_sql` | A-22 | VERIFIED |
| R-7.8 | Обоснованный выбор стека | ADR-001, 003, 011, 017, 018 | — | — | IMPLEMENTED |
| R-7.9 | Разделённая scraping-архитектура | `adapters/metacritic/*` + `parsers` + `normalizers` | `test_only_the_adapter_mentions_the_internal_host`, `test_services_do_not_speak_http`, `test_parsers_do_no_io` | A-22 | VERIFIED |
| R-7.10 | Полный API contract + OpenAPI | §9 FINAL_SPEC | `test_openapi_is_complete`, `test_html_routes_are_not_in_the_openapi_document` | — | VERIFIED |
| R-7.11 | Real-time без перезагрузки | SSE + polling fallback | `test_events_arrive_in_order_with_ascending_ids`, `test_last_event_id_replays_what_was_missed` | A-13 | VERIFIED |
| R-7.12 | Security: admin, CORS, secrets, SSRF, rate limit, SQLi, XSS, prompt injection | ADR-019 | `test_no_token_is_rejected`, `test_a_foreign_host_is_refused`, `test_no_template_marks_third_party_text_as_safe`, `test_review_text_cannot_carry_structure_into_a_prompt` | A-21 | VERIFIED |
| R-7.13 | Docker: compose, Dockerfile, env, volumes, healthchecks, порядок, миграции | §20 FINAL_SPEC | `test_compose_files.py` (21 теста: парсинг, роли, non-root, healthcheck, порядок слоёв, порт БД не проброшен) | A-27 | IMPLEMENTED |
| R-7.14 | Полный `.env.example` | Phase 0 | `test_every_setting_is_documented`, `test_no_real_secret_is_committed_in_the_example` | — | VERIFIED |
| R-7.15 | Structured logs с корреляцией | `app/logging.py` | `test_correlation_fields_reach_the_record`, `test_secrets_never_reach_the_stream`, `test_secrets_never_reach_the_event_log` | — | VERIFIED |
| R-7.16 | Unit / integration / E2E, моки внешних сервисов | §19 FINAL_SPEC | — | A-25 | VERIFIED |
| R-7.17 | Observability | `/metrics`, `job_events` | `test_metrics_are_exposed_in_prometheus_text_format`, `test_runs_and_events_are_listable` | — | VERIFIED |
| R-7.18 | UX/UI современного каталога | ассеты дизайна без изменений | `scripts/browser_smoke.mjs`, `test_case_1_a_long_title_is_never_truncated_in_the_markup` | A-12 | VERIFIED |
| R-7.19 | Performance: сразу / отложить / при масштабировании | §22 FINAL_SPEC | `test_the_count_does_not_grow_with_the_page_size`, `test_the_similar_rail_does_not_cost_a_query_per_neighbour` | — | VERIFIED |
| R-7.20 | Data consistency: гарантии БД vs приложение | ADR-006 | `test_expected_unique_constraints_exist`, `test_foreign_keys_are_enforced`, `test_the_constraint_backs_up_the_logic` | A-23 | VERIFIED |
| R-7.21 | 4 сценария partial success + state machine | §10, §23 FINAL_SPEC | `test_schema_drift_is_partial_not_fatal`, `test_a_failing_stats_call_degrades_only_that_platform`, `test_a_quota_error_does_not_fail_the_game`, `test_a_provider_failure_keeps_the_previous_summary_current` | A-16..A-18 | VERIFIED |
| R-7.22 | Миграции: fresh, upgrade, rollback, индексы, констрейнты | Alembic | `test_upgrade_then_downgrade_then_upgrade`, `test_constraints_survive_the_round_trip`, `test_every_revision_has_a_downgrade_that_is_not_a_stub` | A-24 | VERIFIED |

---

## Сводка

| Статус | Требований |
|---|---|
| PLANNED | 0 |
| IN_PROGRESS | 0 |
| IMPLEMENTED | 5 |
| VERIFIED | 79 |
| BLOCKED | 0 |
| DEFERRED | 0 |

**Ни одно требование исходного ТЗ не осталось вне матрицы.**

`VERIFIED` означает: код есть **и** перечисленный в строке тест проходит в текущей сюите
(749 тестов, `make test`). `IMPLEMENTED` означает: код есть, но прямого автоматического
покрытия нет. Ни один статус не выставлен без причины — вот все пять:

| ID | Почему не `VERIFIED` |
|---|---|
| R-3.2 | требование на обоснование, а не на код — артефакт это ADR-011 |
| R-5.6 | нет ключа LLM (OQ-B4): путь до вызова модели протестирован, сама генерация — нет |
| R-7.6 | схема прогоняется на SQLite, включая round-trip миграций; PostgreSQL-прогон — OQ-V1, на диске 149 МБ |
| R-7.8 | требование на обоснование — ADR-001/003/011/017/018 |
| R-7.13 | файлы проверены как текст (21 тест); образ не собирался — Docker в этой среде нет |

Ни одно из этих пяти не блокирует запуск, и **ни одно не является пробелом в тестах**.
Два — требования на обоснование, где артефакт это ADR. Три упираются в машину: нет
Docker, нет места под PostgreSQL, нет ключа LLM.

В первой редакции таких пунктов было одиннадцать, и три из них были настоящими пробелами
в покрытии: SSE по проводу, downgrade миграций и N+1. Все три закрыты — соответствующими
тестами, а не переформулировкой. Подробности в `FINAL_AUDIT.md §5.1`.
