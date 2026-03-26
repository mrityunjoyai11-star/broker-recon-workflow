# Brokerage Reconciliation POC — Implementation Plan

> Master reference document. Read this before starting any phase.

---

## System Overview

Restructure from monolithic Streamlit app (PDF-vs-Excel comparison) into **Streamlit frontend + FastAPI backend** with **LangGraph-based agentic orchestration** at its core:
1. **Agentic pipeline (LangGraph)** — 7-node agent graph handles the entire workflow: verify → classify → extract → [HITL] → reconcile_vs_ms → generate → persist
2. **FastAPI** — thin API layer that triggers and manages LangGraph workflows (does NOT call agents directly)
3. Extracts trades from 350+ broker formats via 4-tier extraction (rules → fuzzy match → LLM → cache)
4. Reconciles extracted broker data against pre-loaded MS receivables data
5. Persists results in SQLite for audit trail (IB-standard pattern)
6. Generates Excel reports for ops teams


### Core Architecture Principle
```
Streamlit UI ──HTTP──▶ FastAPI ──triggers──▶ LangGraph Agent Graph ──calls──▶ Agent Functions
                          │                        │                            │
                          │                   (orchestration)              (business logic)
                          │                   State management            PDF/Excel parsing
                          │                   Routing decisions           LLM calls
                          │                   HITL interrupts             Reconciliation
                          │                        │
                          ◀──── results ───────────┘
```
FastAPI is a **thin trigger layer**. All intelligence, sequencing, error handling, and state management lives in **LangGraph agents**.

---

## Phase 1: Backend Foundation — FastAPI + DB + LangGraph Core

### Step 1.1 — FastAPI skeleton
- Create `backend/` directory structure
- Files: `backend/main.py`, `backend/api/__init__.py`, `backend/api/routes/{upload,pipeline,download,status}.py`
- CORS middleware for Streamlit frontend
- Reuse existing `config.py` pattern (load from `dev.yaml`)
- **Key:** FastAPI routes are thin — they create sessions, trigger LangGraph, and return results. No business logic in routes.

### Step 1.2 — SQLite database layer *(parallel with 1.1)*
- `backend/db/__init__.py`, `backend/db/models.py`, `backend/db/database.py`
- SQLAlchemy models:
  - `ReconciliationSession` — session_id, broker_name, upload_time, status, ms_data_version
  - `ExtractedTrade` — FK to session, all canonical fields, source_file, source_type
  - `ReconciliationResult` — FK to session + extracted_trade, status (MATCH/MISMATCH/NEW/MISSING), broker vs MS values, mismatch_reason, confidence_score
  - `TemplateCache` — broker_name, column_mapping JSON, extraction_rules, usage_count, last_used, confidence

### Step 1.3 — MS Data Service *(depends on 1.2)*
- `backend/services/ms_data_service.py`
- Load `ms_receivables_client_base_data.xlsx` at startup into indexed pandas DataFrame
- Build indexes on: trade_id, composite key (trade_date+client+qty+price)
- Expose: `find_by_trade_id()`, `find_by_composite_key()`, `get_summary_stats()`

### Step 1.4 — LangGraph Agent Graph (Updated Topology)
- Update `graph/workflow.py` — new 7-node graph with updated reconciliation target
- Update `graph/state.py` — add MS data fields, DB session tracking
- Update `graph/nodes.py` — new nodes for MS reconciliation and result persistence
- **This is the orchestration backbone — all processing flows through the graph**

#### Updated LangGraph Topology
```
┌─────────────────────────────────────────────────────────────────────┐
│                    LangGraph StateGraph                            │
│                                                                     │
│  verify_node ──▸ classify_node ──▸ extract_node                    │
│       │                                  │                          │
│    (route)                          (route)                         │
│    ├─ fail                          ├─ fail                         │
│    └─ next                          └─ hitl_gate                    │
│                                          │                          │
│                                    ┌─ INTERRUPT ─┐                  │
│                                    │  (HITL pause)│                  │
│                                    └──────┬───────┘                  │
│                                           │                          │
│                               reconcile_ms_node ──▸ generate_node   │
│                                    │                      │          │
│                                 (route)              persist_node    │
│                                 ├─ fail                   │          │
│                                 └─ next                  END         │
│                                                                     │
│  failed_node ──▸ END                                                │
└─────────────────────────────────────────────────────────────────────┘
```

#### Agent Nodes (7 nodes + 1 terminal)

