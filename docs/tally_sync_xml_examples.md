# Tally Vendor Bill Sync — XML Payload Reference

This document is the **contract between BillMunshi and the Tally TDL/TCP
connector** for the vendor-bill sync endpoint.

```
GET  https://billmunshi.com/api/v1/tally/org/<ORG_ID>/vendor-bills/sync/
Content-Type: application/xml; charset=utf-8
```

The endpoint always returns a `<data>` root containing zero or more
`<bill>` elements. The Tally TDL must iterate `<bill>` elements.

---

## Field reference

### Top-level `<bill>` elements (always present)

| Tag | Meaning | Example |
|---|---|---|
| `<bill_no>` | Vendor's printed invoice number | `MS/2025-26/3054` |
| `<bill_date>` | Invoice date in `DD-MM-YYYY` | `20-12-2025` |
| `<vendor>` | Vendor ledger name (Sundry Creditor in Tally) | `AGGARWAL TRADE LINK` |
| `<company>` | Tally company name to post into | `Spectrum Poly Pack and Packaging` |
| `<total_amount>` | Bill grand total — always 2 decimals | `3776.00` |
| `<notes>` | Free-text narration; includes BillMunshi back-link | `Bill from … entered via BillMunshi https://…` |
| `<taxes>` | Tax rollup grouped by ledger (see below) | — |
| `<items>` | Line items (no tax fields) | — |

### `<taxes>` block

The block contains **one child element per `(tax_type, ledger)` bucket**.
For mixed-rate bills there will be multiple `<cgst>`/`<sgst>`/`<igst>`
siblings — one per ledger. **Tally TDL must iterate each tax tag.**

Children of every tax entry:

| Child | Meaning |
|---|---|
| `<amount>` | 2-decimal positive amount (negative for `<round_off>` only) |
| `<ledger>` | Ledger name to debit/credit in Tally |
| `<rate>` | GST rate string (e.g. `18%`). **Only on `<cgst>`/`<sgst>`/`<igst>`** — informational. Not present on extras. |

Possible tax types and emit rules:

| Tag | When emitted | Multiple? |
|---|---|---|
| `<cgst>` | Intrastate bill, line CGST > 0 | yes — one per ledger |
| `<sgst>` | Intrastate bill, line SGST > 0 | yes — one per ledger |
| `<igst>` | Interstate bill, line IGST > 0 | yes — one per ledger |
| `<discount>` | `discount` > 0 at bill level | no — single |
| `<cess>` | `cess` > 0 at bill level | no — single |
| `<freight>` | `freight` > 0 at bill level | no — single |
| `<round_off>` | `round_off` ≠ 0 at bill level (sign matters; can be negative) | no — single |

**Zero-amount entries are dropped entirely.** A bill with no IGST will
have no `<igst>` element — Tally must not synthesize one.

`<cgst>` + `<sgst>` and `<igst>` are mutually exclusive on a single
bill (intrastate vs interstate determined upstream from `gst_type`).

### `<items>` block

Each `<item>` carries only what's needed to book a line in Tally —
**no per-item tax fields**. All tax info is consolidated in `<taxes>`.

| Child | Meaning |
|---|---|
| `<name>` | Stock item name as configured in Tally master |
| `<details>` | Free-text details / vendor's description from the bill |
| `<purchase_ledger>` | Dr-side purchase ledger (e.g. `PURCHASE ACCOUNTS @18%`) |
| `<price>` | Per-unit price, 2 decimals |
| `<quantity>` | Integer quantity |
| `<amount>` | Line total = price × quantity, 2 decimals |

---

## Scenario 1 — Single-rate intrastate (CGST + SGST)

The simplest and most common case. One vendor, one item, single GST rate.

**Math:** 8 × 400 = 3200 base; 18% GST = 576 (288 + 288); total 3776.

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>MS/2025-26/3054</bill_no>
    <bill_date>20-12-2025</bill_date>
    <vendor>AGGARWAL TRADE LINK</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>3776.00</total_amount>
    <notes>Bill from AGGARWAL TRADE LINK entered via BillMunshi https://billmunshi.com/tally/vendor-bill/6a0e3db4-399f-49ec-bb95-70d2e4e975c6</notes>
    <taxes>
      <cgst>
        <amount>288.00</amount>
        <ledger>CGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
      </cgst>
      <sgst>
        <amount>288.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
      </sgst>
    </taxes>
    <items>
      <item>
        <name>1" Cello Tape</name>
        <details>Trophies 9502 Medium</details>
        <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
        <price>400.00</price>
        <quantity>8</quantity>
        <amount>3200.00</amount>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 2 — Single-rate interstate (IGST)

