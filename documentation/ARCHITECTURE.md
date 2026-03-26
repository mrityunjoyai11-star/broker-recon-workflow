# Brokerage Reconciliation System – Detailed Architecture

> Last updated: 2026-03-26 (reflects SIPDO integration)

## 1. System Design Philosophy

1. **Canonical Data Model First** – All broker formats normalize into `TradeRecord` (19-field Pydantic model)
2. **Deterministic Parsing First** – Structured PDF/Excel parsing and fuzzy matching before LLM
3. **LLM as Fallback** – Claude used only when templates, caches, and fuzzy matching fail
4. **Template-based Broker Parsing** – YAML-driven column mappings per known broker
5. **Agent Pipeline Architecture** – 6 modular agents orchestrated by a 10-node LangGraph StateGraph
6. **SIPDO for Unknown Brokers** – Self-Improving Prompt Design & Optimization generates broker-specific extraction prompts through iterative refinement, cached for reuse

## 2. Data Flow

```
┌────────────────────────────────────────────────────────────────────────────┐
│                           UPLOAD                                          │
│  PDF + Excel ──▶ StorageService ──▶ raw_files/ (timestamped)              │
│                                  ──▶ DB: ReconciliationSession (created)  │
└────────────────────────────────────────────┬───────────────────────────────┘
                                             │
┌────────────────────────────────────────────▼───────────────────────────────┐
│                      PHASE 1: EXTRACTION PIPELINE                         │
│                                                                           │
│  verify_node ──▶ classify_node ──▶ [sipdo_choice_gate] ──▶ extract_node  │
│       │                │                    │                    │         │
│  PDF↔Excel         Broker ID          Unknown broker        5-tier       │
│  validation     (4-tier detection)    user chooses:       extraction      │
│                                       quick / optimize                    │
│                                            │                              │
│                                     [sipdo_optimize]                      │
│                                     4-iteration prompt                    │
│                                     refinement loop                       │
└────────────────────────────────────────────┬───────────────────────────────┘
                                             │
                                    ◆ HITL INTERRUPT ◆
                                    User reviews trades
                                    Approve / Reject
                                             │
┌────────────────────────────────────────────▼───────────────────────────────┐
│                     PHASE 2: RECONCILIATION PIPELINE                      │
│                                                                           │
│  reconcile_node ──▶ generate_node ──▶ persist_node ──▶ [sipdo_background]│
│       │                  │                 │                  │            │
│  Broker vs MS      5-sheet Excel     SQLite write      Background SIPDO   │
│  (deterministic)   report generation  (audit trail)    (quick path only)  │
└───────────────────────────────────────────────────────────────────────────┘
```

## 3. LangGraph Implementation

### State Management

The pipeline state (`GraphState` TypedDict in `graph/state.py`) flows through all 10 nodes:

- **Inputs:** `session_id`, `pdf_path`, `excel_path`, `broker_hint`
- **Agent outputs:** `verification`, `classification`, `extraction`, `reconciliation`, `output_files`
- **HITL flags:** `hitl_pending`, `hitl_approved`, `hitl_feedback`
- **SIPDO fields:** `is_unknown_broker`, `sipdo_choice_pending`, `sipdo_strategy`, `sipdo_optimized_prompt`, `sipdo_optimization_trace`
- **Control:** `status` (PipelineStatus enum, 12 values), `current_step`, `error`
- **Persistence:** `db_session_id`, `results_persisted`
- **Audit:** `logs` (list of timestamped strings)

### Graph Topology (10 Nodes)

```
START → verify → classify
                    │
          ┌────────┴────────┐
       known              unknown
       broker              broker
          │                   │
          │         sipdo_choice_gate ◄─ INTERRUPT #1
          │              │
          │     ┌────────┴────────┐
          │   quick           optimize
          │     │                 │
          │     │         sipdo_optimize
          │     │                 │
          └─────┴────────┬───────┘
                         │
                      extract
                         │
                    hitl_gate ◄──── INTERRUPT #2
                         │
               ┌─────────┴─────────┐
            approved             rejected
               │                    │
           reconcile               END
               │
           generate
               │
            persist
               │
        ┌──────┴──────┐
   quick+unknown     else
        │               │
  sipdo_background    END
        │
       END
```

