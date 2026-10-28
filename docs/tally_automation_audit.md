# Tally module — automation audit

Written 2026-10-28, against `release-1`.

The goal this is measured against: **a bill should arrive filled in**.
Every field an operator has to touch is a field the automation failed
at. So this ranks findings by how many touches they cost, not by how
interesting the code is.

Two notes on how to read it:

- Where I say "verified", I ran it. Where I say "appears" or "likely",
  I read the code and did not exercise it. Those are marked.
- The Tally TCP sync payload is not changed by anything proposed here.
  That constraint was checked against each item, and the discount work
  in §1 turns out not to need it either.

---

## 1. Pre-tax discount — the gap that prompted this

### What the model does today

`compute_round_off` reconciles a bill as

    subtotal(lines) + igst + cgst + sgst + cess + discount + freight
        + round_off  ==  total

`discount` sits **beside the taxes**, not before them. It is a post-tax
adjustment, stored signed negative, in the same family as freight and
round-off. The sync payload carries it as one bill-level ledger entry.

That models a discount deducted from the invoice total. It does not
model the far more common trade discount, which is deducted from the
sub-total **before** GST is computed — the case on the bill that
prompted this.

The difference is not cosmetic. Under s.15(3)(a) of the CGST Act a
discount shown on the invoice and given at or before supply is excluded
from the taxable value. On a ₹10,000 bill with a ₹1,000 trade discount
at 18%, the correct tax is ₹1,620 on ₹9,000. Treating it as post-tax
computes ₹1,800 on ₹10,000 — ₹180 of input credit claimed that is not
due. That is a return-level error, not a display one.

### What it would take — less than it looks

The sync payload already supports it. The TCP receives:

    items:          [{name, details, purchase_ledger, price, quantity, amount}]
    ledger_entries: [{amount, ledger, rate?}, ...]

and the voucher balances as `Σ(item amounts) + Σ(ledger entries) ==
total`. With a pre-tax discount:

    items (gross)   10,000
    discount        -1,000
    CGST               810
    SGST               810
                   -------
    total           10,620     ✓ balances

So the lines stay at their printed gross amounts (which is what an
auditor comparing voucher to invoice needs), the discount keeps its own
ledger entry (which it already has), and the only thing that changes is
**which base the GST is computed on**. No new payload field, no TDL
change.

### Proposed design

1. **`discount_before_tax` boolean** on `TallyVendorAnalyzedBill` and
   `TallySalesAnalyzedBill`. Default `False`, so every existing bill
   keeps its current meaning and nothing in production shifts.

2. **Taxable base** becomes `Σ(line amounts) + discount` when the flag
   is set (discount is already stored negative), and `Σ(line amounts)`
   otherwise.

3. **Per-line taxable value.** The discount is apportioned across lines
   in proportion to their amounts — which is how the taxable value is
   arrived at per line for the return. A line's tax is then
   `taxable_value × rate`, not `amount × rate`.

   This changes the invariant `apps/common/services/line_gst.py`
   currently enforces, from

       line.tax == line.amount × line.rate / 100

   to

       line.tax == line.taxable_value × line.rate / 100
       line.taxable_value == line.amount − line's share of the discount

   `assign_line_gst` needs a `taxable_base` argument for this. Its slab
   solver already works off a base; it is one parameter, not a rewrite.

4. **Extraction.** The prompt must tell the two apart. The signal is on
   the invoice: a discount row *above* the taxable-value line is
   pre-tax, one *below* the tax lines is post-tax. The HSN summary table
   settles it outright — its "Taxable Value" column is the post-discount
   figure, so if that column sums to less than the line amounts, the
   discount was pre-tax.

5. **UI.** A toggle beside the discount field, defaulting to whatever
   the extraction decided, with the taxable base shown next to it so the
   operator can see which reading was used.

### Interaction worth knowing about

`assign_line_gst` currently takes `Σ(line amounts)` as its base. On a
pre-tax-discount bill the real tax is lower than that base implies, so
no slab matches, the solver fails, and the bill falls through to the
proportional fallback. The rows stay self-consistent (the fallback was
built for that), but the rates will be odd numbers rather than slabs.

**So pre-tax-discount bills are mis-rated today, and they will stay
mis-rated until step 2 lands.** It is worth knowing this is already
happening rather than being introduced.

