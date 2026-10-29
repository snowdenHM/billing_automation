"""Create a vendor or customer in the client's Zoho Books.

Everything else in this module reads from Zoho. This writes a master
into it, which is a different kind of act and is treated as one.

Why it exists: a bill arrives from a vendor Zoho has never heard of, and
the app's answer was "sync vendors from Zoho Books first". So the
operator left the app, created the vendor in Zoho, came back, re-synced,
and carried on — the most common interruption in the product's core
loop, on the most common kind of bill.

Why it is not automatic. A vendor master is shared and permanent: the
client's accountant reads GST returns, TDS, ageing and payments off it.
OCR'd names are not consistent — "Acme Traders", "ACME TRADERS PVT LTD",
"Acme Trdrs" — so creating one per unrecognised bill would quietly breed
near-duplicate vendors, and a duplicated vendor splits the ledger and
the ageing report in a way nobody notices for a month. That is worse
than the unmapped bill it was meant to save. So:

* an operator asks for it explicitly, having seen the fields;
* existing contacts are looked for first and offered back instead;
* creating one anyway takes a second, deliberate call;
* it needs ``manage_masters``, which an Accountant does not have.

Chart of accounts and tax codes are deliberately **not** creatable here.
An account is a structural decision belonging to the accountant, and a
tax rate we invented would end up on a GST return.

Zoho's own API shapes two things here. Only ``contact_name`` is required
on ``POST /contacts``, and ``GET /contacts`` has no GSTIN search
parameter at all — it searches names and notes. So the GSTIN check, the
one that actually catches a duplicate, runs against the masters already
synced locally, and the name search goes to Zoho to catch contacts that
have not been synced yet.
"""

import logging
import re

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common import roles
from apps.common.models import BillActivity
from apps.common.permissions import HasOrgPermission, requires
from apps.common.services.activity import record_activity

from ..models import ZohoCustomer, ZohoVendor
from .helpers import (
    ZohoApiError, get_organization_from_request, get_zoho_credentials,
    make_zoho_api_request,
)

logger = logging.getLogger(__name__)

#: The full 15-character GSTIN shape, not just "15 characters".
#: A number that passes this is one we can safely compare against the
#: masters; a garbled read is rejected before it can become a vendor.
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")

#: Zoho's India-only values. Sending anything else is a 400 from Zoho
#: with a message the operator cannot act on, so it is checked here.
GST_TREATMENTS = ("business_gst", "business_none", "overseas", "consumer")

CONTACT_TYPES = {
    "vendor": ZohoVendor,
    "customer": ZohoCustomer,
}

_PUNCT = re.compile(r"[^a-z0-9]+")

#: Dropped before comparing two company names. "Acme Traders Pvt Ltd"
#: and "Acme Traders" are the same supplier, and treating them as two
#: is how a duplicate gets created.
_SUFFIXES = (
    "private limited", "pvt ltd", "pvt limited", "private ltd",
    "limited", "ltd", "llp", "inc", "corporation", "corp", "co",
    "company", "and co", "enterprises", "enterprise",
)


def normalise_company_name(text):
    """A comparable form of a company name.

    Deliberately blunt: lowercased, punctuation removed, common legal
    suffixes dropped. It is a duplicate *warning*, not a merge, so a
    false positive costs the operator one glance while a false negative
    costs them a split ledger.
    """
    name = _PUNCT.sub(" ", str(text or "").lower()).strip()
    for suffix in _SUFFIXES:
        if name.endswith(" " + suffix):
            name = name[: -(len(suffix) + 1)].strip()
    return re.sub(r"\s+", " ", name)


def _local_matches(model, organization, gst_no, company_name):
    """Existing local masters that look like the one being asked for."""
    found, seen = [], set()

    # GSTIN first: it is the only identifier that is actually unique,
    # and a match on it is conclusive.
    if gst_no:
        for row in model.objects.filter(organization=organization, gstNo__iexact=gst_no):
            seen.add(row.id)
            found.append((row, "same GSTIN"))

    wanted = normalise_company_name(company_name)
    if wanted:
        # Compared in Python: the stored names carry whatever casing and
        # punctuation Zoho holds, so no index would serve this anyway
        # and these tables are small.
        for row in model.objects.filter(organization=organization):
            if row.id in seen:
                continue
            if normalise_company_name(row.companyName) == wanted:
                found.append((row, "same name"))

    return found


