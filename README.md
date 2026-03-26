# Brokerage Reconciliation System

> **POC v2.0** — Automated reconciliation of broker brokerage statements (PDF + Excel) against Morgan Stanley Receivables data, powered by a LangGraph agentic pipeline with Human-in-the-Loop (HITL) review and SIPDO prompt optimization for unknown brokers.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [System Components](#system-components)
- [Pipeline Flow](#pipeline-flow)
  - [Known Broker Flow](#known-broker-flow)
  - [Unknown Broker Flow (SIPDO)](#unknown-broker-flow-sipdo)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Configuration](#configuration)
  - [Starting the System](#starting-the-system)
- [API Reference](#api-reference)
- [LangGraph Workflow](#langgraph-workflow)
  - [Graph Topology](#graph-topology)
  - [Node Descriptions](#node-descriptions)
  - [Routing Logic](#routing-logic)
  - [Interrupt Points](#interrupt-points)
- [Agent Pipeline](#agent-pipeline)
- [Data Extraction Strategy](#data-extraction-strategy)
- [Reconciliation Engine](#reconciliation-engine)
- [SIPDO Prompt Optimization](#sipdo-prompt-optimization)
- [Database Schema](#database-schema)
- [Broker Templates](#broker-templates)
- [Project Structure](#project-structure)
- [Known Limitations](#known-limitations)

---

## Architecture Overview

```
┌─────────────────────┐         ┌──────────────────────┐         ┌───────────────────────────┐
│   Streamlit UI      │  HTTP   │    FastAPI Backend    │ trigger │  LangGraph Agent Graph    │
│   (Port 8501)       │────────▶│    (Port 8001)        │────────▶│  (10-node StateGraph)     │
│                     │◀────────│                       │◀────────│                           │
│  • Upload           │         │  • /api/upload        │         │  • verify_node            │
│  • Review (HITL)    │         │  • /api/pipeline/*    │         │  • classify_node          │
│  • Results          │         │  • /api/status/*      │         │  • extract_node           │
│  • History          │         │  • /api/download/*    │         │  • reconcile_node         │
│  • MS Data          │         │  • /health            │         │  • generate_node          │
│  • Prompt Cache     │         │                       │         │  • persist_node           │
└─────────────────────┘         └──────────┬───────────┘         │  • sipdo_choice_gate_node │
                                           │                     │  • sipdo_optimize_node    │
                                           │                     │  • sipdo_background_node  │
                                           │                     │  • hitl_gate_node         │
                                           ▼                     └───────────┬───────────────┘
                                ┌──────────────────────┐                     │
                                │   SQLite Database     │◀────────────────────
                                │   (data/recon.db)     │
                                │                       │
                                │  • ReconciliationSession
                                │  • ExtractedTrade     │
                                │  • ReconciliationResult│
                                │  • TemplateCache      │
                                │  • OptimizedPromptCache│
                                └──────────────────────┘
```

**Design philosophy:**
- **FastAPI** is a thin trigger layer — it calls `graph.stream()` / `graph.update_state()`, never agent functions directly.
- **LangGraph** owns all sequencing, routing, error handling, and HITL pauses.
- **Deterministic first** — rule-based parsing and fuzzy matching are always tried before LLM.
- **LLM is a fallback** — Claude is only invoked when templates, caches, and fuzzy matching fail.

---

## System Components

| Component | Technology | Purpose |
|---|---|---|
| Frontend | Streamlit | 6-page interactive UI (upload, review, results, history, MS data, prompt cache) |
| Backend API | FastAPI + Uvicorn | Thin HTTP layer over LangGraph, CORS-enabled |
| Orchestration | LangGraph (StateGraph) | 10-node agentic pipeline with conditional routing and HITL interrupts |
| LLM | Claude Sonnet 4 (Anthropic) | Fallback extraction, classification, verification, SIPDO optimization |
| Database | SQLite + SQLAlchemy | Audit trail, result persistence, template caching, SIPDO prompt caching |
| Parsers | pdfplumber, pandas, openpyxl | PDF text/table extraction, Excel multi-sheet parsing |
| Matching | rapidfuzz | Fuzzy column name matching (synonym-based) |
| Config | YAML + env vars | All settings in `dev.yaml`, API key from `ANTHROPIC_API_KEY` env var |

---

## Pipeline Flow

### Known Broker Flow

When the system recognizes the broker (via keywords, templates, or cached mappings):

```
Upload PDF+Excel
       │
       ▼
  ┌─────────┐    ┌───────────┐    ┌──────────┐    ┌──────────────┐
  │ Verify  │───▶│ Classify  │───▶│ Extract  │───▶│ HITL Review  │
  └─────────┘    └───────────┘    └──────────┘    └──────┬───────┘
                                                         │
                                              Approve ◄──┤──► Reject → END
                                                  │
                                                  ▼
                                           ┌─────────────┐    ┌───────────┐    ┌──────────┐
                                           │ Reconcile   │───▶│ Generate  │───▶│ Persist  │──▶ END
                                           └─────────────┘    └───────────┘    └──────────┘
```

1. **Verify** — Confirms PDF and Excel belong to the same invoice (metadata + keyword matching)
2. **Classify** — Identifies broker and selects parsing strategy (4-tier: hint → keywords → DB cache → LLM)
3. **Extract** — Pulls trade records from documents (5-tier: YAML → cache → fuzzy → SIPDO → LLM)
4. **HITL Review** — Pipeline pauses; user reviews extracted trades, approves or rejects
5. **Reconcile** — Matches broker trades against MS Receivables (deterministic, no LLM)
6. **Generate** — Produces 5-sheet Excel report (Summary, Matched, Mismatches, Exceptions, Raw)
7. **Persist** — Saves results to SQLite (sessions, trades, reconciliation outcomes, template cache)

### Unknown Broker Flow (SIPDO)

When the broker is not recognized, the user is given a choice:

```
Upload PDF+Excel
       │
       ▼
  ┌─────────┐    ┌───────────┐    ┌───────────────────┐
  │ Verify  │───▶│ Classify  │───▶│ SIPDO Choice Gate │ ◄── Pipeline pauses here
  └─────────┘    └───────────┘    └─────────┬─────────┘
                                            │
                              ┌─────────────┴──────────────┐
                              │                            │
                         "⚡ Quick"                   "🎯 Optimize"
                              │                            │
                              │                    ┌───────────────┐
                              │                    │ SIPDO Optimize│ ← 4-iteration prompt
                              │                    │ (2-5 min)     │   refinement loop
                              │                    └───────┬───────┘
                              │                            │
                              ▼                            ▼
                        ┌──────────┐               ┌──────────┐
                        │ Extract  │               │ Extract  │ ← uses optimized prompt
                        │ (LLM)    │               │ (SIPDO)  │
                        └────┬─────┘               └────┬─────┘
                             │                          │
                             ▼                          ▼
                     [HITL → Reconcile → Generate → Persist]
                             │
                             ▼
                    ┌──────────────────┐
                    │ SIPDO Background │ ← Runs optimization AFTER persist
                    │ (uses HITL data  │   using approved trades as ground truth
                    │  as ground truth)│
                    └──────────────────┘
```

- **Quick path**: Uses generic LLM extraction immediately, then runs SIPDO in the background after HITL approval (using approved trades as ground truth for optimization).
- **Optimize path**: Runs the full SIPDO optimization pipeline upfront (~2-5 min one-time cost), then extracts using the optimized prompt.
- Both paths cache the optimized prompt in `OptimizedPromptCache` for instant reuse on future uploads from the same broker.

---

## Getting Started

### Prerequisites

- Python 3.11+
- An Anthropic API key (Claude access)

### Installation

```bash
cd broker_recon_flow

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

All configuration lives in `dev.yaml`. The only required environment variable is the API key:

```bash
export ANTHROPIC_API_KEY="your-anthropic-api-key"
```

Key configuration sections in `dev.yaml`:

| Section | Purpose |
|---|---|
| `server` | FastAPI host/port (default: `0.0.0.0:8001`) |
| `ui` | Streamlit API base URL, page title, layout |
| `llm` | Model (`claude-sonnet-4-20250514`), max tokens, temperature |
| `storage` | Paths for raw files, parsed output, logs |
| `database` | SQLite connection URL |
| `ms_data` | Path to MS Receivables Excel file |
| `agents` | Per-agent thresholds (confidence, tolerances, fuzzy match) |
| `hitl` | Enable/disable HITL, checkpoints, auto-approve threshold |
| `brokers` | Known broker names, templates, keyword lists |

### Starting the System

The `start.sh` script launches both services:

```bash
# Start both FastAPI + Streamlit
./start.sh

# Or start individually:
./start.sh api    # FastAPI only (port 8001)
./start.sh ui     # Streamlit only (port 8501)
./start.sh both   # Both (default)
```

**What `start.sh` does:**
1. Detects and activates the Python virtual environment (`venv/`)
2. Sets `PYTHONPATH` to include the parent directory (`ms_payables/`)
3. Launches services based on mode:
   - `api` → `uvicorn broker_recon_flow.backend.main:app --port 8001 --reload`
   - `ui` → `streamlit run ui/app.py --server.port 8501`
   - `both` → Starts API in background, waits 3s, starts UI in background

**After startup:**
| Service | URL |
|---|---|
| Streamlit UI | http://localhost:8501 |
| FastAPI Backend | http://localhost:8001 |
| API Docs (Swagger) | http://localhost:8001/docs |
| Health Check | http://localhost:8001/health |

**On first launch**, the FastAPI lifespan hook automatically:
- Initializes the SQLite database and creates all tables
- Loads MS Receivables data into memory (indexes by trade_id and composite key)

---

## API Reference

### Upload

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/upload` | Upload PDF + Excel files. Creates a DB session row. Returns `{session_id, pdf_path, excel_path}` |

### Pipeline Control

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/pipeline/start` | Run Phase 1: verify → classify → extract → HITL pause. Body: `{session_id, pdf_path, excel_path, broker_hint?}` |
| `POST` | `/api/pipeline/resume` | Run Phase 2: reconcile → generate → persist. Body: `{session_id, approved, feedback?}` |
| `POST` | `/api/pipeline/sipdo-choice` | Inject SIPDO strategy for unknown brokers. Body: `{session_id, strategy: "quick"\|"optimize"}` |
| `GET` | `/api/pipeline/state/{session_id}` | Poll current pipeline state (status, trades, logs, SIPDO fields) |

### Status & History

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/status/sessions` | List all past reconciliation sessions |
| `GET` | `/api/status/sessions/{session_id}` | Get session detail |
| `GET` | `/api/status/sessions/{session_id}/results` | Trade-level reconciliation results from DB |
| `GET` | `/api/status/ms-data` | MS data stats (total rows, index sizes) |
| `GET` | `/api/status/ms-data/preview?limit=50` | Paginated MS data preview (10-500 rows) |
| `GET` | `/api/status/sipdo/prompts` | List all cached SIPDO-optimized prompts |

### Download

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/download/{filename}` | Download generated Excel output file |

---

## LangGraph Workflow

### Graph Topology

The pipeline is a **10-node LangGraph StateGraph** with 2 interrupt points:

```
                    ┌─────────┐
          START ───▶│  verify │
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │classify │
                    └────┬────┘
                         │
              ┌──────────┴──────────┐
              │ route_after_classify │
              └──────────┬──────────┘
                    ┌────┴────┐
           known    │         │  unknown
           broker   │         │  broker
              ▼     │         ▼
         ┌────────┐ │  ┌──────────────────┐
         │        │ │  │sipdo_choice_gate │ ◄── INTERRUPT #1
         │        │ │  └────────┬─────────┘
         │        │ │     ┌─────┴─────┐
         │        │ │  quick         optimize
         │        │ │     │           │
         │        │ │     │    ┌──────▼───────┐
         │        │ │     │    │sipdo_optimize│
         │        │ │     │    └──────┬───────┘
         │        │ │     │           │
         └────┬───┘ │     ▼           ▼
              │     └────▶┌──────────┐◄──────
              └──────────▶│ extract  │
                          └────┬─────┘
                               │
                         ┌─────▼─────┐
                         │ hitl_gate │ ◄── INTERRUPT #2
                         └─────┬─────┘
                               │
                    ┌──────────┴──────────┐
                    │  route_after_hitl   │
                    └──┬──────────────┬───┘
                 approved          rejected
                       │              │
                  ┌────▼────┐       END
                  │reconcile│
                  └────┬────┘
                       │
                  ┌────▼────┐
                  │generate │
                  └────┬────┘
                       │
                  ┌────▼────┐
                  │ persist │
                  └────┬────┘
                       │
            ┌──────────┴──────────┐
            │ route_after_persist │
            └──┬──────────────┬───┘
          quick+unknown     else
               │              │
        ┌──────▼────────┐    END
        │sipdo_background│
        └───────┬────────┘
                │
               END
```

### Node Descriptions

| Node | Function | Purpose |
|---|---|---|
| `verify` | `verify_node()` | Confirm PDF + Excel belong to same invoice |
| `classify` | `classify_node()` | Identify broker, select parsing strategy |
| `sipdo_choice_gate` | `sipdo_choice_gate_node()` | Pause for user choice (quick vs optimize) — unknown brokers only |
| `sipdo_optimize` | `sipdo_optimize_node()` | Run SIPDO prompt optimization pipeline |
| `extract` | `extract_node()` | Extract trade records from documents |
| `hitl_gate` | `hitl_gate_node()` | Pause for human review of extracted trades |
| `reconcile` | `reconcile_node()` | Match broker trades vs MS Receivables |
| `generate` | `generate_node()` | Produce 5-sheet Excel report |
| `persist` | `persist_node()` | Save all results to SQLite |
| `sipdo_background` | `sipdo_background_node()` | Background SIPDO optimization (quick path only) |

### Routing Logic

| Router | Condition | Destination |
|---|---|---|
| `route_after_verify` | Verification passed | `classify` |
| | Verification failed | `END` |
| `route_after_classify` | Known broker (has template/cache) | `extract` |
| | Unknown broker | `sipdo_choice_gate` |
| `route_after_sipdo_choice` | Strategy = "quick" | `extract` |
| | Strategy = "optimize" | `sipdo_optimize` |
| `route_after_sipdo_optimize` | Always | `extract` |
| `route_after_hitl` | Approved | `reconcile` |
| | Rejected | `END` |
| `route_after_persist` | Quick + unknown broker | `sipdo_background` |
| | Otherwise | `END` |

### Interrupt Points

The graph uses `interrupt_before` on two nodes:

1. **`sipdo_choice_gate`** — Pauses when an unknown broker is detected. The UI shows a choice screen ("Quick Extract" vs "Optimize First"). The user's choice is injected via `graph.update_state()` through `POST /api/pipeline/sipdo-choice`.

2. **`hitl_gate`** — Pauses after extraction completes. The UI shows the extracted trades table for human review. The user approves or rejects via `POST /api/pipeline/resume`.

---

## Agent Pipeline

### Agent 1: Verify (`agents/verify_agent.py`)

Ensures the uploaded PDF and Excel belong to the same invoice.

- **Tier 1 (Rule-based):** Weighted metadata scoring — broker keyword overlap (+0.3), invoice ID match (+0.4), date match (+0.2), Excel broker hint (+0.1). Passes at ≥0.5 confidence with no mismatches.
- **Tier 2 (LLM fallback):** Sends first 5000 chars of PDF + Excel preview to Claude for comparison.

### Agent 2: Classify (`agents/classify_agent.py`)

Identifies the broker and selects a parsing strategy.

- **Tier 1:** Direct match from user-provided broker hint against config keywords
- **Tier 2:** Rule-based keyword detection in document content + filenames
- **Tier 3:** TemplateCache DB lookup (reuses HITL-approved column mappings)
- **Tier 4:** LLM classification (sends document snippets to Claude)

### Agent 3: Extract (`agents/extract_agent.py`)

Pulls canonical trade records from documents via a 5-tier fallback strategy (see [Data Extraction Strategy](#data-extraction-strategy)).

### Agent 4: Reconcile (`agents/reconcile_agent.py`)

Matches extracted broker trades against MS Receivables data. Fully deterministic — no LLM involved (see [Reconciliation Engine](#reconciliation-engine)).

### Agent 5: Generate (`agents/template_agent.py`)

Produces a 5-sheet Excel workbook:

| Sheet | Contents |
|---|---|
| Summary | KPIs — match rate, trade counts, brokerage totals, difference |
| Broker Trades | All extracted trades (19 canonical columns) |
| Matched | Green-highlighted matched broker↔MS trade pairs |
| Mismatches | Red-highlighted mismatched pairs with reason + field differences |
| Exceptions | Yellow-highlighted new (broker-only) and missing (MS-only) trades |

### Agent 6: Persist (`agents/persist_agent.py`)

Saves to SQLite:
- Updates `ReconciliationSession` with status + metrics
- Inserts `ExtractedTrade` rows (all 19 fields)
- Inserts `ReconciliationResult` rows (match status, differences, MS snapshot)
- Upserts `TemplateCache` on HITL approval (column mappings cached for reuse)

---

## Data Extraction Strategy

Extraction uses a 5-tier fallback hierarchy — deterministic methods are always tried first:

| Tier | Method | LLM? | Description |
|---|---|---|---|
| **1** | YAML Template | No | Column mapping from `templates/{broker}.yaml` (known brokers) |
| **2** | TemplateCache DB | No | HITL-approved column mappings cached from prior sessions |
| **3** | Fuzzy Match | No | rapidfuzz synonym matching (threshold 0.70) against 16 canonical field synonyms |
| **4** | SIPDO Prompt | Yes | Optimized extraction prompt (inline from optimize path, or cached from prior SIPDO run) |
| **5a** | LLM Column Map | Yes | Claude maps column names → canonical fields, then DataFrame extraction |
| **5b** | LLM Full Extract | Yes | Claude extracts trade records directly from raw document text |

The first tier that succeeds is used. Each extracted trade is normalized to a `TradeRecord` with 19 canonical fields.

---

## Reconciliation Engine

Matching is fully deterministic (no LLM) with 3-level matching strategy:

1. **Exact trade_id match** — Direct lookup in MS trade_id index
2. **Composite key match** — `trade_date + instrument + client_account` composite index
3. **Fuzzy match** — Instrument similarity + date match + buy/sell direction match

**Field comparison tolerances** (configurable in `dev.yaml`):

| Field | Tolerance |
|---|---|
| Quantity | ±0.001 |
| Price | ±0.01 |
| Brokerage | ±1.0 currency unit |
| Currency | Exact (case-insensitive) |
| Buy/Sell | Exact (case-insensitive) |

**Confidence scoring** (0-4 points): trade_id match (+1), quantity match (+1), price match (+1), brokerage match (+1).

**Output categories:** MATCH, MISMATCH (with reason + field differences), NEW (broker-only), MISSING (MS-only).

---

## SIPDO Prompt Optimization

SIPDO (Self-Improving Prompt Design & Optimization) generates broker-specific extraction prompts for unknown brokers through iterative refinement.

### Pipeline Steps

1. **Document Analysis** — LLM analyzes broker document structure (layout type, date format, currency conventions, anomalies)
2. **Field Decomposition** — Classifies each canonical field as SIMPLE (direct column map) or COMPLEX (requires logic)
3. **Seed Prompt Generation** — Creates initial extraction prompt from analysis + field decomposition + domain knowledge
4. **Optimization Loop** (4 iterations, progressive difficulty):
   - **Iteration 1:** Column reorder / rename
   - **Iteration 2:** Date/number format variations
   - **Iteration 3:** Missing or merged columns
   - **Iteration 4:** Combined challenges
   - Each iteration: generate synthetic data → evaluate prompt → analyze errors → refine prompt → regression check
5. **Consistency Audit** — Final evaluation against original document

### Caching

Optimized prompts are stored in `OptimizedPromptCache` (SQLite), keyed by `broker_name`. Once cached, subsequent uploads from the same broker skip optimization entirely and use the cached prompt (Tier 4 in extraction).

---

## Database Schema

5 SQLAlchemy ORM models in `db/models.py`:

### ReconciliationSession
Top-level record per upload/run.

| Column | Type | Description |
|---|---|---|
| id | Integer (PK) | Auto-increment |
| session_id | String (unique) | UUID from upload |
| status | String | Pipeline status enum value |
| broker_name | String | Detected broker |
| invoice_id | String | Detected invoice ID |
| pdf_filename, excel_filename | String | Original uploaded file names |
| extraction_method | String | Which tier was used |
| extraction_confidence | Float | Confidence score |
| trade_count | Integer | Number of extracted trades |
| match_count, mismatch_count, new_count, missing_count | Integer | Reconciliation metrics |
| created_at, updated_at | DateTime | Timestamps |

### ExtractedTrade
One row per extracted trade (19 canonical fields + metadata).

### ReconciliationResult
Per-trade reconciliation outcome — status (MATCH/MISMATCH/NEW/MISSING), mismatch_reason, field differences (JSON), confidence score, MS trade snapshot.

### TemplateCache
HITL-approved column mappings cached per broker. Includes `column_mapping` (JSON), `use_count`, `hitl_approved` flag.

### OptimizedPromptCache
SIPDO-optimized extraction prompts per broker.

| Column | Type | Description |
|---|---|---|
| broker_name | String (unique) | Broker identifier |
| prompt_text | Text | The optimized extraction prompt |
| accuracy_score | Float | Final optimization accuracy |
| optimization_trace | JSON | Full iteration trace |
| source_session_id | String | Session that triggered optimization |
| created_at, updated_at | DateTime | Timestamps |

---

## Broker Templates

YAML templates in `templates/` define column mappings and parsing rules for known brokers:

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
  "Quantity": "quantity"
  ...
value_rules:
  buy_sell:
    "B|Buy": "BUY"
    "S|Sell": "SELL"
  currency:
    default: "USD"
```

**Available templates:** `evolution.yaml`, `tfs.yaml`, `icap.yaml`, `marex.yaml`

Templates are loaded by `parsers/template_parser.py`. The `templates/auto/` directory is reserved for future auto-generated templates from SIPDO optimization.

---

## Project Structure

```
broker_recon_flow/
├── README.md                    # This file
├── config.py                    # YAML config loader (@lru_cache)
├── dev.yaml                     # Master configuration
├── start.sh                     # Launch script (api | ui | both)
├── requirements.txt             # Python dependencies
│
├── agents/                      # Agent modules (business logic)
│   ├── verify_agent.py          # Agent 1: PDF/Excel verification
│   ├── classify_agent.py        # Agent 2: Broker classification
│   ├── extract_agent.py         # Agent 3: Trade extraction (5-tier)
│   ├── reconcile_agent.py       # Agent 4: Broker vs MS matching
│   ├── template_agent.py        # Agent 5: Excel report generation
│   └── persist_agent.py         # Agent 6: DB persistence + template caching
│
├── backend/                     # FastAPI application
│   ├── main.py                  # App factory, lifespan, CORS, routers
│   └── api/routes/
│       ├── upload.py            # POST /api/upload
│       ├── pipeline.py          # /start, /resume, /sipdo-choice, /state
│       ├── status.py            # /sessions, /ms-data, /sipdo/prompts
│       └── download.py          # GET /api/download/{filename}
│
├── graph/                       # LangGraph orchestration
│   ├── state.py                 # GraphState TypedDict definition
│   ├── nodes.py                 # 10 node functions + 6 routing functions
│   └── workflow.py              # StateGraph build + compile (10 nodes, 2 interrupts)
│
├── db/                          # Database layer
│   ├── database.py              # Engine, session factory, init_db()
│   └── models.py                # 5 ORM models (SQLAlchemy)
│
├── schemas/                     # Pydantic data models
│   └── canonical_trade.py       # TradeRecord, MSTradeRecord, enums, result types
│
├── services/                    # Shared services
│   ├── llm_service.py           # Claude API wrapper (invoke_llm, invoke_llm_json)
│   ├── column_matcher.py        # Fuzzy column matching (rapidfuzz + synonyms)
│   ├── ms_data_service.py       # MS Receivables loader + indexing
│   ├── storage_service.py       # File I/O (upload, output, listing)
│   ├── prompt_optimizer.py      # SIPDO optimization pipeline
│   └── prompt_cache.py          # SIPDO prompt CRUD (OptimizedPromptCache)
│
├── parsers/                     # Document parsers
│   ├── pdf_parser.py            # PDFParser (pdfplumber)
│   ├── excel_parser.py          # ExcelParser (pandas + openpyxl)
│   └── template_parser.py       # YAML template loading + column mapping
│
├── templates/                   # Broker YAML templates
│   ├── evolution.yaml
│   ├── tfs.yaml
│   ├── icap.yaml
│   ├── marex.yaml
│   └── auto/                    # Reserved for SIPDO auto-generated templates
│
├── ui/                          # Streamlit frontend
│   └── app.py                   # 6-page UI (upload, review, results, history, MS data, prompt cache)
│
├── utils/
│   └── logger.py                # Logging wrapper (file + console, rotation)
│
├── data/                        # Runtime data (gitignored)
│   ├── raw_files/               # Uploaded PDFs + Excels (timestamped)
│   ├── parsed_files/            # Intermediate parsed output
│   ├── normalized_output/       # Generated Excel reports
│   └── reconciliation.db        # SQLite database
│
├── documentation/               # Design docs + reference data
│   ├── ARCHITECTURE.md
│   ├── AGENTS.md
│   └── ms_receivables_client_base_data.xlsx
│
├── plan_and_execution/          # Development tracking
│   ├── IMPLEMENTATION_PLAN.md
│   ├── CURRENT_STATE.md
│   └── CHANGELOG.md
│
└── input_data_from_client/      # Sample broker data for testing
    └── Receivables/Invoices_Jan26/
        ├── BNP PARIBAS (SINGAPORE)_BNP/
        ├── CITADEL SECURITIES LLC_Merrill Lynch/
        ├── DBS BANK LTD AUSTRALIA BRANCH_JPM/
        ├── Goldman Sachs/
        ├── HSBC/
        ├── JP MORGAN SYDNEY - SPI_JPM/
        ├── Marex/
        └── NOMURA SYDNEY_SOCGEN/
```

---

## Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| **MemorySaver checkpointer** | Pipeline state lost on server restart | Replace with SqliteSaver for production |
| **MS data loaded once at startup** | Changes to MS Excel require restart | Add hot-reload endpoint |
| **No authentication** | API endpoints are open (POC only) | Add auth middleware for production |
| **In-memory graph state** | Single-instance only, no horizontal scaling | Persist checkpoints to DB |
| **PDF table extraction accuracy** | pdfplumber may miss complex layouts | SIPDO optimization compensates |
| **SIPDO optimization time** | 2-5 minutes per new broker (one-time) | Cached forever after first run |
| **No cancel/timeout for SIPDO** | Long optimization cannot be cancelled mid-run | Add cancellation token support |