### HITL (Human-in-the-Loop) Design

Two interrupt points implemented via `interrupt_before`:

| Interrupt | When It Fires | UI Action | Resume Mechanism |
|-----------|---------------|-----------|------------------|
| `sipdo_choice_gate` | Unknown broker detected | User picks "Quick Extract" or "Optimize First" | `POST /api/pipeline/sipdo-choice` → `graph.update_state()` |
| `hitl_gate` | After extraction completes | User reviews trades, approves or rejects | `POST /api/pipeline/resume` → `graph.update_state()` |

**Mechanism:**
1. LangGraph's `interrupt_before` pauses **before** entering the interrupt node
2. `extract_node` pre-sets `status=HITL_REVIEW` / `classify_node` pre-sets `status=SIPDO_CHOICE` (because the interrupt fires before the gate node runs)
3. Streamlit detects status and renders the appropriate UI panel
4. User action calls FastAPI → `graph.update_state()` injects decision into state
5. `graph.stream(None, config)` resumes from the checkpoint

### Routing Functions

| Function | Input | Output |
|----------|-------|--------|
| `route_after_verify` | verification result | `classify` or `END` (on failure) |
| `route_after_classify` | `is_unknown_broker` flag | `extract` (known) or `sipdo_choice_gate` (unknown) |
| `route_after_sipdo_choice` | `sipdo_strategy` | `extract` (quick) or `sipdo_optimize` (optimize) |
| `route_after_sipdo_optimize` | always | `extract` |
| `route_after_hitl` | `hitl_approved` flag | `reconcile` (approved) or `END` (rejected) |
| `route_after_persist` | `is_unknown_broker` + `sipdo_strategy` | `sipdo_background` (quick+unknown) or `END` |

## 4. Agent Details

### Agent 1: Document Verification (`agents/verify_agent.py`)

**Two-tier approach:**

1. **Rule-based (fast):** Weighted metadata scoring
   - Broker keyword overlap → +0.3 confidence
   - Invoice ID match → +0.4 confidence
   - Date match → +0.2 confidence
   - Excel broker hint → +0.1 confidence
   - Threshold: 0.5 confidence + no mismatches to pass

2. **LLM fallback (Claude):** Send first 5000 chars of PDF + Excel preview
   - Structured JSON output with match verdict
   - Used only when rule-based confidence is insufficient

### Agent 2: Broker Classification (`agents/classify_agent.py`)

**Four-tier approach:**

1. **Broker hint match:** Direct match from user-provided hint against config keywords
2. **Keyword detection:** Scan document content + filenames for known broker keywords
3. **TemplateCache DB:** Lookup HITL-approved column mappings from prior sessions
4. **LLM classification:** Send document snippets to Claude (4000 chars PDF + Excel columns)

On classification, `classify_node` also:
- Checks SIPDO prompt cache for previously optimized prompts
- Sets `is_unknown_broker=True` when no template/cache/SIPDO match is found
- Sets `sipdo_choice_pending=True` to trigger the SIPDO choice gate

### Agent 3: Field Extraction (`agents/extract_agent.py`)

**5-tier fallback extraction pipeline:**

| Tier | Method | LLM? |
|------|--------|------|
| 1 | YAML template column mapping | No |
| 2 | TemplateCache DB (HITL-approved mappings) | No |
| 3 | Fuzzy column matching (rapidfuzz, threshold 0.70) | No |
| 4 | SIPDO optimized prompt (inline or cached) | Yes |
| 5a | LLM column mapping + DataFrame extraction | Yes |
| 5b | LLM full-text extraction | Yes |

The first tier that succeeds is used. Each trade normalizes to a 19-field `TradeRecord`.

