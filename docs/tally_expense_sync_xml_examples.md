# Tally Expense Bill Sync — XML Payload Reference

This document is the **contract between BillMunshi and the Tally TDL/TCP
connector** for the **expense-bill** sync endpoint.

```
GET  https://billmunshi.com/api/v1/tally/org/<ORG_ID>/expense-bills/sync/
Content-Type: application/xml; charset=utf-8
```

The endpoint always returns a `<data>` root containing zero or more
`<bill>` elements. The Tally TDL must iterate `<bill>` elements.

> **Difference from vendor bill:** expense bills are posted as **Journal
> vouchers** in Tally (not Purchase vouchers), so each `<ledger>` and
> `<item>` carries an explicit `<debit_or_credit>` direction. There is no
> implicit "vendor is the credit side" rule — every ledger posting,
> including the vendor itself, is stated explicitly.

---

## Field reference

### Top-level `<bill>` elements (always present)

| Tag | Meaning | Example |
|---|---|---|
| `<bill_no>` | Vendor's printed invoice number | `EXP/2025-26/0042` |
| `<bill_date>` | Invoice date in `DD-MM-YYYY` | `22-12-2025` |
| `<voucher_type>` | **Always `Journal`** for expense bills (vendor bill omits this and defaults to Purchase) | `Journal` |
| `<vendor>` | Vendor / payee ledger name | `SUNDARAM PROPERTIES` |
| `<company>` | Tally company name to post into | `Spectrum Poly Pack and Packaging` |
| `<total_amount>` | Bill grand total — always 2 decimals | `54000.00` |
| `<notes>` | Free-text narration; includes BillMunshi back-link | `Bill from … entered via BillMunshi https://…` |
| `<ledgers>` | Flat list of ledger postings (see below) | — |
| `<items>` | Expense line items (journal-entry style, no qty/price) | — |

### `<ledgers>` block

A single flat collection. **Every** posting that hits a Tally ledger
lives here — vendor, CGST, SGST, IGST, TDS, other adjustment, round-off.
The Tally TDL iterates `<ledger>` children and posts each as its own
voucher line; the ledger name on the Tally master classifies it.

Children of every `<ledger>` entry:

| Child | Meaning |
|---|---|
| `<amount>` | 2-decimal amount. Positive normally; **negative allowed only on Round Off** when the vendor took less than computed. |
| `<ledger>` | Tally ledger name to debit/credit. Must match a ledger that already exists in the Tally company. |
| `<debit_or_credit>` | **Required.** Either `debit` or `credit`. Defaults to `debit` if missing (defensive only — backend always emits it). |
| `<rate>` | *Currently not emitted on expense bills.* (Vendor bill emits it on GST entries for cross-check. Expense bill GST is a single bill-level amount, no per-rate breakdown.) |

Emit rules:

- **Zero-amount entries are dropped entirely.** A bill with no TDS has
  no TDS-named `<ledger>` entry. Tally must not synthesize one.
