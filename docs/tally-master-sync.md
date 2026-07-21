# Tally ↔ Bill Munshi — Master Sync Contract

> Version: **1.0** · Last updated: **2026-07-21**
> Audience: **Tally TDL/TCP developer**

This doc explains the endpoints your TDL script must call so that
masters (Vendors, Purchase / Expense Ledgers, Stock Items) created by
users inside Bill Munshi (BM) reach Tally, and how BM confirms the
sync.

**Non-goal:** changing any existing XML payload. The bill sync XML
you already consume is **unchanged** — this doc only adds two new
endpoints and describes when to call them.

---

## Why this exists

Until now, all Tally masters (Vendors, Ledgers, Stock Items) were
created **on the Tally side first**, then imported into BM via your
existing bulk endpoints (`/ledgers/`, `/parent-ledgers/`,
`/masters/`).

Users have asked to **create masters directly from Bill Munshi** while
filling out a bill — e.g., "this invoice is from a new vendor I don't
have in Tally yet." Those BM-created rows must be pushed to Tally
**before** the bill's sync XML lands, otherwise Tally will reject
the voucher with `Ledger XYZ not found`.

The two new endpoints below give your TDL a clean pull/callback loop
to handle this.

---

## The flow (Option C — backend orchestrated)

```
┌──────────────────────┐          ┌──────────────────────┐          ┌──────────────────────┐
│  User (BM UI)        │          │  Bill Munshi Backend │          │  Tally TCP / TDL     │
└──────────────────────┘          └──────────────────────┘          └──────────────────────┘
        │                                    │                                    │
        │ 1. "+ Add New Vendor"              │                                    │
        │ (form submit)                      │                                    │
        │ ──────────────────────────────────►│                                    │
        │                                    │                                    │
        │                                    │ Row saved:                         │
        │                                    │   source=billmunshi                │
        │                                    │   tally_synced=False               │
        │                                    │                                    │
        │ 2. Selects vendor, clicks "Sync"   │                                    │
        │ ──────────────────────────────────►│                                    │
        │                                    │                                    │
        │        409 WAITING_FOR_MASTERS     │                                    │
        │ ◄──────────────────────────────────│                                    │
        │                                    │                                    │
        │  (UI: "⏳ Waiting for Tally…")     │                                    │
        │                                    │                                    │
        │                                    │   3. GET /masters/pending_sync/    │
        │                                    │ ◄──────────────────────────────────│
        │                                    │                                    │
        │                                    │   returns {parent_ledgers, ledgers,│
        │                                    │            items}                  │
        │                                    │ ──────────────────────────────────►│
        │                                    │                                    │
        │                                    │           TDL creates each in Tally│
        │                                    │           (existing LEDGER / STOCK-│
        │                                    │            ITEM creation code)     │
        │                                    │                                    │
        │                                    │   4. POST /masters/mark_synced/    │
        │                                    │      {"results": [{type, id,       │
        │                                    │         status:"ok",               │
        │                                    │         master_id, alter_id}, …]}  │
        │                                    │ ◄──────────────────────────────────│
        │                                    │                                    │
        │                                    │ Rows flipped to tally_synced=True  │
        │                                    │                                    │
        │ 5. Retry "Sync" (auto poll)        │                                    │
        │ ──────────────────────────────────►│                                    │
        │        200 OK — bill XML returned  │                                    │
        │ ◄──────────────────────────────────│                                    │
        │                                    │ ──────────────────────────────────►│
        │                                    │              (unchanged bill sync) │
```

**Key invariants:**

- Bill sync XML shape is **identical** to what it was before. Zero
  changes to the `<voucher_type>`, `<ledgers>`, `<items>` structure
  or their fields.
- The 409 response is the **only** way BM tells you a bill is
  blocked on missing masters. The UI polls the sync endpoint on
  15-second intervals until you've marked the masters synced.
- Auth for the new endpoints uses the same
  `Authorization: Api-Key <org-key>` header your TDL already uses for
  `/masters/` and the bill sync endpoints.

---

## Endpoint 1 — Pull pending masters

**Request**

```
GET /api/v1/tally/org/{org_id}/masters/pending_sync/
Authorization: Api-Key <org-api-key>
```

**Response — 200 OK**

