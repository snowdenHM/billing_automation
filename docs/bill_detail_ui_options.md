# Bill detail page — three directions

Written for: the product owner and whoever signs off on the redesign.

Two complaints to answer here. One is measurable, one is a judgement:

1. Purchase, Sales and Journal do not look like the same product.
2. The detail page is not engaging.

The second is the harder one, so the audit comes first — half of "not
engaging" turns out to be "three pages that behave differently and a
single column you scroll for a long time".

---

## What is actually inconsistent today

Measured from the source, not from memory.

| | Purchase | Sales | Journal |
|---|---|---|---|
| Route | `/tally/vendor-bill` | `/tally/sales-bill` | `/tally/expense-bill` |
| Menu calls it | Purchase › Vendor Bill | Sales › Sales | **Journal Entry** |
| Page header says | Vendor bill | Sales bill | **Expense bill** |
| Toasts say | vendor bill | sales bill | **Journal entry** |
| Party section | Bill from | **Bill from** | **Bill Information** |
| Line section | Line items | Line items | **Credit / Debit items** |
| Tax section | GST Items | GST Items | **Tax and Other Items** › GST Items |
| Adjustments | Its own section | Its own section | **Folded into the tax section** |
| File size | 5,549 lines | 5,556 lines | 4,486 lines |

Four things fall out of that table:

- **Journal has three names.** The menu says Journal Entry, the URL says
  expense-bill, the header says Expense bill, the toasts say Journal
  entry. A user who is told "open the journal entry" has to guess.
- **Sales says "Bill from" above a customer.** It is a copy of the
  purchase page and the word was never changed. On a sales invoice the
  party is who we billed *to*.
- **Journal moves the furniture.** Same four things to fill in, grouped
  and named differently, so muscle memory from one screen does not
  carry to the next.
- **5,549 and 5,556 lines are the same page twice.** Every fix has to be
  made two or three times — this document exists partly because that is
  how the inconsistencies got in.

**None of the three options below is worth doing without fixing this
first**, and the fix is the same in all three: one `BillDetailShell`
component holding the layout, with the three pages passing in their own
labels and sections. That is the piece that makes them consistent; the
options are about what the shell *is*.

---

## What the page has to hold

Any option has to fit all of this, because it is what posting a voucher
to Tally actually needs:

- the scanned document, readable and zoomable
- party, invoice number, two dates, GSTIN
- place of supply, IRN, Ack No., reverse-charge flag
- line items: item, HSN, ledger, price, qty, amount, GST%
- GST by rate, with a ledger per rate
- adjustments: cess, discount, freight, round-off — each with a ledger
  and, for discount and freight, a tax-base choice
- the three tax-base toggles (discount, freight, MRP-inclusive lines)
- whether our total agrees with the printed one
- blockers that stop Verify, and advisory checks that do not
- notes, activity, currency, approval
- Prev / Next, Save, Verify, Sync

Today all of it is one scrolling column. That is the root of "not
engaging": nothing tells the operator where they are, how much is left,
or whether they are close to done.

---

## Option A — Anchored document, one column

