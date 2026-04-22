# SettleAI Full Pipeline — Gap Fill & Phase Roadmap

> **Date:** 3 April 2026  
> **Authors:** System Architect + Senior Broker Recon Analyst  
> **Purpose:** Map the current `broker_recon_flow` system against the full SettleAI vision, identify gaps, and define a phased build plan.

---

## 1. Current System vs. SettleAI Target

The current build is solid for the **intake-to-reconciliation half** of the pipeline. The entire **post-reconciliation half** — affirmation, case management, break resolution, evidence compilation, escalation, and the full audit trail — is absent.

| SettleAI Stage | Status | Notes |
|---|---|---|
| Intake Agent (upload + parse structured fields) | ✅ Complete | verify + classify + extract (5-tier SIPDO) |
| HITL Gate 1 (validate extracted fields) | ✅ Complete | `hitl_gate` interrupt fires after extraction |
| Matching Agent (search OMS) | ⚠️ Thin | MS Excel = our OMS; acceptable for POC |
| Reconciliation Agent (field diffs, narrative, confidence) | ✅ Complete | MATCH / MISMATCH / NEW / MISSING with per-field deltas |
| **HITL Gate 2 — Affirmation** | ❌ Missing | Results page shows output but no "Affirm" gate |
| **Case Management** | ❌ Missing | No Case entity or lifecycle |
| **Resolution Agent** (root cause + draft broker email) | ❌ Missing | Core of the break path |
| **HITL Gate 3 — Break Review** (approve email) | ❌ Missing | |
| **Evidence Agent** (compile proof from sources) | ❌ Missing | |
| **Escalation Agent** (draft email to TSG) | ❌ Missing | |
| **HITL Gate 4 — Escalation Approval** | ❌ Missing | |
| **Ghost Trade explicit decision gate** | ⚠️ Partial | "NEW" bucket exists; no human decision gate |
| Audit Trail (per event, per actor, each timestamp) | ⚠️ Partial | Session-level only; no AuditEvent log, no reviewer identity |
| Email Ingestion / Mailbox Trigger | ❌ Deferred | Phase 7 |
| Send Email to Broker / TSG | ❌ Deferred | Phase 7 |

---

## 2. New Graph Shape

```
[existing: verify → classify → sipdo_choice? → sipdo_optimize? → extract → hitl_gate (Gate 1 ✅)]
                                                                                    ↓
                                                                         reconcile_node ✅
                                                                                    ↓
                                                                       case_router_node 🆕
                                                                       (creates Case rows per trade;
                                                                        sets has_breaks / has_ghosts flags)
                                                                                    ↓
                                                             ◆ affirmation_gate (Gate 2) 🆕 HITL INTERRUPT
                                                     User sees: Matched (Affirm) | Mismatches (Request Resolution)
                                                                Ghost (Reject / Book / Escalate TSG) | Missing
                                                                                    ↓
                                          ┌───────────── route_post_affirmation ──────────────────┐
                                          │ has_breaks = True                    no_breaks = False │
                                   resolution_node 🆕                        generate_node ✅
                                   (LLM: root cause,                         → persist_node ✅
                                    severity, draft
                                    broker email)
                                          ↓
                            ◆ break_review_gate (Gate 3) 🆕 HITL INTERRUPT
                            User sees: per-break root cause, severity badge,
                                       editable broker email draft
                                          ↓ (all breaks approved)
                                  evidence_node 🆕
                                  (compile proof: PDF data, MS snapshot,
                                   recon diff, broker metadata, stubs for
                                   Bloomberg / SWIFT / custodian)
                                          ↓
                                  escalation_node 🆕
                                  (LLM: draft escalation email to TSG
                                   with case ref, break summary,
                                   evidence, urgency rating)
                                          ↓
                            ◆ escalation_gate (Gate 4) 🆕 HITL INTERRUPT
                            User sees: evidence source list +
                                       editable escalation email draft
                                          ↓ (approved)
                                  generate_node ✅ → persist_node ✅
```

---

## 3. Phase Breakdown

