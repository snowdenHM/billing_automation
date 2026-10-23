# Tally Connector — Sales Voucher, Receipt Voucher & Reverse Charge

Written for the Tally TDL/TCP developer.

This document is the **contract between BillMunshi and the Tally TDL/TCP
connector** for three additions: a Sales voucher feed, a Receipt voucher
feed, and reverse charge on the existing Journal and Payment vouchers.

The backend is finished and serving. Every endpoint and every tag below
was captured from real responses, not drafted — see [Verifying against a
live response](#verifying-against-a-live-response) to reproduce them.

---

## Scope

Each piece is a copy of something the connector already does.

| # | Work | Base to copy | What changes |
|---|---|---|---|
| 1 | Sales voucher handler | Purchase handler | 4 tag renames |
| 2 | Receipt voucher handler | Payment handler | 2 tag renames |
| 3 | Reverse charge | — | Nothing to build |

The XML envelope, the `<ledgers>` collection, the `<items>` collection,
the inline-master extras (`<parent>`, `<unit>`, `<gst_rate>`,
`<hsn_code>`) and the ampersand handling are all unchanged. Both new
feeds use the same document root as the existing ones:

```
XML Object Path : "data:1"
```

No collection needs recompiling for a changed root.

---

## 1. Sales Voucher

```
GET  https://billmunshi.com/api/v1/tally/org/<ORG_ID>/sales-bills/sync_bills/
Authorization: Api-Key <ORG_API_KEY>
Content-Type: application/xml; charset=utf-8
```

Returns the same `<data>` root with zero or more `<bill>` children, and
feeds bills whose status is `Synced` and which Tally has not yet
confirmed — identical selection to the Purchase feed.

### Full response (inventory ON)

```xml
<data>
  <bill>
    <id>3135103d-5638-4ffa-8fb4-15cf7416ab4b</id>
    <bill_no>INV/2026-27/0042</bill_no>
    <bill_date>23-10-2026</bill_date>
    <voucher_type>Sales</voucher_type>
    <customer_name>ACME TRADERS</customer_name>
    <customer_gst_in>27AABCA1234D1ZX</customer_gst_in>
    <customer_parent>Sundry Debtors</customer_parent>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>11800.00</total_amount>
    <notes>Sales invoice to ACME TRADERS entered via BillMunshi https://billmunshi.com/tally/sales-bill/3135103d-…</notes>
    <ledgers>
      <ledger>
        <amount>900.00</amount>
        <ledger>CGST (Output) @ 9%</ledger>
        <parent>Duties &amp; Taxes</parent>
        <rate>18%</rate>
      </ledger>
      <ledger>
        <amount>900.00</amount>
        <ledger>SGST (Output) @ 9%</ledger>
        <parent>Duties &amp; Taxes</parent>
        <rate>18%</rate>
      </ledger>
    </ledgers>
    <items>
      <item>
        <name>1" PACKING TAPE</name>
        <unit>Nos</unit>
        <gst_rate>18%</gst_rate>
        <hsn_code />
        <details>Box of 12</details>
        <sales_ledger>SALES ACCOUNTS @18%</sales_ledger>
        <sales_ledger_parent>Sales Accounts</sales_ledger_parent>
        <price>500.00</price>
        <quantity>20</quantity>
        <amount>10000.00</amount>
      </item>
    </items>
  </bill>
</data>
```

### Inventory OFF

When the organisation has `tally_product_allow_sync` off, there is no
`<items>` block at all and each line's amount arrives as another
`<ledger>` row against its sales account:

```xml
<ledgers>
  <ledger>
    <amount>10000.00</amount>
    <ledger>SALES ACCOUNTS @18%</ledger>
    <parent>Sales Accounts</parent>
  </ledger>
  <ledger>
    <amount>900.00</amount>
    <ledger>CGST (Output) @ 9%</ledger>
    <parent>Duties &amp; Taxes</parent>
    <rate>18%</rate>
  </ledger>
  …
</ledgers>
```

Exactly how the Purchase feed behaves under the same setting.

### The renames from the Purchase handler

| Purchase feed | Sales feed |
|---|---|
| `<vendor_name>` | `<customer_name>` |
| `<vendor_gst_in>` | `<customer_gst_in>` |
| `<vendor_parent>` | `<customer_parent>` |
| `<purchase_ledger>` (inside `<item>`) | `<sales_ledger>` |
| `<purchase_ledger_parent>` (inside `<item>`) | `<sales_ledger_parent>` |

`<voucher_type>` carries `Sales` rather than `Purchase`.

**That is the whole difference.** Every other tag, its ordering, its
formatting (amounts always two decimals, dates always `DD-MM-YYYY`) is
the Purchase feed's.

### Posting side

A sales voucher debits the party and credits revenue and output GST —
the mirror of a purchase. The feed carries no `<debit_or_credit>` on
this voucher type, the same as the Purchase feed: the direction is
implicit in the voucher type, and the TDL decides it.

`<customer_parent>` defaults to `Sundry Debtors` and is only read when
Tally has to create the ledger.

---

## 2. Receipt Voucher

```
GET  https://billmunshi.com/api/v1/tally/org/<ORG_ID>/receipt-vouchers/sync_bills/
Authorization: Api-Key <ORG_API_KEY>
Content-Type: application/xml; charset=utf-8
```

### Full response

```xml
<data>
  <bill>
    <id>4afad31d-69b1-47b6-b4ae-4fd96c3b298c</id>
    <bill_no>RCPT/2026-27/0007</bill_no>
    <bill_date>23-10-2026</bill_date>
    <voucher_type>Receipt</voucher_type>
    <customer />
    <receipt_mode>Bank</receipt_mode>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>11800.00</total_amount>
    <notes>Receipt voucher entered via BillMunshi https://billmunshi.com/tally/receipt-voucher/4afad31d-…</notes>
    <ledgers>
      <ledger>
        <amount>11800.00</amount>
        <ledger>HDFC Bank</ledger>
        <parent>Bank Accounts</parent>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>11800.00</amount>
        <ledger>SALES ACCOUNTS @18%</ledger>
        <parent>Sales Accounts</parent>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
    </ledgers>
  </bill>
</data>
```

No `<items>` block — a Receipt voucher, like a Payment voucher, posts
everything through `<ledgers>`.

### The renames from the Payment handler

| Payment feed | Receipt feed |
|---|---|
| `<vendor>` | `<customer>` |
| `<payment_mode>` | `<receipt_mode>` |

`<voucher_type>` carries `Receipt` rather than `Payment`.

### Two things worth knowing

**`<customer>` is deliberately empty.** The party picker on this screen
is for identification only and its name and GSTIN are never sent — the
same rule the Payment feed follows (Client Correction 26). Do not treat
an empty `<customer>` as missing data.

**The Bank/Cash row is debited.** Money arrives rather than leaves, so
the Bank/Cash ledger carries `debit` where the Payment feed carries
`credit`. Nothing is hardcoded at either end: `<debit_or_credit>` is on
every row and reflects what the operator confirmed on the verify screen.
Read the tag, don't assume the side from the voucher type.

`<receipt_mode>` holds the classification (`Bank` or `Cash`), not the
ledger name. The ledger itself is the first `<ledger>` row.

---

## 3. Reverse Charge — nothing to build

Under RCM the recipient pays the GST instead of the supplier, so the
same tax figure belongs on the voucher twice: once as input credit, once
as the liability owed to the government.

Both the Journal and Payment feeds already emit one `<ledger>` row per
GST line, each carrying its own `<debit_or_credit>`. The RCM liability
arrives as more rows of exactly that shape:

A ₹50,000 rent bill at 18% RCM, as the Journal feed actually returns it:

```xml
<data>
  <bill>
    <id>e7e2e266-a374-4357-951e-2900eb4bcd9e</id>
    <bill_no>RCM/001</bill_no>
    <bill_date>23-10-2026</bill_date>
    <voucher_type>Journal</voucher_type>
    <vendor_name>SHREE ENTERPRISES</vendor_name>
    <vendor_gst_in/>
    <vendor_parent>Sundry Creditors</vendor_parent>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>50000.00</total_amount>
    <notes>Bill from SHREE ENTERPRISES entered via BillMunshi https://billmunshi.com/tally/expense-bill/e7e2e266-…</notes>
    <ledgers>
      <ledger>
        <amount>50000.00</amount>
        <ledger>SHREE ENTERPRISES</ledger>
        <parent>Sundry Creditors</parent>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>50000.00</amount>
        <ledger>Office Rent</ledger>
        <parent>Indirect Expenses</parent>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>4500.00</amount>
        <ledger>CGST (Input) @ 9%</ledger>
        <parent>Duties &amp; Taxes</parent>
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>4500.00</amount>
        <ledger>SGST (Input) @ 9%</ledger>
        <parent>Duties &amp; Taxes</parent>
        <rate>18%</rate>
        <debit_or_credit>debit</debit_or_credit>
      </ledger>

      <!-- the two rows reverse charge adds -->
      <ledger>
        <amount>4500.00</amount>
        <ledger>CGST RCM Payable</ledger>
        <parent>Duties &amp; Taxes - RCM</parent>
        <rate>RCM 18%</rate>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
      <ledger>
        <amount>4500.00</amount>
        <ledger>SGST RCM Payable</ledger>
        <parent>Duties &amp; Taxes - RCM</parent>
        <rate>RCM 18%</rate>
        <debit_or_credit>credit</debit_or_credit>
      </ledger>
    </ledgers>
  </bill>
</data>
```

Debits 59,000 (rent 50,000 + input GST 9,000), credits 59,000 (party
50,000 + liability 9,000). The voucher balances.

**Note what the party is credited: 50,000, not 59,000.** Under reverse
charge the supplier is owed the base amount only — the tax is
self-assessed, debited as input and credited as liability, netting to
zero. An operator who enters the gross amount against the party is
refused at verify with a debit/credit imbalance, so an unbalanced
voucher never reaches this feed.

**No new tag, no new attribute, no new voucher type.** If the handler
already iterates `<ledgers>` and honours `<debit_or_credit>`, reverse
charge works the day the operator switches it on.

The only visible difference is the `<rate>` string: a liability row
carries `RCM ` in front of the source rate (`RCM 18%`). That marker is
for BillMunshi's own bookkeeping — it is how the backend tells its own
generated rows from an operator's. Treat `<rate>` as informational, as
you do today.

The feature is off per organisation by default, so until an operator
turns it on these feeds are byte-for-byte what they are now.

---

## 4. Status callback

The existing `tally_status` callback gains two `mode` values:

| mode | Voucher |
|---|---|
| `vendor` | Purchase (existing) |
| `expense` | Journal (existing) |
| `payment` | Payment (existing) |
| `sales` | **Sales (new)** |
| `receipt` | **Receipt (new)** |

The payload is unchanged. Echo the `<id>` from the bill back, as today,
so the backend can flag the right record as posted without matching on
bill number and date.

---

## 5. What has not changed

| Feed | Status |
|---|---|
| Purchase (`vendor-bills`) | Source file untouched. `git diff` is empty. |
| Journal (`expense-bills`) | Payload builder untouched; the only edits are on the verify path, behind the RCM flag. |
| Payment (`payment-vouchers`) | Same. |

Reverse charge defaults to off for every organisation, so a running
installation receives exactly the XML it receives today. No connector
release is required before the backend ships; the new feeds simply
return bills that nothing is polling yet.

---

## 6. Test checklist

Before handing the build back:

1. **Sales, inventory ON** — one invoice, mixed GST rates, posts as a
   Sales voucher with the customer debited and each `<item>` landing
   against the ledger in its `<sales_ledger>`.
2. **Sales, inventory OFF** — the same invoice with
   `tally_product_allow_sync` off posts the same totals, with the line
   amounts arriving as `<ledger>` rows instead.
3. **Sales, new customer** — a `<customer_name>` that does not exist in
   Tally is created under `<customer_parent>` with its
   `<customer_gst_in>`.
4. **Receipt** — Bank/Cash debited, counterpart credited, voucher
   balances. Confirm the empty `<customer />` is ignored rather than
   treated as an error.
5. **Reverse charge** — a Journal voucher with RCM on posts six rows and
   balances: three debits, three credits, the liability against the RCM
   ledger.
6. **Regression** — one Purchase, one Journal and one Payment voucher
   still post exactly as before.
7. **Callback** — a Sales and a Receipt voucher both report back with
   their new `mode`, and the bill flips to posted in BillMunshi.

---

## Verifying against a live response

Every example above can be reproduced:

```bash
curl -H "Authorization: Api-Key <ORG_API_KEY>" \
  "https://billmunshi.com/api/v1/tally/org/<ORG_ID>/sales-bills/sync_bills/"

curl -H "Authorization: Api-Key <ORG_API_KEY>" \
  "https://billmunshi.com/api/v1/tally/org/<ORG_ID>/receipt-vouchers/sync_bills/"
```

An empty feed returns `<data />`, which is a correct response and not an
error — a bill only appears once an operator has verified it and pressed
Sync.

---

## Related

- [`tally_sync_xml_examples.md`](tally_sync_xml_examples.md) — Purchase voucher contract
- [`tally_expense_sync_xml_examples.md`](tally_expense_sync_xml_examples.md) — Journal voucher contract
- [`tally-master-sync.md`](tally-master-sync.md) — ledger and stock-item master sync