---

## 2. The single biggest automation win: the HSN summary table

Every GST tax invoice carries a mandatory summary table:

    HSN/SAC   Taxable Value   CGST Rate  Amount   SGST Rate  Amount   Total Tax
    19021900       9,047.60       2.50%  226.19       2.50%  226.19     452.38
    11042900      14,400.00          0%                  0%

**The extraction prompt does not read it.** It asks for line items,
bill-level tax totals, and GST numbers. It does not ask for HSN, nor for
a per-line or per-HSN rate.

Reading that one table gives, in a single pass:

- **GST rate per line, exactly.** `assign_line_gst` stops inferring —
  its `ocr-rates` branch already exists and is the first thing it tries.
  Every mixed-rate bill becomes exact rather than solved.
- **HSN per line.** This is tracker tasks #6 / #16 / #26 ("HSN code
  integration and automatic item mapping"), all three Not Started. The
  data is right there.
- **Taxable value per HSN**, which is both the pre-tax-discount signal
  from §1 and a free cross-check on the line amounts.

I would do this before anything else on this list. It is one prompt
change plus plumbing, it feeds three separate tracker tasks, and it
removes the need for the solver to guess on the common case.

---

## 3. Item auto-match barely fires

Both the backend (`_resolve_stock_item`, `vendor_bills.py:2483`) and the
frontend (`detail.jsx`, the `stockItemsInitialMatchedRef` effect) match a
stock item by **exact name**, case-insensitively.

A vendor's description is almost never a Tally item name. On the bill in
hand, "Sadabahar Soyabadi 20 Kg" has to become "FORTUNE SOYA 15LTR".
Exact matching cannot do that, and nothing else is tried — so the item
dropdown is a manual pick on essentially every line of every bill.

That is probably the single most repeated touch in the product.

What would actually work, in order of effort:

1. **Learned mapping.** When an operator picks item X for description D
   from vendor V, store `(organization, vendor, normalised D) → X`. Next
   time, fill it. This is exactly tracker task #4 ("user behaviour
   learning", In Progress) and it needs no AI — a table and a lookup.
   Most vendors send the same descriptions month after month, so this
   reaches high coverage within one billing cycle.
2. **Alias matching** against `StockItem.alias`, which the model already
   has and nothing matches on.
3. **HSN-narrowed fuzzy match** once §2 lands — match within the items
   carrying that HSN, which makes a loose string match safe enough to
   use.

Ordering matters here: a wrong item auto-fill is worse than an empty
one, because it posts the wrong stock movement and nobody re-reads a
field that looks filled. (1) is safe because the operator taught it. (3)
is only safe with the HSN constraint.

---

## 4. Fields the extraction never asks for

Beyond HSN and rate, these are printed on the bill, needed downstream,
and not extracted — so they are manual or absent:

| Field | Needed for | Today |
|---|---|---|
| Unit / UOM per line ("BAGS") | Tally stock item, quantity meaning | Manual |
| Place of supply / state code | inter- vs intra-state | Inferred from the two GSTINs |
| Per-line discount | line-level trade discount | Not modelled at all |
| E-invoice IRN / Ack No. | duplicate detection, audit trail | Ignored |

The IRN is worth calling out: it is a **globally unique government
identifier for the invoice**. Both bills reviewed carry one. Matching on
IRN would make duplicate detection exact for every e-invoiced bill,
rather than relying on content hashing and fuzzy vendor+number+amount
comparison. It is a cheap, high-certainty win.

Place of supply matters because inter-state is currently derived from
comparing GSTIN state codes. That is usually right, but the invoice
prints "Place of Supply" precisely because there are cases where it
differs from the buyer's registered state, and getting it wrong swaps
IGST for CGST+SGST on the whole voucher.

---

## 5. Bugs found and fixed in this pass

All four were verified by reproducing them and then re-running:

1. **Line GST contradicted the line rate on mixed-rate bills.** Tax was
   split across lines in proportion to amount, then the rate was
   back-derived from the split and snapped to a slab. A 0%-rated line
   received ₹277.82 of tax and then described itself as 0%. Four copies
   of the same code — fresh-analysis and existing-data paths in both
   `vendor_bills.py` and `sales_bills.py`. Replaced with
   `apps/common/services/line_gst.py`, which picks the rate first and
   computes tax from it. 9 tests.

