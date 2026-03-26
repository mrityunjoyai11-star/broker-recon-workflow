# Agent Documentation – Brokerage Reconciliation System

---

## System Overview

The Brokerage Reconciliation System uses a **5-agent agentic pipeline** orchestrated by **LangGraph**. Each agent is a self-contained module with a single responsibility. The agents execute sequentially, with **Human-in-the-Loop (HITL)** checkpoints at three critical decision points.

```
┌──────────┐    ┌───────────┐    ┌──────────┐    ┌─────────────┐    ┌───────────┐
│  Agent 1 │──▸ │  Agent 2  │──▸ │ Agent 3  │──▸ │   Agent 4   │──▸ │  Agent 5  │
│  Verify  │    │ Classify  │    │ Extract  │    │ Reconcile   │    │ Generate  │
└────┬─────┘    └───────────┘    └────┬─────┘    └──────┬──────┘    └───────────┘
     │                                │                 │
  [HITL ⏸]                        [HITL ⏸]          [HITL ⏸]
```

**Design Principles:**
- **Deterministic parsing first** — LLM is used only as a fallback when rule-based methods fail
- **Canonical data model** — all broker formats normalize to a single `TradeRecord` schema
- **Template-driven** — adding new brokers requires YAML configuration, not code
- **No LLM for math** — reconciliation is purely algorithmic with configurable tolerances

---

## Agent 1 – Document Verification Agent

| Property | Value |
|----------|-------|
| **File** | `agents/verify_agent.py` |
| **LangGraph Node** | `verify_node` (in `graph/nodes.py`) |
| **HITL Checkpoint** | Yes – pauses if confidence < 0.95 or mismatch detected |
| **LLM Usage** | Fallback only (when rule-based is inconclusive) |
| **Logger** | `brokerage_recon.agent.verify` |

### Purpose

Verifies that the uploaded PDF and Excel brokerage statements belong to the **same invoice/trade set** before proceeding with extraction and reconciliation.

### Entry Point

```python
def run_verification(pdf_path: str, excel_path: str) -> VerificationResult
```

### How It Works

**Step 1 – Parse Both Documents**
- Uses `PDFParser` to extract text, tables, and metadata from the PDF
- Uses `ExcelParser` to read sheets, detect headers, and extract metadata
- Both parsers auto-detect invoice IDs, dates, and broker keywords via regex patterns

**Step 2 – Rule-Based Verification** (`_rule_based_verify`)

Compares extracted metadata using a weighted scoring system:

| Check | Confidence Weight | Condition |
|-------|-------------------|-----------|
| Broker keywords overlap | +0.30 | Same broker keywords found in both files |
| Invoice ID match | +0.40 | Invoice IDs are identical (case-insensitive) |
| Date match | +0.20 | Dates are identical |
| Excel broker hint match | +0.10 | Excel header broker name matches detected broker |

A document is considered matched if `confidence >= 0.50` AND zero mismatches exist.

**Step 3 – LLM Fallback** (`_llm_verify`)

If rule-based confidence is below the configured threshold (default 0.75), the agent sends the first 3000 characters of PDF text + Excel preview to Claude with a structured system prompt. The LLM returns a JSON verdict.

### Output Schema

```python
class VerificationResult(BaseModel):
    broker_detected: Optional[str]     # Detected broker name
    invoice_id: Optional[str]          # Extracted invoice ID
    doc_match: bool                    # Whether documents match
    confidence: float                  # 0.0 to 1.0
    pdf_metadata: dict                 # Raw PDF metadata
    excel_metadata: dict               # Raw Excel metadata
    mismatches: List[str]              # Specific mismatches found
    message: str                       # Human-readable explanation
```

### HITL Behavior

| Condition | Action |
|-----------|--------|
| `confidence >= 0.95` and `doc_match = True` | Auto-approved, pipeline continues |
| `confidence < 0.95` or `doc_match = False` | Pipeline pauses, Streamlit shows review panel |
| User clicks "Approve" | Pipeline resumes to Agent 2 |
| User clicks "Reject" | Pipeline terminates with failure |

### Configuration (`dev.yaml`)

```yaml
agents:
  verification:
    confidence_threshold: 0.75    # Minimum for rule-based to succeed
    max_retries: 2
hitl:
  auto_approve_confidence: 0.95   # Skip HITL if confidence >= this
```

---

## Agent 2 – Broker Template Classifier

