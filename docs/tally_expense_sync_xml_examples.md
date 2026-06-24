# Tally Expense Bill Sync — XML Payload Reference

This document is the **contract between BillMunshi and the Tally TDL/TCP
connector** for the **expense-bill** sync endpoint.

```
GET  https://billmunshi.com/api/v1/tally/org/<ORG_ID>/expense-bills/sync/
Content-Type: application/xml; charset=utf-8
```

The endpoint always returns a `<data>` root containing zero or more
`<bill>` elements. The Tally TDL must iterate `<bill>` elements.

> **Big-picture difference from vendor bill:**
>
> 1. **Voucher type is `Journal`** (vendor bill is `Purchase`). Every
>    posting needs explicit DR/CR direction.
> 2. **There is no `<items>` block.** Every line that hits a Tally
>    ledger — vendor, expense line, GST, TDS, adjustments — lives
>    inside the single `<ledgers>` collection. The journal voucher
>    needs only one collection to iterate.
> 3. **Multi-rate GST is supported.** The same tax type (CGST / SGST /
>    IGST) can repeat with different `<rate>` + `<ledger>` — one per
>    rate bucket. Each GST entry also carries an optional `<rate>` child.

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
| `<ledgers>` | **The only collection.** Flat list of every ledger posting on the journal voucher (vendor, expense lines, GST, TDS, adjustments). | — |

### `<ledgers>` block

A single flat collection. **Every** posting that hits a Tally ledger
lives here:

- **The vendor** (the balancing party — typically `credit`)
- **Each expense item line** (DR against the chart-of-accounts ledger)
- **GST entries** (CGST / SGST / IGST — possibly multiple, one per rate bucket)
- **TDS, Other Adjustment, Round Off** (single bill-level entries)

The Tally TDL iterates `<ledger>` children and posts each as its own
voucher line; the ledger name on the Tally master classifies it.

Children of every `<ledger>` entry:

| Child | Meaning |
|---|---|
| `<amount>` | 2-decimal amount. Positive normally; **negative allowed only on Round Off** when the vendor took less than computed. |
| `<ledger>` | Tally ledger name to debit/credit. Must match a ledger that already exists in the Tally company. |
| `<debit_or_credit>` | **Required.** Either `debit` or `credit`. |
| `<rate>` | GST rate string (e.g. `18%`). **Only emitted for GST entries (CGST / SGST / IGST)** — informational, helps Tally tag for GSTR reports. Absent on vendor / expense-line / TDS / other_adjustment / round_off entries. |

Emit rules:

- **Zero-amount entries are dropped entirely.** A bill with no TDS has
  no TDS-named `<ledger>` entry. Tally must not synthesize one.
- **Entries with a missing ledger FK are also dropped.** If a GST line
  has an amount > 0 but no ledger assigned, the entry doesn't appear.
- **CGST + SGST and IGST are mutually exclusive on a single bill**
  (intrastate vs interstate). The frontend enforces this.
- **Same tax type may repeat with different rates / ledgers.** A
  mixed-rate bill emits multiple CGST entries (or SGST or IGST), one
  per (rate, ledger) bucket. The Tally TDL must iterate.
- **Vendor is always a `<ledger>` entry** (typically `credit`) because
  a journal voucher needs every line stated explicitly. Vendor bill
  (Purchase voucher) omits this since the vendor is the implicit
  balancing party there.
- **Expense lines have no narration** in the XML. The bill-level
  `<notes>` is the only free-text field. (Item descriptions are
  stored internally on BillMunshi for the UI but not sent to Tally,
  since the chart-of-accounts ledger name + the bill-level notes give
  Tally enough context.)

> **There is no `<items>` block.** This is the key structural
> difference from earlier versions of this contract. The journal
> voucher only emits `<ledgers>`.

---

## Scenario 1 — Simple expense, no GST (e.g. cash payment, exempt vendor)

A flat ₹5,000 stationery bill from a composition vendor.

