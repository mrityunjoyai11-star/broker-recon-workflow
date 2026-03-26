# Changelog — Brokerage Reconciliation POC

> Record of all implementation changes. Append new entries at the top.
> Format: `## [Date] Phase X.Y — Short Description`

---

## [2026-03-26] SIPDO Integration — Option C Hybrid (User-Choice)

### New Files
- **services/prompt_optimizer.py** (~300 lines) — Full SIPDO optimization pipeline: Document Analysis → Field Decomposition → Seed Prompt → 4-iteration optimization loop (progressive difficulty) → Consistency Audit. Uses Claude for 7 specialized sub-agents.
- **services/prompt_cache.py** (~100 lines) — CRUD for OptimizedPromptCache DB table: `get_cached_prompt()`, `save_optimized_prompt()` (upsert), `list_all_prompts()`.

### Graph Changes (graph/)
- **state.py:** Added 5 new fields — `is_unknown_broker`, `sipdo_choice_pending`, `sipdo_strategy`, `sipdo_optimized_prompt`, `sipdo_optimization_trace`.
- **nodes.py:** Added 3 new node functions (`sipdo_choice_gate_node`, `sipdo_optimize_node`, `sipdo_background_node`) and 4 new routing functions (`route_after_classify`, `route_after_sipdo_choice`, `route_after_sipdo_optimize`, `route_after_persist`). Updated `classify_node` to detect unknown brokers + check SIPDO prompt cache. Updated `extract_node` to pass `sipdo_prompt` parameter.
- **workflow.py:** Complete rewrite of `build_workflow()` — now compiles a 10-node StateGraph (was 7) with conditional edges for SIPDO routing and `interrupt_before=["sipdo_choice_gate", "hitl_gate"]`.

### Schema Changes
- **schemas/canonical_trade.py:** Added `SIPDO_CHOICE` and `OPTIMIZING` to `PipelineStatus` enum (now 12 values).
- **db/models.py:** Added `OptimizedPromptCache` model — columns: `broker_name` (unique, indexed), `prompt_text`, `accuracy_score`, `optimization_trace` (JSON), `source_session_id`, timestamps.

### Agent Changes
- **agents/extract_agent.py:** Added `sipdo_prompt` parameter. Inserted Tier 4 (SIPDO optimized prompt) between fuzzy match and generic LLM. Added `_sipdo_extract()` helper. Tiers 4a/4b renamed to 5a/5b. New extraction methods: `sipdo_optimized` (inline), `sipdo_cached` (from DB).

### API Changes
- **backend/api/routes/pipeline.py:** New `POST /sipdo-choice` endpoint (`SipdoChoiceRequest` model). Updated `_serialise_state()` to include `is_unknown_broker`, `sipdo_choice_pending`, `sipdo_strategy`, `sipdo_optimization_trace`.
- **backend/api/routes/status.py:** New `GET /sipdo/prompts` endpoint — lists all cached SIPDO-optimized prompts.

### UI Changes (ui/app.py)
- **SIPDO Choice Screen:** When unknown broker detected, renders two-column layout with "⚡ Quick Extract" and "🎯 Optimize First" buttons. POSTs to `/api/pipeline/sipdo-choice`.
- **Optimization Progress:** Shows progress bar and SIPDO iteration logs during optimization, polls with 10s max_wait.
- **Prompt Cache Page:** New sidebar page listing all cached SIPDO prompts with broker, accuracy (%), source session, timestamps.
- Navigation updated to 6 pages (added "Prompt Cache").
- `_post()` now accepts `timeout` parameter (default 120, SIPDO passes 600).

### Verification
- All 11 modified/created files pass `py_compile` syntax checks
- All module imports resolve correctly (no circular deps)
- All 9 routing logic tests pass (known→extract, unknown→sipdo_choice_gate, quick→extract, optimize→sipdo_optimize, persist routing)
- LangGraph workflow compiles with 10 nodes and 2 interrupt points
- API models and serialization include all SIPDO fields

---

## [2026-03-26] Phase 6 — Streamlit Frontend Rewrite

### Backend Changes
- **pipeline.py:** Added `_serialise_match()` helper; `_serialise_state()` now returns reconciliation match detail arrays (`recon_matched`, `recon_mismatched`, `recon_new`, `recon_missing`) and `last_column_mapping`. Logs bumped from 10 to 20.
- **status.py:** New endpoint `GET /sessions/{id}/results` returns trade-level extracted trades + reconciliation results from DB for history drill-down. New endpoint `GET /ms-data/preview?limit=50` returns paginated MS data preview.