2. **A garbled GSTIN silently vetoed a correct vendor match.**
   `vendorMatch.js` refused a name match when the bill's GSTIN had a
   different PAN — correct in principle, but it trusted a GSTIN that
   failed its own check digit. `isValidGstin` existed, with a full
   checksum implementation, and had **zero callers anywhere in the
   codebase**. Now the veto only applies to a GSTIN that validates.

3. **Quick-create invalidated query keys nothing was listening to.**
   Creating a stock item invalidated `tallyStockItems`; the dropdown
   reads `tallyMasters`. Creating a ledger invalidated the expense COA
   key; the vendor bill's Purchase Ledger dropdown reads
   `tallyPurchaseLedgers`. Both lists therefore never refreshed.

4. **A created master stayed invisible even once the key was right.**
   `SearchableDropdown` resolves its label with `options.find(...)`, so
   between the create and the refetch landing it showed the placeholder
   — the row was created and selected and the control looked empty. The
   quick-create hooks now seed the cache before invalidating.

---

## 6. Still open, ranked

Ordered by touches saved per unit of work.

1. **Extract the HSN summary table** (§2). Feeds exact per-line GST,
   HSN mapping, and the pre-tax-discount signal. Highest leverage item
   on this list by a wide margin.
2. **Pre-tax discount** (§1). Compliance-affecting, and mis-rating these
   bills today. Design is settled above and needs no payload change.
3. **Learned item mapping** (§3). Removes the most repeated manual touch
   in the product.
4. **Extract and match on IRN** (§4). Makes duplicate detection exact
   for e-invoiced bills.
5. **Extract place of supply** (§4). Removes a whole class of
   IGST-vs-CGST/SGST error.
6. **Extract unit/UOM** (§4).

---

## 7. Second pass — the five gaps, now examined

Each of these was listed as unexamined in the first pass. All five have
now been looked at and four of them turned up something.

### 7.1 `calculate_gst_rate` — now dead code, risk gone

Zero call sites remain anywhere in `apps/`. Five files still import it
(`vendor_bills`, `sales_bills`, `expense_bills`, `payment_bills`,
`receipt_bills`) and none calls it — the last two callers were the ones
replaced in §5.1.

So the "expects a tax-inclusive amount, reads 28% as ~40%" hazard is no
longer reachable. What is left is five unused imports and a function
that looks live. **Cleanup, not a bug.** Delete the function and the
imports, so nobody reaches for it again.

### 7.2 Zoho — a different bug of the same family, and worse

Zoho products carry **no per-line igst/cgst/sgst at all**, so the
allocate-then-derive bug cannot exist there. Instead each line carries a
`taxes` FK, and Zoho Books computes the tax from it. The assignment is:

```python
taxes = find_appropriate_zoho_tax_ledger(
    organization, 'igst' if igst_val > 0 else 'cgst', igst_val or cgst_val)
```

Three problems, compounding:

1. `igst_val` / `cgst_val` are **bill-level totals**, so every line on
   the bill is assigned the *same* tax. On a mixed-rate bill the exempt
   line gets the taxable line's rate.
2. `find_appropriate_zoho_tax_ledger` takes a `tax_amount` argument and
   **never uses it** — it appears only in the signature. Selection is by
   name pattern plus `.first()`, so an org holding "CGST 2.5%", "CGST
   6%" and "CGST 9%" gets whichever row the database returns first.
3. Its last fallback is "the tax most commonly used by this org" — a
   guess with no relationship to the bill in hand.

This is worse than the Tally bug was. Tally stored a wrong number that
the verify screen could show; Zoho sends a `tax_id` and **Zoho Books
recomputes the tax from it**, so a wrong rate becomes a wrong invoice in
the client's books.

### 7.3 Payment / Receipt / Journal — clean on this, gaps elsewhere

Their products carry only `amount` — no per-line GST — so the §5.1 bug
class is absent by construction.

Two things did turn up:

- They have **no `discount` / `freight` / `cess` fields**, only
  `round_off`. Expense (Journal) absorbs such amounts into a generic
  `other_adjustment`, so the voucher still balances, but Tally receives
  one lumped "other adjustment" line instead of three correctly named
  ledger entries. Fidelity gap, not an arithmetic one.