### Phase 1 — Foundation: DB Schema + State Extensions
> No graph changes yet. Everything downstream depends on this.

**DB Models to add in `db/models.py`:**

| Model | Key Fields |
|---|---|
| `Case` | `case_id`, `session_id` (FK), `extracted_trade_id` (FK), `case_type` (matched / break / ghost / missing), `status` (open / affirmed / in_dispute / resolved / rejected / pending_booking / escalated), `created_at`, `resolved_at` |
| `AuditEvent` | `id`, `session_id` (FK), `case_id` (nullable FK), `event_type` (enum), `actor` ("system" / "human"), `timestamp`, `details` (JSON) — **append-only, never updated** |
| `BreakResolution` | `id`, `case_id` (FK), `root_cause`, `severity` (high / medium / low), `draft_broker_email`, `human_approved` (bool), `reviewer_notes`, `approved_at` |
| `EvidencePackage` | `id`, `case_id` (FK), `sources` (JSON: list of `{source_name, source_type, data, corroborates_firm}`), `corroboration_summary` |
| `EscalationRecord` | `id`, `case_id` (FK), `draft_email`, `urgency` (high / medium / low), `human_approved` (bool), `reviewer_notes`, `approved_at`, `dispatched` (bool, default False) |

**`AuditEvent.event_type` enum values:**
`session_started` · `extracted` · `hitl1_approved` · `reconciled` · `case_created` · `affirmed` · `resolution_drafted` · `resolution_approved` · `evidence_compiled` · `escalation_drafted` · `escalation_approved` · `completed`

**State fields to add in `graph/state.py`:**
```python
cases: list[dict]                       # serialised Case records from case_router
affirmation_pending: bool
affirmation_decisions: dict             # {case_id: "affirm"|"request_resolution"|"reject"|"flag_booking"|"escalate_tsg"}
has_breaks: bool
has_ghosts: bool
resolution_results: list[dict]
break_review_pending: bool
break_review_decisions: dict            # {case_id: {approved: bool, reviewer_notes: str, edited_email: str}}
evidence_packages: list[dict]
escalation_drafts: list[dict]
escalation_pending: bool
escalation_decisions: dict              # {case_id: {approved: bool, reviewer_notes: str}}
```

**`PipelineStatus` enum additions:**
`pending_affirmation` · `resolving_breaks` · `pending_break_review` · `compiling_evidence` · `escalating` · `pending_escalation_review`

**Deliverables:** Updated `db/models.py`, `graph/state.py`, schema migration (drop-and-recreate acceptable for SQLite POC).

---

### Phase 2 — HITL Gate 2: Case Routing + Affirmation

**New files / changes:**

- **`agents/case_router_agent.py`** (new): Iterates over all `ReconciliationResult` rows for the session; creates one `Case` row per trade; sets `has_breaks=True` if any MISMATCH cases exist; sets `has_ghosts=True` if any NEW (broker-only) cases exist; logs `AuditEvent(case_created)` per case and `AuditEvent(reconciled)` for the session.

- **`graph/nodes.py`**: Add `case_router_node` (deterministic, no LLM) + `affirmation_gate_node` (sets `status=pending_affirmation`, logs, pauses via `interrupt_before`).

- **`graph/workflow.py`**: Insert `case_router_node` between `reconcile_node` and `generate_node`; insert `affirmation_gate_node` after it; add `affirmation_gate_node` to `interrupt_before`; add `route_post_affirmation` conditional edge (has_breaks → `resolution_node`, else → `generate_node`).

- **`backend/api/routes/pipeline.py`**: New endpoint `POST /api/pipeline/affirm`:
  ```json
  { "session_id": "...", "thread_id": "...", "affirmation_decisions": {"case_id": "affirm"|"request_resolution"|...} }
  ```
  Updates state → resumes graph.

- **`ui/app.py`**: New **Affirmation Screen** (shown when `status=pending_affirmation`):
  - 4-section reconciliation grid: **Matched** (Affirm button), **Mismatches** (Request Resolution per break or Accept as-is), **New / Ghost** (Reject / Flag for Booking / Escalate to TSG), **Missing** (acknowledge).
  - Submit button calls `POST /api/pipeline/affirm`.