```json
{
  "organization": {
    "id": "83a77d8e-524e-41a6-b6b6-af3d16fcc6c3",
    "name": "Spectrum Poly Pack"
  },
  "parent_ledgers": [
    {
      "id": "0c9c1c1a-...",
      "name": "Bank Accounts",
      "created_at": "2026-07-21T10:15:00+00:00"
    }
  ],
  "ledgers": [
    {
      "id": "9b3fa2d6-...",
      "name": "ABC Enterprises",
      "parent": "Sundry Creditors",
      "alias": "",
      "gst_in": "07AAVFA5574E1Z4",
      "opening_balance": 0.0,
      "created_at": "2026-07-21T10:16:32+00:00"
    },
    {
      "id": "d0e4ffaa-...",
      "name": "Freight Charges",
      "parent": "Indirect Expenses",
      "alias": "",
      "gst_in": "",
      "opening_balance": 0.0,
      "created_at": "2026-07-21T10:18:04+00:00"
    }
  ],
  "items": [
    {
      "id": "45a8bb10-...",
      "name": "Cable 10m",
      "unit": "Nos",
      "gst_rate": "18%",
      "hsn_code": "8544",
      "parent": "Primary",
      "alias": "",
      "item_code": "",
      "created_at": "2026-07-21T10:19:11+00:00"
    }
  ],
  "counts": {
    "parent_ledgers": 1,
    "ledgers": 2,
    "items": 1
  }
}
```

**TDL responsibilities**

1. Call this endpoint on a schedule — **~30-second cadence** is
   enough for most orgs. Also call it on demand right before you
   process a bill sync response.
2. If `counts` is all-zero, do nothing.
3. Otherwise create records in Tally in this order (parents first,
   then children):
   - **parent_ledgers** — create each as a Tally Group (Under: user's
     default primary group; recommend the "Primary" of the same
     nature they specified). Capture `MASTER_ID` and `ALTER_ID`.
   - **ledgers** — create each as a Tally Ledger. `Under` = the
     ledger's `parent` value (which is already an existing Tally
     group name — either legacy or one you just created above).
     `GST Registration` = `Regular` if `gst_in` is non-empty. Capture
     `MASTER_ID`, `ALTER_ID`.
   - **items** — create each as a Tally Stock Item. `Under` = `parent`
     (typically `"Primary"`). Unit, GST rate and HSN code should
     populate the standard stock-item fields. Capture `MASTER_ID`,
     `ALTER_ID`.
4. Batch the results and call `/masters/mark_synced/` below.

**Error handling on your side**

- If Tally rejects a record (duplicate name, missing group, invalid
  GSTIN, etc.), still report it back — with `status: "error"` and a
  human `message`. BM keeps the row unsynced and shows the message
  to the user so they can fix it.
- Retry logic: BM does not require you to retry; if a record was
  skipped due to a transient Tally issue, it will simply be returned
  on the next poll (as long as `status: "ok"` was not sent).

---

## Endpoint 2 — Mark records synced (callback)

**Request**

```
POST /api/v1/tally/org/{org_id}/masters/mark_synced/
Authorization: Api-Key <org-api-key>
Content-Type: application/json

{
  "results": [
    {
      "type": "parent_ledger",
      "id": "0c9c1c1a-...",
      "status": "ok",
      "master_id": "1001",
      "alter_id": "1"
    },
    {
      "type": "ledger",
      "id": "9b3fa2d6-...",
      "status": "ok",
      "master_id": "1245",
      "alter_id": "1"
    },
    {
      "type": "item",
      "id": "45a8bb10-...",
      "status": "error",
      "message": "Duplicate stock item name: Cable 10m already exists in Tally"
    }
  ]
}
```

**Field rules**

| Field       | Values                                       | Notes                                                    |
|-------------|----------------------------------------------|----------------------------------------------------------|
| `type`      | `parent_ledger` \| `ledger` \| `item`        | Required. Case-insensitive.                              |
| `id`        | UUID from the pending-sync response          | Required. Must match a row in the org.                   |
| `status`    | `ok` \| `error`                              | Required.                                                |
| `master_id` | Tally's `MASTER_ID` for this record          | Required when `status=ok`. Recommended for reconciliation. |
| `alter_id`  | Tally's `ALTER_ID`                           | Optional. Stored if provided.                            |
| `message`   | Freetext                                     | Required when `status=error`. Surfaced to the BM user.   |

**Response — 200 OK**

```json
{
  "summary": {"ok": 2, "error": 1, "not_found": 0},
  "message": "Processed 3 result(s): 2 ok, 1 error, 0 unknown."
}
```

- `ok` count → rows flipped to `tally_synced=True` and `master_id`
  persisted.
- `error` count → rows kept unsynced, `tally_sync_message` stored.
- `not_found` count → entries whose `type`/`id` couldn't be resolved
  (indicates a bug in your batching or a stale ID). Safe to ignore
  after logging.

You can send **partial** batches — no need to hold results back
waiting for the whole batch. If a single ledger takes longer than the
others, POST it alone.

---

## Existing bill-sync endpoints — new 409 response

The three bill sync endpoints your TDL polls (Purchase / Journal /
Payment) can now return **409 Conflict** with a `MASTERS_PENDING`
error code:

```
POST /api/v1/tally/org/{org_id}/{vendor-bills|expense-bills|payment-vouchers}/sync/
Content-Type: application/json

{"bill_id": "..."}
```