| Node | Agent Module | Purpose | LLM? |
|------|-------------|---------|------|
| `verify_node` | `agents/verify_agent.py` | Light PDF-vs-Excel sanity check | Fallback only |
| `classify_node` | `agents/classify_agent.py` | Detect broker, select template/strategy | Fallback only |
| `extract_node` | `agents/extract_agent.py` | 4-tier extraction → canonical trades | Tier 3 only |
| `hitl_gate` | (inline) | HITL interrupt — pause for human review | No |
| `reconcile_ms_node` | `agents/reconcile_agent.py` | **NEW** — match broker trades vs MS data | No |
| `generate_node` | `agents/template_agent.py` | Create output Excel (5-sheet format) | No |
| `persist_node` | `agents/persist_agent.py` | **NEW** — write results to SQLite | No |
| `failed_node` | (inline) | Terminal error state | No |

#### Graph State (Updated `GraphState` TypedDict)
```python
class GraphState(TypedDict, total=False):
    # Inputs
    pdf_path: str
    excel_path: str
    session_id: str
    db_session_id: int  # NEW — DB ReconciliationSession FK

    # Pipeline status
    status: str
    current_step: str
    error: Optional[str]

    # Agent 1: Verification
    verification: Optional[VerificationResult]

    # Agent 2: Classification
    classification: Optional[ClassificationResult]
    broker_name: Optional[str]
    template_type: Optional[str]

    # Agent 3: Extraction
    extraction: Optional[ExtractionResult]
    extraction_approved: Optional[bool]  # HITL flag

    # Agent 4: Reconciliation vs MS  (CHANGED)
    ms_data_loaded: bool  # NEW
    reconciliation: Optional[ReconciliationResult]

    # Agent 5: Output Generation
    output_files: dict[str, bytes]

    # Agent 6: Persistence
    results_persisted: bool  # NEW

    # HITL
    hitl_pending: Optional[str]
    hitl_feedback: Optional[str]

    # Audit
    logs: list[str]
```

#### Conditional Routing Logic
```python
# After verify: continue or fail
def route_after_verify(state) -> str:
    return "failed" if state["status"] == "failed" else "classify"

# After extract: always pause for HITL
def route_after_extract(state) -> str:
    return "failed" if state["status"] == "failed" else "hitl_gate"

# After HITL: approved → reconcile, rejected → fail
def route_after_hitl(state) -> str:
    return "reconcile_ms" if state.get("extraction_approved") else "failed"

# After reconcile: continue or fail
def route_after_reconcile(state) -> str:
    return "failed" if state["status"] == "failed" else "generate"
```

#### How FastAPI Triggers LangGraph
```python
# POST /api/pipeline/start — runs Phase 1 (verify → classify → extract → HITL pause)
async def start_pipeline(session_id, pdf_path, excel_path):
    graph, checkpointer = build_workflow()
    initial_state = create_initial_state(pdf_path, excel_path, session_id)
    config = {"configurable": {"thread_id": session_id}}
    result = None
    for event in graph.stream(initial_state, config, stream_mode="values"):
        result = event  # Graph pauses at hitl_gate interrupt
    return result  # Returns extracted data for HITL review

# POST /api/pipeline/resume — runs Phase 2 (reconcile → generate → persist)
async def resume_pipeline(session_id, approved, feedback):
    graph.update_state(config, {"extraction_approved": approved, ...})
    result = None
    for event in graph.stream(None, config, stream_mode="values"):
        result = event  # Runs to completion
    return result
```