| Property | Value |
|----------|-------|
| **File** | `agents/classify_agent.py` |
| **LangGraph Node** | `classify_node` (in `graph/nodes.py`) |
| **HITL Checkpoint** | No – proceeds directly to Agent 3 |
| **LLM Usage** | Fallback only (when keyword detection fails) |
| **Logger** | `brokerage_recon.agent.classify` |

### Purpose

Detects which broker the documents belong to and selects the appropriate **YAML parsing template** that defines column mappings for field extraction.

### Entry Point

```python
def run_classification(
    pdf_path: str | None = None,
    excel_path: str | None = None,
    broker_hint: str | None = None,
) -> ClassificationResult
```

### How It Works

**Step 1 – Broker Hint Matching** (`_match_broker_hint`)

If Agent 1 already detected a broker name, the classifier checks it against all configured broker keywords. If a match is found with confidence ≥ threshold, classification completes immediately without scanning documents again.

**Step 2 – Rule-Based Keyword Detection**

Scans both PDF text and Excel content for broker-identifying keywords configured in `dev.yaml`:

```yaml
brokers:
  - name: "Evolution Markets"
    template: "evolution"
    keywords: ["Evolution Markets", "EVOLUTION", "EVM"]
  - name: "TFS Energy"
    template: "tfs"
    keywords: ["TFS ENERGY", "TFS", "Tradition Financial Services"]
  # ... etc
```

Detected keywords are scored per broker. The broker with the highest keyword match count is selected. Confidence is calculated as: `0.5 + (best_score / total_keywords) * 0.5`.

**Step 3 – LLM Fallback** (`_llm_classify`)

If no keywords are confidently detected, the agent sends PDF text (first 4000 chars) and Excel column names + sample rows to Claude. The LLM identifies the broker from the content and returns a template name.

### Supported Brokers & Templates

| Broker | Template File | Keywords |
|--------|--------------|----------|
| Evolution Markets | `templates/evolution.yaml` | Evolution Markets, EVOLUTION, EVM |
| TFS Energy | `templates/tfs.yaml` | TFS ENERGY, TFS, Tradition Financial Services |
| ICAP / TP ICAP | `templates/icap.yaml` | ICAP, TP ICAP, Tullett Prebon |
| Marex | `templates/marex.yaml` | Marex, MAREX, Marex Spectron |

### Output Schema

```python
class ClassificationResult(BaseModel):
    template_type: Optional[str]        # e.g., "evolution", "tfs"
    parser_strategy: Optional[str]      # Always "template" when matched
    confidence: float                   # 0.0 to 1.0
    detected_keywords: List[str]        # Keywords found in documents
    method: str                         # "rule_based" or "llm"
```

### Adding a New Broker

1. Create `templates/<broker_name>.yaml` with `column_mappings` and `value_rules`
2. Add an entry to `dev.yaml` → `brokers` list with `name`, `template`, and `keywords`
3. No code changes required

### Configuration (`dev.yaml`)

```yaml
agents:
  classifier:
    confidence_threshold: 0.80    # Minimum for rule-based to succeed
    use_llm_fallback: true        # Set false to disable LLM classification
```

---

## Agent 3 – Field Extraction Agent

| Property | Value |
|----------|-------|
| **File** | `agents/extract_agent.py` |
| **LangGraph Node** | `extract_node` (in `graph/nodes.py`) |
| **HITL Checkpoint** | Yes – pauses for human review of extracted trades |
| **LLM Usage** | Fallback for ambiguous column mappings |
| **Logger** | `brokerage_recon.agent.extract` |

### Purpose

Performs the main data parsing: extracts trade records from both PDF and Excel files and maps them into the **canonical `TradeRecord` schema** using template-defined column mappings.

### Entry Point

```python
def run_extraction(
    pdf_path: str | None,
    excel_path: str | None,
    template_type: str | None,
    broker_name: str | None = None,
    invoice_id: str | None = None,
) -> ExtractionResult
```

### How It Works

**PDF Extraction Pipeline** (`_extract_from_pdf`):

```
pdfplumber → Table Detection → DataFrame Creation → Template Column Mapping → TradeRecord List
```