---

### Phase 3 — Resolution Agent + HITL Gate 3

**New files / changes:**

- **`agents/resolution_agent.py`** (new):
  - Input: list of MISMATCH cases with `broker_trade`, `ms_trade`, `differences` dict, `confidence_score`.
  - LLM prompt (single batch call for all breaks in the session):
    - Classify break type (partial fill / booking error / price source discrepancy / direction mismatch / other).
    - Rate severity: `HIGH` if both quantity and price affected; `MEDIUM` if a single critical field; `LOW` if brokerage-only.
    - Draft a professional email to the broker citing specific field discrepancies and requesting an amended recap.
  - Writes `BreakResolution` rows to DB; logs `AuditEvent(resolution_drafted)`.

- **`graph/nodes.py`**: Add `resolution_node` + `break_review_gate_node` (sets `status=pending_break_review`, interrupts).

- **`graph/workflow.py`**: Wire `resolution_node → break_review_gate_node → evidence_node`.

- **`backend/api/routes/pipeline.py`**: New endpoint `POST /api/pipeline/approve-resolution`:
  ```json
  { "session_id": "...", "thread_id": "...", "break_review_decisions": {"case_id": {"approved": true, "reviewer_notes": "...", "edited_email": "..."}} }
  ```
  Sets `BreakResolution.human_approved=True`, `approved_at=now`; logs `AuditEvent(resolution_approved)`.

- **`ui/app.py`**: New **Break Review Screen** (shown when `status=pending_break_review`):
  - Per-break card: break type label, severity badge (colour-coded: red / amber / yellow), per-field diff table, editable email draft text area.
  - Approve All button + individual approve / reject per break.

---

### Phase 4 — Evidence Agent + Escalation Agent + HITL Gate 4

**New files / changes:**

- **`agents/evidence_agent.py`** (new):
  - Compiles `EvidencePackage` per approved break case from 5 sources:
    1. **Extracted trade fields** — from the broker PDF/Excel (source of truth for what the broker claimed).
    2. **MS trade snapshot** — the internal booking record from `ReconciliationResult.ms_trade_snapshot`.
    3. **Reconciliation diff** — the per-field delta dict (`differences`).
    4. **Broker template / extraction metadata** — extraction method, confidence, template version.
    5. **Future sources** — placeholder stubs: `{source_name: "Bloomberg Execution", corroborates_firm: null, data: "pending_integration"}` (visible to user as "not yet connected").
  - Each source includes `corroborates_firm: bool` to indicate whether it supports the firm's position.
  - Writes `EvidencePackage` to DB; logs `AuditEvent(evidence_compiled)`.

- **`agents/escalation_agent.py`** (new):
  - Input: `EvidencePackage` + `BreakResolution` + session metadata.
  - LLM drafts a formal email to the Trade Support Group with: case reference, break summary table, evidence source list, urgency rating, recommended next steps (e.g., "Await amended recap by EOD; escalate to senior ops if no response by T+1").
  - Writes `EscalationRecord(dispatched=False)` to DB; logs `AuditEvent(escalation_drafted)`.

- **`graph/nodes.py`**: Add `evidence_node`, `escalation_node`, `escalation_gate_node` (interrupts).

- **`graph/workflow.py`**: Wire `break_review_gate → evidence_node → escalation_node → escalation_gate → generate_node → persist_node`.

- **`backend/api/routes/pipeline.py`**: New endpoint `POST /api/pipeline/approve-escalation`:
  ```json
  { "session_id": "...", "thread_id": "...", "escalation_decisions": {"case_id": {"approved": true, "reviewer_notes": "..."}} }
  ```
  Sets `EscalationRecord.human_approved=True`; logs `AuditEvent(escalation_approved)`. Email is **saved but not dispatched** — dispatching wired in Phase 7.