- **Entries with a missing ledger FK are also dropped.** If `cgst_taxes`
  isn't set, the CGST entry doesn't appear even if `cgst > 0` (the
  amount can't be posted without a ledger to post to).
- **CGST + SGST and IGST are mutually exclusive** (intrastate vs
  interstate). The frontend enforces this; backend just emits whatever
  is present.
- The **vendor** itself is one of the `<ledger>` entries (typically
  with `<debit_or_credit>credit</debit_or_credit>`), because a journal
  voucher needs every line stated. Vendor bill (Purchase voucher) omits
  this since the vendor is the implicit balancing party.

### `<items>` block

Each `<item>` is a journal-style line — books an amount against a
chart-of-accounts ledger with explicit DR/CR. **No price / quantity** —
expense bills are flat-amount entries (rent, fees, utilities, etc.).

| Child | Meaning |
|---|---|
| `<details>` | Free-text description from the bill (e.g. `Office rent — November`) |
| `<expense_ledger>` | Chart-of-accounts ledger to book the amount against (e.g. `RENT EXPENSE`). *Vendor bill uses `<purchase_ledger>` — different name to reflect the different domain.* |
| `<amount>` | Line amount, 2 decimals |
| `<debit_or_credit>` | `debit` or `credit` (typically `debit` for the expense, but reverse-charge scenarios can flip it). |

---

## Scenario 1 — Simple expense, no GST (e.g. cash payment, exempt vendor)

A flat ₹5,000 stationery bill from a composition vendor.

**Math:**
- Expense line: 5000 (DR)
- Vendor: 5000 (CR)
- Net: balanced

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>SP/2025-26/0011</bill_no>
    <bill_date>20-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor>STAR STATIONERY</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>5000.00</total_amount>
    <notes>Bill from STAR STATIONERY entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>5000.00</amount>
        <ledger>STAR STATIONERY</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>Office stationery — pens, files, sticky notes</details>
        <expense_ledger>OFFICE EXPENSES</expense_ledger>
        <amount>5000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 2 — Intrastate expense with GST (CGST + SGST)

Internet bill ₹2,000 base + 18% GST = ₹2,360. Vendor is in the same state.

**Math:**
- Internet expense: 2000 (DR)
- CGST @ 9%: 180 (DR) → input credit
- SGST @ 9%: 180 (DR) → input credit
- Vendor (TELCO): 2360 (CR)
- Net: balanced

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>TELCO/12-25/9821</bill_no>
    <bill_date>21-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor>BHARAT TELCO LTD</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>2360.00</total_amount>
    <notes>Bill from BHARAT TELCO LTD entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>2360.00</amount>
        <ledger>BHARAT TELCO LTD</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>180.00</amount>
        <ledger>CGST (ITC) @ 9%</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>180.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>Office internet — December broadband</details>
        <expense_ledger>INTERNET CHARGES</expense_ledger>
        <amount>2000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 3 — Interstate expense with IGST

Same internet bill but vendor is in another state — CGST/SGST replaced
by a single IGST entry.

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>TELCO/12-25/9822</bill_no>
    <bill_date>21-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor>BHARAT TELCO LTD</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>2360.00</total_amount>
    <notes>Bill from BHARAT TELCO LTD entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>2360.00</amount>
        <ledger>BHARAT TELCO LTD</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>360.00</amount>
        <ledger>IGST (ITC) @ 18%</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>Office internet — December broadband</details>
        <expense_ledger>INTERNET CHARGES</expense_ledger>
        <amount>2000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 4 — Rent with GST and TDS

Office rent ₹50,000 + 18% GST. TDS @ 10% on the **base** rent (₹5,000)
is deducted at source, so the actual cash paid to the landlord is
54,000 (50000 + 9000 GST − 5000 TDS).

**Math:**
- Rent expense: 50000 (DR)
- CGST @ 9%: 4500 (DR) → input credit
- SGST @ 9%: 4500 (DR) → input credit
- TDS payable: 5000 (CR) → liability to the govt
- Vendor (landlord): 54000 (CR)
- Net: 50000+4500+4500 = 59000 DR vs 5000+54000 = 59000 CR — balanced

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>EXP/2025-26/0042</bill_no>
    <bill_date>22-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor>SUNDARAM PROPERTIES</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>54000.00</total_amount>
    <notes>Bill from SUNDARAM PROPERTIES entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>54000.00</amount>
        <ledger>SUNDARAM PROPERTIES</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>4500.00</amount>
        <ledger>CGST (ITC) @ 9%</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>4500.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>5000.00</amount>
        <ledger>TDS on Rent @ 10%</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>Office rent — November 2025</details>
        <expense_ledger>RENT EXPENSE</expense_ledger>
        <amount>50000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 5 — Multiple expense lines (one bill, several heads)

Travel reimbursement booking flights + hotels + meals in a single
voucher.

**Math:**
- Flights: 12000 (DR)
- Hotels: 8000 (DR)
- Meals: 1500 (DR)
- Vendor (TRAVEL DESK): 21500 (CR)

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>TD/12-25/0007</bill_no>
    <bill_date>23-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor>TRAVEL DESK</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>21500.00</total_amount>
    <notes>Bill from TRAVEL DESK entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>21500.00</amount>
        <ledger>TRAVEL DESK</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>Bengaluru ↔ Delhi return flight</details>
        <expense_ledger>TRAVEL — AIRFARE</expense_ledger>
        <amount>12000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
      <item>
        <details>3 nights hotel — Delhi</details>
        <expense_ledger>TRAVEL — LODGING</expense_ledger>
        <amount>8000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
      <item>
        <details>Meals during travel</details>
        <expense_ledger>TRAVEL — MEALS</expense_ledger>
        <amount>1500.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 6 — Bill with all adjustments (GST + TDS + other adjustment + round-off)

A professional-services bill that exercises every adjustment path.

**Math:**
- Professional fee: 100000 (DR)
- CGST @ 9%: 9000 (DR)
- SGST @ 9%: 9000 (DR)
- TDS @ 10%: 10000 (CR)
- Other adjustment (penalty): 500 (CR)
- Round off: 0.50 (CR) — vendor rounded down
- Vendor: 107499.50 (CR)
- Net: 118000 DR = 118000 CR — balanced

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>CONS/2025-26/0099</bill_no>
    <bill_date>24-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor>ACME CONSULTANTS</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>107499.50</total_amount>
    <notes>Bill from ACME CONSULTANTS entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>107499.50</amount>
        <ledger>ACME CONSULTANTS</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>9000.00</amount>
        <ledger>CGST (ITC) @ 9%</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>9000.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>10000.00</amount>
        <ledger>TDS on Professional Fees @ 10%</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>500.00</amount>
        <ledger>Late Payment Penalty</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>0.50</amount>
        <ledger>Round Off</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>Consulting engagement — Q3 quarterly review</details>
        <expense_ledger>PROFESSIONAL FEES</expense_ledger>
        <amount>100000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

> **Note on `<round_off>` sign and DR/CR:** if the vendor charged extra
> (rounded up), the round-off line is a `debit`; if the vendor took
> less (rounded down), it's a `credit`. The amount itself stays
> positive — direction is signalled by `<debit_or_credit>`.

---

## Scenario 7 — Reverse Charge Mechanism (GST flipped to credit side)

An imported services bill where the recipient pays GST under RCM. The
GST is **booked but immediately reversed** — same amount appears once
as input credit (DR) and once as payable (CR).

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>IMP/2025-26/0003</bill_no>
    <bill_date>20-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor>OVERSEAS SOFTWARE INC</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>50000.00</total_amount>
    <notes>Bill from OVERSEAS SOFTWARE INC entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>50000.00</amount>
        <ledger>OVERSEAS SOFTWARE INC</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>9000.00</amount>
        <ledger>IGST (ITC) @ 18%</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>9000.00</amount>
        <ledger>IGST (RCM Payable) @ 18%</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>SaaS subscription — December</details>
        <expense_ledger>SOFTWARE SUBSCRIPTIONS</expense_ledger>
        <amount>50000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 8 — Consolidated bill

When the user toggles **"Consolidate items"** in the UI, the original
expense lines are collapsed into a small set (typically one). The XML
shape is identical — Tally cannot distinguish consolidated from
individual mode.

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>UTL/12-25/0005</bill_no>
    <bill_date>25-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor>STATE ELECTRICITY BOARD</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>15000.00</total_amount>
    <notes>Bill from STATE ELECTRICITY BOARD entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>15000.00</amount>
        <ledger>STATE ELECTRICITY BOARD</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>Electricity charges — Consolidated (3 sites)</details>
        <expense_ledger>ELECTRICITY EXPENSES</expense_ledger>
        <amount>15000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 9 — Multiple bills in one response

The sync endpoint returns **all** verified-but-not-posted expense bills
in a single response. Tally TDL iterates `<bill>` siblings and posts
each as a separate Journal voucher.

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>SP/2025-26/0011</bill_no>
    <bill_date>20-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor>STAR STATIONERY</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>5000.00</total_amount>
    <notes>Bill from STAR STATIONERY entered via BillMunshi https://...</notes>
    <ledgers>
      <ledger>
        <amount>5000.00</amount>
        <ledger>STAR STATIONERY</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>Office stationery</details>
        <expense_ledger>OFFICE EXPENSES</expense_ledger>
        <amount>5000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
  <bill>
    <bill_no>TELCO/12-25/9821</bill_no>
    <bill_date>21-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor>BHARAT TELCO LTD</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>2360.00</total_amount>
    <notes>Bill from BHARAT TELCO LTD entered via BillMunshi https://...</notes>
    <ledgers>
      <ledger><amount>2360.00</amount><ledger>BHARAT TELCO LTD</ledger><debit_or_credit>credit</debit_or_credit></ledger>
      <ledger><amount>180.00</amount><ledger>CGST (ITC) @ 9%</ledger><debit_or_credit>debit</debit_or_credit></ledger>
      <ledger><amount>180.00</amount><ledger>SGST (ITC) @ 9%</ledger><debit_or_credit>debit</debit_or_credit></ledger>
    </ledgers>
    <items>
      <item>
        <details>Office internet — December</details>
        <expense_ledger>INTERNET CHARGES</expense_ledger>
        <amount>2000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 10 — Empty response (no bills awaiting sync)

Returned when there is nothing pending. Valid XML, just no bills inside.

```xml
<?xml version='1.0' encoding='utf-8'?>
<data/>
```

---

## Edge cases & rules summary (cheat sheet)

| Situation | Behaviour |
|---|---|
| Amount = 0 on any tax / adjustment | The `<ledger>` entry is **omitted entirely**. |
| `*_taxes` FK is missing | The `<ledger>` entry is omitted even if its amount > 0 (no ledger to post to). |
| CGST + SGST + IGST together | Will never happen — bill is either intrastate (CGST + SGST) or interstate (IGST). |
| Vendor line | **Always present** as a `<ledger>` entry when `vendor_amount > 0`. Usually `credit`. |
| TDS direction | Usually `credit` (deducted at source, becomes liability to govt). |
| GST direction | Usually `debit` (input tax credit). Flipped to `credit` only under reverse-charge (RCM). |
| Round-off sign | Amount is positive; **direction is signalled by `<debit_or_credit>`** — vendor charged more → `debit`, vendor took less → `credit`. |
| Item `<details>` with `"` or `&` | XML serializer auto-handles escaping for `<`, `>`, `&`. Double quotes stay literal. |
| `<amount>` everywhere | Always 2 decimals (e.g. `50000.00`, never `50000` or `50000.0`). |
| `<bill_date>` | `DD-MM-YYYY` always. |
| Per-line tax breakdown | **Not provided.** Unlike vendor bills, expense bill items don't carry per-line GST. GST is bill-level only. |
| `<rate>` on GST entries | Not emitted (vendor bill emits it; expense bill omits it). |

---

## Difference table: Vendor bill vs Expense bill payload

| Field | Vendor bill | Expense bill |
|---|---|---|
| `<voucher_type>` | omitted (implicit `Purchase`) | **`Journal`** (always present) |
| `<vendor>` inside `<ledgers>` | **no** — implicit credit on Purchase voucher | **yes** — every Journal line stated explicitly |
| `<debit_or_credit>` per `<ledger>` | not emitted | **emitted on every entry** |
| `<rate>` on GST `<ledger>` entries | emitted (`18%`, etc.) | not emitted |
| GST per-rate grouping | yes (mixed-rate bills produce multiple GST entries) | no — single bill-level CGST/SGST/IGST |
| Adjustment fields | `discount`, `cess`, `freight`, `round_off` | `tds`, `other_adjustment`, `round_off` |
| `<items>` inner tag | `<purchase_ledger>` + `<price>` + `<quantity>` | `<expense_ledger>` + `<debit_or_credit>` (no qty/price) |
| `<debit_or_credit>` per `<item>` | not emitted | **emitted on every item** |

---

## Required Tally TDL changes (from the old expense JSON shape)

The previous expense sync returned **JSON** with `DR_LEDGER` / `CR_LEDGER`
arrays. The new payload is **XML** matching the vendor-bill shape (with
the expense-specific additions above).

1. **Switch the response parser from JSON to XML** for the expense bill
   sync endpoint.
2. **Replace `DR_LEDGER`/`CR_LEDGER` array reads** with a single
   iteration over `<ledger>` children of `<ledgers>`, branching on each
   entry's `<debit_or_credit>` value for the posting direction.
3. **`<items>` collection rename:** the old payload didn't have items
   either (everything was flattened into DR/CR arrays). Now there's a
   proper `<items>` block — iterate and post each as a journal line
   against `<expense_ledger>` with its own `<debit_or_credit>`.
4. **`<voucher_type>Journal</voucher_type>`** is the new explicit voucher
   type — use it to select the Tally voucher template (vs the implicit
   `Purchase` on vendor bills).
5. **Negative `<amount>` not used for expense bills.** Sign is always
   conveyed via `<debit_or_credit>` instead.

Vendor bill TDL code can share most of the iteration logic; the only
differences are the extra `<debit_or_credit>` reads and the
`<expense_ledger>` rename.