### Agent 4: Reconciliation (`agents/reconcile_agent.py`)

**Pure algorithmic – no LLM:**

1. **Index MS data** by broker code, build trade_id + composite indexes
2. **3-level matching** per broker trade:
   - Exact `trade_id` match → compare fields with tolerance
   - Composite key: `trade_date + instrument + client_account`
   - Fuzzy: instrument similarity + date match + buy/sell direction
3. **Tolerance settings** (configurable in `dev.yaml`):
   - Quantity: ±0.001, Price: ±0.01, Brokerage: ±1.0
4. **Confidence scoring** (0-4): trade_id (+1), qty (+1), price (+1), brokerage (+1)
5. **Output:** MATCH, MISMATCH (with reason + differences), NEW (broker-only), MISSING (MS-only)

### Agent 5: Template Generator (`agents/template_agent.py`)

**5-sheet Excel workbook** (xlsxwriter):

| Sheet | Contents | Formatting |
|-------|----------|------------|
| Summary | KPIs, match rate, brokerage totals, difference | Bold headers |
| Broker Trades | All 19 canonical columns | Number format, auto-width |
| Matched | Side-by-side broker↔MS pairs | Green highlight |
| Mismatches | Pairs with mismatch_reason + diff columns | Red highlight |
| Exceptions | New (broker-only) + Missing (MS-only) | Yellow highlight |

### Agent 6: Persist (`agents/persist_agent.py`)

Saves to SQLite:
- Updates `ReconciliationSession` with status + metrics
- Inserts `ExtractedTrade` rows (all 19 canonical fields, linked by FK)
- Inserts `ReconciliationResult` rows (status, differences, MS snapshot)
- Upserts `TemplateCache` on HITL approval (column mappings cached for reuse)

## 5. Broker Template System

Templates define how to map broker-specific column names to canonical fields:

```yaml
broker_name: "Evolution Markets"
aliases: ["EVOLUTION", "EVM"]
pdf:
  header_patterns: [...]
  invoice_id_patterns: [...]
  table_start_keywords: [...]
column_mappings:
  "Trade Date": "trade_date"
  "Buy/Sell": "buy_sell"
  "Qty": "quantity"
value_rules:
  buy_sell:
    "B|Buy|BUY": "BUY"
    "S|Sell|SELL": "SELL"
  currency:
    default: "USD"
```

**Available templates:** `evolution.yaml`, `tfs.yaml`, `icap.yaml`, `marex.yaml`

The template parser (`parsers/template_parser.py`) does fuzzy column matching: if the exact name doesn't match a column header, it checks if the mapping key is contained in any column name.

The `templates/auto/` directory is reserved for SIPDO auto-generated templates.

## 6. SIPDO Prompt Optimization

SIPDO (Self-Improving Prompt Design & Optimization) generates broker-specific extraction prompts for unknown brokers through iterative refinement.

### Pipeline

```
Document Analysis (LLM)      → Understand layout, date format, anomalies
         │
Field Decomposition (LLM)    → Classify each canonical field as SIMPLE or COMPLEX
         │
Seed Prompt Generation (LLM) → Create initial extraction prompt
         │
Optimization Loop (4 iterations, progressive difficulty):
  ├── Iteration 1: Column reorder / rename
  ├── Iteration 2: Date/number format variations
  ├── Iteration 3: Missing or merged columns
  └── Iteration 4: Combined challenges
  Each: Generate synthetic data → Evaluate prompt → Analyze errors → Refine prompt → Regression check
         │
Consistency Audit (LLM)      → Final evaluation against original document
```

### Integration Paths

- **Optimize-first path**: SIPDO runs before extraction. Optimized prompt is used directly in Tier 4 extraction, then cached in `OptimizedPromptCache`.
- **Quick path**: Generic LLM extraction runs immediately. After HITL approval and persistence, SIPDO runs in the background using the approved trades as ground truth. Result cached for future uploads.