1. `PDFParser.extract_tables()` uses pdfplumber to detect and extract all tables from every page
2. Each table becomes a pandas DataFrame with the first row as column headers
3. The YAML template's `column_mappings` renames broker columns to canonical fields (e.g., `"Trade Date"` → `"trade_date"`)
4. `value_rules` clean and normalize values (e.g., `"B"` → `"BUY"`, strip currency symbols from numbers)
5. Rows missing all key fields (`trade_id`, `trade_date`, `instrument`, `quantity`) are skipped as non-data rows

**Excel Extraction Pipeline** (`_extract_from_excel`):

```
pandas.read_excel → Header Row Detection → Primary Table Selection → Template Column Mapping → TradeRecord List
```

1. `ExcelParser.read_all_sheets()` reads all sheets, auto-detecting the header row by finding the row with the most non-null string values
2. `get_primary_table()` selects the sheet with the most trade-relevant column names (matching keywords like "trade", "price", "quantity")
3. Same template mapping and value cleaning as PDF

**LLM Fallback** (`_llm_extract_table`):

When no template is available (unknown broker), the agent sends column names and 5 sample rows to Claude. The LLM returns a `column_mapping` JSON that maps source columns to canonical fields. A synthetic template is constructed from the LLM response and used for standard `dataframe_to_trades` processing.

### Template Column Mapping Engine (`parsers/template_parser.py`)

The `map_columns` function performs intelligent column matching:

1. **Exact match** (case-insensitive): `"Trade Date"` matches `"trade date"`
2. **Containment match**: `"Trade Date"` matches `"Execution Trade Date"`
3. Unmapped columns are preserved as-is

Value cleaning (`clean_value`):
- **buy_sell**: Regex-based normalization (`"B"`, `"Buy"`, `"BUY"`, `"BOUGHT"` → `"BUY"`)
- **Numeric fields**: Strips `$`, `,`, `€`, `£`, converts parenthesized negatives `(100)` → `-100`
- **Currency**: Uppercase normalization, falls back to template default (usually `"USD"`)

### Output Schema

```python
class ExtractionResult(BaseModel):
    pdf_trades: List[TradeRecord]     # Trades extracted from PDF
    excel_trades: List[TradeRecord]   # Trades extracted from Excel
    pdf_count: int                    # Number of PDF trades
    excel_count: int                  # Number of Excel trades
    extraction_method: str            # "structured" or "llm_assisted"
    confidence: float                 # 0.9 (template) or 0.7 (LLM)
    warnings: List[str]              # Any issues encountered
```

### Canonical TradeRecord Schema

Every trade from every broker normalizes to this 18-field model:

```python
class TradeRecord(BaseModel):
    id: str                              # Auto-generated UUID
    invoice_id: Optional[str]
    broker_name: Optional[str]
    invoice_date: Optional[str]
    trade_id: Optional[str]
    trade_date: Optional[str]
    instrument: Optional[str]
    exchange: Optional[str]
    buy_sell: Optional[str]              # "BUY" or "SELL"
    quantity: Optional[float]
    unit: Optional[str]
    price: Optional[float]
    delivery_start: Optional[str]
    delivery_end: Optional[str]
    counterparty: Optional[str]
    brokerage_rate: Optional[float]
    brokerage_amount: Optional[float]
    currency: Optional[str]
    source_file: Optional[str]           # Origin filename
    source_type: Optional[str]           # "pdf" or "excel"
    raw_row: Optional[dict]              # Original row for audit trail
```

### HITL Behavior

| Condition | Action |
|-----------|--------|
| `confidence >= 0.95` | Auto-approved |
| `confidence < 0.95` | Pipeline pauses; Streamlit shows extracted trades in tabbed DataFrames (PDF tab + Excel tab) for human review |

### Configuration (`dev.yaml`)

```yaml
agents:
  extraction:
    use_llm_fallback: true
    llm_confidence_threshold: 0.70
    supported_formats: ["pdf", "xlsx", "xls", "csv"]
```

---

## Agent 4 – Reconciliation Engine

| Property | Value |
|----------|-------|
| **File** | `agents/reconcile_agent.py` |
| **LangGraph Node** | `reconcile_node` (in `graph/nodes.py`) |
| **HITL Checkpoint** | Yes – pauses when breaks or missing records are found |
| **LLM Usage** | **None** – purely deterministic/algorithmic |
| **Logger** | `brokerage_recon.agent.reconcile` |

### Purpose

Performs deterministic trade-level matching between PDF-extracted and Excel-extracted records. Identifies matched trades, breaks (mismatches), missing records, and duplicates.

### Entry Point