### UI Changes (ui/app.py — full rewrite)
- **Results page:** Added 5 tabs — Matched, Mismatched, New (Broker Only), Missing (MS Only), All Extracted Trades. Each tab shows a broker-vs-MS comparison table with trade_id, qty, price, brokerage side-by-side. Mismatch tab includes reason + differences columns. Direct download button (no 2-click).
- **Review page:** Added column mapping display (expandable) so reviewers can see how source columns were mapped. Added extraction warnings in a collapsible expander. Improved summary bar with 4 metric cards (Broker, Method, Confidence, Trade Count). Auto-poll for pipeline state when still running.
- **History page:** Added session drill-down — select a session and load its extracted trades + reconciliation results from DB. Inline MS trade snapshot data expansion. Download button for historical reports.
- **MS Data page:** Added data preview table with adjustable row limit slider (10–200). Shows paginated MS receivables data alongside stats.
- **Navigation:** Sidebar now shows current step in addition to status. Custom CSS for metric styling and status badges.
- **Progress indicators:** Upload and resume now use `st.progress()` bars.
- **Session state:** Consolidated defaults with `_DEFAULTS` dict pattern.

### Verified
- All 3 modified files pass py_compile syntax check
- All module imports resolve correctly
- `_serialise_match()` integration test passes (MATCH status, all fields present)

---

## [2026-03-26] Full Code Audit — 12 Critical Bugs Fixed

### Audit Performed
Complete codebase review against IMPLEMENTATION_PLAN.md. All Python files, YAML
configs, templates, parsers, services, agents, graph layer, backend routes, UI,
and database models verified for correctness.

### Critical Bugs Fixed

#### 1. SECURITY: API Key Hardcoded (dev.yaml + llm_service.py)
- **Impact:** Real Anthropic API key committed to source code
- **Fix:** Replaced with `${ANTHROPIC_API_KEY}` placeholder in dev.yaml; `llm_service.py` now reads from `os.environ["ANTHROPIC_API_KEY"]` with error if unset

#### 2. HITL Flow Completely Broken (graph/nodes.py + ui/app.py)
- **Impact:** Users could never approve/reject extractions — the HITL review form never appeared
- **Root cause:** `interrupt_before=["hitl_gate"]` pauses BEFORE `hitl_gate_node` runs. That node sets `status=hitl_review`, but it never executes. UI checks `status == "hitl_review"` which is never true at the pause point.
- **Fix:** `extract_node` now sets `status=hitl_review` before interrupt. UI also checks `hitl_pending` flag as secondary condition.

#### 3. classify_agent NameError Crash (agents/classify_agent.py)
- **Impact:** Pipeline crashes with `NameError: name 'result' is not defined` when broker keywords aren't found in documents
- **Root cause:** Tier 3 (TemplateCache lookup) referenced `result.broker_name_detected` but `result` was only assigned inside `if detected_keywords:` block which could be skipped
- **Fix:** Initialize `result = None`; use `broker_for_cache` fallback combining result + broker_hint

#### 4. Template Auto-Learning Dead (agents/extract_agent.py + graph/nodes.py)
- **Impact:** HITL-approved column mappings were never cached — every unknown broker always hit LLM
- **Root cause:** `run_extraction()` returned only `ExtractionResult`, never the column mapping used. `extract_node` never set `last_column_mapping` in state. `persist_node` then received `None`.
- **Fix:** `run_extraction()` now returns `(ExtractionResult, column_mapping_used)` tuple. `extract_node` stores the mapping in state. `_extract_fuzzy_or_llm` and `_llm_map_and_extract` also return mappings.

#### 5. classify Failure Doesn't Stop Pipeline (graph/workflow.py)
- **Impact:** If classify_node fails (exception or low confidence), extract_node still runs and likely fails with confusing errors
- **Root cause:** Unconditional edge `builder.add_edge("classify", "extract")`
- **Fix:** Added `route_after_classify` conditional edge that routes to END on failure

#### 6. UI Loads Config From Wrong Path (ui/app.py)
- **Impact:** UI always fell back to hardcoded `http://localhost:8001` because `ui/dev.yaml` doesn't exist
- **Fix:** Changed from `Path(__file__).parent / "dev.yaml"` to `Path(__file__).parent.parent / "dev.yaml"`

#### 7. start.sh Wrong Venv Path (start.sh)
- **Impact:** `start.sh` couldn't find venv (looked in parent dir but venv is in project dir)
- **Fix:** Check `$SCRIPT_DIR/venv` first, then `$ROOT_DIR/venv` as fallback

