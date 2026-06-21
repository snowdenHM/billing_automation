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
>
> **Multi-rate GST is supported.** The `<ledgers>` block can carry
> multiple `<ledger>` entries of the same tax type (e.g. two CGST
> entries at 18% and 28%) — one per (rate, ledger) bucket. Each GST
> entry now also carries an optional `<rate>` child.

---

## Field reference

### Top-level `<bill>` elements (always present)

| Tag | Meaning | Example |
|---|---|---|
| `<id>` | BillMunshi bill UUID — **echo this back** when calling the `tally_status` API so the backend can flag the right bill as posted | `6a0e3db4-…` |
| `<bill_no>` | Vendor's printed invoice number | `EXP/2025-26/0042` |
| `<bill_date>` | Invoice date in `DD-MM-YYYY` | `22-12-2025` |
| `<voucher_type>` | **Always `Journal`** for expense bills (vendor bill emits `Purchase`) | `Journal` |
| `<vendor_name>` | Vendor / payee ledger name | `SUNDARAM PROPERTIES` |
| `<company>` | Tally company name to post into | `Spectrum Poly Pack and Packaging` |
| `<total_amount>` | Bill grand total — always 2 decimals | `54000.00` |
| `<notes>` | Free-text narration; includes BillMunshi back-link | `Bill from … entered via BillMunshi https://…` |
| `<ledgers>` | Flat list of ledger postings (see below) | — |
| `<items>` | Expense line items (journal-entry style, no qty/price) | — |

### `<ledgers>` block

A single flat collection. **Every** posting that hits a Tally ledger
lives here — vendor, CGST, SGST, IGST (any/all of the three, possibly
multiple times each at different rates), TDS, other adjustment,
round-off. The Tally TDL iterates `<ledger>` children and posts each
as its own voucher line; the ledger name on the Tally master
classifies it.

Children of every `<ledger>` entry:

| Child | Meaning |
|---|---|
| `<amount>` | 2-decimal amount. Positive normally; **negative allowed only on Round Off** when the vendor took less than computed. |
| `<ledger>` | Tally ledger name to debit/credit. Must match a ledger that already exists in the Tally company. |
| `<debit_or_credit>` | **Required.** Either `debit` or `credit`. Defaults to `debit` if missing (defensive only — backend always emits it). |
| `<rate>` | GST rate string (e.g. `18%`). **Only emitted for GST entries (CGST / SGST / IGST)** — informational, helps Tally tag for GSTR reports. Absent on discount / TDS / other_adjustment / round_off. |

Emit rules:

- **Zero-amount entries are dropped entirely.** A bill with no TDS has
  no TDS-named `<ledger>` entry. Tally must not synthesize one.