- Expense/Payment/Receipt reconcile on a proper DR == CR rule rather
  than the vendor/sales `subtotal + taxes + adjustments` formula. That
  is the right model for a journal and is not a defect.

### 7.4 Reverse charge — does not interact, but is missing where it matters most

`sync_rcm_lines` works off `gst_lines`, and `gst_lines` exists only on
**Expense, Payment and Receipt**. Vendor and Sales have no such model.
So RCM and the new line-GST resolver never meet, and §5.1 could not have
disturbed it.

The real finding is what that table shows:

| Voucher | `is_reverse_charge` | `gst_lines` |
|---|---|---|
| **Purchase (Vendor)** | **no** | no |
| Sales | no | no |
| Expense (Journal) | yes | yes |
| Payment | yes | yes |
| Receipt | no | yes |

**A Purchase Voucher cannot be marked reverse charge.** For a trading
business that is where most RCM actually lands — goods and services
under s.9(3) arrive as a tax invoice stamped "Tax payable under reverse
charge", and that is a purchase, not a journal.

Tracker task #28 asked for RCM "on Journal / payment voucher template",
and that is genuinely delivered. Purchase-side RCM is a **new** piece of
work, not an unfinished one.

### 7.5 Freight — the same bug as the discount, reproduced

Under s.15(2)(c) freight charged on the invoice is part of the taxable
value. The reconciliation formula adds it *after* tax:

    subtotal + igst + cgst + sgst + cess + discount + freight

and freight is a bill-level field, not a product line, so it is outside
the base `assign_line_gst` works from.

Reproduced on a bill of ₹10,000 goods + ₹500 taxable freight at 18%:

    strategy  = proportional-fallback
    line rate = 18.9%        (should be 18%)
    cgst      = 945.00       (amount right, rate wrong)

Putting the freight into the base instead gives `single-rate`, both
lines at 18%.

So freight needs the same treatment as the pre-tax discount in §1: a
flag saying whether it is taxable, and inclusion in the taxable base
when it is. Same shape of fix, same lack of payload change.

**Cess is different and less urgent.** Compensation cess is taken from
the bill as printed rather than derived, so there is no derivation to
get wrong. What is missing is only a check that the printed cess is
consistent with the taxable value and cess rate.

## 8. TODO — the work, broken down

Nothing below has been started. Each item says what it touches and what
"done" means, so it can be handed to someone without re-deriving the
analysis.

### T1 — Extract the HSN summary table  ⭐ do this first

Touches: `apps/common/services/bill_analysis.py` (prompt + schema),
`apps/module/tally/views/{vendor,sales}_bills.py` (read the new fields).

- [ ] Add to the extraction schema, per line: `hsn`, `gst_rate`, `unit`
- [ ] Add a top-level `hsn_summary: [{hsn, taxable_value, cgst_rate,
      cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount}]`
- [ ] Prompt: the HSN/SAC summary table near the bottom of a tax invoice
      is authoritative for rate and taxable value; read it and use it to
      cross-check the line amounts
- [ ] Map `hsn_summary` rate back onto each line by HSN, so
      `assign_line_gst` receives a non-null `rate` and takes its
      `ocr-rates` branch
- [ ] Store `hsn` on `TallyVendorAnalyzedProduct` / `TallySalesAnalyzedProduct`
      (new field + migration)

Done when: a mixed-rate bill resolves via `ocr-rates`, not
`solved-mixed`, and the per-line HSN is stored.

Note: this is also tracker tasks #6 / #16 / #26.

### T2 — Pre-tax discount

Touches: both analysed-bill models (+ migration),
`apps/common/services/line_gst.py`, both bill views, the five detail pages.

- [ ] `discount_before_tax` BooleanField, default `False`, on
      `TallyVendorAnalyzedBill` and `TallySalesAnalyzedBill`
- [ ] `assign_line_gst(..., taxable_base=None)` — when given, the solver
      and the single-rate check work off it instead of Σ(line amounts)
- [ ] Per-line `taxable_value` = `amount − (amount/subtotal × discount)`;
      line tax computed from `taxable_value × rate`
- [ ] New invariant in the tests: `tax == taxable_value × rate`, and
      `taxable_value == amount` whenever the flag is off
- [ ] Extraction: decide pre- vs post-tax (the `hsn_summary` taxable
      value from T1 settles it — if it sums below the line amounts, the
      discount was pre-tax)
