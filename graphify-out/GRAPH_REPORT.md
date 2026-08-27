# Graph Report - bc-assessment-service  (2026-08-27)

## Corpus Check
- 2 files · ~28,928 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 430 nodes · 932 edges · 16 communities
- Extraction: 91% EXTRACTED · 8% INFERRED · 0% AMBIGUOUS · INFERRED: 79 edges (avg confidence: 0.91)
- Token cost: 68,076 input · 0 output

## Community Hubs (Navigation)
- Lookup Orchestration and Alerting
- Rate Limiting and Concurrency Guards
- Browser Lifecycle Management
- Scraping and HTML Parsing
- Failure Taxonomy and Negative Cache
- Setup, Deployment and Graph Workflow
- Outage Postmortem and Verification
- Assessment Cache Persistence
- API Surface and Schemas
- API Contract Tests
- Load Test Harness
- Browser Health and Test Fixtures
- Settings and Telegram Config

## God Nodes (most connected - your core abstractions)
1. `FailureType` - 48 edges
2. `LookupOutcome` - 23 edges
3. `get_connection()` - 22 edges
4. `lookup_by_address()` - 21 edges
5. `cache_get()` - 21 edges
6. `BrowserManager` - 19 edges
7. `scrape_address()` - 18 edges
8. `AssessmentResult` - 17 edges
9. `record_failure()` - 16 edges
10. `make_manager()` - 15 edges

## Surprising Connections (you probably didn't know these)
- `Telegram alert setup (BotFather token + chat ids in .env)` --references--> `send_telegram_message_background()`  [INFERRED]
  CONFIGURATION_GUIDE.md → app/services/telegram_notify.py
- `Autouse Test Fixtures in conftest.py` --references--> `reset_for_tests()`  [INFERRED]
  CLAUDE.md → app/db/connection.py
- `Blocking SQLite calls dispatched through asyncio.to_thread` --rationale_for--> `lookup_by_address()`  [EXTRACTED]
  how-it-works.html → app/services/lookup.py
- `Invariant: Only Valid Results Reach assessment_cache` --conceptually_related_to--> `is_valid_assessment()`  [INFERRED]
  CLAUDE.md → app/services/stats.py
- `Defect 2: Stale readiness regex cached the previous property's values` --references--> `cache_set()`  [INFERRED]
  README.md → app/db/cache.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Five Invariants That Together Prevent the Production Outage** — claude_production_outage, claude_invariant_fresh_context_per_lookup, claude_invariant_rate_limiter_before_scrape_lock, claude_invariant_only_valid_results_cached, claude_invariant_only_not_found_negatively_cached, claude_invariant_address_response_contract [EXTRACTED 1.00]
- **Mandatory graphify Workflow Loop (query, change, update)** — claude_graphify_knowledge_graph, claude_graph_first_workflow, claude_mandatory_graph_update, claude_graphify_outputs, claude_instruction_not_enforcement, claude_failuretype_bridge_node [EXTRACTED 1.00]
- **Failure Classification Machinery** — claude_failure_taxonomy, claude_named_scraper_steps, claude_not_found_vs_timeout_classification, claude_invariant_only_not_found_negatively_cached, claude_invariant_address_response_contract [INFERRED 0.85]
- **Modules participating in one /address lookup** — app_main_get_address, app_services_lookup_lookup_by_address, app_db_cache_cache_get, app_db_failures_get_negative_cache, app_services_rate_limit_bcratelimitgroup, app_services_lookup_run_scrape, app_services_scraper_scrape_address, app_services_lookup_record_outcome [EXTRACTED 1.00]
- **The four compounding defects behind the production outage** — readme_root_cause_of_hang, readme_defect_1_stale_page_reuse, readme_defect_2_silently_wrong_data, readme_defect_3_browser_never_recycled, readme_defect_4_rate_limit_held_scrape_lock, readme_undifferentiated_failure_none [EXTRACTED 1.00]
- **The five invariants whose violation reintroduces the outage** — claude_invariant_fresh_context_per_lookup, claude_invariant_rate_limiter_before_scrape_lock, claude_invariant_only_valid_results_cached, claude_invariant_only_not_found_negatively_cached, claude_invariant_address_response_contract [EXTRACTED 1.00]