- **`ui/app.py`**: New **Escalation Review Screen** (shown when `status=pending_escalation_review`):
  - Per-break accordion: evidence sources list with `corroborates_firm` indicators, editable escalation email draft.
  - "Approve for Dispatch" button with note: *"Email will be sent once email integration is enabled (Phase 7)."*

---

### Phase 5 — Audit Trail + UI Hardening

**Changes:**

1. Ensure `AuditEvent` is logged in all nodes: existing (`verify_node`, `extract_node`, `hitl_gate_node`, `reconcile_node`) + all new nodes.
2. Capture `reviewer_notes` on every human approval action and write to `AuditEvent.details`.
3. New endpoints in `backend/api/routes/status.py`:
   - `GET /api/cases/{session_id}` — returns all Case rows with status + linked trade summary.
   - `GET /api/audit/{session_id}` — returns chronological `AuditEvent` log for a session.
4. UI additions in `ui/app.py`:
   - **Audit Trail tab** on the Results page: chronological event log with actor (system / human), event type, timestamp, and expandable details JSON.
   - **Cases Summary panel** on Results page: case status breakdown with icons (✅ affirmed, 🔴 in_dispute, 📋 pending_booking, ⬆️ escalated).
5. Auto-approval config flag in `config.py` — `AUTO_AFFIRM_PERFECT_MATCH: bool` (default `False`). When `True`, sessions where all trades are MATCH with `confidence=4` skip Gate 2 and auto-affirm. Aligns with SettleAI "30 seconds of human time" happy path.

---

### Phase 6 — Durable Session Persistence (SqliteSaver)

**Changes:**

1. Replace `MemorySaver` with `SqliteSaver` from `langgraph.checkpoint.sqlite` in `graph/workflow.py`.
2. Add `CHECKPOINT_DB_PATH` to `config.py` / `dev.yaml` (can share the main SQLite file or use a separate `checkpoints.db`).
3. Test resume-after-restart: kill API mid-session at each gate, restart, verify graph resumes from correct interrupt point.

---

### Phase 7 — Email Integration (Deferred)

> **Explicitly deferred.** Keep in plan; do not implement until all prior phases are complete and tested.

1. **`services/mailbox_service.py`** — IMAP / Exchange mailbox polling service; auto-triggers pipeline on receipt of broker email (email body + attachments routed as upload).
2. **`services/email_sender.py`** — SMTP / Exchange outbound email dispatch.
3. Wire `EscalationRecord.dispatched = True` after Phase 4 approval + actual send in Phase 7.
4. Wire `BreakResolution.draft_broker_email` send to broker after Phase 3 approval.
5. Config additions in `config.py` and `dev.yaml`: mailbox credentials (env var), SMTP settings.

---

## 4. File Change Matrix

| File | Change Type | Phase |
|---|---|---|
| `db/models.py` | Add 5 models: Case, AuditEvent, BreakResolution, EvidencePackage, EscalationRecord | 1 |
| `db/database.py` | Schema migration / recreate | 1 |
| `graph/state.py` | Add 11 new state fields; extend PipelineStatus enum | 1 |
| `schemas/canonical_trade.py` | Add Pydantic models for new entities | 1 |
| `agents/case_router_agent.py` | **NEW** — deterministic case creation | 2 |
| `graph/nodes.py` | Add case_router, affirmation_gate | 2 |
| `graph/workflow.py` | Insert new nodes; extend interrupt_before; add conditional edge | 2 |
| `backend/api/routes/pipeline.py` | Add `POST /affirm` endpoint | 2 |
| `ui/app.py` | Add Affirmation Screen | 2 |
| `agents/resolution_agent.py` | **NEW** — LLM root cause + email draft | 3 |
| `graph/nodes.py` | Add resolution_node, break_review_gate | 3 |
| `graph/workflow.py` | Wire resolution path | 3 |
| `backend/api/routes/pipeline.py` | Add `POST /approve-resolution` endpoint | 3 |
| `ui/app.py` | Add Break Review Screen | 3 |
| `agents/evidence_agent.py` | **NEW** — evidence package compilation | 4 |
| `agents/escalation_agent.py` | **NEW** — LLM escalation email draft | 4 |
| `graph/nodes.py` | Add evidence_node, escalation_node, escalation_gate | 4 |
| `graph/workflow.py` | Wire evidence + escalation path | 4 |
| `backend/api/routes/pipeline.py` | Add `POST /approve-escalation` endpoint | 4 |
| `ui/app.py` | Add Escalation Review Screen | 4 |
| `graph/nodes.py` | Add AuditEvent logging to all nodes | 5 |
| `backend/api/routes/status.py` | Add `GET /cases/{session_id}`, `GET /audit/{session_id}` | 5 |
| `ui/app.py` | Add Audit Trail tab + Cases Summary panel | 5 |
| `config.py` | Add `AUTO_AFFIRM_PERFECT_MATCH` flag | 5 |
| `graph/workflow.py` | Replace MemorySaver → SqliteSaver | 6 |
| `config.py` / `dev.yaml` | Add `CHECKPOINT_DB_PATH` | 6 |
| `services/mailbox_service.py` | **NEW** — IMAP mailbox polling | 7 |
| `services/email_sender.py` | **NEW** — SMTP outbound dispatch | 7 |
| `config.py` / `dev.yaml` | Mailbox / SMTP config | 7 |