Same vendor in another state. CGST/SGST replaced by a single IGST line.

**Math:** 3200 base × 18% = 576 IGST; total 3776.

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>MS/2025-26/3055</bill_no>
    <bill_date>20-12-2025</bill_date>
    <vendor>AGGARWAL TRADE LINK</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>3776.00</total_amount>
    <notes>Bill from AGGARWAL TRADE LINK entered via BillMunshi https://billmunshi.com/tally/vendor-bill/...</notes>
    <taxes>
      <igst>
        <amount>576.00</amount>
        <ledger>IGST (ITC) @ 18%</ledger>
        <rate>18%</rate>
      </igst>
    </taxes>
    <items>
      <item>
        <name>1" Cello Tape</name>
        <details>Trophies 9502 Medium</details>
        <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
        <price>400.00</price>
        <quantity>8</quantity>
        <amount>3200.00</amount>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 3 — Multiple line items, **same** GST rate (intrastate)

Two items both at 18%. Tax block STILL has only one `<cgst>` + one
`<sgst>` because they share a ledger — amounts are summed.

**Math:**
- Item 1: 3200 × 18% = 288 + 288 = 576 tax
- Item 2:  500 × 18% =  45 +  45 =  90 tax
- Bill total = 3700 base + 666 tax = 4366

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>MS/2025-26/3056</bill_no>
    <bill_date>21-12-2025</bill_date>
    <vendor>AGGARWAL TRADE LINK</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>4366.00</total_amount>
    <notes>Bill from AGGARWAL TRADE LINK entered via BillMunshi https://billmunshi.com/tally/vendor-bill/...</notes>
    <taxes>
      <cgst>
        <amount>333.00</amount>
        <ledger>CGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
      </cgst>
      <sgst>
        <amount>333.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
      </sgst>
    </taxes>
    <items>
      <item>
        <name>1" Cello Tape</name>
        <details>Trophies 9502 Medium</details>
        <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
        <price>400.00</price>
        <quantity>8</quantity>
        <amount>3200.00</amount>
      </item>
      <item>
        <name>Carton Box A4</name>
        <details>Brown corrugated, 6-ply</details>
        <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
        <price>50.00</price>
        <quantity>10</quantity>
        <amount>500.00</amount>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 4 — Mixed GST rates (intrastate)

Two items at **different** GST rates → tax block has **two `<cgst>`
and two `<sgst>`** entries, one per (rate, ledger). **TDL must iterate.**

**Math:**
- Item 1 (3200 @ 18%): CGST 288 + SGST 288
- Item 2 (500 @ 28%):  CGST 70  + SGST 70
- Bill total = 3700 + 716 = 4416

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>MS/2025-26/3057</bill_no>
    <bill_date>21-12-2025</bill_date>
    <vendor>AGGARWAL TRADE LINK</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>4416.00</total_amount>
    <notes>Bill from AGGARWAL TRADE LINK entered via BillMunshi https://billmunshi.com/tally/vendor-bill/...</notes>
    <taxes>
      <cgst>
        <amount>288.00</amount>
        <ledger>CGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
      </cgst>
      <cgst>
        <amount>70.00</amount>
        <ledger>CGST (ITC) @ 14%</ledger>
        <rate>28%</rate>
      </cgst>
      <sgst>
        <amount>288.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
      </sgst>
      <sgst>
        <amount>70.00</amount>
        <ledger>SGST (ITC) @ 14%</ledger>
        <rate>28%</rate>
      </sgst>
    </taxes>
    <items>
      <item>
        <name>1" Cello Tape</name>
        <details>Trophies 9502 Medium</details>
        <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
        <price>400.00</price>
        <quantity>8</quantity>
        <amount>3200.00</amount>
      </item>
      <item>
        <name>Premium Packaging Roll</name>
        <details>Bubble wrap 30m</details>
        <purchase_ledger>PURCHASE ACCOUNTS @28%</purchase_ledger>
        <price>500.00</price>
        <quantity>1</quantity>
        <amount>500.00</amount>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 5 — Mixed GST rates (interstate)

Same as Scenario 4 but interstate — two `<igst>` entries instead.