**Math:**
- Stationery expense: 5000 (DR)
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
      <ledger>
        <amount>5000.00</amount>
        <ledger>OFFICE EXPENSES</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
    </ledgers>
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
        <amount>2000.00</amount>
        <ledger>INTERNET CHARGES</ledger>
        <debit_or_credit>debit</debit_or_credit>
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
        <amount>2000.00</amount>
        <ledger>INTERNET CHARGES</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>360.00</amount>
        <ledger>IGST (ITC) @ 18%</ledger>
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
    </ledgers>
  </bill>
</data>
```

---

## Scenario 4 — **Mixed-rate intrastate (the new capability)**

AC maintenance contract: service charges at 18% + spare parts at 28%.
Each rate produces its own CGST + SGST pair. **4 GST entries total**,
plus 2 expense lines and 1 vendor line — 7 ledger postings.

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
      <ledger>
        <amount>2000.00</amount>
        <ledger>REPAIRS &amp; MAINTENANCE</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>500.00</amount>
        <ledger>REPAIRS &amp; MAINTENANCE</ledger>
        <debit_or_credit>debit</debit_or_credit>
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
        <amount>2000.00</amount>
        <ledger>REPAIRS &amp; MAINTENANCE</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>500.00</amount>
        <ledger>REPAIRS &amp; MAINTENANCE</ledger>
        <debit_or_credit>debit</debit_or_credit>
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
  </bill>
</data>
```

---

## Scenario 6 — Rent bill with TDS

Office rent ₹50,000 + 18% GST. TDS @ 10% on the **base** rent (₹5,000)
is deducted at source; actual cash paid to the landlord is
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
        <amount>50000.00</amount>
        <ledger>RENT EXPENSE</ledger>
        <debit_or_credit>debit</debit_or_credit>
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
  </bill>
</data>
```

---

## Scenario 7 — Multiple expense lines (one bill, several heads, no GST)

Travel reimbursement booking flights + hotels + meals in a single voucher.
Three expense lines, all posted to different chart-of-accounts ledgers.

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
      <ledger>
        <amount>12000.00</amount>
        <ledger>TRAVEL — AIRFARE</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>8000.00</amount>
        <ledger>TRAVEL — LODGING</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>1500.00</amount>
        <ledger>TRAVEL — MEALS</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
    </ledgers>
  </bill>
</data>
```

---

## Scenario 8 — Bill with all adjustments (mixed GST + TDS + other adjustment + round-off)

The "kitchen sink" — professional-services bill at two GST rates plus
every adjustment type.

**Math:**
- Professional fee: 100000 (DR), Travel reimbursement: 5000 (DR) → 105000 base
- 18% on 100000: CGST 9000 + SGST 9000 (DR)
- 28% on 5000:   CGST  700 + SGST  700 (DR)
- TDS @ 10%: 10000 (CR)
- Late penalty: 500 (CR)
- Round off: 0.50 (CR)
- Vendor: 107499.50 (CR) — balancing party

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
      <ledger>
        <amount>100000.00</amount>
        <ledger>PROFESSIONAL FEES</ledger>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>5000.00</amount>
        <ledger>TRAVEL EXPENSES</ledger>
        <debit_or_credit>debit</debit_or_credit>
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
        <amount>50000.00</amount>
        <ledger>SOFTWARE SUBSCRIPTIONS</ledger>
        <debit_or_credit>debit</debit_or_credit>
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
  </bill>
