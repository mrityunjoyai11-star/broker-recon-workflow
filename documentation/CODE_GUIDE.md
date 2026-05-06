# SettleAI — Code Architecture Guide

> **For developers who want to understand, modify, or extend the codebase.** This document maps the request lifecycle, key abstractions, and where to make common changes.

---

## Table of Contents

- [High-level architecture](#high-level-architecture)
- [Project structure](#project-structure)
- [Request lifecycle: end-to-end](#request-lifecycle-end-to-end)
- [LangGraph pipeline](#langgraph-pipeline)
- [Agent layer](#agent-layer)
- [Extraction tier ladder](#extraction-tier-ladder)
- [SIPDO prompt optimization](#sipdo-prompt-optimization)
- [Batch processor](#batch-processor)
- [Database schema](#database-schema)
- [State management](#state-management)
- [How to add a new broker](#how-to-add-a-new-broker)
- [How to add a new agent node](#how-to-add-a-new-agent-node)
- [Testing strategy](#testing-strategy)
- [Common gotchas](#common-gotchas)

---

## High-level architecture

```
┌──────────────────┐     HTTP     ┌──────────────────┐    ┌─────────────────────────┐
│   Streamlit UI   │ ───────────▶ │  FastAPI Backend │───▶│    LangGraph Pipeline   │
│   ui/app.py      │              │  backend/main.py │    │    graph/workflow.py    │
└──────────────────┘              └──────────────────┘    └────────────┬────────────┘
        ▲                                  │                            │
        │                                  ▼                            ▼
        │                         ┌──────────────────┐         ┌────────────────┐
        │                         │   SQLite (ORM)    │         │  Agent layer   │
        │                         │   db/models.py   │         │   agents/      │
        │                         └──────────────────┘         └────────────────┘
        │                                                              │
        │   ┌────────────────────────────────────────────────────┐    ▼
        └───│ Background batch watcher                            │  External:
            │ services/batch_processor.py                         │  - Anthropic API
            │ - polls NAS_PATH/{payables,receivable}/             │  - Local PDFs
            │ - runs full pipeline auto with HITL auto-approve    │  - Excel files
            └────────────────────────────────────────────────────┘
```

**Tech stack:**
- **Python 3.12** (pydantic 2, sqlalchemy 2)
- **FastAPI** — REST API with async lifespan
- **Streamlit** — UI frontend
- **LangGraph** — state machine orchestration with HITL interrupts
- **Anthropic Claude Sonnet 4** — primary LLM
- **pdfplumber + pypdf** — PDF parsing (text + tables)
- **pandas + openpyxl** — Excel I/O

---

## Project structure

```
broker_recon_flow/
├── start.sh / stop.sh          # daemon mode start/stop
├── dev.yaml                    # configuration
├── config.py                   # YAML loader + env-var interpolation
├── requirements.txt
│
├── backend/                    # FastAPI app
│   ├── main.py                 # app + lifespan + router registration
│   └── api/routes/
│       ├── upload.py           # POST /api/upload
│       ├── pipeline.py         # POST /api/pipeline/* — start/resume/SIPDO
│       ├── status.py           # GET /api/status/* — sessions, MS data
│       ├── batch.py            # /api/batch/* — watcher endpoints
│       └── download.py         # GET /api/download/{filename}
│
├── graph/                      # LangGraph pipeline
│   ├── state.py                # GraphState (Pydantic) — flows through every node
│   ├── nodes.py                # node functions (one per pipeline step)
│   └── workflow.py             # StateGraph builder + interrupt config
│
├── agents/                     # business logic per agent
│   ├── verify_agent.py         # PDF metadata extraction
│   ├── classify_agent.py       # broker name detection
│   ├── extract_agent.py        # 5-tier extraction ladder
│   ├── reconcile_agent.py      # broker vs MS matching
│   ├── template_agent.py       # 5-sheet Excel report builder
│   └── persist_agent.py        # DB writes
│
├── services/                   # cross-cutting infrastructure
│   ├── llm_service.py          # Anthropic client wrapper
│   ├── ms_data_service.py      # MS Excel loader + indexes + recon_status writer
│   ├── prompt_optimizer.py     # SIPDO 5-step optimization loop
│   ├── prompt_cache.py         # OptimizedPromptCache DB ops
│   ├── column_matcher.py       # fuzzy column-name matching
│   ├── storage_service.py      # file I/O (raw_files, normalized_output, …)
│   ├── sipdo_progress.py       # in-memory side-channel for SIPDO progress
│   └── batch_processor.py      # NAS_PATH file watcher + batch runner
│
├── parsers/                    # raw file parsing
│   ├── pdf_parser.py           # pdfplumber wrapper
│   ├── excel_parser.py         # openpyxl wrapper
│   └── template_parser.py      # YAML template → trades
│
├── schemas/                    # Pydantic models
│   └── canonical_trade.py      # TradeRecord, MSTradeRecord, *Result models, enums
│
├── db/
│   ├── database.py             # engine + session factory
│   └── models.py               # SQLAlchemy ORM models
│
├── ui/
│   └── app.py                  # entire Streamlit app (single file, ~1700 lines)
│
├── templates/auto/             # cached YAML extraction templates per broker
└── data/
    ├── raw_files/              # uploaded PDFs/Excels
    ├── parsed_files/           # parsed-trades Excel exports
    ├── normalized_output/      # final 5-sheet recon reports
    ├── email_drafts_saved/     # auto-generated email drafts
    ├── reconciliation.db       # SQLite DB
    ├── sample_ms_payables.xlsx
    ├── sample_ms_receivables.xlsx
    └── NAS_PATH/
        ├── payables/{Processed,Error}
        └── receivable/{Processed,Error}
```

---

## Request lifecycle: end-to-end

### Manual upload flow

```
1. User → POST /api/upload (PDF + flow_type)
       └─ upload.py: save bytes, create ReconciliationSession row, return session_id

2. UI → POST /api/pipeline/start { session_id, pdf_path, flow_type }
       └─ pipeline.py:
            graph.stream(initial_state, config={thread_id: session_id})
            ┌─→ verify_node     (verifies PDF readable)
            ├─→ classify_node   (LLM identifies broker)
            ├─→ if unknown: interrupt at sipdo_choice_gate
            │   else: extract_node directly
            └─→ interrupt at hitl_gate
       Returns serialized state.

3. UI → POST /api/pipeline/sipdo-choice { strategy: "optimize" | "quick" }
       └─ pipeline.py: update graph state, resume stream
            ├─→ if optimize: sipdo_optimize_node (in background thread)
            ├─→ extract_node
            └─→ interrupt at hitl_gate

4. UI polls GET /api/pipeline/state/{session_id} every 1s during extraction.

5. User clicks Approve → POST /api/pipeline/resume { approved: true }
       └─ pipeline.py: update graph state with hitl_approved=True, resume
            ├─→ reconcile_node       (writes recon_status to MS Excel)
            ├─→ generate_node        (builds Excel report bytes)
            └─→ persist_node         (writes to DB) → END

6. UI → GET /api/download/{filename} → user downloads Excel.

7. UI → POST /api/pipeline/save-email-draft → file persisted to email_drafts_saved/
```

### Batch flow

```
1. File appears in data/NAS_PATH/payables/foo.pdf

2. batch_processor._watch_loop() detects within 5s:
       └─ submits _process_file(path, "payable") to ThreadPoolExecutor(max_workers=4)

3. _process_file runs the FULL pipeline manually:
       a. save_uploaded_file → data/raw_files/
       b. Insert ReconciliationSession (status="uploaded")
       c. graph.stream(initial_state, config) → runs to first interrupt
       d. graph.update_state(sipdo_strategy="optimize") → resume → next interrupt (hitl_gate)
       e. Read state.extraction.confidence
          - If < MIN_CONFIDENCE: raise → caught by outer try → file moves to Error/
       f. graph.update_state(hitl_approved=True) → resume → END
       g. _autosave_email_draft(state) → writes .txt + AuditEvent
       h. Move PDF to Processed/<ts>_<name>.pdf
       i. _record_job(status="processed", confidence, sipdo_accuracy, ...)

4. UI Batch page polls /api/batch/jobs every 3s, renders live status.
```

---

## LangGraph pipeline

`graph/workflow.py` defines the state machine. Current topology (after Round 4 simplification):

```
verify ─► classify ─► [conditional]
                          │
                          ├─ known broker ─► extract ─► [interrupt: hitl_gate] ─► reconcile ─► generate ─► persist ─► [conditional]
                          │                                          │                                                       │
                          │                                          └─ rejected ─► re_extract_gate ─► sipdo_choice_gate     ├─ batch + unknown ─► sipdo_background ─► END
                          │                                                                                                  └─ END
                          └─ unknown ─► [interrupt: sipdo_choice_gate] ─► [conditional]
                                                                              ├─ "optimize" ─► sipdo_optimize ─► extract
                                                                              └─ "quick"    ─► extract
```

**Interrupts** (where the graph pauses for external input):
- `sipdo_choice_gate` — UI shows the Quick/Optimize choice screen
- `hitl_gate` — UI shows the trade review + Approve/Reject buttons

**Removed in Round 4** (auto-skipped now):
- `affirmation_gate` (Gate 2) — was per-case affirm/escalate decisions
- `break_review_gate` (Gate 3) — was per-break resolution review
- `escalation_gate` (Gate 4) — was per-escalation approval

**Why removed:** UX feedback — "all mismatches and ghosts will be in the email draft anyway, no need for per-trade decisions."

---

## Agent layer

Each agent is a Python module with one main entry function. Nodes in `graph/nodes.py` call these functions and write the result back to `GraphState`.

| Agent | Entry Point | Purpose |
|---|---|---|
| `verify_agent` | `run_verification()` | Extract PDF metadata, confirm readable |
| `classify_agent` | `run_classification()` | Identify broker name (LLM + rule-based fallback) |
| `extract_agent` | `run_extraction()` | The 5-tier ladder (see below) |
| `reconcile_agent` | `run_reconciliation()` | Match broker trades to MS data, build summary |
| `template_agent` | `run_template_generation()` | Build the 5-sheet Excel report |
| `persist_agent` | `persist_results()` | Write everything to the DB |

**Important:** agents return Pydantic result types from `schemas/canonical_trade.py`. These flow through `GraphState`. Don't return raw dicts from agents.

---

## Extraction tier ladder

`agents/extract_agent.py` runs through tiers in order. Each tier calls `_is_extraction_adequate()` to decide whether to stop or continue.

| Tier | Method | When it fires | Speed |
|---|---|---|---|
| 1 | YAML template | Cached template exists for `broker_name` | < 50ms |
| 2 | Cached column mapping | TemplateCache hit on `broker_name + pdf_fingerprint` | < 100ms |
| 3 | Fuzzy column matching | Combined PDF tables match canonical fields by name similarity | ~500ms |
| 4 | LLM column-mapping | Generic LLM call to map raw columns → canonical fields | ~5s |
| 4b | SIPDO chunked extract | Cached SIPDO prompt exists, applied in row chunks | ~10-30s |
| 5 | Concurrent page-by-page LLM | Page-level extraction with up to 10 parallel workers | ~10s/page |

**Adequacy gate:**

```python
def _field_completeness(trades) -> float:
    core = ("trade_date", "instrument", "quantity", "price")
    alt = ("brokerage_amount",)  # alt for brokerage-only invoices (e.g. ICAP)
    well_populated = 0
    for t in trades:
        c = sum(1 for f in core if getattr(t, f, None) is not None)
        a = min(1, sum(1 for f in alt if getattr(t, f, None) is not None))
        if (c + a) >= 3:
            well_populated += 1
    return well_populated / len(trades)
```

If completeness ≥ 40%, accept this tier's result. Otherwise, fall through.

**Confidence formula** (after Round 4):

```python
if extraction_method == "template":          base = 0.95
elif "sipdo" in m or "cached" in m:           base = 0.90  # SIPDO ≥85% guaranteed
elif "fuzzy" in m:                            base = 0.80
else:                                         base = 0.70
confidence = base × (0.5 + 0.5 × completeness)
```

So:
- `sipdo_concurrent_page` with 100% completeness → 0.90 × 1.0 = **90%**
- `template` with 80% completeness → 0.95 × 0.9 = **86%**
- `llm_concurrent_page` with 0% completeness → 0.70 × 0.5 = **35%** (rejected)

---

## SIPDO prompt optimization

**Location:** `services/prompt_optimizer.py`

SIPDO = **Self-Improving Prompt-Driven Optimization**. For unknown brokers, it generates a custom extraction prompt through a 5-step LLM-driven loop:

```
Step 1: Analyze document structure   (LLM identifies columns, layout, key fields)
Step 2: Decompose extraction fields  (LLM enumerates expected canonical fields)
Step 3: Generate seed prompt         (LLM writes initial extraction prompt)
Step 4: Optimization iterations      (up to 3 rounds of refine + test)
   ┌──> 4a. Generate synthetic test cases via LLM
   │    4b. Run extraction with current prompt
   │    4c. Evaluate accuracy via LLM judge
   │    4d. If accuracy ≥85%, stop early
   │    4e. Else, increase difficulty + refine prompt
   └─── repeat
Step 5: Final consistency audit      (LLM cross-checks prompt vs document)
```

**Cache behavior** (`services/prompt_cache.py`):
- Key: `(broker_name, flow_type, pdf_fingerprint)`
- `pdf_fingerprint` = SHA256 of first 3 pages' text structure
- Hit → skip optimization, reuse prompt directly

**Trace** captured in `state.sipdo_optimization_trace`:
```python
[
  {"step": 1, "stage": "Analyzing", "duration_ms": 12340, ...},
  {"step": 4, "iteration": 1, "accuracy": 0.65, "errors_found": [...]},
  {"step": 4, "iteration": 2, "accuracy": 0.92, "early_stop": True},
  ...
]
```

---

## Batch processor

**Location:** `services/batch_processor.py`

### Module-level state (thread-safe via `_jobs_lock`)

| Variable | Purpose |
|---|---|
| `_executor: ThreadPoolExecutor(max_workers=4)` | Runs `_process_file()` jobs |
| `_seen_files: set[str]` | Abs paths picked up since startup; prevents double-pickup during the move-to-Processed window |
| `_active_jobs: dict[session_id, dict]` | Recent + current job statuses (file, broker, confidence, sipdo_accuracy, status, ...) — capped at 50 in API |
| `_watcher_thread` | Daemon polling thread |
| `_stop_flag: threading.Event` | Graceful shutdown signal |

### Lifecycle

```python
# main.py lifespan startup:
start_watcher()  # creates executor + spawns _watch_loop daemon

# _watch_loop runs forever:
while not _stop_flag.is_set():
    _scan_and_dispatch()        # for each new file, executor.submit(_process_file, ...)
    _stop_flag.wait(POLL_INTERVAL_SEC)

# main.py lifespan shutdown:
stop_watcher()  # sets _stop_flag, returns immediately
```

### Per-job pipeline

`_process_file()` runs the full LangGraph pipeline manually with the same checkpointer-based pattern as the FastAPI endpoints:

```python
# 1. Phase 1: verify → classify → first interrupt
graph.stream(initial_state, config)

# 2. SIPDO choice (default = optimize)
graph.update_state(config, {sipdo_strategy: "optimize", sipdo_choice_pending: False})
graph.stream(None, config)  # runs sipdo_optimize → extract → hitl_gate

# 3. Auto-approve HITL if quality is sufficient
if confidence < MIN_CONFIDENCE: raise
graph.update_state(config, {hitl_approved: True, hitl_pending: False})
graph.stream(None, config)  # reconcile → generate → persist → END

# 4. Email draft + file move
_autosave_email_draft(state)
_move_file(file_path, processed_dir)
```

### Key concurrency invariants

- Each `_process_file` call uses a unique `thread_id` (session_id) → LangGraph state isolation works.
- `_seen_files` is a set; protected by being only mutated from the watcher thread (single producer) — workers don't touch it.
- `_active_jobs` is mutated from both watcher thread and worker threads → guarded by `_jobs_lock`.

---

## Database schema

**Engine:** SQLite via SQLAlchemy 2.x. Models in `db/models.py`.

| Table | Purpose |
|---|---|
| `reconciliation_sessions` | One row per session (manual + batch). Tracks status, broker, counts. |
| `extracted_trades` | Per-trade rows from `state.extraction.trades`. FK to session. |
| `reconciliation_results` | Per-trade match outcome (status, differences JSON, confidence). |
| `template_cache` | Auto-learned column mappings, promoted on HITL approval. |
| `optimized_prompt_cache` | SIPDO prompts (broker_name + fingerprint → prompt_text + accuracy_score). |
| `cases` | (Legacy from Round 1-3 BrokerAI gates.) Created post-reconciliation; gates 2-4 don't read these anymore. |
| `audit_events` | Append-only event log: `EXTRACTED`, `HITL1_APPROVED`, `RECONCILED`, `EMAIL_DRAFT_SAVED`, ... |
| `break_resolutions` | (Legacy.) AI-generated root-cause analyses. |
| `escalation_records` | (Legacy.) TSG escalation drafts. |

**Key tables for production reads:**
- Session list → `reconciliation_sessions ORDER BY created_at DESC`
- Drill-down → join `extracted_trades` + `reconciliation_results` on session_id

---

## State management

### `GraphState` (graph/state.py)

The single source of truth that flows through every LangGraph node. Pydantic model — Pydantic does mutation isolation between node runs (each `node(state)` returns a partial dict that the runtime merges).

**Key fields by stage:**

| Stage | Field | Type |
|---|---|---|
| Input | `pdf_path`, `pdf_paths`, `flow_type`, `broker_hint` | str / list / str |
| Verify | `verification: VerificationResult` | result obj with `invoice_id`, `doc_match` |
| Classify | `broker_name`, `template_type`, `is_unknown_broker` | str / str / bool |
| SIPDO | `sipdo_choice_pending`, `sipdo_strategy`, `sipdo_optimized_prompt`, `sipdo_accuracy_score`, `sipdo_optimization_trace` | mixed |
| Extract | `extraction: ExtractionResult` | trades list, count, method, confidence, warnings |
| HITL | `hitl_pending`, `hitl_approved`, `hitl_feedback` | bool / bool / str |
| Reconcile | `reconciliation: ReconciliationResult` | matched/mismatched/new/missing lists + summary |
| Persist | `output_filename`, `db_session_id`, `results_persisted`, `parsed_file_path` | str / str / bool / str |
| Logs | `logs: list[str]` | append-only; UI reads last 30 |

### Streamlit session state (ui/app.py)

```python
_DEFAULTS = {
    "page": "Batch",                # default landing
    "session_id": None,
    "pipeline_state": None,         # last polled state from /api/pipeline/state/{sid}
    "pdf_path": None,
    "excel_path": None,
    "history_detail_id": None,      # for cross-page navigation
}
```

The UI never holds business state — every render fetches fresh state via `_get("/api/pipeline/state/...")`. This means a refresh always shows the live pipeline status.

---

## How to add a new broker

### Option A — Use the system as-is

Just upload via the UI. SIPDO will optimize a prompt on first run; subsequent uploads use the cache. No code changes.

### Option B — Pre-build a YAML template (optional, for faster Tier 1 hits)

1. Create `templates/auto/<broker_slug>.yaml`:

```yaml
broker_name: "My Broker"
column_mappings:
  Trade ID: trade_id
  Trade Date: trade_date
  Product: instrument
  Side: buy_sell
  Qty: quantity
  Price: price
  Comm: brokerage_amount
  Ccy: currency
  Account: client_account
value_rules:
  buy_sell:
    "B": "BUY"
    "S": "SELL"
    "Buy|BUY": "BUY"
    "Sell|SELL": "SELL"
  currency:
    default: "USD"
```

2. Reload the system. Next upload from this broker → Tier 1 hit, < 50ms extraction.

### Option C — Pre-populate the OptimizedPromptCache

Run SIPDO manually once and the cache is populated. After that, all future uploads use it.

---

## How to add a new agent node

1. **Implement the agent.** Create `agents/my_agent.py` with a function that takes `GraphState`-relevant fields and returns a result dict or Pydantic object.

2. **Add a node wrapper.** In `graph/nodes.py`:

```python
def my_node(state: GraphState) -> dict:
    logger.info("[my_node] session=%s", state.session_id)
    updates = {"current_step": "my_step", "status": PipelineStatus.MY_STATUS.value}
    try:
        result = my_agent.run_my_thing(state)
        updates["my_field"] = result
        updates["logs"] = state.logs + [f"My step complete: {result.summary}"]
    except Exception as exc:
        logger.exception("my_node error")
        updates["error"] = str(exc)
        updates["status"] = PipelineStatus.FAILED.value
    return updates
```

3. **Add field to `GraphState`** (`graph/state.py`):

```python
my_field: Optional[MyResult] = None
```

4. **Add status enum value** (`schemas/canonical_trade.py` `PipelineStatus`):

```python
MY_STATUS = "my_status"
```

5. **Wire into the graph** (`graph/workflow.py`):

```python
builder.add_node("my_step", my_node)
builder.add_edge("previous_node", "my_step")
builder.add_edge("my_step", "next_node")
```

6. **Expose in API** (`backend/api/routes/pipeline.py` `_serialise_state`):

```python
"my_field": state.my_field.dict() if state.my_field else None,
```

7. **Show in UI** (`ui/app.py`):

```python
if state.get("my_field"):
    st.write("My result:", state["my_field"])
```

---

## Testing strategy

> ⚠️ The test suite is intentionally lightweight (POC stage). Production deployment should add formal tests.

### Manual smoke tests

1. **Health check:** `curl http://localhost:8021/health`
2. **DB initialized:** `sqlite3 data/reconciliation.db ".tables"` → 9 tables
3. **MS data loaded:** `curl http://localhost:8021/api/status/ms-data?flow_type=payable`
4. **Watcher running:** `curl http://localhost:8021/api/batch/status` → `running: true`

### End-to-end smoke test

1. Drop a known-good PDF in `data/NAS_PATH/payables/`.
2. Wait ~90s.
3. Verify file moved to `Processed/`.
4. Verify Excel report exists in `data/normalized_output/`.
5. Verify email draft exists in `data/email_drafts_saved/`.
6. Verify session in DB with `status="completed"`.

### Per-component scripts

```bash
# Test extraction agent in isolation:
python -c "
from broker_recon_flow.agents.extract_agent import run_extraction
result, mapping = run_extraction(file_path='data/raw_files/foo.pdf', flow_type='payable')
print(f'Confidence: {result.confidence}, Trades: {result.trade_count}')
"
```

---

## Common gotchas

### 1. Pydantic int → str validation errors

When the LLM returns `trade_id: 1` (int) instead of `"1"` (str), `TradeRecord` fails validation. Solution: `_parse_llm_trade_result()` uses `_s()` helper to coerce all string fields. **If you add new string fields to `TradeRecord`, remember to coerce in the parser.**

### 2. Streamlit Arrow type mismatch

`st.dataframe` requires column types to be uniform. `pd.DataFrame([{"a": 1}, {"a": "—"}])` will throw on render. Always coerce mixed columns to `str()` before passing to `st.dataframe`.

### 3. LangGraph node mutation

Nodes return a **partial dict** of updates, not a new GraphState. The runtime merges. Don't return `state.dict()` — only return the changed fields.

### 4. MemorySaver on restart

In-flight pipeline jobs are lost when the API restarts (MemorySaver is in-process). Files in `NAS_PATH/<flow>/` get re-detected on next start (the `_seen_files` set is cleared on restart). Don't restart mid-batch unless you're OK with re-running.

### 5. SIPDO progress side-channel

`services/sipdo_progress.py` is a thread-safe in-memory dict mapping `session_id → {messages, done}`. The UI polls `/api/pipeline/sipdo-progress/{session_id}` every 2s during optimization. **It's NOT persisted** — only useful while the optimizer is running.

### 6. Concurrent MS data writes

`update_recon_status()` reads, modifies, writes the MS Excel atomically (no per-row locking). If two pipelines reconcile against the same MS file simultaneously, the last writer wins for that field. **Acceptable for v1** — sessions don't overlap on the same trade_id usually. For production, consider switching MS data to SQLite.

### 7. `data/NAS_PATH/<flow>/` files not picked up after move-out

The `_seen_files` set persists for the API process lifetime. If you manually move a file from `Processed/` back to the watch dir, it's still in `_seen_files` and won't be re-detected. Either restart, or rename the file (different abs path = not in seen set).

### 8. Email draft saves overwrite

`save_email_draft` writes a new timestamped file each call. The "load" endpoint returns the **most recent** by mtime. If users save many drafts for the same session, all are kept on disk — only the latest is shown in the UI. Add a cleanup job for `data/email_drafts_saved/` if disk is a concern.

---

## Next-phase roadmap

| Phase | Theme | Owner |
|---|---|---|
| 6 | Migrate `MemorySaver` → `SqliteSaver` for crash-safe checkpoints | Backend |
| 7 | SMTP / Outlook email integration; enable `Send Email` button | Backend |
| 8 | Multi-user RBAC + session tagging | Backend |
| 9 | Slack/Teams notifications on Error/ entries | Backend |
| 10 | Configurable `BATCH_CONCURRENCY` + dynamic scaling | Backend |
| 11 | Test suite (pytest) + CI | Eng |
| 12 | Migration to FastAPI background tasks (replace ThreadPoolExecutor) | Backend |

---

## Where to start reading code

If you have 30 minutes:
1. `graph/workflow.py` — see the whole pipeline shape
2. `graph/state.py` — what's in the state object
3. `services/batch_processor.py:_process_file` — the auto pipeline runner
4. `agents/extract_agent.py:run_extraction` — the 5-tier ladder

If you have 2 hours:
- All of the above, plus:
5. `services/prompt_optimizer.py` — SIPDO loop
6. `agents/reconcile_agent.py` — matching logic
7. `ui/app.py:page_batch` and `_render_batch_job_activity` — live UI
8. `db/models.py` — DB schema