**Math:**
- Item 1 (3200 @ 18%): IGST 576
- Item 2 (500 @ 28%):  IGST 140
- Bill total = 3700 + 716 = 4416

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>MS/2025-26/3058</bill_no>
    <bill_date>21-12-2025</bill_date>
    <vendor>AGGARWAL TRADE LINK</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>4416.00</total_amount>
    <notes>Bill from AGGARWAL TRADE LINK entered via BillMunshi https://billmunshi.com/tally/vendor-bill/...</notes>
    <taxes>
      <igst>
        <amount>576.00</amount>
        <ledger>IGST (ITC) @ 18%</ledger>
        <rate>18%</rate>
      </igst>
      <igst>
        <amount>140.00</amount>
        <ledger>IGST (ITC) @ 28%</ledger>
        <rate>28%</rate>
      </igst>
    </taxes>
    <items>
      <item>
        <name>1" Cello Tape</name>
        <details>Trophies 9502 Medium</details>
        <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
        <price>400.00</price>
        <quantity>8</quantity>
        <amount>3200.00</amount>
      </item>
      <item>
        <name>Premium Packaging Roll</name>
        <details>Bubble wrap 30m</details>
        <purchase_ledger>PURCHASE ACCOUNTS @28%</purchase_ledger>
        <price>500.00</price>
        <quantity>1</quantity>
        <amount>500.00</amount>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 6 — Bill with all extras (discount + cess + freight + round-off)

Interstate bill where vendor charged cess + freight, gave a discount,
and the printed total was rounded.

**Math:**
- Item base: 3200.00
- + IGST @ 18%: +576.00
- + Cess: +32.00
- + Freight: +150.00
- − Discount: −100.00
- − Round-off: −0.50  (vendor took 50 paisa less)
- = Bill total: **3857.50**

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>MS/2025-26/3059</bill_no>
    <bill_date>22-12-2025</bill_date>
    <vendor>AGGARWAL TRADE LINK</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>3857.50</total_amount>
    <notes>Bill from AGGARWAL TRADE LINK entered via BillMunshi https://billmunshi.com/tally/vendor-bill/...</notes>
    <taxes>
      <igst>
        <amount>576.00</amount>
        <ledger>IGST (ITC) @ 18%</ledger>
        <rate>18%</rate>
      </igst>
      <discount>
        <amount>100.00</amount>
        <ledger>Discount Received</ledger>
      </discount>
      <cess>
        <amount>32.00</amount>
        <ledger>GST Cess @ 1%</ledger>
      </cess>
      <freight>
        <amount>150.00</amount>
        <ledger>Freight Inward</ledger>
      </freight>
      <round_off>
        <amount>-0.50</amount>
        <ledger>Round Off</ledger>
      </round_off>
    </taxes>
    <items>
      <item>
        <name>1" Cello Tape</name>
        <details>Trophies 9502 Medium</details>
        <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
        <price>400.00</price>
        <quantity>8</quantity>
        <amount>3200.00</amount>
      </item>
    </items>
  </bill>
</data>
```

> **Note on `<round_off>` sign:** positive amount = vendor charged extra
> (debit Round Off), negative = vendor took less (credit Round Off).
> Tally TDL must respect the sign.

---

## Scenario 7 — Zero-tax / exempted bill

Some bills have no GST at all (composition vendor, exempted goods,
or non-GST registration). The `<taxes>` block is **completely empty**
(but still present, so TDL parsing is consistent).

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>MS/2025-26/3060</bill_no>
    <bill_date>22-12-2025</bill_date>
    <vendor>FRESH MARKET FARMS</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>1500.00</total_amount>
    <notes>Bill from FRESH MARKET FARMS entered via BillMunshi https://billmunshi.com/tally/vendor-bill/...</notes>
    <taxes/>
    <items>
      <item>
        <name>Fresh Vegetables (Exempted)</name>
        <details>Mixed produce — exempt under Schedule III</details>
        <purchase_ledger>PURCHASE ACCOUNTS — Exempt</purchase_ledger>
        <price>150.00</price>
        <quantity>10</quantity>
        <amount>1500.00</amount>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 8 — Consolidated bill

When the user toggles **"Consolidate items"** in the UI, the original
line items are collapsed into a small set of consolidated lines
(typically one). The XML shape is identical — Tally cannot distinguish
consolidated from individual mode.

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>MS/2025-26/3061</bill_no>
    <bill_date>22-12-2025</bill_date>
    <vendor>AGGARWAL TRADE LINK</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>11800.00</total_amount>
    <notes>Bill from AGGARWAL TRADE LINK entered via BillMunshi https://billmunshi.com/tally/vendor-bill/...</notes>
    <taxes>
      <cgst>
        <amount>900.00</amount>
        <ledger>CGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
      </cgst>
      <sgst>
        <amount>900.00</amount>
        <ledger>SGST (ITC) @ 9%</ledger>
        <rate>18%</rate>
      </sgst>
    </taxes>
    <items>
      <item>
        <name>Packaging Material — Consolidated</name>
        <details>Tape + Boxes + Bubble Wrap (15 SKUs)</details>
        <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
        <price>10000.00</price>
        <quantity>1</quantity>
        <amount>10000.00</amount>
      </item>
    </items>
  </bill>
</data>
```

