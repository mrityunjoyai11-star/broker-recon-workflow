# SettleAI — Setup & Usage Guide

> **For first-time users / GitHub readers.** Complete instructions to clone, configure, run, and operate the brokerage reconciliation system.

---

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the System](#running-the-system)
- [User Interface Tour](#user-interface-tour)
- [Manual Upload Flow](#manual-upload-flow)
- [Batch Processing Flow](#batch-processing-flow)
- [Reviewing Results](#reviewing-results)
- [Email Drafts](#email-drafts)
- [MS Data Management](#ms-data-management)
- [Troubleshooting](#troubleshooting)
- [Operational Notes](#operational-notes)

---

## Overview

This system reconciles broker brokerage statements (PDF) against Morgan Stanley internal trade data. It supports two flows:

| Flow | Description | Use Case |
|---|---|---|
| **Receivable** | MS receives brokerage from broker | Incoming brokerage invoices |
| **Payable** | MS pays brokerage to broker | Outgoing brokerage settlements |

Two operating modes are supported:

- **Manual** (Upload tab) — analyst uploads, reviews each step, approves at the HITL gate
- **Batch** (Batch tab — default page) — drop files in `data/NAS_PATH/{payables,receivable}/`, system runs end-to-end and auto-approves at ≥80% confidence

---

## Prerequisites

- Python **3.12** or newer
- 4 GB RAM minimum (8 GB recommended for large PDFs)
- An Anthropic API key (Claude Sonnet 4)
- Linux / macOS (Windows: use WSL2)

---

## Installation

```bash
# 1. Clone the repository
git clone <repo-url>
cd ms_payables/broker_recon_flow

# 2. Create and activate a virtual environment
python3.12 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Initialize directory structure (auto-created on first run, but you can pre-create)
mkdir -p data/NAS_PATH/payables/{Processed,Error}
mkdir -p data/NAS_PATH/receivable/{Processed,Error}
mkdir -p data/email_drafts_saved
mkdir -p logs
```

---

## Configuration

### Step 1 — Set API key

Create `.env` in the project root:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

### Step 2 — Review `dev.yaml`

The default configuration ships ready-to-use, but you can customize:

```yaml
server:
  host: "0.0.0.0"
  port: 8021

ui:
  api_base_url: "http://localhost:8021"

llm:
  provider: "anthropic"
  model: "claude-sonnet-4-20250514"
  api_key: ${ANTHROPIC_API_KEY}

ms_data:
  receivables_file: "data/sample_ms_receivables.xlsx"
  payables_file: "data/sample_ms_payables.xlsx"
```

### Step 3 — Provide MS reference data

Two Excel files required (samples shipped with the repo):

- `data/sample_ms_receivables.xlsx` — MS internal trades for receivable flow
- `data/sample_ms_payables.xlsx` — MS internal trades for payable flow

**Required columns** (case-insensitive aliases supported):

```
trade_id | trade_date | instrument | buy_sell | quantity | price |
client_account | brokerage_amount | commission_rate | currency | broker_code
```

After every reconciliation, an extra column `recon_status` is added/updated:
- `resolved` — broker trade matched
- `unresolved` — broker trade had a mismatch

---

## Running the System

### Start (daemon mode)

```bash
./start.sh           # both API and UI in background
./start.sh api       # API only
./start.sh ui        # UI only
```

The script writes PID files to `.pids/` and logs to `logs/api.log` and `logs/ui.log`. The terminal returns immediately.

```
============================================
  API:  http://localhost:8021
  UI:   http://192.168.130.49:8503
  Docs: http://localhost:8021/docs

  Logs:    tail -f logs/api.log
           tail -f logs/ui.log
  Stop:    ./stop.sh
============================================
```

### Watch logs

```bash
tail -f logs/api.log     # backend logs
tail -f logs/ui.log      # streamlit logs
```

### Stop

```bash
./stop.sh            # stop both
./stop.sh api        # stop API only
./stop.sh ui         # stop UI only
```

The stop script gracefully terminates (SIGTERM, 8s wait, then SIGKILL) and cleans up PID files. If PID files are missing, it falls back to `pkill` against the process pattern.

### Health check

```bash
curl http://localhost:8021/health
# {"status":"ok","version":"2.0.0"}
```

---

## User Interface Tour

Open **http://192.168.130.49:8503** in a browser.

| Tab | Purpose |
|---|---|
| **Batch** (default) | Drop files for automatic processing, monitor active jobs, view folder contents |
| **Upload** | Manually upload a PDF + optional Excel, walk through HITL gates |
| **Review** | View extracted trades, approve/reject extraction (only after Upload) |
| **Results** | Reconciliation summary, downloads, email draft |
| **History** | All past sessions with drill-down — including batch sessions |
| **MS Data** | Preview MS receivables/payables data, check column mapping |
| **Prompt Cache** | View SIPDO-optimized prompts cached per broker |

The sidebar always shows:
- **Pipeline progress tracker** — which agent node is currently running
- **📋 Session Activity timeline** — log lines with icons

---

## Manual Upload Flow

Use this when you want to review each step before committing.

1. **Upload tab** → choose Receivable or Payable, drop PDF(s), click "Upload & Run Pipeline"
2. The system runs verify → classify → extract.
3. If unknown broker → **SIPDO Choice screen** (Quick Extract or Optimize First)
   - **Quick** = ~30s generic extraction, optimization runs in background after approval
   - **Optimize First** = ~3min — generates broker-specific prompt with live progress
4. **Review tab** — see extracted trades + 5-metric summary (Broker, Method, Confidence, SIPDO Accuracy, Trade count)
5. Click **✅ Approve & Reconcile** or **❌ Reject & Re-extract**
6. **Results tab** opens automatically → view recon counts, download Excel, see email draft

---

## Batch Processing Flow

The default landing page. Designed for hands-off processing of many files.

### How to feed files

Two equivalent options:

**Option A — Drop in the watch folder directly:**
```bash
cp my_invoice.pdf data/NAS_PATH/payables/
# Watcher picks up within 5s
```

**Option B — Use the UI drop zone:**
- Batch tab → drop files in **💰 Payables** or **💵 Receivables** uploader → click "Queue N file(s)"

### What happens automatically

1. Watcher detects file → creates session
2. Pipeline runs end-to-end:
   - Verify → Classify → SIPDO Optimize (or use cached prompt) → Extract → Reconcile → Generate → Persist → Email Draft
3. **Auto-approve at 80%+ confidence** — no human gate
4. On success → file moves to `Processed/<timestamp>_<name>`
5. On failure or low confidence → file moves to `Error/<timestamp>_<name>` with `<name>.error.txt` sidecar containing the traceback
6. Email draft saved to `data/email_drafts_saved/`
7. DB session row created + status tracked in History

### Live monitoring

The Batch tab auto-refreshes every 3s while jobs run. Each job shows:
- **Status icon + label** — ⏳ verifying → 🎯 optimizing → 📊 extracting → 🔄 reconciling → ✅ processed
- **Confidence + SIPDO accuracy** — live as they update
- **Pipeline tracker pills** — colored by completion state
- **Per-node grouped logs** — collapsible expanders, current node auto-opens
- **📥 Download Report** + **📧 View Email Draft** (after completion)

### Configuration

In `services/batch_processor.py`:

```python
POLL_INTERVAL_SEC = 5      # how often to scan dirs
BATCH_CONCURRENCY = 4      # max parallel jobs
MIN_CONFIDENCE = 0.80      # threshold for auto-approve
SUPPORTED_EXTS = (".pdf",) # other formats can be added
```

---

## Reviewing Results

After any pipeline run (manual or batch):

### Results tab

- 6 KPI metrics: broker trades, MS trades, matched, mismatched, new, missing
- **Brokerage totals** — broker total vs MS total + difference
- **6 sub-tabs**:
  1. **Matched** — trades that match exactly
  2. **Mismatched** — same trade_id but different qty/price/brokerage
  3. **New** — broker reported it but it's not in MS data
  4. **Missing** — MS has it but broker didn't bill
  5. **All Extracted Trades**
  6. **📧 Email Draft** — auto-generated reconciliation summary, editable, save-draft button

### History tab

- Lists every session (manual + batch + failed)
- Click "Load Session Details" to drill down
- Same KPIs + extracted trades table + recon results
- **Email Draft expander** — view/edit/re-save the draft for that session

### MS Data tab

- Preview the loaded MS Excel
- Filter by flow type
- See the live `recon_status` column updated by every reconciliation

### Output files

| Path | What |
|---|---|
| `data/raw_files/<ts>_<name>.pdf` | Original uploaded file |
| `data/parsed_files/parsed_trades_<broker>_<ts>.xlsx` | All extracted trades |
| `data/normalized_output/recon_<broker>_<ts>.xlsx` | Final 5-sheet recon report |
| `data/email_drafts_saved/email_draft_<broker>_<sid8>_<ts>.txt` | Generated email |
| `data/NAS_PATH/<flow>/Processed/<ts>_<name>.pdf` | Successfully processed batch input |
| `data/NAS_PATH/<flow>/Error/<ts>_<name>.pdf[.error.txt]` | Failed batch input + traceback |

---

## Email Drafts

Every completed pipeline auto-generates a template-based email:

**Clean match (no breaks):**
```
Subject: Trade Reconciliation Confirmation — <Broker> — Invoice <ID>
Body:
  Total broker trades reviewed:  N
  Matched trades:                M ✅
  Total brokerage confirmed:     CCY 0.00
  → Approved for settlement.
```

**With breaks:**
```
Subject: Trade Reconciliation — Discrepancies Found — <Broker> — Invoice <ID>
Body:
  Matched: M
  Mismatched: B
  Unmatched (broker-only): G
  Missing (MS-only): X
  → Please provide amended recap.
```

The `📧 Send Email` button is **currently disabled** (Phase 7 — pending SMTP integration). Drafts are persisted to `data/email_drafts_saved/` and linked from the AuditEvent table.

---

## MS Data Management

### Updating the reference data

Edit `data/sample_ms_payables.xlsx` or `data/sample_ms_receivables.xlsx` directly with Excel/Python. After each reconciliation, the system automatically writes back the `recon_status` column.

To force a reload without restarting:

```bash
curl -X POST http://localhost:8021/api/status/ms-data?force_reload=true
```

### Adding a new broker's MS-side reference rows

The MS data file is broker-agnostic — just append rows with the right `broker_code`. Use any spreadsheet tool.

---

## Troubleshooting

### "Watcher not running" in Batch tab

```bash
curl -X POST http://localhost:8021/api/batch/start
# Or restart the API: ./stop.sh api && ./start.sh api
```

### Files stuck in watch dir, not picked up

Check `logs/api.log` for `[batch]` lines. If watcher isn't logging, run:

```bash
curl http://localhost:8021/api/batch/status
```

If `running: false`, the watcher thread crashed. Restart API.

### Low confidence on every file

- Check the broker's PDF actually has extractable text:
  `pdftotext file.pdf - | head`
- If it's a scan, run OCR first (`ocrmypdf in.pdf out.pdf`)
- If text exists but extraction fails: try the Manual flow with **Optimize First** to generate a custom SIPDO prompt for this broker. The prompt will be cached for future uploads.

### "Failed" rows in History with no broker name

These are batch sessions where the pipeline failed before extraction (typically classify or verify error). Check the `error_message` column in the History detail. Most often: corrupted PDF, encrypted PDF, or LLM API outage.

### Restart cleanly

```bash
./stop.sh
sleep 2
./start.sh
```

### Reset the database (⚠️ destructive)

```bash
./stop.sh
rm data/reconciliation.db
./start.sh
# DB is re-initialized empty on first request
```

---

## Operational Notes

- **Auto-restart on file changes:** disabled by default. Edit `start.sh` to add `--reload` to the uvicorn args during development.
- **Concurrent batch jobs:** capped at `BATCH_CONCURRENCY=4`. To raise/lower, edit `services/batch_processor.py`.
- **Auto-approve threshold:** `MIN_CONFIDENCE=0.80`. Lower = more files auto-processed but more risk; higher = more files punted to Error/ for manual review.
- **In-flight jobs lost on restart:** the LangGraph checkpointer is `MemorySaver` (in-memory). Files in `NAS_PATH/<flow>/` (i.e., not yet moved) will be auto-picked up on next start. Migration to `SqliteSaver` is on the Phase 6 roadmap.
- **Disk usage:** processed PDFs accumulate in `Processed/`. Set up a monthly cleanup or move-to-archive job.
- **API logs rotate manually:** `logs/api.log` grows unbounded. Use logrotate or truncate periodically.

---

## Quick API reference

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check |
| `/api/upload` | POST | Manual file upload |
| `/api/pipeline/start` | POST | Start a pipeline (after upload) |
| `/api/pipeline/resume` | POST | Approve/reject HITL gate |
| `/api/pipeline/sipdo-choice` | POST | Choose Quick or Optimize for unknown broker |
| `/api/pipeline/state/{session_id}` | GET | Poll current state |
| `/api/pipeline/email-draft/{session_id}` | GET | Load saved email draft |
| `/api/pipeline/save-email-draft` | POST | Save edited draft |
| `/api/batch/status` | GET | Watcher status + config |
| `/api/batch/jobs` | GET | List active/recent batch jobs |
| `/api/batch/folders` | GET | List files in watch/Processed/Error |
| `/api/batch/upload` | POST | Drop file into watch dir |
| `/api/status/sessions` | GET | Session history |
| `/api/status/ms-data` | GET | MS reference data stats |
| `/api/download/{filename}` | GET | Download an output Excel |

Full OpenAPI spec at **http://localhost:8021/docs**.