</data>
```

**TDL behaviour:** same loop, two GST entries with the same `<rate>` but
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
      <ledger><amount>5000.00</amount><ledger>STAR STATIONERY</ledger><debit_or_credit>credit</debit_or_credit></ledger>
      <ledger><amount>5000.00</amount><ledger>OFFICE EXPENSES</ledger><debit_or_credit>debit</debit_or_credit></ledger>
    </ledgers>
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
      <ledger><amount>2000.00</amount><ledger>INTERNET CHARGES</ledger><debit_or_credit>debit</debit_or_credit></ledger>
      <ledger><amount>180.00</amount><ledger>CGST (ITC) @ 9%</ledger><rate>18%</rate><debit_or_credit>debit</debit_or_credit></ledger>
      <ledger><amount>180.00</amount><ledger>SGST (ITC) @ 9%</ledger><rate>18%</rate><debit_or_credit>debit</debit_or_credit></ledger>
    </ledgers>
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
| Expense item amount = 0 or no ledger | Omitted (no zero ledger posting). |
| Same tax type repeats with different `<rate>` | **Each entry is a separate voucher line** — don't merge, they belong to different GSTR slabs. |
| CGST + SGST + IGST together | Will never happen — bill is either intrastate (CGST + SGST) or interstate (IGST). |
| Vendor line | **Always present** as a `<ledger>` entry when vendor amount > 0. Usually `credit`. |
| Same expense ledger appears twice (e.g. two REPAIRS lines) | Each is a separate voucher line. Don't merge — they may belong to different cost centres / GSTR categories. |
| TDS direction | Usually `credit` (deducted at source, becomes liability to govt). |
| GST direction | Usually `debit` (input tax credit). Flipped to `credit` on the RCM payable side. |
| Round-off sign | Amount is positive; **direction is signalled by `<debit_or_credit>`**. |
| `<amount>` everywhere | Always 2 decimals (e.g. `50000.00`, never `50000` or `50000.0`). |
| `<bill_date>` | `DD-MM-YYYY` always. |
| `<rate>` on GST entries | Format `<int>%`, e.g. `18%`. Informational only — use for GSTR tagging if needed, else ignore. |
| `<rate>` on non-GST entries | **Not emitted.** Presence of `<rate>` is a fast "is this a GST line?" check. |
| `<id>` (bill UUID) | **Always echo back** via the `tally_status` callback so the backend can flag the right bill. |
| `<items>` block | **Does not exist.** Expense lines are inside `<ledgers>`. |

---

## Difference table: Vendor bill vs Expense bill payload

| Field | Vendor bill | Expense bill |
|---|---|---|
| `<voucher_type>` | `Purchase` | **`Journal`** |
| `<vendor_name>` inside `<ledgers>` | **no** — implicit credit on Purchase voucher | **yes** — every Journal line stated explicitly |
| `<debit_or_credit>` per `<ledger>` | not emitted | **emitted on every entry** |
| `<rate>` on GST `<ledger>` entries | emitted (`18%`, etc.) | **emitted** |
| Multi-rate GST | yes | **yes** |
| `<items>` block | **yes** — separate from `<ledgers>` (carries `<price>` + `<quantity>` + `<purchase_ledger>` + `<details>`) | **no** — expense lines are folded into `<ledgers>` as plain ledger entries |
| Adjustment fields | `discount`, `cess`, `freight`, `round_off` | `tds`, `other_adjustment`, `round_off` |

---

## Required Tally TDL changes for the Journal voucher

The vendor-bill (Purchase voucher) side is already on this contract and
shipping. The Journal voucher side needs **only one structural change**:
iterate `<ledger>` entries inside `<ledgers>` — that's the only
collection on the voucher.

1. **Switch the response parser from JSON to XML** (already done for
   vendor bills — if shared, no work). The Content-Type header on the
   sync endpoint is `application/xml; charset=utf-8`.
2. **Iterate `<ledger>` children of `<ledgers>`** and post each as a
   voucher line. Each entry carries its own direction via
   `<debit_or_credit>` — no need to classify by tag name.
3. **There is no `<items>` block on expense bills.** Don't look for
   one. Expense lines are inside `<ledgers>` (no `<rate>` on them, so
   they're trivially distinguishable from GST entries if needed).
4. **Mixed-rate support:** the loop must not assume "one CGST" or
   "one SGST". A single bill can emit N entries per tax type, one per
   (rate, ledger) bucket. Same loop pattern the vendor-side TDL
   already uses.
5. **Optional:** read `<rate>` on GST entries if you want them tagged
   for GSTR-2A/2B reports. Skip otherwise — safe to ignore.
6. **`<voucher_type>Journal</voucher_type>`** at the top of each bill
   tells the TDL to use the Journal voucher template (vs the implicit
   `Purchase` on vendor bills).
7. **`<id>`** (bill UUID) at the top of each bill — **echo this back**
   via the `tally_status` callback after posting. This is how the
   backend flags the right bill as posted without fuzzy matching.

That's the full scope.

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
