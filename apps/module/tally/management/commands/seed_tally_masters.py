"""Masters for a development organisation, so party matching can be seen working.

Without these the matching code cannot be judged: every bill analysed on
a fresh dev database comes back with no party, and that looks identical
whether the matcher is working or silently broken. Seventeen real bills
were analysed here and matched nothing, because the only two creditors
in the database happened to be from an older test.

So the vendors and customers below are the real counterparties printed
on those bills, with their real GSTINs — the matcher is keyed on GSTIN,
and a made-up one would prove nothing. The tax, bank and nominal ledgers
around them are ordinary Tally groups.

A handful of parties with no bill behind them are included too, so the
dropdowns have something to scroll and a match is a match rather than
the only row on offer.

Idempotent: run it as often as you like.

    python manage.py seed_tally_masters                # first organisation
    python manage.py seed_tally_masters --org <uuid>
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.organizations.models import Organization
from apps.module.tally.models import Ledger, ParentLedger, StockItem

# (name, gstin) — taken off the analysed bills in the sample corpus.
VENDORS = [
    ("Multisoft Infocom Private Limited", "07AAECM8336J1Z6"),
    ("2 WAY ADVERTISING", "24AABFZ3811D1ZL"),
    ("TUSKER SECURITY PVT. LTD.", "06AAEFT0742E1ZP"),
    ("Saletancy Consulting Private Limited", "09AAZCS2998D1ZG"),
    ("The Southern Gujarat Chamber of Commerce & Industry", "24AAAAA7243H1ZS"),
    ("MakeMyTrip (India) Private Limited", "06AADCM5146R1ZZ"),
    ("KAILASH MEDICAL", "20ANXPA9219M2ZM"),
    ("NEW SHARDA MEDICAL", "20AGYPB7244B1ZQ"),
    ("STERISAFE HEALTH SOLUTIONS", "08ADVFS1180M1ZB"),
    ("V K Control System Private Limited", "27AAFCV2449G1Z7"),
    ("Reliance Industries Limited", "08AAACR5055K1Z7"),
    ("AMBIENCE AIRCO", "07AAVFA5574E1Z4"),
    ("ASM FACILITY MANAGEMENT", "06AATCA6940R1ZR"),
    # The bill prints the name without a space, "AvancePR", which scores
    # 0.37 against "AVANCE PR" — below the similarity threshold. Its
    # GSTIN is what makes this one match, which is the case for keying
    # on GSTIN first.
    ("AVANCE PR", "06CIXPG7651N1ZD"),
    ("COMPUTER JUNCTION", ""),
    # Handwritten, no GSTIN printed: name matching is the only route.
    ("ASHISH FURNISHERS", ""),
    ("KAILASH HOSPITAL", ""),
    # Filler, so a correct match is not the only row in the list.
    ("BHARAT TRADERS", "27AACCB4321K1Z9"),
    ("DELHI STATIONERS", "07AAGCD8812L1ZK"),
]

CUSTOMERS = [
    ("HOSTBOOKS LIMITED", "06AADCK7940H1ZG"),
    ("ACME TRADERS", "27AABCA1234D1ZX"),
    ("ZENITH SOFTWARE LLP", "29AAFFZ6677M1ZD"),
    ("ORION RETAIL PRIVATE LIMITED", "33AABCO1122N1ZT"),
]

# Rate-wise nominal and tax ledgers. GST 2.0 dropped 12% and 28% and
# added 40%, but bills from before 22 Sep 2025 still carry the old
# rates, so both sets are here.
RATES = [0, 5, 12, 18, 28, 40]

BANK_CASH = [
    ("Bank Accounts", ["HDFC Bank", "ICICI Bank", "Axis Bank"]),
    ("Cash-in-Hand", ["Cash"]),
]

NOMINAL = [
    ("Indirect Expenses", [
        "Office Rent", "Round Off", "Discount Allowed", "Freight & Cartage",
        "Travelling Expenses", "Security Charges", "Advertisement Expenses",
        "Professional Fees", "Repairs & Maintenance",
    ]),
    ("Direct Expenses", ["Carriage Inward", "Packing Charges"]),
    ("Indirect Incomes", ["Discount Received", "Interest Received"]),
]

STOCK = [
    # (name, unit, hsn, gst rate)
    ("LIZOMED I V", "NOS", "30049099", 5),
    ("CIPRO 100ML IV", "NOS", "30049099", 5),
    ("METROGYL IV INJ", "NOS", "30049099", 5),
    ("A4 COPIER PAPER 75GSM", "REAM", "48025690", 12),
    ("TONER CARTRIDGE", "NOS", "84439959", 18),
    ("ANNUAL MAINTENANCE CONTRACT", "NOS", "998719", 18),
    ("EXHIBITION STALL SPACE", "SQM", "998596", 18),
    ("SECURITY SERVICE", "NOS", "998525", 18),
]


class Command(BaseCommand):
    help = "Seed Tally masters (ledgers, parties, stock items) for development."

    def add_arguments(self, parser):
        parser.add_argument("--org", help="Organisation UUID (default: the first).")

    @transaction.atomic
    def handle(self, *args, **options):
        if options.get("org"):
            try:
                org = Organization.objects.get(id=options["org"])
            except Organization.DoesNotExist:
                raise CommandError(f"No organisation {options['org']}")
        else:
            org = Organization.objects.first()
            if org is None:
                raise CommandError("No organisations exist.")

        self.stdout.write(f"Seeding masters for {org.name}")
        made = {"parent": 0, "ledger": 0, "item": 0}

        def parent(name):
            obj, created = ParentLedger.objects.get_or_create(
                organization=org, parent=name,
                defaults={"source": "manual"},
            )
            made["parent"] += int(created)
            return obj

        def ledger(group, name, gstin=""):
            obj, created = Ledger.objects.get_or_create(
                organization=org, name=name,
                defaults={"parent": parent(group), "gst_in": gstin,
                          "source": "manual"},
            )
            # Backfill a GSTIN onto a row seeded earlier without one; the
            # matcher reads it, so an empty one quietly never matches.
            if not created and gstin and not obj.gst_in:
                obj.gst_in = gstin
                obj.save(update_fields=["gst_in"])
            made["ledger"] += int(created)
            return obj

        for name, gstin in VENDORS:
            ledger("Sundry Creditors", name, gstin)
        for name, gstin in CUSTOMERS:
            ledger("Sundry Debtors", name, gstin)

        for group, names in BANK_CASH + NOMINAL:
            for name in names:
                ledger(group, name)

        for rate in RATES:
            ledger("Purchase Accounts", f"PURCHASE ACCOUNTS @{rate}%")
            ledger("Sales Accounts", f"SALES ACCOUNTS @{rate}%")
            if rate == 0:
                continue
            half = rate / 2
            half_s = f"{half:g}"
            ledger("Duties & Taxes", f"CGST (Input) @ {half_s}%")
            ledger("Duties & Taxes", f"SGST (Input) @ {half_s}%")
            ledger("Duties & Taxes", f"CGST (Output) @ {half_s}%")
            ledger("Duties & Taxes", f"SGST (Output) @ {half_s}%")
            ledger("Duties & Taxes", f"IGST (Input) @ {rate:g}%")
            ledger("Duties & Taxes", f"IGST (Output) @ {rate:g}%")
            ledger("Duties & Taxes - RCM", f"CGST RCM @ {half_s}%")
            ledger("Duties & Taxes - RCM", f"SGST RCM @ {half_s}%")
            ledger("Duties & Taxes - RCM", f"IGST RCM @ {rate:g}%")

        ledger("Duties & Taxes", "TDS Payable")
        ledger("Current Liabilities", "Other Adjustment")

        item_parent = parent("Primary")
        for name, unit, hsn, rate in STOCK:
            _, created = StockItem.objects.get_or_create(
                organization=org, name=name,
                defaults={"parent": item_parent, "unit": unit,
                          "hsn_code": hsn, "gst_rate": rate,
                          "gst_applicable": True, "source": "manual"},
            )
            made["item"] += int(created)

        self.stdout.write(self.style.SUCCESS(
            f"groups +{made['parent']}, ledgers +{made['ledger']}, "
            f"stock items +{made['item']}"
        ))
        self.stdout.write(
            f"totals: {ParentLedger.objects.filter(organization=org).count()} groups, "
            f"{Ledger.objects.filter(organization=org).count()} ledgers, "
            f"{StockItem.objects.filter(organization=org).count()} stock items"
        )