def _zoho_matches(credentials, contact_type, company_name):
    """Contacts already in Zoho that this workspace has not synced.

    Searched by name because Zoho offers no GSTIN filter. Failure here
    is not fatal: it would be wrong to block creating a vendor because
    a *duplicate check* could not reach Zoho, so it is reported as an
    unchecked condition instead.
    """
    name = str(company_name or "").strip()
    if not name:
        return [], None

    try:
        payload = make_zoho_api_request(
            credentials,
            f"contacts?contact_type={contact_type}"
            f"&company_name_contains={name[:100]}&filter_by=Status.All",
        )
    except ZohoApiError as exc:
        return [], exc.message
    except Exception:
        logger.exception("Zoho contact search failed")
        return [], "Zoho could not be searched for existing contacts."

    wanted = normalise_company_name(name)
    matches = []
    for contact in payload.get("contacts") or []:
        label = contact.get("company_name") or contact.get("contact_name") or ""
        if normalise_company_name(label) == wanted:
            matches.append({
                "contact_id": contact.get("contact_id"),
                "company_name": label,
                "gst_no": contact.get("gst_no") or "",
                "status": contact.get("status") or "",
            })
    return matches, None


def _upsert_local(model, organization, contact_id, values):
    """Record the new contact locally so no re-sync is needed.

    Without this the operator would create the vendor and still have to
    press Sync before they could pick it — the round trip this endpoint
    exists to remove.
    """
    row = model.objects.filter(
        organization=organization, contactId=contact_id,
    ).first()
    if row is None:
        return model.objects.create(
            organization=organization, contactId=contact_id, **values,
        )
    for field, value in values.items():
        setattr(row, field, value)
    row.save(update_fields=list(values))
    return row


def _bill_for_activity(organization, bill_module, bill_id):
    """The bill this was done from, when the request named one."""
    if not bill_module or not bill_id:
        return None
    from .bill_move import MODULES

    module = MODULES.get(str(bill_module).lower())
    if module is None:
        return None
    return module.bill.objects.filter(
        id=bill_id, organization=organization,
    ).first()