- [ ] UI: toggle beside the discount field, showing the resulting
      taxable base
- [ ] Confirm the voucher still balances: `Σ(items) + Σ(ledger entries)
      == total`, with items gross and the discount as its own entry

Done when: the real bill that prompted this posts with GST on the net
sub-total, line amounts matching the printed invoice, and no payload
change.

Depends on T1 for the automatic pre/post decision, but the model and
solver work can start without it.

### T3 — Learned item mapping

Touches: a new model + migration, the verify save path, both detail pages.

- [ ] Model `ItemDescriptionMapping(organization, vendor, normalised_description,
      stock_item, hit_count, last_used_at)`, unique on the first three
- [ ] On verify, for each line where the operator picked an item, upsert
      the mapping
- [ ] On analysis, look the mapping up before falling back to exact-name
- [ ] Normalisation: lowercase, collapse whitespace and punctuation,
      strip pack sizes — same shape as `normalizeVendorName`
- [ ] Never auto-fill from a mapping belonging to a different vendor
      unless the description matches exactly

Done when: the second bill from a vendor arrives with its items already
picked.

This is tracker task #4 and needs no model/AI.

### T4 — Extract and match on IRN

Touches: extraction schema, bill models (+ migration), duplicate detection.

- [ ] Extract `irn`, `ack_no`, `ack_date`
- [ ] Store `irn` on the bill, indexed
- [ ] Duplicate detection: an exact IRN match is a certain duplicate —
      check it before content hashing
- [ ] Show the IRN on the detail page (it is what an auditor cross-checks
      against the government portal)

Done when: re-uploading the same e-invoice is caught by IRN, not by hash.

### T5 — Place of supply

Touches: extraction schema, bill models (+ migration), the inter-state decision.

- [ ] Extract `place_of_supply` (name and/or state code)
- [ ] Prefer it over the GSTIN state-code comparison when present
- [ ] Flag on the detail page when the two disagree — that is exactly the
      case worth a human look

Done when: a bill whose place of supply differs from the buyer's
registered state gets the right tax heads.

### T6 — Unit / UOM per line

- [ ] Extract `unit` per line (T1 covers the schema change)
- [ ] Carry it into the stock-item quick-create default and the sync payload
- [ ] Flag when the bill's unit disagrees with the Tally item's unit

### T7 — Close the audit gaps from §7

Each is an investigation, not a known bug. Size unknown until looked at.

- [ ] Read Zoho's line-GST path properly for the §5.1 bug class
- [ ] Trace Payment / Receipt / Journal voucher paths
- [ ] Check reverse charge against the new line-GST resolver
- [ ] Audit remaining `calculate_gst_rate` call sites for the
      tax-inclusive-amount assumption
- [ ] Check whether freight and cess carry the same pre/post-tax
      ambiguity the discount did

### Suggested order

T1 first — it unblocks the automatic half of T2 and makes T3's fuzzy
matching safe. Then T2, because it is compliance-affecting and bills are
being mis-rated today. T3 next for the volume of touches it removes.
T4–T6 are each small and independent. T7 runs alongside.

### T8 — Zoho per-line tax (from §7.2)

Touches: `apps/module/zoho/views/helpers.py`,
`apps/module/zoho/views/{vendor,sales,expense,journal}_bills.py`.

- [ ] `find_appropriate_zoho_tax_ledger` must select on **rate**, not
      just name — it already receives `tax_amount` and ignores it
- [ ] Resolve the tax per line from that line's own rate (which T1
      supplies), not from the bill-level totals
- [ ] Drop the "most commonly used tax" fallback, or make it return
      nothing and surface "no tax matched" to the operator; a guess that
      posts to the client's books is worse than a blank
- [ ] Zero-rated lines must get the zero-rated tax, or none

Done when: a mixed-rate Zoho bill posts each line at its own rate, and
an exempt line carries no tax in Zoho Books.

Higher severity than it looks: Zoho Books recomputes tax from the id we
send, so this writes wrong numbers into the client's accounts.

### T9 — Taxable freight (from §7.5)