- **Entries with a missing ledger FK are also dropped.** If a GST line
  has an amount > 0 but no ledger assigned, the entry doesn't appear
  (there's no ledger to post to).
- **CGST + SGST and IGST are mutually exclusive on a single bill**
  (intrastate vs interstate). The frontend enforces this.
- **Same tax type may repeat with different rates / ledgers.** A
  mixed-rate bill emits multiple CGST entries (or SGST or IGST), one
  per (rate, ledger) bucket. The Tally TDL must iterate.
- The **vendor** itself is one of the `<ledger>` entries (typically
  `<debit_or_credit>credit</debit_or_credit>`), because a journal
  voucher needs every line stated. Vendor bill (Purchase voucher)
  omits this since the vendor is the implicit balancing party.

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
<data>
  <bill>
    <id>6a0e3db4-…-stationery</id>
    <bill_no>SP/2025-26/0011</bill_no>
    <bill_date>20-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>STAR STATIONERY</vendor_name>
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

## Scenario 2 — Single-rate intrastate (CGST + SGST)

Internet bill ₹2,000 base + 18% GST = ₹2,360. Vendor is in the same state.
Single rate → **2 GST entries** (one CGST + one SGST), each with `<rate>18%</rate>`.

**Math:**
- Internet expense: 2000 (DR)
- CGST @ 9%: 180 (DR) → input credit
- SGST @ 9%: 180 (DR) → input credit
- Vendor (TELCO): 2360 (CR)
- Net: balanced

```xml
<data>
  <bill>
    <id>6a0e3db4-…-telco</id>
    <bill_no>TELCO/12-25/9821</bill_no>
    <bill_date>21-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>BHARAT TELCO LTD</vendor_name>
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
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>180.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
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

## Scenario 3 — Single-rate interstate (IGST)

Same internet bill but vendor is in another state — CGST/SGST replaced
by a single IGST entry.

```xml
<data>
  <bill>
    <id>6a0e3db4-…-telco-igst</id>
    <bill_no>TELCO/12-25/9822</bill_no>
    <bill_date>21-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>BHARAT TELCO LTD</vendor_name>
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
        <rate>18%</rate>
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

## Scenario 4 — **Mixed-rate intrastate (the new capability)**

AC maintenance contract: service charges at 18% + spare parts at 28%.
Each rate produces its own CGST + SGST pair. **4 GST entries total.**

**Math:**
- Service expense: 2000 (DR), Spare parts: 500 (DR) → 2500 base
- 18% on 2000 = 360 → CGST 180 + SGST 180 (both DR)
- 28% on 500  = 140 → CGST  70 + SGST  70 (both DR)
- Vendor (SHARMA): 3000 (CR)
- Net: 2500 + 500 tax = 3000 = balanced

```xml
<data>
  <bill>
    <id>uuid-mixed-intra</id>
    <bill_no>MAINT/2025-26/045</bill_no>
    <bill_date>22-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>SHARMA SERVICES</vendor_name>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>3000.00</total_amount>
    <notes>Bill from SHARMA SERVICES entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>3000.00</amount>
        <ledger>SHARMA SERVICES</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <!-- 18% rate bucket -->
      <ledger>
        <amount>180.00</amount>
        <ledger>CGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>180.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <!-- 28% rate bucket -->
      <ledger>
        <amount>70.00</amount>
        <ledger>CGST (ITC) @ 14%</ledger>
        <rate>28%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>70.00</amount>
        <ledger>SGST (ITC) @ 14%</ledger>
        <rate>28%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>AC maintenance contract — Q3</details>
        <expense_ledger>REPAIRS &amp; MAINTENANCE</expense_ledger>
        <amount>2000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
      <item>
        <details>Spare parts replacement (gas, valve)</details>
        <expense_ledger>REPAIRS &amp; MAINTENANCE</expense_ledger>
        <amount>500.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

**TDL implication:** the loop over `<ledger>` children of `<ledgers>`
must NOT assume "one CGST" or "one SGST" — there can be N of each.
Each posts to a different Tally ledger master.

---

## Scenario 5 — **Mixed-rate interstate (IGST)**

Same bill but interstate — two IGST entries at different rates.

```xml
<data>
  <bill>
    <id>uuid-mixed-inter</id>
    <bill_no>MAINT/2025-26/046</bill_no>
    <bill_date>22-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>SHARMA SERVICES (Pune)</vendor_name>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>3000.00</total_amount>
    <notes>Bill from SHARMA SERVICES (Pune) entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>3000.00</amount>
        <ledger>SHARMA SERVICES (Pune)</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>360.00</amount>
        <ledger>IGST (ITC) @ 18%</ledger>
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>140.00</amount>
        <ledger>IGST (ITC) @ 28%</ledger>
        <rate>28%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
    </ledgers>
    <items>
      <item>
        <details>AC maintenance contract — Q3</details>
        <expense_ledger>REPAIRS &amp; MAINTENANCE</expense_ledger>
        <amount>2000.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
      <item>
        <details>Spare parts replacement</details>
        <expense_ledger>REPAIRS &amp; MAINTENANCE</expense_ledger>
        <amount>500.00</amount>
        <debit_or_credit>debit</debit_or_credit>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 6 — Rent bill with TDS (single-rate intrastate)

Office rent ₹50,000 + 18% GST. TDS @ 10% on the **base** rent (₹5,000)
is deducted at source; the actual cash paid to the landlord is
₹54,000 (50000 + 9000 GST − 5000 TDS).

**Math:**
- Rent expense: 50000 (DR)
- CGST @ 9%: 4500 (DR) → input credit
- SGST @ 9%: 4500 (DR) → input credit
- TDS payable: 5000 (CR) → liability to the govt
- Vendor (landlord): 54000 (CR)
- DR: 50000+4500+4500 = 59000; CR: 5000+54000 = 59000 — balanced

```xml
<data>
  <bill>
    <id>uuid-rent</id>
    <bill_no>EXP/2025-26/0042</bill_no>
    <bill_date>22-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>SUNDARAM PROPERTIES</vendor_name>
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
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>4500.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
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

## Scenario 7 — Multiple expense lines (one bill, several heads, no GST)

Travel reimbursement booking flights + hotels + meals in a single voucher.

```xml
<data>
  <bill>
    <id>uuid-travel</id>
    <bill_no>TD/12-25/0007</bill_no>
    <bill_date>23-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>TRAVEL DESK</vendor_name>
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

## Scenario 8 — Bill with all adjustments (mixed GST + TDS + other adjustment + round-off)

The "kitchen sink" scenario — professional-services bill at two GST
rates plus every adjustment type.

**Math:**
- Professional fee: 100000 (DR), Travel reimbursement: 5000 (DR)
- 18% on 100000: CGST 9000 + SGST 9000 (DR)
- 28% on 5000:   CGST  700 + SGST  700 (DR) … illustrative
- TDS @ 10%: 10000 (CR)
- Late penalty: 500 (CR)
- Round off: 0.50 (CR)
- Vendor: balancing entry on CR side

```xml
<data>
  <bill>
    <id>uuid-consulting</id>
    <bill_no>CONS/2025-26/0099</bill_no>
    <bill_date>24-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>ACME CONSULTANTS</vendor_name>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>107499.50</total_amount>
    <notes>Bill from ACME CONSULTANTS entered via BillMunshi https://billmunshi.com/tally/expense-bill/...</notes>
    <ledgers>
      <ledger>
        <amount>107499.50</amount>
        <ledger>ACME CONSULTANTS</ledger>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <!-- Multi-rate GST -->
      <ledger>
        <amount>9000.00</amount>
        <ledger>CGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>9000.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>700.00</amount>
        <ledger>CGST (ITC) @ 14%</ledger>
        <rate>28%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>700.00</amount>
        <ledger>SGST (ITC) @ 14%</ledger>
        <rate>28%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <!-- Bill-level adjustments (no rate) -->
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
      <item>
        <details>Travel reimbursement (28% GST)</details>
        <expense_ledger>TRAVEL EXPENSES</expense_ledger>
        <amount>5000.00</amount>
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

## Scenario 9 — Reverse Charge Mechanism (RCM) with multi-rate

An imported services bill where the recipient pays GST under RCM. The
GST is **booked but immediately reversed** — same amount appears once
as input credit (DR) and once as RCM payable (CR), at the same `<rate>`
but on different `<ledger>` masters.

```xml
<data>
  <bill>
    <id>uuid-rcm</id>
    <bill_no>IMP/2025-26/0003</bill_no>
    <bill_date>20-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>OVERSEAS SOFTWARE INC</vendor_name>
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
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>9000.00</amount>
        <ledger>IGST (RCM Payable) @ 18%</ledger>
        <rate>18%</rate>
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

**TDL behaviour:** same loop, two entries with the same `<rate>` but
different `<ledger>` masters and opposite directions. No special branch.

---

## Scenario 10 — Multiple bills in one response

The sync endpoint returns **all** verified-but-not-posted expense bills
in a single response. Tally TDL iterates `<bill>` siblings and posts
each as a separate Journal voucher.

```xml
<data>
  <bill>
    <id>uuid-bill-1</id>
    <bill_no>SP/2025-26/0011</bill_no>
    <bill_date>20-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>STAR STATIONERY</vendor_name>
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
    <id>uuid-bill-2</id>
    <bill_no>TELCO/12-25/9821</bill_no>
    <bill_date>21-12-2025</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>BHARAT TELCO LTD</vendor_name>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>2360.00</total_amount>
    <notes>Bill from BHARAT TELCO LTD entered via BillMunshi https://...</notes>
    <ledgers>
      <ledger><amount>2360.00</amount><ledger>BHARAT TELCO LTD</ledger><debit_or_credit>credit</debit_or_credit></ledger>
      <ledger><amount>180.00</amount><ledger>CGST (ITC) @ 9%</ledger><rate>18%</rate><debit_or_credit>debit</debit_or_credit></ledger>
      <ledger><amount>180.00</amount><ledger>SGST (ITC) @ 9%</ledger><rate>18%</rate><debit_or_credit>debit</debit_or_credit></ledger>
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

## Scenario 11 — Empty response (no bills awaiting sync)

Returned when there is nothing pending. Valid XML, just no bills inside.

```xml
<data/>
```

---

## Edge cases & rules (cheat sheet)

| Situation | Behaviour |
|---|---|
| GST amount = 0 on a tax line | The `<ledger>` entry is **omitted entirely**. |
| Missing ledger FK on a GST line | Omitted, even if amount > 0 (no ledger to post to). |
| Same tax type repeats with different `<rate>` | **Each entry is a separate voucher line** — don't merge, they belong to different GSTR slabs. |
| CGST + SGST + IGST together | Will never happen — bill is either intrastate (CGST + SGST) or interstate (IGST). |
| Vendor line | **Always present** as a `<ledger>` entry when vendor amount > 0. Usually `credit`. |
| TDS direction | Usually `credit` (deducted at source, becomes liability to govt). |
| GST direction | Usually `debit` (input tax credit). Flipped to `credit` on the RCM payable side. |
| Round-off sign | Amount is positive; **direction is signalled by `<debit_or_credit>`** — vendor charged more → `debit`, vendor took less → `credit`. |
| Item `<details>` with `"` or `&` | XML serializer auto-handles escaping for `<`, `>`, `&`. Double quotes stay literal. |
| `<amount>` everywhere | Always 2 decimals (e.g. `50000.00`, never `50000` or `50000.0`). |
| `<bill_date>` | `DD-MM-YYYY` always. |
| `<rate>` on GST entries | Format `<int>%`, e.g. `18%`. Informational only — use for GSTR tagging if needed, else ignore. |
| `<rate>` on non-GST entries | **Not emitted.** Presence of `<rate>` is a fast "is this a GST line?" check. |
| Per-line tax breakdown | **Not provided.** Unlike vendor bills, expense bill items don't carry per-line GST. GST is at bill level via `<ledgers>` entries. |
| `<id>` (bill UUID) | **Always echo back** via the `tally_status` callback so the backend can flag the right bill. |

---

## Difference table: Vendor bill vs Expense bill payload

| Field | Vendor bill | Expense bill |
|---|---|---|
| `<voucher_type>` | `Purchase` | **`Journal`** |
| `<vendor_name>` inside `<ledgers>` | **no** — implicit credit on Purchase voucher | **yes** — every Journal line stated explicitly |
| `<debit_or_credit>` per `<ledger>` | not emitted | **emitted on every entry** |
| `<rate>` on GST `<ledger>` entries | emitted (`18%`, etc.) | **emitted (NEW)** |
| Multi-rate GST | yes (mixed-rate bills produce multiple GST entries) | **yes (NEW — multi-row GST Lines)** |
| Adjustment fields | `discount`, `cess`, `freight`, `round_off` | `tds`, `other_adjustment`, `round_off` |
| `<items>` inner tag | `<purchase_ledger>` + `<price>` + `<quantity>` | `<expense_ledger>` + `<debit_or_credit>` (no qty/price) |
| `<debit_or_credit>` per `<item>` | not emitted | **emitted on every item** |

---

## Required Tally TDL changes for the Journal voucher

The vendor-bill (Purchase voucher) side is already on this contract and
shipping. The Journal voucher side needs **only one structural change**:
support multiple `<ledger>` entries of the same tax type.

1. **Switch the response parser from JSON to XML** (already done for
   vendor bills — if shared, no work). The Content-Type header on the
   sync endpoint is `application/xml; charset=utf-8`.
2. **Replace any branch that read named CGST/SGST/IGST tags** with a
   uniform iteration over `<ledger>` children of `<ledgers>`. Each entry
   carries its own direction via `<debit_or_credit>` — no need to
   classify by tag name.
3. **Mixed-rate support:** the loop must not assume "one CGST" or
   "one SGST". A single bill can emit N entries per tax type, one per
   (rate, ledger) bucket. Same loop pattern the vendor-side TDL
   already uses.
4. **Optional:** read `<rate>` on GST entries if you want them tagged
   for GSTR-2A/2B reports. Skip otherwise — safe to ignore.
5. **`<voucher_type>Journal</voucher_type>`** at the top of each bill
   tells the TDL to use the Journal voucher template (vs the implicit
   `Purchase` on vendor bills).
6. **`<id>`** (bill UUID) at the top of each bill — **echo this back**
   via the `tally_status` callback after posting. This is how the
   backend flags the right bill as posted without fuzzy matching.

That's the full scope. If your TDL already iterates `<ledger>` children
for vendor bills, the same loop handles expense bills correctly —
including all the multi-rate / RCM / TDS / mixed-adjustment scenarios
above.

---

## tally_status callback (unchanged from before)

When the voucher is posted in Tally, call back to BillMunshi:

```
POST  https://billmunshi.com/api/v1/tally/org/<ORG_ID>/bills/tally_status/
Authorization: Api-Key <org-api-key>
Content-Type: application/json
```

**Single update:**
```json
{
  "id": "<bill UUID from <id> tag>",
  "status": true,
  "mode": "expense",
  "message": "Posted as Journal voucher #12345"
}
```

**Bulk update:**
```json
{
  "Data": [
    { "id": "uuid1", "status": true,  "mode": "expense", "message": "OK" },
    { "id": "uuid2", "status": false, "mode": "expense", "message": "Vendor master not found in Tally" }
  ]
}
```

- `mode` = `"expense"` for Journal vouchers, `"vendor"` for Purchase
  vouchers.
- `status: false` with a descriptive `message` is encouraged for
  failures — the message shows on the BillMunshi UI so the operator
  can fix the underlying issue (missing ledger master, GST registration
  mismatch, etc.) without us having to guess.