---

## Scenario 9 — Multiple bills in one response

The sync endpoint returns **all** verified-but-not-posted bills in a
single response. Tally TDL must iterate `<bill>` siblings and post
each as a separate Purchase voucher.

```xml
<?xml version='1.0' encoding='utf-8'?>
<data>
  <bill>
    <bill_no>MS/2025-26/3054</bill_no>
    <bill_date>20-12-2025</bill_date>
    <vendor>AGGARWAL TRADE LINK</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>3776.00</total_amount>
    <notes>Bill from AGGARWAL TRADE LINK entered via BillMunshi https://...</notes>
    <taxes>
      <cgst><amount>288.00</amount><ledger>CGST (ITC) @ 9%</ledger><rate>18%</rate></cgst>
      <sgst><amount>288.00</amount><ledger>SGST (ITC) @ 9%</ledger><rate>18%</rate></sgst>
    </taxes>
    <items>
      <item>
        <name>1" Cello Tape</name>
        <details>Trophies 9502 Medium</details>
        <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
        <price>400.00</price>
        <quantity>8</quantity>
        <amount>3200.00</amount>
      </item>
    </items>
  </bill>
  <bill>
    <bill_no>RT/12-25/0009</bill_no>
    <bill_date>21-12-2025</bill_date>
    <vendor>RELIABLE TRADERS</vendor>
    <company>Spectrum Poly Pack and Packaging</company>
    <total_amount>5900.00</total_amount>
    <notes>Bill from RELIABLE TRADERS entered via BillMunshi https://...</notes>
    <taxes>
      <igst><amount>900.00</amount><ledger>IGST (ITC) @ 18%</ledger><rate>18%</rate></igst>
    </taxes>
    <items>
      <item>
        <name>Stationery Set</name>
        <details>Pens, pads, clips bundle</details>
        <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
        <price>500.00</price>
        <quantity>10</quantity>
        <amount>5000.00</amount>
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
| Tax amount = 0 | Tag is **omitted entirely** from `<taxes>`. Don't iterate over and synthesize zero entries. |
| Same `(tax_type, ledger)` across multiple lines | **Summed** into a single tag with combined amount. |
| Different ledgers for same `tax_type` (mixed rates) | **Multiple sibling tags** — TDL must iterate. |
| `<cgst>` + `<igst>` together | Will never happen — bill is either intrastate or interstate, not both. |
| Negative `<round_off>` | Means vendor accepted a lower amount. Sign matters; treat as credit to Round Off ledger. |
| Item `<name>` has `"` (e.g. `1" Tape`) | Allowed inside XML element content. Pass through unchanged — no escaping needed. |
| Item `<name>` has `<`, `>`, `&` | Auto-escaped by the XML serializer. TDL's standard XML parser un-escapes them. |
| `<price>`, `<amount>`, `<total_amount>` | Always 2 decimals (e.g. `400.00`, never `400` or `400.0`). |
| `<quantity>` | Integer (no decimals). |
| `<rate>` | Only on `<cgst>`/`<sgst>`/`<igst>`. Format `<int>%`, e.g. `18%`. Informational — TDL can use for cross-check or ignore. |
| `<bill_date>` | `DD-MM-YYYY` always. Tally TDL date format. |

---

## Required Tally TDL changes (from the previous nested-per-item shape)

1. **Field renames:** `<vendor_name>` → `<vendor>`, `<company_id>` → `<company>`.
2. **Items collection rename:** `<products>/<product>` → `<items>/<item>`.
3. **Per-item tax fields are gone:** stop reading `<cgst>`/`<sgst>`/`<igst>`/
   `<cgst_ledger>` etc. from inside each item. Read all tax info from the
   bill-level `<taxes>` block instead.
4. **Tax loops must iterate:** `<cgst>`, `<sgst>`, `<igst>` can now appear
   multiple times under `<taxes>` (one per ledger). Set `Iterate=Yes` on
   the corresponding TDL collections.
5. **New optional `<rate>` child** under each GST tax entry — informational.
6. **`<round_off>` may carry a negative `<amount>`** — make sure the TDL
   doesn't strip the sign.

Nothing else changes in the wire protocol.
