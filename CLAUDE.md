# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Install dependencies:**
```bash
pip install -e ".[test]"
```

**Run the server:**
```bash
python pallet_pro_core.py
# or with a custom DB path:
PALLET_PRO_DB=my.db python pallet_pro_core.py
```
Server starts on `0.0.0.0:8000`.

**Run all tests:**
```bash
pytest
```

**Run a single test:**
```bash
pytest tests/test_smoke.py::PalletProSmokeTests::test_health_endpoint
```

## Architecture

This is a single-process Flask REST API backed by SQLite. Everything runs in `pallet_pro_core.py` (~12,700 lines), which registers all routes directly on the `app` Flask instance. Three modules handle domain-specific concerns:

| File | Role |
|---|---|
| `pallet_pro_core.py` | Main app: all routes, `init_db()`, core business logic |
| `db.py` | `get_conn()`, `make_id(prefix)`, `now_iso()` |
| `audit.py` | `audit_event()` — write a row to `audit_events` |
| `modules/system_routes.py` | `/health`, `/constitution`, `/api/system/status` |
| `modules/subscription_access.py` | Subscription guard, pricing plans, temporary users, billing |
| `modules/transaction_reporting.py` | Transaction summary and CSV export routes |

**Database** is controlled by the `PALLET_PRO_DB` environment variable (default: `pallet_pro.db`). Schema is created at startup via `init_db()` using `CREATE TABLE IF NOT EXISTS`. Schema migrations are done inline by checking `PRAGMA table_info()` and executing `ALTER TABLE ... ADD COLUMN` for any missing columns — there is no migration framework.

**ID format:** all entity IDs are `make_id("prefix")` → `prefix_<12-char hex>` (e.g. `org_`, `txn_`, `led_`, `audit_`, `pend_`).

**Timestamps:** always use `now_iso()`, which returns a UTC ISO string without timezone suffix.

## Core Invariants (the Constitution)

The laws in `modules/system_routes.py::PALLET_PRO_CONSTITUTION` are hard constraints. The most important ones for code changes:

- **LAW-001/LAW-002:** Transactions are append-only. Never edit or delete a posted transaction — create a linked correction transaction instead.
- **LAW-003:** Every write that matters must call `audit_event()` within the same DB connection before commit.
- **LAW-004:** All queries must scope to `organisation_id`. One org must never see another org's data except through the approved shared-transaction workflow.
- **LAW-007:** When a user action is blocked (e.g. by subscription state), save it to `pending_approval_entries` rather than silently discarding it.

## Transaction and Ledger Flow

1. A transaction row is created with `status = PENDING` or `AWAITING_APPROVAL`.
2. `post_transaction_to_ledger(conn, txn)` posts it: writes a `ledger_entries` row, upserts `balance_projection`, and sets `transactions.status = POSTED`.
3. `balance_projection` is the live stock position (depot × resource). It is derived from ledger entries and must stay consistent with them.

## Subscription / Access Guard

`modules/subscription_access.py` owns the `require_active_org_access` decorator and the `classify_org_access_state()` function. Subscription state drives whether org operations are allowed or pushed to `pending_approval_entries`. Temporary users get 28 days of access from activation.

## Tests

Tests use the Flask test client and a real SQLite database (`test_pallet_pro.db`) that is created and deleted per test class. The test suite re-imports all modules fresh each run to pick up the `PALLET_PRO_DB` env override. There is no mocking of the database layer.