The smallest change that fixes the complaint. Keep today's two panes.
Make the document actually stay put, and put a progress rail on top of
the form.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 20261023TB00006 · Analysed      [‹ Prev] [Next ›] [⏱] [₹] [Save] [✓] │
├──────────────────────────────────────────────────────────────────────┤
│  ①Party ──── ②Lines ──── ③Tax ──── ④Post        Bill ₹397.05  Δ ₹0 │ ← sticky
├───────────────────────────┬──────────────────────────────────────────┤
│                           │  BILL FROM                               │
│      ┌─────────────┐      │  Vendor [Kailash Medical      ▾] [+]     │
│      │             │      │  GSTIN  [20ANXPA9219M2ZM] ✓ matches      │
│      │   scanned   │      │  Inv No [A-000057]  Date [19/05/2026]    │
│      │  document   │      │  PoS [20-Jharkhand]  IRN [        ]      │
│      │             │      │                                          │
│      │   sticky,   │      │  LINE ITEMS            [Consolidate ◯]   │
│      │   zoomable  │      │  ┌────────────────────────────────────┐  │
│      │             │      │  │ Item │HSN │Ledger│Rate│Qty│Amt│GST││  │
│      │  [−][+][⟳]  │      │  └────────────────────────────────────┘  │
│      └─────────────┘      │  Sub-total ₹397.05  [Incl GST|Before]    │
│                           │                                          │
│                           │  GST ITEMS                               │
│                           │  5%  ₹378.14   CGST 9.46   SGST 9.46     │
│                           │                                          │
│                           │  ADJUSTMENTS                             │
│                           │  Discount  [GST After|Post GST]  0.00    │
│                           │  Freight   [GST After|Post GST]  0.00    │
│                           │  Round off                       0.00    │
│                           │  ─────────────────────────────────────   │
│                           │  TOTAL ₹397.05        ✓ matches bill     │
└───────────────────────────┴──────────────────────────────────────────┘
```

**Changes:** document pane becomes sticky and full height instead of
scrolling away. A sticky rail shows the four stages and the live
bill-vs-ours delta. Section order becomes identical across the three
pages. Journal gets renamed to one name everywhere.

**Fixes:** consistency, the scroll-away document (today the operator
scrolls the image out of view exactly when they need it), and "where am
I".

**Does not fix:** it is still one long column. The rail tells you there
are four stages; it does not shorten them.

**Cost:** smallest. Mostly moving existing markup into the shell.
**Risk:** lowest. No change to how anything is entered.

---

## Option B — Guided steps

Same two panes, but the form is four steps instead of one column. A
finished step collapses to one line carrying its own headline figure,
so the page gets shorter as the operator works.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 20261023TB00006 · Analysed      [‹ Prev] [Next ›] [⏱] [₹] [Save] [✓] │
├───────────────────────────┬──────────────────────────────────────────┤
│                           │ ✓ 1  PARTY                      [edit]   │ ← done,
│      ┌─────────────┐      │      Kailash Medical · A-000057          │   folded
│      │             │      ├──────────────────────────────────────────┤
│      │   scanned   │      │ ✓ 2  LINE ITEMS                 [edit]   │
│      │  document   │      │      3 items · ₹397.05                   │
│      │             │      ├──────────────────────────────────────────┤
│      │   sticky,   │      │ ▸ 3  TAX                                 │ ← open
│      │   zoomable  │      │   Line amounts [Include GST|Before GST]  │
│      │             │      │   ┌────────────────────────────────────┐ │
│      │  [−][+][⟳]  │      │   │Rate│Taxable│ CGST  │ SGST  │ Total│ │
│      └─────────────┘      │   │ 5% │378.14 │ 9.46  │ 9.46  │18.92 │ │
│                           │   └────────────────────────────────────┘ │
│   ⚠ Line amounts look     │   Discount [GST After|Post GST]  −17.05  │
│     MRP-priced — tax is   │   Freight  [GST After|Post GST]    0.00  │
│     already inside them.  │                                          │
│     [Mark as inclusive]   │              [ Back ]  [ Continue → ]    │
│                           ├──────────────────────────────────────────┤
│                           │ ○ 4  REVIEW & POST                       │
└───────────────────────────┴──────────────────────────────────────────┘
```

**Changes:** the form is a stepper. Each step collapses when complete
and shows its figure. Advisory checks move to the document pane, beside
the evidence they refer to, and carry a one-click fix where we have one
("Mark as inclusive" sets the toggle). Verify unlocks at step 4.

**Fixes:** the length problem, and the discoverability problem — the
three tax-base toggles are currently easy to miss, and step 3 puts them
in front of the operator at the moment they matter.

**Costs:** a confident operator fixing one field now clicks through a
step. Mitigate with an "expand all" that returns to Option A's layout
and is remembered per user.