@extend_schema(
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "contact_type": {"type": "string", "enum": sorted(CONTACT_TYPES)},
                "contact_name": {"type": "string"},
                "company_name": {"type": "string"},
                "gst_no": {"type": "string"},
                "gst_treatment": {"type": "string", "enum": list(GST_TREATMENTS)},
                "place_of_contact": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "description": "Create even though similar contacts exist.",
                },
                "bill_module": {"type": "string"},
                "bill_id": {"type": "string"},
            },
            "required": ["contact_type", "contact_name"],
        }
    },
    responses={
        201: {"description": "Contact created in Zoho Books and recorded locally"},
        400: {"description": "Invalid request"},
        409: {"description": "Similar contacts already exist; resend with force"},
        502: {"description": "Zoho rejected the contact"},
    },
    tags=["Zoho Ops"],
)
@requires(roles.MANAGE_MASTERS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def zoho_contact_create_view(request, org_id):
    """Create one vendor or customer in Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response(
            {"detail": "Organization not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    data = request.data or {}
    contact_type = str(data.get("contact_type") or "").lower()
    if contact_type not in CONTACT_TYPES:
        return Response({
            "detail": f"contact_type must be one of: {', '.join(sorted(CONTACT_TYPES))}",
        }, status=status.HTTP_400_BAD_REQUEST)

    contact_name = str(data.get("contact_name") or "").strip()
    company_name = str(data.get("company_name") or "").strip() or contact_name
    if not contact_name:
        return Response(
            {"detail": "contact_name is required — it is the name Zoho displays."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    gst_no = re.sub(r"\s+", "", str(data.get("gst_no") or "")).upper()
    if gst_no and not GSTIN_RE.match(gst_no):
        return Response({
            "detail": (
                f"'{gst_no}' is not a valid GSTIN. A GSTIN is 15 characters: "
                f"two state digits, five letters, four digits, a letter, one "
                f"character, 'Z', then one character. Check the reading "
                f"against the bill before creating the contact."
            ),
        }, status=status.HTTP_400_BAD_REQUEST)

    gst_treatment = str(data.get("gst_treatment") or "").strip().lower()
    if not gst_treatment:
        # The number itself says which it is, so there is nothing to ask.
        gst_treatment = "business_gst" if gst_no else "business_none"
    if gst_treatment not in GST_TREATMENTS:
        return Response({
            "detail": f"gst_treatment must be one of: {', '.join(GST_TREATMENTS)}",
        }, status=status.HTTP_400_BAD_REQUEST)
    if gst_treatment == "business_gst" and not gst_no:
        return Response({
            "detail": (
                "A GST-registered contact needs its GSTIN. Either add the "
                "number or set gst_treatment to business_none."
            ),
        }, status=status.HTTP_400_BAD_REQUEST)

    model = CONTACT_TYPES[contact_type]

    try:
        credentials = get_zoho_credentials(organization)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if not credentials.organisationId or not credentials.is_connected:
        return Response({
            "detail": (
                "This workspace is not connected to Zoho Books. Connect it "
                "from Config -> Credentials first."
            ),
        }, status=status.HTTP_400_BAD_REQUEST)

    force = bool(data.get("force"))
    search_warning = None

    if not force:
        local = _local_matches(model, organization, gst_no, company_name)
        remote, search_warning = _zoho_matches(
            credentials, contact_type, company_name,
        )

        if local or remote:
            return Response({
                "detail": (
                    f"A {contact_type} like this already exists. Pick one of "
                    f"these instead of creating a second — a duplicate "
                    f"splits the ledger and the ageing report."
                ),
                "existing_local": [
                    {
                        "id": str(row.id),
                        "contact_id": row.contactId,
                        "company_name": row.companyName,
                        "gst_no": row.gstNo,
                        "matched_on": reason,
                    }
                    for row, reason in local
                ],
                "existing_in_zoho": remote,
                "search_warning": search_warning,
                "resend_with": {"force": True},
            }, status=status.HTTP_409_CONFLICT)

    body = {
        "contact_name": contact_name[:200],
        "company_name": company_name[:200],
        "contact_type": contact_type,
        "gst_treatment": gst_treatment,
    }
    if gst_no:
        body["gst_no"] = gst_no
    place_of_contact = str(data.get("place_of_contact") or "").strip().upper()
    if place_of_contact:
        body["place_of_contact"] = place_of_contact[:10]

    try:
        payload = make_zoho_api_request(
            credentials, "contacts", method="POST", data=body,
        )
    except ZohoApiError as exc:
        return Response(
            {"detail": f"Zoho did not accept the {contact_type}. {exc.message}",
             "zoho_code": exc.zoho_code},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except Exception:
        logger.exception("Creating a Zoho %s failed", contact_type)
        return Response(
            {"detail": f"Could not create the {contact_type}. Check the server logs."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    contact = payload.get("contact") or {}
    contact_id = contact.get("contact_id")
    if not contact_id:
        # Zoho answered without an id, so there is nothing to record and
        # nothing to select. Saying "created" here would leave the
        # operator picking a contact that is not there.
        logger.error("Zoho created a %s but returned no contact_id: %s",
                     contact_type, payload)
        return Response({
            "detail": (
                f"Zoho accepted the {contact_type} but did not return its id. "
                f"Run the {contact_type}s sync to pick it up."
            ),
        }, status=status.HTTP_502_BAD_GATEWAY)

    values = {
        "companyName": (contact.get("company_name") or company_name)[:100],
        "gstNo": (contact.get("gst_no") or gst_no)[:30],
        "gst_treatment": contact.get("gst_treatment") or gst_treatment,
    }
    if contact_type == "customer":
        values["place_of_supply"] = (
            contact.get("place_of_contact") or place_of_contact or ""
        )[:10]

    row = _upsert_local(model, organization, contact_id, values)

    # Recorded against the bill it was done from, when there was one:
    # that entry is what later explains where the bill's vendor came
    # from. EDITED is the action for an endpoint that writes on its own,
    # outside the verify flow.
    bill = _bill_for_activity(
        organization, data.get("bill_module"), data.get("bill_id"),
    )
    if bill is not None:
        record_activity(
            bill, BillActivity.Action.EDITED,
            actor=request.user, organization=organization,
            summary=f"Created {contact_type} {values['companyName']} in Zoho Books"[:255],
            metadata={
                "created_in_zoho": contact_type,
                "contact_id": contact_id,
                "gst_no": values["gstNo"],
                "forced_past_duplicates": force,
            },
        )

    logger.info(
        "Created Zoho %s %s (%s) for organisation %s by %s",
        contact_type, values["companyName"], contact_id,
        organization.id, getattr(request.user, "email", "?"),
    )

    return Response({
        "detail": f"{values['companyName']} created in Zoho Books.",
        "id": str(row.id),
        "contact_id": contact_id,
        "company_name": values["companyName"],
        "gst_no": values["gstNo"],
        "gst_treatment": values["gst_treatment"],
        "contact_type": contact_type,
        "search_warning": search_warning,
    }, status=status.HTTP_201_CREATED)