**409 response body:**

```json
{
  "error": "WAITING_FOR_MASTERS",
  "error_code": "MASTERS_PENDING",
  "message": "This bill references 2 master records that Tally hasn't imported yet. …",
  "pending_masters": [
    {
      "type": "ledger",
      "id": "9b3fa2d6-...",
      "name": "ABC Enterprises",
      "parent": "Sundry Creditors",
      "role": "vendor",
      "message": "Waiting for Tally to create this ledger."
    }
  ]
}
```

**What your TDL should do on 409:**

- The BM frontend will handle this by polling the sync endpoint every
  15s until you've marked the pending masters synced.
- Your TDL doesn't need to react to the 409 directly — its job is
  just to keep polling `/masters/pending_sync/` and calling
  `mark_synced` promptly.

**200 response** (bill actually synced) — **unchanged from today**.
Same XML shape, same fields.

---

## Existing endpoints — unchanged reference

| Endpoint                                                          | Method | Purpose                                              |
|-------------------------------------------------------------------|--------|------------------------------------------------------|
| `/api/v1/tally/org/{org_id}/ledgers/`                             | GET/POST | Existing bulk ledger import from Tally.              |
| `/api/v1/tally/org/{org_id}/parent-ledgers/`                      | GET/POST | Existing parent-ledger import.                       |
| `/api/v1/tally/org/{org_id}/masters/`                             | GET/POST | Existing STOCKITEM import.                           |
| `/api/v1/tally/org/{org_id}/vendor-bills/sync/`                   | POST     | Purchase Voucher bill sync (XML unchanged).          |
| `/api/v1/tally/org/{org_id}/expense-bills/sync/`                  | POST     | Journal Entry bill sync (XML unchanged).             |
| `/api/v1/tally/org/{org_id}/payment-vouchers/sync/`               | POST     | Payment Voucher bill sync (XML unchanged).           |
| `/api/v1/tally/org/{org_id}/bills/tally_status/`                  | POST     | Existing bill-status callback.                       |

**Nothing in this table changed.**

---

## Sample XML — for reference (unchanged)

Purchase voucher sync response — same shape as today:

```xml
<data>
  <bill_no>INV-001</bill_no>
  <bill_date>21-07-2026</bill_date>
  <voucher_type>Purchase</voucher_type>
  <vendor_name>ABC Enterprises</vendor_name>
  <company>Spectrum Poly Pack</company>
  <total_amount>57386.94</total_amount>
  <notes>AI Analysed Purchase Bill</notes>
  <ledgers>
    <ledger>
      <amount>48633.00</amount>
      <ledger>Purchase A/c</ledger>
      <debit_or_credit>debit</debit_or_credit>
    </ledger>
    <ledger>
      <amount>8753.94</amount>
      <ledger>IGST (ITC) @18%</ledger>
      <rate>18%</rate>
      <debit_or_credit>debit</debit_or_credit>
    </ledger>
    <ledger>
      <amount>57386.94</amount>
      <ledger>ABC Enterprises</ledger>
      <debit_or_credit>credit</debit_or_credit>
    </ledger>
  </ledgers>
</data>
```

**No `<inline_masters>` block, no new tags.** The vendor named
`ABC Enterprises` above must already exist in Tally by the time this
XML lands — that's precisely what `/masters/pending_sync/` +
`/masters/mark_synced/` ensure.

---

## Recommended TDL schedule

| Task                                    | Cadence           |
|-----------------------------------------|-------------------|
| `GET /masters/pending_sync/`            | Every ~30 seconds |
| `POST /masters/mark_synced/`            | Immediately after processing a batch (or one-by-one) |
| Existing bill-sync polling              | Unchanged         |

---

## Questions / edge cases

- **What if the user creates a vendor named the same as an existing
  Tally ledger?** BM's create endpoint rejects duplicates within its
  own scope (409). If Tally-side rejects your create as a duplicate,
  return `status: "error"` with the message — the BM user will see it
  and can rename or use the existing ledger.

- **Do I need to sync `parent_ledger` even if the parent already
  exists in Tally?** The pending-sync response only includes parents
  that BM created new. Parents that were imported from Tally are
  already in sync and won't appear. If a returned parent name matches
  an existing Tally group, treat it as a no-op success (`status: "ok"`
  with the existing group's `master_id`).

- **Can I sync a bill BEFORE its masters?** No. BM's sync endpoint
  will return 409 until all referenced masters are marked synced.

- **What happens if `mark_synced` is called with a stale ID?** BM
  returns `not_found` in the summary and does nothing to the DB. Safe
  no-op.

---

## Contact

For clarifications, ping the BillMunshi backend team. This document
lives at `backend/docs/tally-master-sync.md` in the BM repo — always
prefer the version in the repo over any forwarded copy.
