"""Shared machinery for the Zoho receipt and payment voucher feeds.

A receipt and a payment are the same Zoho object read from opposite sides
of the ledger: ``customerpayments`` takes ``customer_id`` + ``account_id``,
``vendorpayments`` takes ``vendor_id`` + ``paid_through_account_id``, and
everything else — mode, amount, date, reference, description, the array of
documents the money settles — is identical. So the verify and sync bodies
live here once and the two view modules only name their halves.

The payload builders are deliberately pure functions of an analysed
header: no request, no credentials, no network. That is what lets the
``/invoices``-shaped assertion tests run without a live Zoho org.
"""

import json
import logging

import requests
from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from apps.common.services import approval as approval_service
from apps.common.models import BillActivity
from apps.common.services.activity import (
    BILL_FIELD_LABELS, diff_fields, record_activity, snapshot_bill_fields,
)
from apps.common.utils import get_organization_from_request

from .helpers import get_zoho_credentials, refresh_zoho_access_token

logger = logging.getLogger(__name__)

ZOHO_HTTP_TIMEOUT = 30

ZOHO_CUSTOMER_PAYMENTS_URL = "https://www.zohoapis.in/books/v3/customerpayments"
ZOHO_VENDOR_PAYMENTS_URL = "https://www.zohoapis.in/books/v3/vendorpayments"


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def _applied_documents(raw, *, id_key):
    """Normalise the stored settle-against list into Zoho's array shape.

    Stored loosely as JSON because the operator edits it on the verify
    screen; anything that doesn't carry both an id and an amount is
    dropped rather than posted as a half-formed row Zoho would reject.
    """
    rows = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        doc_id = entry.get(id_key) or entry.get("id")
        if not doc_id:
            continue
        try:
            applied = float(entry.get("amount_applied") or 0)
        except (TypeError, ValueError):
            continue
        rows.append({id_key: str(doc_id), "amount_applied": applied})
    return rows


def _common_payment_fields(analysed):
    payload = {
        "payment_mode": analysed.payment_mode or "Cash",
        "amount": float(analysed.amount or 0),
    }
    if analysed.bill_date:
        payload["date"] = analysed.bill_date.strftime('%Y-%m-%d')
    if analysed.bill_no:
        payload["reference_number"] = str(analysed.bill_no)
    if analysed.note:
        payload["description"] = analysed.note
    if analysed.bank_charges:
        payload["bank_charges"] = float(analysed.bank_charges)
    return payload


def build_customer_payment_payload(analysed):
    """Build the ``POST /books/v3/customerpayments`` body.

    Returns ``(payload, error)``; ``error`` is a user-facing string when
    the voucher cannot be posted, in which case ``payload`` is ``None``.
    """
    if not analysed.customer:
        return None, "No customer is associated with this receipt. Assign a customer before syncing."
    if not analysed.deposit_to_account:
        return None, "No deposit-to account selected. Choose the bank or cash account the money arrived into."
    if not analysed.amount or float(analysed.amount) <= 0:
        return None, "Receipt amount must be greater than zero."

    payload = {
        "customer_id": str(analysed.customer.contactId),
        "account_id": str(analysed.deposit_to_account.accountId),
        # Zoho marks ``invoices`` required even for an unapplied advance,
        # where it expects the empty array rather than the key's absence.
        "invoices": _applied_documents(analysed.applied_invoices, id_key="invoice_id"),
        **_common_payment_fields(analysed),
    }
    return payload, None