**Key existing code to reuse:**
- `config.py` → `load_config()`, `get_agent_config()`
- `services/storage_service.py` → `save_uploaded_file()`, file I/O patterns
- `graph/workflow.py` → `build_workflow()`, routing pattern (adapt, don't rewrite)
- `graph/nodes.py` → existing node functions (keep verify/classify/extract, update reconcile)
- `graph/state.py` → `PipelineState` pattern (extend)

---

## Phase 2: Multi-Tier Extraction Engine (350+ Broker Support)

### Step 2.1 — Level 1: Rule-based/Regex *(parallel with Phase 1)*
- Enhance `parsers/template_parser.py` with regex-based row detection
- Add to YAML template schema: `row_patterns`, `header_patterns`, `skip_patterns`
- For known brokers: date format regex, quantity/price column patterns

### Step 2.2 — Level 2: Fuzzy column name matching
- New file: `backend/services/column_matcher.py`
- Canonical field synonym dictionary:
  - trade_date → ["date", "trade date", "trd date", "trd dt", "settlement date"]
  - quantity → ["qty", "volume", "lots", "watt", "notional", "amount", "size"]
  - price → ["rate", "px", "unit price", "trade price", "execution price"]
  - brokerage_amount → ["commission", "brokerage", "comm", "fee", "broker fee"]
  - currency → ["ccy", "curr"]
  - buy_sell → ["direction", "side", "b/s", "type"]
  - trade_id → ["ref", "reference", "trade ref", "order id", "deal id", "ticket"]
- Scoring: exact → substring → token overlap → edit distance
- Use mapping without LLM if confidence > 0.7

### Step 2.3 — Level 3: LLM fallback *(no changes needed)*
- Existing `_llm_map_and_extract()` and `_llm_full_extract()` in `agents/extract_agent.py`

### Step 2.4 — Level 4: Template auto-learning *(depends on 1.2)*
- After HITL approval: persist learned mapping to `TemplateCache` DB + YAML in `templates/auto/`
- On next upload: check cache first → skip LLM if found with high confidence
- Track usage_count for reliability scoring

### Step 2.5 — Orchestrate in `agents/extract_agent.py` *(depends on 2.1–2.4)*
- Modified `run_extraction()` flow:
  1. Check YAML template (existing)
  2. Check `TemplateCache` DB
  3. Try fuzzy column matching
  4. Fall back to LLM
  5. Cache result on approval
- Return extraction_method: "template", "cached_template", "fuzzy_match", "llm_assisted"

**Key existing code:**
- `agents/extract_agent.py` → `run_extraction()`, `_llm_map_and_extract()`, `_llm_full_extract()`
- `parsers/template_parser.py` → `map_columns()`, `dataframe_to_trades()`, `clean_value()`
- `parsers/pdf_parser.py` → `PDFParser.extract_tables()`, `extract_full_text()`
- `parsers/excel_parser.py` → `ExcelParser.get_primary_table()`, `read_all_sheets()`

---

## Phase 3: Agent Modules — Reconciliation & Persistence

### Step 3.1 — Rewrite `agents/reconcile_agent.py` (reconcile_ms_node)
This agent is called by the `reconcile_ms_node` in the LangGraph graph.
- **Current:** PDF trades vs Excel trades
- **New:** Extracted broker trades vs MS receivables data (loaded via `ms_data_service`)
- The graph node receives extracted trades from state, loads MS data, calls this agent
- Match strategy (deterministic, no LLM):
  - Level 1: Exact `trade_id`
  - Level 2: Composite `trade_date + client_account + quantity + price`
  - Level 3: Fuzzy `trade_date + client_account` with tolerance on qty/price

### Step 3.2 — Field comparison rules
- Quantity: exact ±0.001
- Price: `abs(broker - ms) <= 0.01`
- Brokerage: recalculate `price × qty × commission_rate`, compare ≤1 currency unit
- Currency/Direction: exact after normalization

### Step 3.3 — Status assignment
- **MATCH** — found, values within tolerance
- **MISMATCH** — found, values differ (reason: MISMATCH_QTY, MISMATCH_PRICE, MISMATCH_BROKERAGE, MULTIPLE_ISSUES)
- **NEW** — in broker only
- **MISSING** — in MS only

### Step 3.4 — Confidence scoring
- trade_id +1, qty +1, price +1, brokerage +1 (max 4)

### Step 3.5 — New `agents/persist_agent.py` (persist_node)
This agent is called by the `persist_node` in the LangGraph graph (runs after generate_node).
- Receives reconciliation results + extraction data from graph state
- Writes to SQLite: `ExtractedTrade` rows, `ReconciliationResult` rows
- Updates `ReconciliationSession` status to COMPLETED
- If template was auto-learned (LLM extraction), writes to `TemplateCache`
- Returns `{"results_persisted": True}` to graph state

**Key existing code to adapt:**
- `agents/reconcile_agent.py` → `_make_match_key()`, `_compare_trades()`, `_fuzzy_find()`
- `schemas/canonical_trade.py` → `ReconciliationResult`, `ReconciliationMatch`
- `graph/nodes.py` → node function pattern

---

## Phase 4: Update Schemas for Agentic Pipeline

### Step 4.1 — Update `schemas/canonical_trade.py`
- `ReconciliationMatch`: broker_trade/ms_trade (not pdf/excel), add mismatch_reason, confidence_score
- Add `MSTradeRecord` model
- Update `ReconciliationResult.summary`: broker_total, ms_total, difference

### Step 4.2 — Update graph modules (already designed in Phase 1.4)
The full LangGraph topology, state schema, node definitions, and routing logic are specified in **Phase 1 Step 1.4** above. Implementation happens in Phase 1, schema updates happen here.
- `graph/state.py` — extend with `ms_data_loaded`, `db_session_id`, `results_persisted`
- `graph/nodes.py` — add `reconcile_ms_node()`, `persist_node()`; update `reconcile_node()` → calls MS-based reconciliation
- `graph/workflow.py` — rebuild graph with 7 nodes + routing

**Key existing code:**
- `graph/workflow.py` → `build_workflow()`, `GraphState` (extend, don't rewrite from scratch)
- `graph/nodes.py` → `verify_node()`, `classify_node()`, `extract_node()` (keep as-is); `reconcile_node()` (rewrite), `generate_node()` (update output format)

---

## Phase 5: FastAPI Routes (Thin Layer over LangGraph)

FastAPI routes do NOT contain business logic. They:
1. Create DB sessions
2. Trigger/resume LangGraph workflows
3. Return graph state as JSON

| Endpoint | Method | What it does |
|---|---|---|
| `/api/upload` | POST | Save files, create `ReconciliationSession` in DB, return session_id |
| `/api/pipeline/start/{session_id}` | POST | **Trigger LangGraph Phase 1** (verify → classify → extract → HITL pause). Returns extracted data for review. |
| `/api/pipeline/resume/{session_id}` | POST | **Resume LangGraph Phase 2** after HITL (reconcile_ms → generate → persist). Returns final results. |
| `/api/pipeline/status/{session_id}` | GET | Return current graph state (which node, status, logs) |
| `/api/results/{session_id}` | GET | Reconciliation results from DB |
| `/api/download/{session_id}/{file_type}` | GET | Download output Excel files |
| `/api/history` | GET | Past sessions from DB |
| `/api/ms-data/stats` | GET | MS data summary |

### Important: No `/api/extract` or `/api/reconcile` endpoints
These are NOT separate endpoints because extraction and reconciliation are **graph nodes**, not standalone operations. The graph handles sequencing, error routing, and state management. FastAPI just triggers `start` and `resume`.

---

## Phase 6: Streamlit Frontend Rewrite

- Replace direct agent imports with HTTP calls to FastAPI
- Pages: Upload → Extraction Review (HITL) → Reconciliation Results → History
- Show broker-vs-MS comparison (not PDF-vs-Excel)
- Metrics: Matched, Mismatched, New, Missing, Broker Total, MS Total, Difference
- Excel output 5 sheets: Summary, Reconciliation_Details, New_Trades, Missing_Trades, Raw_Extracted_Data

---

## Phase 7: Verification

1. Test extraction with all 8 sample brokers in `input_data_from_client/`
2. Test reconciliation against `ms_receivables_client_base_data.xlsx`
3. End-to-end: Streamlit → FastAPI → HITL → reconcile → download
4. Template auto-caching: process unknown broker twice, verify cache hit
5. Verify SQLite audit records
6. Verify output Excel matches design doc format

---

## Architecture Decisions

| Decision | Rationale |
|---|---|
| **LangGraph = orchestration backbone** | All sequencing, routing, error handling, HITL lives in the graph. FastAPI is a thin trigger layer. |
| **7-node agent graph** | verify → classify → extract → [HITL] → reconcile_ms → generate → persist. Each node is a modular agent. |
| **FastAPI does NOT call agents directly** | Routes trigger `graph.stream()` / `graph.update_state()`. No business logic in routes. |
| **HITL via LangGraph interrupt** | `interrupt_before=["hitl_gate"]` pauses graph. Resume via `graph.update_state()` + `graph.stream(None)`. |
| Reconciliation = broker vs MS data | PDF vs Excel is verification only (light check) |
| SQLite for POC | Audit trail, never modify MS source (IB standard) |
| MS data pre-loaded at startup | Static file for POC, DB later |
| 4-tier extraction | Rules → fuzzy → LLM → cache. Minimizes LLM cost at scale |
| DB stores results only | Creates exception reports, does NOT mutate MS source data |

---

## New Files to Create

```
backend/
├── main.py                         # FastAPI app (thin layer)
├── api/
│   ├── __init__.py
│   └── routes/
│       ├── __init__.py
│       ├── upload.py               # File upload only
│       ├── pipeline.py             # Trigger/resume LangGraph (start, resume, status)
│       ├── download.py             # Excel download
│       └── status.py               # History, MS data stats
├── db/
│   ├── __init__.py
│   ├── models.py                   # SQLAlchemy models
│   ├── database.py                 # Engine + session factory
│   └── init_ms_data.py             # MS data loader
└── services/
    ├── __init__.py
    ├── ms_data_service.py           # MS data access (used by reconcile agent)
    └── column_matcher.py            # Fuzzy matching (used by extract agent)

agents/
├── persist_agent.py                 # NEW — persist results to DB (graph node)
└── (existing agents updated)

graph/
├── workflow.py                      # UPDATED — 7-node graph topology
├── nodes.py                         # UPDATED — reconcile_ms_node, persist_node added
└── state.py                         # UPDATED — new fields for MS data, DB tracking

templates/auto/                      # auto-generated broker templates
```

## Open Questions
1. **Commission rate source** — Is it in MS data Excel, or per-broker config?
2. **Batch upload** — Single broker pair for POC. Batch as stretch goal?
3. **MS data refresh** — Manual reload button in Streamlit for admin?