## Communities (16 total, 0 thin omitted)

### Community 0 - "Lookup Orchestration and Alerting"
Cohesion: 0.10
Nodes (35): LookupOutcome, Result of one lookup attempt, carrying enough context to alert and retry., Playwright browser lifecycle: one browser process, a fresh context per lookup.…, lookup_by_address(), _mock_lookup(), _normalize_address(), Lookup orchestration: cache -> negative cache -> scrape -> classify -> record.…, Return assessment data for an address, or a classified failure. (+27 more)

### Community 1 - "Rate Limiting and Concurrency Guards"
Cohesion: 0.05
Nodes (26): BCMinIntervalLimiter, BCRateLimiter, BCRateLimitGroup, _RateLimiter, Rate limiters for bcassessment.ca requests (window caps + minimum spacing)., Apply multiple rate-limit tiers (all must pass before proceeding)., Allow at most `max_calls` BC site operations per `window_seconds`., Enforce a minimum delay between consecutive BC site operations. (+18 more)

### Community 2 - "Browser Lifecycle Management"
Cohesion: 0.06
Nodes (20): BrowserManager, _now_iso(), Page, Mark the browser for relaunch before the next lookup., Launch or relaunch the browser as needed. Returns True if restarted., Yield a page on a brand-new context, plus whether we just restarted. The…, Owns the Playwright browser and hands out clean, disposable contexts., _route_handler() (+12 more)

### Community 3 - "Scraping and HTML Parsing"
Cohesion: 0.09
Nodes (36): _classify_missing_autocomplete(), _clean(), detect_block(), _find_total_value(), _find_value_after_label(), parse_html(), _prune_snapshots(), Page (+28 more)

### Community 4 - "Failure Taxonomy and Negative Cache"
Cohesion: 0.12
Nodes (34): Any, failure_counts(), get_negative_cache(), list_failures(), mark_resolved(), _next_retry_at(), _now(), Retry queue for failed / incomplete lookups. Two jobs: 1. Keep a durable record… (+26 more)

### Community 5 - "Setup, Deployment and Graph Workflow"
Cohesion: 0.08
Nodes (33): close_connection(), db_path(), init_db(), Path, Shared SQLite connection and schema. The previous code opened and closed a new…, Drop the cached connection so a test can point at a new database., Create the database file and apply the schema. Idempotent., reset_for_tests() (+25 more)

### Community 6 - "Outage Postmortem and Verification"
Cohesion: 0.09
Nodes (34): Rate-limit, then scrape under a hard timeout and a single-flight lock., _run_scrape(), Invariant: Never Reuse a Page or BrowserContext Across Lookups, Invariant: Acquire bc_rate_limiter Before _scrape_lock, Mock Lookup Provider, Production Hang and False Property-Not-Found Outage, Autouse Test Fixtures in conftest.py, Troubleshooting: every lookup returns Property not found and /health is 503 (+26 more)

### Community 7 - "Assessment Cache Persistence"
Cohesion: 0.15
Nodes (28): cache_count(), cache_delete(), cache_get(), cache_set(), _is_expired(), SQLite-backed persistent cache for successful BC Assessment lookups. Only valid…, Remove a cache entry (e.g. after TTL expiry)., Return cached assessment for a normalized address, or None on miss. (+20 more)

### Community 8 - "API Surface and Schemas"
Cohesion: 0.12
Nodes (27): get_address(), health(), lifespan(), list_failures(), Inspect the retry queue: what failed, why, when, and what is due., Re-attempt queued failures whose backoff has elapsed., Report real scraper health, not just process liveness. Returns 503 when the…, retry_failures() (+19 more)

