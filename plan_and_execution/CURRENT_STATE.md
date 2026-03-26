# Current State & Next Steps

> Quick-read file. Check this FIRST before any implementation work.
> Last updated: 2026-03-26

---

## Current State: PHASE 1-6 + SIPDO INTEGRATION COMPLETE

Phases 1-5 (core pipeline) implemented and audited on 2026-03-26.
Phase 6 (Streamlit frontend rewrite) completed on 2026-03-26.
SIPDO integration (Option C — Hybrid user-choice) completed on 2026-03-26.

### What Exists (Working)
- **FastAPI backend** (`backend/main.py`) — thin trigger layer over LangGraph, CORS, lifespan hooks
- **5 API route modules** — upload, pipeline (start/resume/sipdo-choice/state), download, status (sessions/ms-data/sipdo-prompts)
- **SQLite + SQLAlchemy** (`db/`) — 5 ORM models: ReconciliationSession, ExtractedTrade, ReconciliationResult, TemplateCache, OptimizedPromptCache
- **MS Data Service** (`services/ms_data_service.py`) — loads MS receivables Excel, builds trade_id + composite indexes
- **10-node LangGraph pipeline** (`graph/workflow.py`) — verify → classify → [sipdo_choice_gate → sipdo_optimize] → extract → [hitl_gate] → reconcile → generate → persist → [sipdo_background]
- **6 agent modules** — verify, classify, extract, reconcile, template_agent, persist_agent
- **5-tier extraction** — YAML template → cached template → fuzzy column match → SIPDO optimized prompt → LLM fallback
- **Fuzzy column matcher** (`services/column_matcher.py`) — rapidfuzz-based synonym matching (16 canonical fields)
- **Reconciliation engine** — broker vs MS data (3-level matching: trade_id → composite → fuzzy), deterministic, no LLM
- **Template auto-learning** — column mappings cached in TemplateCache DB on HITL approval
- **SIPDO prompt optimization** (`services/prompt_optimizer.py`) — 4-iteration refinement loop for unknown broker prompts
- **SIPDO prompt caching** (`services/prompt_cache.py`) — optimized prompts stored in OptimizedPromptCache DB, reused on future uploads
- **5-sheet Excel output** — Summary, Broker Trades, Matched, Mismatches, Exceptions
- **Streamlit UI** (`ui/app.py`) — 6-page frontend: Upload, Review (HITL + SIPDO choice), Results, History, MS Data, Prompt Cache
- **4 YAML broker templates** — evolution, tfs, icap, marex
- **PDF/Excel parsers** — pdfplumber + pandas with header detection

### SIPDO Integration (Option C — Hybrid)
- **3 new graph nodes**: `sipdo_choice_gate_node`, `sipdo_optimize_node`, `sipdo_background_node`
- **4 new routing functions**: `route_after_classify`, `route_after_sipdo_choice`, `route_after_sipdo_optimize`, `route_after_persist`
- **2 interrupt points**: `sipdo_choice_gate` (unknown broker choice) + `hitl_gate` (extraction review)
- **New API endpoint**: `POST /api/pipeline/sipdo-choice` (inject user strategy: quick or optimize)
- **New API endpoint**: `GET /api/status/sipdo/prompts` (list cached prompts)
- **New DB model**: `OptimizedPromptCache` (broker_name, prompt_text, accuracy_score, trace)
- **New UI pages**: SIPDO choice screen in Review page, Prompt Cache page in sidebar
- **5 new GraphState fields**: `is_unknown_broker`, `sipdo_choice_pending`, `sipdo_strategy`, `sipdo_optimized_prompt`, `sipdo_optimization_trace`
- **2 new PipelineStatus values**: `SIPDO_CHOICE`, `OPTIMIZING`
- **Extraction tier 4**: SIPDO optimized prompt (inline from optimize path, or cached from prior run)

### Bugs Fixed in Audit (2026-03-26)
1. **SECURITY: API key was hardcoded** in dev.yaml → moved to env var ANTHROPIC_API_KEY
2. **HITL flow was completely broken** — extract_node now sets status; UI also checks hitl_pending flag
3. **classify_agent NameError crash** — safe fallback when no keywords detected
4. **Template auto-learning was dead** — extraction returns mapping tuple
5. **classify failure didn't stop pipeline** — added route_after_classify conditional edge
6. **UI loaded config from wrong path** — fixed to parent dir
7. **start.sh wrong venv path** — checks project dir first
8. **reconcile_agent: ms_total included ALL brokers** — filter by broker
9. **persist_agent: extracted_trade_id FK never set** — build lookup, link FK
10. **Upload didn't create DB session** — creates row on upload
11. **MS data: duplicate column mapping** — keep first occurrence only
12. **MS data: composite index collision** — indexes store lists

### What Remains To Do
1. **Phase 7: E2E Testing** — end-to-end with all 8 sample brokers (both known + unknown paths)
2. **SIPDO live test** — test both "Quick Extract" and "Optimize First" paths with an unknown broker
3. **More YAML templates** — only 4 exist (fuzzy + SIPDO + LLM handles unknowns)
4. **Batch upload** — single broker pair only; batch as stretch goal
5. **SIPDO cancel/timeout** — no cancellation token during optimization yet

### Known Limitations
- MemorySaver checkpointer = in-memory only (sessions lost on restart)
- MS data loaded once at startup (no hot-reload)
- No authentication on API endpoints (POC only)
- PDF table extraction depends on pdfplumber accuracy
- SIPDO optimization takes 2-5 min per new broker (one-time cost, cached forever)
- No cancel button during SIPDO optimization

---

## Architecture (Verified Working)

```
Streamlit UI ──HTTP──▶ FastAPI ──triggers──▶ LangGraph Agent Graph ──calls──▶ Agent Functions
   (8501)               (8001)                 (10-node StateGraph)              (6 agents)
                                                 2 interrupt points           + SIPDO service
                                                                              + prompt cache
```

### API Endpoints
| Endpoint | Method | Purpose |
|---|---|---|
| /api/upload | POST | Save files + create DB session |
| /api/pipeline/start | POST | Run Phase 1 (verify → classify → [sipdo] → extract → HITL pause) |
| /api/pipeline/resume | POST | Run Phase 2 (reconcile → generate → persist → [sipdo_background]) |
| /api/pipeline/sipdo-choice | POST | Inject SIPDO strategy (quick \| optimize) for unknown brokers |
| /api/pipeline/state/{id} | GET | Poll pipeline state (includes SIPDO fields) |
| /api/download/{filename} | GET | Download output Excel |
| /api/status/sessions | GET | List past sessions |
| /api/status/sessions/{id} | GET | Session detail |
| /api/status/sessions/{id}/results | GET | Trade-level recon results from DB |
| /api/status/ms-data | GET | MS data stats |
| /api/status/ms-data/preview | GET | MS data preview (paginated, 10-500 rows) |
| /api/status/sipdo/prompts | GET | List all cached SIPDO-optimized prompts |
| /health | GET | Health check |