```python
def run_reconciliation(
    pdf_trades: list[TradeRecord],
    excel_trades: list[TradeRecord],
) -> ReconciliationResult
```

### How It Works

**Step 1 – Build Match Index**

Creates a composite key for each trade: `trade_id | trade_date | instrument` (uppercased). Excel trades are indexed by this key for O(n) lookup.

```python
def _make_match_key(trade: TradeRecord) -> str:
    # Produces: "T001|2024-03-15|CRUDE OIL WTI"
```

**Step 2 – Primary Matching**

For each PDF trade:
1. Look up the composite key in the Excel index
2. If found, compare all `match_fields` with configurable tolerances
3. If no exact key match, fall through to fuzzy matching

**Step 3 – Fuzzy Matching** (`_fuzzy_find`)

When composite key fails, the agent attempts two fallback strategies:
- **Trade ID only match**: If `trade_id` matches (case-insensitive), candidate is returned
- **Instrument + Quantity match**: If both instrument and quantity match exactly, candidate is returned

**Step 4 – Field-Level Comparison** (`_compare_trades`)

For each matched pair, every field in `match_fields` is compared:

| Field Type | Comparison Method |
|-----------|-------------------|
| Numeric fields (quantity, price, brokerage_amount) | Absolute difference must be ≤ configured tolerance |
| String fields (trade_id, currency) | Case-insensitive exact match |
| Missing values | Recorded as `"missing_value"` difference |

Configurable tolerances (from `dev.yaml`):

```yaml
agents:
  reconciliation:
    tolerance:
      quantity: 0.001
      price: 0.01
      brokerage: 0.01
    match_fields:
      - trade_id
      - quantity
      - price
      - brokerage_amount
      - currency
```

**Step 5 – Duplicate Detection** (`_find_duplicates`)

Scans each source independently for trades sharing the same composite key. Flagged separately from breaks.

**Step 6 – Summary Statistics**

Produces a summary dict with counts and match rate percentage.

### Output Categories

| Category | Description |
|----------|-------------|
| **matched_trades** | PDF and Excel trades that agree on all match fields within tolerance |
| **breaks** | Matched pairs with one or more field differences beyond tolerance |
| **missing_in_pdf** | Trades present in Excel but not found in PDF |
| **missing_in_excel** | Trades present in PDF but not found in Excel |
| **duplicates** | Trades with identical composite keys within the same source |

### Output Schema

```python
class ReconciliationResult(BaseModel):
    matched_trades: List[ReconciliationMatch]
    breaks: List[ReconciliationMatch]
    missing_in_pdf: List[ReconciliationMatch]
    missing_in_excel: List[ReconciliationMatch]
    duplicates: List[ReconciliationMatch]
    summary: dict     # Counts + match_rate percentage

class ReconciliationMatch(BaseModel):
    pdf_trade: Optional[TradeRecord]
    excel_trade: Optional[TradeRecord]
    status: str          # "matched", "break", "missing_pdf", "missing_excel", "duplicate"
    differences: dict    # Per-field diff detail for breaks
```

### HITL Behavior

| Condition | Action |
|-----------|--------|
| Zero breaks and zero missing records | Auto-approved |
| Any breaks or missing records exist | Pipeline pauses; Streamlit shows tabbed view (Matched / Breaks / Missing / Duplicates) |

### Why No LLM

Financial reconciliation must be deterministic and auditable. The architecture doc explicitly states:

> *"LLM should **not** perform: numeric calculations, reconciliation logic, financial validation"*

---

## Agent 5 – Template Generator

| Property | Value |
|----------|-------|
| **File** | `agents/template_agent.py` |
| **LangGraph Node** | `generate_node` (in `graph/nodes.py`) |
| **HITL Checkpoint** | No – final step, outputs are immediately available |
| **LLM Usage** | **None** |
| **Logger** | `brokerage_recon.agent.template_gen` |

### Purpose

Creates standardized, formatted Excel output files from the extracted and reconciled trade data. These files are suitable for payment approval workflows.

### Entry Point

```python
def run_template_generation(
    extraction: ExtractionResult,
    reconciliation: ReconciliationResult,
    broker_name: str | None = None,
) -> dict[str, bytes]
```

### Output Files

The agent generates **two Excel workbooks** using XlsxWriter:

#### 1. `normalized_trades_<timestamp>.xlsx`