### Community 9 - "API Contract Tests"
Cohesion: 0.07
Nodes (9): End-to-end API tests through the mock provider (no browser, no network)., End-to-end guard: unknown addresses are normal traffic, not an outage. These…, The counterpart: real faults must still surface as 503., Regression guard for the stale-page defect., The core fix: a blocked scraper must not look like a missing property., Second call is served by the negative cache, not another scrape., TestFailures, TestOperationalEndpoints (+1 more)

### Community 10 - "Load Test Harness"
Cohesion: 0.20
Nodes (20): AsyncClient, Call, call_address(), main(), mode_cache(), mode_concurrent(), mode_malformed(), mode_soak() (+12 more)

### Community 11 - "Browser Health and Test Fixtures"
Cohesion: 0.17
Nodes (12): BrowserHealth, reset_alert_state_for_tests(), client(), fresh_browser_health(), mock_provider(), fixture, quiet_alerts(), Shared fixtures. Every test runs against a throwaway SQLite database and the… (+4 more)

### Community 12 - "Settings and Telegram Config"
Cohesion: 0.22
Nodes (9): Settings, BaseSettings, Module-Level settings Singleton, Environment Variable Reference (Configuration Guide), Telegram alert setup (BotFather token + chat ids in .env), Environment Variable Reference (README), Security fix: Telegram token removed from config defaults, .env gitignored, pydantic (>=2.0) (+1 more)

## Ambiguous Edges - Review These
- `Troubleshooting PRD (acceptance criteria source)` → `Deferred optimization: httpx fast path instead of Playwright`  [AMBIGUOUS]
  README.md · relation: conceptually_related_to
- `Troubleshooting: every lookup returns Property not found and /health is 503` → `Known limitation: a capped-out hourly rate-limit wait is not bounded`  [AMBIGUOUS]
  CONFIGURATION_GUIDE.md · relation: conceptually_related_to

## Knowledge Gaps
- **6 isolated node(s):** `Ubuntu 24.04 libasound2 -> libasound2t64 dependency rename`, `pytest-asyncio (>=0.23.0)`, `httpx (>=0.27.0)`, `uvicorn[standard] (>=0.30.0)`, `Deployment Verification Checklist` (+1 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Troubleshooting PRD (acceptance criteria source)` and `Deferred optimization: httpx fast path instead of Playwright`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `Troubleshooting: every lookup returns Property not found and /health is 503` and `Known limitation: a capped-out hourly rate-limit wait is not bounded`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `FailureType` connect `Failure Taxonomy and Negative Cache` to `Lookup Orchestration and Alerting`, `Rate Limiting and Concurrency Guards`, `Browser Lifecycle Management`, `Scraping and HTML Parsing`, `Setup, Deployment and Graph Workflow`, `Outage Postmortem and Verification`, `API Surface and Schemas`?**
  _High betweenness centrality (0.273) - this node is a cross-community bridge._
- **Why does `Mock Lookup Provider` connect `Outage Postmortem and Verification` to `Lookup Orchestration and Alerting`, `Load Test Harness`?**
  _High betweenness centrality (0.085) - this node is a cross-community bridge._
- **Why does `_mock_lookup()` connect `Lookup Orchestration and Alerting` to `Failure Taxonomy and Negative Cache`, `Outage Postmortem and Verification`, `Assessment Cache Persistence`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Are the 27 inferred relationships involving `FailureType` (e.g. with `get_negative_cache()` and `record_failure()`) actually correct?**
  _`FailureType` has 27 INFERRED edges - model-reasoned connections that need verification._
- **Are the 10 inferred relationships involving `LookupOutcome` (e.g. with `lookup_by_address()` and `_mock_lookup()`) actually correct?**
  _`LookupOutcome` has 10 INFERRED edges - model-reasoned connections that need verification._