### Caching

Optimized prompts are stored in `OptimizedPromptCache` (SQLite), keyed by `broker_name`. Once cached, future uploads from the same broker use the cached prompt at Tier 4, skipping both SIPDO and generic LLM.

## 7. Logging Architecture

```
Logger: brokerage_recon
├── .agent.verify      # Agent 1 logs
├── .agent.classify    # Agent 2 logs
├── .agent.extract     # Agent 3 logs
├── .agent.reconcile   # Agent 4 logs
├── .agent.template_gen# Agent 5 logs
├── .graph.nodes       # LangGraph node execution
├── .graph.workflow    # Workflow compilation
├── .pdf_parser        # PDF parsing details
├── .excel_parser      # Excel parsing details
├── .template_parser   # Column mapping details
├── .llm_service       # LLM invocations
├── .storage           # File I/O
└── .streamlit_app     # UI events
```

All logs go to both console and `logs/app.log` with rotation (10 MB, 5 backups).

## 8. Where LLM Is Used (and Where It Isn't)

| Task | LLM Used? | Notes |
|------|-----------|-------|
| Document verification (primary) | No | Rule-based metadata comparison |
| Document verification (fallback) | Yes | When rule-based confidence < threshold |
| Broker classification (primary) | No | Keyword matching + DB cache |
| Broker classification (fallback) | Yes | When keywords are insufficient |
| Column name mapping (Tier 1-3) | No | YAML template / DB cache / fuzzy match |
| Column name mapping (Tier 4: SIPDO) | Yes | Optimized extraction prompt |
| Column name mapping (Tier 5: generic) | Yes | For unknown brokers without SIPDO cache |
| SIPDO prompt optimization | Yes | 7 specialized sub-agents (one-time per broker) |
| Numeric extraction | No | Regex + type casting |
| Reconciliation | No | Pure algorithmic with configurable tolerances |
| Financial calculations | No | Deterministic only |
| Output generation | No | pandas + XlsxWriter |

## 9. Adding a New Broker

**Option A: YAML template (manual)**
1. Create `templates/<broker>.yaml` with column mappings + value rules
2. Add to `dev.yaml` → `brokers` list with name, template, keywords
3. No code changes required

**Option B: SIPDO auto-learning (automatic)**
1. Upload the broker's PDF + Excel as usual
2. System detects unknown broker → SIPDO choice gate fires
3. Choose "Optimize First" → SIPDO generates a broker-specific prompt (~2-5 min)
4. Prompt is cached in `OptimizedPromptCache` — future uploads use it automatically

**Option C: HITL auto-learning (semi-automatic)**
1. Upload and let extraction use fuzzy match or LLM fallback
2. Review and approve trades in HITL
3. `persist_agent` caches the column mapping in `TemplateCache`
4. Future uploads hit Tier 2 (cached template) — no LLM needed

## 10. Database Schema

5 SQLAlchemy ORM models in `db/models.py`:

| Model | Purpose | Key Columns |
|-------|---------|-------------|
| `ReconciliationSession` | Top-level record per upload/run | session_id, status, broker_name, metrics |
| `ExtractedTrade` | One row per extracted trade | All 19 canonical fields + FK to session |
| `ReconciliationResult` | Per-trade recon outcome | status, mismatch_reason, differences (JSON), MS snapshot |
| `TemplateCache` | HITL-approved column mappings | broker_name, column_mapping (JSON), hitl_approved |
| `OptimizedPromptCache` | SIPDO-optimized prompts | broker_name (unique), prompt_text, accuracy_score, trace (JSON) |

## 11. Security Notes

- **API key**: Stored as `ANTHROPIC_API_KEY` environment variable (never in config files)
- **File uploads**: Stored locally with timestamped names (no external storage)
- **No authentication**: POC only — add auth middleware for production
- **Network**: No external calls except to Anthropic API
- **Session data**: In-memory MemorySaver (ephemeral) — use SqliteSaver for persistence