- [ ] `freight_taxable` BooleanField on both vendor and sales analysed
      bills, default `False` (preserves today's behaviour)
- [ ] When set, freight joins the taxable base passed to
      `assign_line_gst` — shares T2's `taxable_base` parameter
- [ ] Extraction: freight appearing above the taxable-value line, or
      included in the HSN summary's taxable value, is taxable
- [ ] Cess: add a consistency check (printed cess vs taxable × cess
      rate) rather than deriving it

Do this together with T2 — it is the same parameter and the same tests.

### T10 — Reverse charge on Purchase Vouchers (from §7.4)

- [ ] `is_reverse_charge` on `TallyVendorAnalyzedBill`
- [ ] A `gst_lines` model for vendor bills, or reuse the per-line
      product tax to build the mirror rows
- [ ] Wire `sync_rcm_lines` to the purchase path
- [ ] Extraction: "Tax payable under reverse charge" on the invoice sets
      the flag
- [ ] Confirm the voucher still balances with the liability rows added

This is new work, not a gap in tracker #28 — that task asked for Journal
and Payment and got them.

### T11 — Cleanup (from §7.1, §7.3)

- [ ] Delete `calculate_gst_rate` and its five unused imports
- [ ] Decide whether Expense/Payment/Receipt should gain real
      `discount` / `freight` / `cess` fields instead of lumping them
      into `other_adjustment`

### Revised order

T1 → then T2 **and T9 together** (one parameter, one test suite) → T8
(severity: it writes to the client's books) → T3 → T10 → T4–T6 → T11.

---

## T10 — reverse charge on Purchase Vouchers (done 2026-10-29)

### The constraint that shaped it

The Journal and Payment stacks keep GST in a `gst_lines` table where
every row carries its own debit/credit, so RCM there is extra rows in
that table. A purchase voucher has no such table: its GST lives per
product and is rolled up into the payload's `<ledger>` rows.

The shared XML emitter already supports `debit_or_credit` as an optional
element — but its own docstring records that **purchase vouchers have
never been sent it**, because DR/CR is implicit there.

That is the whole risk. If the deployed purchase importer ignores the
field, the liability rows post as further **debits**: the input credit
doubles, the liability never appears, and the voucher still balances
against the party total. Nothing looks wrong. It is simply wrong, by the
whole tax amount, on a GST return.

### What was built

- `is_reverse_charge` on `TallyVendorAnalyzedBill`, default `False`.
- `purchase_rcm_rows()` — builds the liability from the bill's own
  rolled-up GST, one row per tax head. CGST and SGST are owed to
  different governments, so a single lump cannot be apportioned back.
- `purchase_rcm_blockers()` — why this bill must not sync yet.
- `VENDOR_BILL_EMIT_RCM_ROWS = False` in `vendor_bills.py`, the same
  shape as the existing `*_EMIT_LEDGER_EXTRAS` switches, which exist for
  exactly this question.
- The sync **refuses** a reverse-charge purchase bill rather than
  posting half a voucher — when no RCM ledger is configured, when the
  bill carries no GST, or while the emit switch is off.
- Extraction reads the stamp ("Tax payable under reverse charge",
  "RCM: Yes") into `is_reverse_charge`.
- 9 tests in `apps/common/tests/test_purchase_rcm.py`.

### Proof the connector is unaffected

Two directions, both measured rather than asserted:

```
emit switch OFF, RCM flag ON   -> payload byte-identical to today
emit switch ON,  RCM flag ON   -> 3 ledger rows become 5
                                  CGST RCM Payable  900.00  credit
                                  SGST RCM Payable  900.00  credit
emit switch OFF               -> no row carries debit_or_credit at all
```

Every other payload-building function (`get_structured_bill_data`,
`vendor_bill_sync`, `sales_bill_sync`, the two sync-list feeds,
`vendor_bill_sync_external_handler`) was AST-compared against HEAD and
is **identical**.

### The one thing left, and it is not ours

**Ask the Tally developer: does the purchase importer read
`debit_or_credit` on a `<ledger>` row?**

- **Yes** → flip `VENDOR_BILL_EMIT_RCM_ROWS` to `True`. That is the
  entire change; everything else is already in place and tested.
- **No** → the TDL needs to read it before reverse-charge purchase bills
  can post. Until then the sync refuses them with a message saying so,
  which is the correct behaviour either way.

Nothing about this is blocking for ordinary bills: with the switch off
and the flag off, which is every bill in the database today, the
connector receives exactly what it receives now.