**Cost:** medium. Same fields, new container plus step state.
**Risk:** medium. This is the one to put in front of two real operators
before building — it is the only option that changes how people work.

---

## Option C — Reconciliation cockpit

Frame the whole page as one job: *make our figures agree with the
printed bill*. A balance strip sits under the header and never
scrolls away.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 20261023TB00006 · Analysed      [‹ Prev] [Next ›] [⏱] [₹] [Save] [✓] │
├──────────────────────────────────────────────────────────────────────┤
│           ON THE BILL        OURS         │                          │
│ Lines        397.05        397.05   ✓     │   ✓ RECONCILED           │ ← sticky
│ Tax           18.92         18.92   ✓     │     Ready to verify      │
│ Discount     −17.05        −17.05   ✓     │                          │
│ Round off      0.00          0.00   ✓     │   [ Verify & Post ]      │
│ TOTAL        380.00        380.00   ✓     │                          │
├───────────────────────────┬──────────────────────────────────────────┤
│      ┌─────────────┐      │  PARTY      Kailash Medical  A-000057    │
│      │   scanned   │      │  ────────────────────────────────────    │
│      │  document   │      │  LINES   [Incl GST|Before GST]           │
│      │             │      │  ┌────────────────────────────────────┐  │
│      │   sticky,   │      │  │Item │HSN│Ledger│Rate│Qty│Amt│GST%│ │  │
│      │   zoomable  │      │  └────────────────────────────────────┘  │
│      │  [−][+][⟳]  │      │  TAX  5% ₹378.14 → CGST 9.46 SGST 9.46   │
│      └─────────────┘      │  ADJUSTMENTS  Disc [GST After|Post GST]  │
└───────────────────────────┴──────────────────────────────────────────┘

When it does not agree, the strip is the alarm and the diagnosis:

│ Tax           18.92         19.86   ✗  +0.94                         │
│ TOTAL        397.05        416.91   ✗  +19.86                        │
│ ⚠ The gap is exactly the tax. The line amounts look MRP-priced.      │
│   [ Mark lines as including GST ]                                    │
```

**Changes:** a persistent bill-vs-ours strip, red until it reconciles.
Every row names which figure disagrees and by how much. Where the gap
has a known shape — the gap equalling the tax means MRP pricing — it
says so and offers the fix. The form below gets denser, because the
strip carries the summary.

**Fixes:** the most engaging of the three for someone who does this all
day, because it gives the work a scoreboard and a finish line. It also
turns the checks we already compute into something actionable instead of
a banner to scroll past.

**Costs:** the densest screen of the three, and the most new logic — the
"gap equals the tax" diagnosis has to be written per failure shape, and
each one has to be right, or it sends the operator the wrong way.

**Cost:** largest.
**Risk:** highest, and the most upside.

---

## Recommendation

**Do the shell first, then B.**

The shell (one `BillDetailShell`, one set of section names, one name for
Journal) is not optional and is not a judgement call — it is the fix for
complaint 1, it pays for itself the next time any of these bugs recurs,
and all three options need it.

Then B, because the complaint is "not engaging" and B is the only option
that shortens the page. A does not fix the length. C is the strongest
idea here but it is a bigger bet, and its best part — the balance strip
with a named diagnosis — can be added to B later as step 4. So B now
does not close the door on C.

One caveat, stated plainly: **C is more engaging than B for a
professional operator.** A scoreboard beats a progress bar for someone
on their fortieth bill of the day. If the higher authority is optimising
for a demo or for new users, B. If for the accountant who lives in this
screen, C is the better answer and is worth the extra build.

---

## What this does not cover

- The Zoho module has the same five detail pages with the same
  duplication. Whatever is decided here should be applied there in the
  same pass, or the two halves of the product will drift apart again.
- Mobile. All three wireframes are desktop. The document-beside-form
  layout has to become document-above-form under about 1024px, and that
  needs its own wireframe before anyone builds it.
- None of this changes the Tally sync XML payload.