---

## 5. Key Architectural Decisions

| Decision | Rationale |
|---|---|
| Keep LangGraph as orchestration backbone | All sequencing, routing, and HITL interrupts stay in the graph; FastAPI routes remain thin |
| All HITL gates are **batched** (session-level, not per-trade) | Matches real ops workflow: ops reviews the full invoice at each gate, not trade by trade |
| Resolution + escalation are **bulk LLM calls** | One prompt for all breaks in a session; per-break results returned as list — cheaper and faster than per-trade calls |
| Email drafts **saved but not sent** (Phases 3–4) | Dispatch logic wired only in Phase 7; no partial email integration |
| One session = one reconciliation run | Cases are sub-entities of a session; no cross-session case merging in POC |
| Matching Agent in SettleAI terminology = `reconcile_node` | No separate OMS API integration needed for POC; MS Excel file = OMS |
| `AuditEvent` is **append-only** | Never UPDATE audit rows; insert only. Immutability is required for regulatory-grade audit trail |
| `AUTO_AFFIRM_PERFECT_MATCH` is **off by default** | Happy path auto-affirm (SettleAI demo: "30 seconds human time") is opt-in — ops team decides when to enable |
| Ghost trade "manually book" is a **no-op stub** | Captures human intent (`Case.status=pending_booking`) without writing to OMS; full OMS write deferred post-POC |

---

## 6. Verification Checklist

- [ ] **Happy Path**: Upload matching broker file → Gate 2 shows all Matched → click Affirm → Excel generated → audit log has `session_started`, `extracted`, `hitl1_approved`, `reconciled`, `case_created` (×N), `affirmed`, `completed`
- [ ] **Break Path**: Upload file with qty/price discrepancy → Gate 2 shows break → request resolution → Gate 3 shows root cause + email → approve → Gate 4 shows evidence + escalation email → approve → completed; `BreakResolution.human_approved=True` in DB
- [ ] **Ghost Trade**: Session with no MS match → Gate 2 shows ghost decision options → select "Escalate to TSG" → `Case.status=escalated` in DB
- [ ] **Reject at Gate 2**: Click reject on a break → `Case.status=in_dispute`; pipeline still completes for non-disputed trades
- [ ] **Audit Trail endpoint**: `GET /api/audit/{session_id}` returns chronological log with actor, event_type, timestamp, details
- [ ] **Phase 6 — Restart resilience**: Kill API mid-session at Gate 3 → restart → hit `/api/pipeline/state/{session_id}` → verify `status=pending_break_review` restored; resume from Gate 3 succeeds
- [ ] **Auto-affirm flag**: Set `AUTO_AFFIRM_PERFECT_MATCH=true` in config → run clean match session → verify Gate 2 skipped, `AuditEvent(affirmed, actor=system)` logged