| Sheet | Contents |
|-------|----------|
| **PDF Trades** | All trades extracted from the PDF, in canonical column order |
| **Excel Trades** | All trades extracted from the Excel file, in canonical column order |
| **Summary** | Broker name, generation time, trade counts, extraction method, confidence |

#### 2. `reconciliation_report_<timestamp>.xlsx`

| Sheet | Contents | Color Code |
|-------|----------|------------|
| **Summary** | Metric/value pairs from reconciliation summary | — |
| **Matched** | All matched trade pairs (PDF side + Excel side) | Green (`#d4edda`) |
| **Breaks** | Trade pairs with field-level differences | Red (`#f8d7da`) |
| **Missing** | Records present in one source but not the other | Yellow (`#fff3cd`) |
| **Duplicates** | Duplicate records detected within a single source | — |

### Canonical Column Order

All trade sheets use this consistent 19-column layout:

```
invoice_id | broker_name | invoice_date | trade_id | trade_date |
instrument | exchange | buy_sell | quantity | unit | price |
delivery_start | delivery_end | counterparty | brokerage_rate |
brokerage_amount | currency | source_file | source_type
```

### Excel Formatting

- **Headers**: Bold, white text on blue (`#1f77b4`) background, bordered, text-wrapped
- **Data cells**: Bordered, vertically centered
- **Numbers**: Formatted as `#,##0.00`
- **Title rows**: 14pt bold blue text with broker name and timestamp
- **Column widths**: Auto-fitted to content (capped at 40 characters)
- **Sheet-specific colors**: Green for matched, red for breaks, yellow for missing

### Configuration (`dev.yaml`)

```yaml
agents:
  template_generator:
    output_format: "xlsx"
    include_summary: true
```

---

## LangGraph Orchestration

### Graph Structure (`graph/workflow.py`)

```python
StateGraph(GraphState)
├── verify          → conditional → classify | hitl_verification | failed
├── classify        → extract
├── extract         → conditional → reconcile | hitl_extraction | failed
├── reconcile       → conditional → generate | hitl_reconciliation | failed
├── generate        → END
├── hitl_verification   → conditional → classify | failed
├── hitl_extraction     → conditional → reconcile | failed
├── hitl_reconciliation → conditional → generate | failed
└── failed          → END
```

### HITL Mechanism

The workflow uses LangGraph's `interrupt_before` parameter to pause execution before HITL nodes:

```python
compiled = workflow.compile(
    checkpointer=MemorySaver(),
    interrupt_before=["hitl_verification", "hitl_extraction", "hitl_reconciliation"],
)
```

When a HITL node is reached:
1. Graph execution pauses and returns current state
2. Streamlit UI renders the appropriate review panel
3. User approves or rejects
4. `graph.update_state(config, updates)` injects the human decision
5. `graph.stream(None, config)` resumes from the interrupt point

### State Flow (`graph/state.py`)

All agents read from and write to a shared `GraphState` TypedDict that flows through the graph. Key fields:

| Field | Set By | Read By |
|-------|--------|---------|
| `pdf_path`, `excel_path` | Initial state | Agent 1, 2, 3 |
| `verification` | Agent 1 | Streamlit HITL panel |
| `broker_name`, `template_type` | Agent 1 + 2 | Agent 3 |
| `extraction` | Agent 3 | Agent 4, 5, Streamlit |
| `reconciliation` | Agent 4 | Agent 5, Streamlit |
| `output_files` | Agent 5 | Streamlit download |
| `hitl_pending` | Routing logic | Streamlit (determines which panel to show) |
| `logs` | All nodes | Streamlit sidebar |

---

## LLM Integration Summary

| Agent | LLM Usage | System Prompt Purpose |
|-------|-----------|----------------------|
| Agent 1 (Verify) | Fallback | Compare PDF/Excel metadata, determine if same invoice |
| Agent 2 (Classify) | Fallback | Identify broker from document text |
| Agent 3 (Extract) | Fallback | Map ambiguous column names to canonical schema fields |
| Agent 4 (Reconcile) | **None** | — |
| Agent 5 (Generate) | **None** | — |

All LLM calls use `invoke_llm_json()` from `services/llm_service.py`, which:
- Sends `SystemMessage` + `HumanMessage` via `ChatAnthropic`
- Strips markdown code fences from response
- Parses JSON; returns `{"parse_error": True}` on failure
- Model: `claude-sonnet-4-20250514`, temperature: `0.0`