#### 8. reconcile_agent: ms_total Includes All Brokers (agents/reconcile_agent.py)
- **Impact:** Summary metrics (ms_total, difference) were meaningless — summed across all 49 MS trades regardless of which broker was being reconciled
- **Fix:** Filter MS trades by broker_code match before computing totals; `ms_trade_count` in summary now reflects filtered count

#### 9. persist_agent: extracted_trade_id FK Never Set (agents/persist_agent.py)
- **Impact:** ReconciliationResult rows in DB couldn't be joined to their ExtractedTrade rows — FK was always NULL
- **Fix:** Build `trade_id_to_orm` lookup keyed by pydantic model id; link FK after flush

#### 10. Upload Doesn't Create DB Session (backend/api/routes/upload.py)
- **Impact:** In-progress pipelines invisible in history/status endpoints until persist_node ran
- **Fix:** Create ReconciliationSession row (status="uploaded") on upload

#### 11. MS Data: Duplicate Column Mapping (services/ms_data_service.py)
- **Impact:** Multiple raw columns mapped to same canonical name (e.g., 4 columns -> "instrument"), causing pandas column shadowing and data loss
- **Fix:** `_normalize_ms_columns` now tracks `seen_canonical` set; keeps first match only, drops duplicate-mapped columns

#### 12. MS Data: Composite Index Collision (services/ms_data_service.py)
- **Impact:** Only last row stored per composite key — trades sharing same (date, instrument, account) were lost from index
- **Fix:** Changed `_trade_id_index` and `_composite_index` from `dict[str, dict]` to `dict[str, list[dict]]`; added `_ms_trades_cache` for `get_all_ms_trades()` performance

### Other Improvements
- Pipeline serialization now includes extracted trades data for HITL review UI
- UI Review page now shows trades table and extraction warnings
- MS data path in dev.yaml fixed from `../documentation/` to `documentation/` (file is inside project)
- `get_all_ms_trades()` now returns cached list instead of rebuilding from DataFrame each call

### Verification
- All 26 Python files pass syntax check
- All module imports verified (no circular deps, no missing modules)
- LangGraph workflow compiles successfully (7 nodes + conditional routing)
- SQLite DB initializes and creates all 4 tables
- MS data loads correctly (49 rows, 23 trade_id entries, 16 composite entries)

## [2026-03-24] Plan Revised — LangGraph Agentic Architecture Restored

### What Changed
- **LangGraph is now the orchestration backbone** — not a Phase 4 afterthought
- FastAPI routes are a **thin trigger layer** — they call `graph.stream()` / `graph.update_state()`, NOT agent functions directly
- Removed separate `/api/extract` and `/api/reconcile` endpoints — replaced with `/api/pipeline/start` and `/api/pipeline/resume` (graph controls sequencing)
- Added **7-node graph topology** to Phase 1 (was missing entirely)
- Added 2 new graph nodes: `reconcile_ms_node` (broker vs MS), `persist_node` (DB write)
- Added new agent: `agents/persist_agent.py` (called by persist_node)
- Updated GraphState with: `ms_data_loaded`, `db_session_id`, `results_persisted`
- Added full routing logic (route_after_verify, route_after_extract, route_after_hitl, route_after_reconcile)
- Phase 1 now includes LangGraph graph setup (not deferred to Phase 4)
- CURRENT_STATE.md updated with graph-first architecture and new tasks

### Why
The plan was missing the agentic orchestration layer. LangGraph StateGraph with HITL interrupt_before is core to the system design — not optional.

---

## [2026-03-24] Planning Complete — Architecture Finalized

### Decisions Made
- Split into Streamlit frontend + FastAPI backend (separate services)
- Reconciliation = broker data vs MS receivables data (not PDF vs Excel)
- SQLite for POC database (audit trail, result persistence, template caching)
- 4-tier extraction strategy: YAML template → cached template → fuzzy column match → LLM → cache result
- MS data pre-loaded at server startup from `ms_receivables_client_base_data.xlsx`
- DB stores reconciliation results only — never mutates MS source data

### Documents Created
- `documentation/IMPLEMENTATION_PLAN.md` — full plan with all 7 phases
- `documentation/CURRENT_STATE.md` — current state + next phase tasks
- `documentation/CHANGELOG.md` — this file

### Existing Code Assessed
- 5 agents, 3 parsers, 3 services, LangGraph workflow — all functional
- Reconciliation agent needs rewrite (currently PDF vs Excel, needs broker vs MS)
- 4 YAML templates exist (evolution, tfs, icap, marex)
- 8 sample broker datasets available for testing
- MS data Excel provided but not yet integrated