def build_vendor_payment_payload(analysed):
    """Build the ``POST /books/v3/vendorpayments`` body.

    Returns ``(payload, error)`` as above.
    """
    if not analysed.vendor:
        return None, "No vendor is associated with this payment. Assign a vendor before syncing."
    if not analysed.paid_through_account:
        return None, "No paid-through account selected. Choose the bank or cash account the money left from."
    if not analysed.amount or float(analysed.amount) <= 0:
        return None, "Payment amount must be greater than zero."

    payload = {
        "vendor_id": str(analysed.vendor.contactId),
        "paid_through_account_id": str(analysed.paid_through_account.accountId),
        **_common_payment_fields(analysed),
    }
    # ``bills`` is optional here, unlike the customer side: omitting it
    # records the payment as unapplied credit against the vendor.
    bills = _applied_documents(analysed.applied_bills, id_key="bill_id")
    if bills:
        payload["bills"] = bills
    return payload, None


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def zoho_payment_verify_base(
    request, org_id, bill_id, *,
    bill_model, analysed_model, serializer_class, label,
):
    """Generic verify view for a receipt / payment voucher.

    No product grid to reconcile, so this is the header update and nothing
    else — the whole reason these two types do not reuse the bill-shaped
    verify path.
    """
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        payload_bill_id = request.data.get('bill_id', bill_id)
        data = request.data.get('zoho_bill', request.data)

        bill = bill_model.objects.get(id=payload_bill_id, organization=organization)

        if bill.status not in ['Analysed', 'Verified']:
            return Response(
                {"detail": f"{label} must be in 'Analysed' or 'Verified' status to save"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            analysed = analysed_model.objects.get(selectBill=bill, organization=organization)
        except analysed_model.DoesNotExist:
            return Response(
                {"detail": f"No analysed {label.lower()} data found. Please analyse it first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            _before_verify = snapshot_bill_fields(analysed)

            serializer = serializer_class(
                analysed, data=data, partial=True, context={'organization': organization},
            )
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            updated = serializer.save()

            bill.status = 'Verified'
            bill.save(update_fields=['status'])

            # The change set is the point of the event: a timeline that
            # only says "verified" cannot answer what the operator altered.
            _changes = diff_fields(
                _before_verify,
                snapshot_bill_fields(updated),
                labels=BILL_FIELD_LABELS,
            )
            record_activity(
                bill, BillActivity.Action.VERIFIED,
                actor=request.user,
                organization=organization,
                summary=(f"Verified with {len(_changes)} change(s)"
                         if _changes else "Verified with no changes"),
                changes=_changes,
            )

        return Response(serializer_class(updated, context={'organization': organization}).data)

    except bill_model.DoesNotExist:
        return Response({"detail": f"{label} not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error("%s verify failed: %s", label, e, exc_info=True)
        return Response({"detail": f"Verification failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _extract_payment_id(response_json):
    """Pull ``payment_id`` out of a Zoho payment response.

    ``customerpayments`` wraps the object in ``payment``; ``vendorpayments``
    returns its fields at the top level. Handle both rather than branching
    at every call site.
    """
    for container in (response_json.get('payment'), response_json.get('vendorpayment'), response_json):
        if isinstance(container, dict) and container.get('payment_id'):
            return container['payment_id']
    return None


def zoho_payment_sync_base(
    request, org_id, bill_id, *,
    bill_model, analysed_model, build_payload_fn, endpoint_url, label, tag,
):
    """Generic sync view: build the payload, POST it, record the remote id."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = bill_model.objects.get(id=bill_id, organization=organization)
    except bill_model.DoesNotExist:
        return Response({"detail": f"{label} not found"}, status=status.HTTP_404_NOT_FOUND)

    # Money, not a document: a re-sync would record the same cash twice.
    # Checked before the status gate, which would otherwise mask the id.
    if bill.zoho_payment_id:
        return Response({
            'error': 'Already Synced',
            'detail': f'This {label.lower()} already exists in Zoho Books.',
            'zoho_payment_id': bill.zoho_payment_id,
        }, status=status.HTTP_409_CONFLICT)

    if bill.status != 'Verified':
        return Response({
            'error': 'Invalid Bill Status',
            'detail': f'{label} must be in "Verified" status to sync. Current status: {bill.status}',
            'current_status': bill.status,
            'required_status': 'Verified',
        }, status=status.HTTP_400_BAD_REQUEST)

    # Nobody may post money the organisation has not approved. The gate
    # lives in this shared base rather than in each thin view, so a voucher
    # type added later cannot quietly skip it.
    blocked = approval_service.gate_sync(bill, organization)
    if blocked:
        message, code = blocked
        return Response({
            'error': 'Approval Required',
            'message': message,
            'error_code': code,
        }, status=status.HTTP_409_CONFLICT)

    try:
        analysed = analysed_model.objects.get(selectBill=bill, organization=organization)
    except analysed_model.DoesNotExist:
        return Response(
            {"detail": f"No analysed {label.lower()} data found. Verify it first."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    payload, error = build_payload_fn(analysed)
    if error:
        return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

    try:
        current_token = get_zoho_credentials(organization)
    except ValueError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    url = f"{endpoint_url}?organization_id={current_token.organisationId}"
    headers = {
        'Authorization': f'Zoho-oauthtoken {current_token.accessToken}',
        'Content-Type': 'application/json',
    }
    body = json.dumps(payload)

    # Zoho answers synchronously, so the terminal event is recorded inline
    # below rather than waiting for a callback as the Tally feed does.
    # This one marks the attempt, so an attempt that dies mid-flight still
    # leaves a trace.
    record_activity(
        bill, BillActivity.Action.SYNC_REQUESTED,
        actor=request.user, organization=organization,
        summary=f"Sent to Zoho Books as a {tag.lower()}",
    )

    try:
        response = requests.post(url, headers=headers, data=body, timeout=ZOHO_HTTP_TIMEOUT)

        if response.status_code == 401:
            new_token = refresh_zoho_access_token(current_token)
            if new_token:
                headers['Authorization'] = f'Zoho-oauthtoken {new_token}'
                response = requests.post(url, headers=headers, data=body, timeout=ZOHO_HTTP_TIMEOUT)

        if response.status_code == 201:
            payment_id = _extract_payment_id(response.json())
            bill.zoho_payment_id = str(payment_id) if payment_id else None
            bill.status = 'Synced'
            bill.save(update_fields=['zoho_payment_id', 'status'])
            record_activity(
                bill, BillActivity.Action.SYNC_CONFIRMED,
                actor=request.user, organization=organization,
                summary="Posted to Zoho Books",
                metadata={"zoho_payment_id": str(payment_id or "")},
            )
            return Response({
                "detail": f"{label} synced to Zoho successfully",
                "zoho_payment_id": payment_id,
            })

        response_json = response.json() if response.content else {}
        error_message = response_json.get("message", f"Failed to send {label.lower()} to Zoho Books")
        logger.error("[%s SYNC] Zoho API error %s: %s", tag, response.status_code, error_message)
        record_activity(
            bill, BillActivity.Action.SYNC_FAILED,
            actor=request.user, organization=organization,
            summary=f"Zoho Books refused the {label.lower()}: {error_message}"[:255],
            metadata={"status_code": response.status_code, "zoho_message": str(error_message)[:500]},
        )
        return Response({
            'error': 'Zoho Sync Failed',
            'detail': f'Failed to sync {label.lower()} to Zoho Books. {error_message}',
            'status_code': response.status_code,
            'zoho_message': error_message,
        }, status=status.HTTP_400_BAD_REQUEST)

    except requests.RequestException as e:
        logger.error("[%s SYNC] Network error: %s", tag, e)
        record_activity(
            bill, BillActivity.Action.SYNC_FAILED,
            actor=request.user, organization=organization,
            summary=f"Network error while posting: {e}"[:255],
            metadata={"error": str(e)[:500], "outcome_unknown": True},
        )
        return Response({
            "detail": (
                f"Network error while syncing: {e}. The {label.lower()} may or may "
                "not have been recorded in Zoho Books — check there before retrying."
            ),
        }, status=status.HTTP_504_GATEWAY_TIMEOUT)
