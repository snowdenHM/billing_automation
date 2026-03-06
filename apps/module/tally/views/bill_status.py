# apps/module/tally/views/bill_status.py
"""
Bill tally sync status update view.
"""
import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.utils import get_organization_from_request

from ..models import TallyVendorBill, TallyExpenseBill
from .helpers import OrganizationAPIKeyOrBearerToken

logger = logging.getLogger(__name__)


@extend_schema(
    tags=["Tally Bill Sync"],
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "format": "uuid", "description": "Bill ID to update"},
                "status": {"type": "boolean", "description": "Tally sync status to set"},
                "mode": {"type": "string", "enum": ["vendor", "expense"], "description": "Bill type mode"},
            },
            "required": ["id", "status", "mode"],
        }
    },
    responses={
        200: {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "message": {"type": "string"},
                "data": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "tally_synced": {"type": "boolean"},
                        "mode": {"type": "string"},
                    },
                },
            },
        },
    },
)
@api_view(["POST"])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def update_bill_tally_sync_status(request, org_id):
    """
    Update the tally_synced status for bills based on mode (vendor or expense).
    Supports both single and bulk updates.
    """
    try:
        organization = get_organization_from_request(request, org_id)
        if not organization:
            return Response(
                {"success": False, "message": f"Organization with ID {org_id} not found or you do not have access"},
                status=status.HTTP_404_NOT_FOUND,
            )

        def _normalize_status(status_value):
            if isinstance(status_value, bool):
                return status_value
            if isinstance(status_value, str):
                if status_value.lower() in ("true", "1"):
                    return True
                if status_value.lower() in ("false", "0"):
                    return False
                raise ValueError(f"Invalid status string: {status_value}")
            raise ValueError(f"Status must be boolean or string, got {type(status_value)}")

        def _process_single_bill(bill_data):
            bill_id = bill_data.get("id")
            sync_status = bill_data.get("status")
            mode = bill_data.get("mode")

            if not all([bill_id is not None, sync_status is not None, mode is not None]):
                return {"error": "Missing required fields: id, status, and mode are all required"}
            if mode not in ("vendor", "expense"):
                return {"error": 'Invalid mode. Must be either "vendor" or "expense"'}
            try:
                sync_status = _normalize_status(sync_status)
            except ValueError as e:
                return {"error": str(e)}

            model = TallyVendorBill if mode == "vendor" else TallyExpenseBill
            try:
                bill = model.objects.get(id=bill_id)
            except model.DoesNotExist:
                return {"error": f"{mode.capitalize()} bill with ID {bill_id} not found"}
            except ValueError:
                return {"error": f"Invalid UUID format for bill ID {bill_id}"}

            bill.tally_synced = sync_status
            bill.save()
            return {"success": True, "id": str(bill.id), "tally_synced": bill.tally_synced, "mode": mode}

        # Bulk update
        bulk_data = request.data.get("Data")
        if bulk_data is not None:
            if not isinstance(bulk_data, list):
                return Response({"success": False, "message": "Data must be an array for bulk updates"}, status=status.HTTP_400_BAD_REQUEST)

            results, errors = [], []
            for i, bill_data in enumerate(bulk_data):
                result = _process_single_bill(bill_data)
                if "error" in result:
                    errors.append({"index": i, "id": bill_data.get("id", "unknown"), "error": result["error"]})
                else:
                    results.append(result)

            response_data = {"success": len(errors) == 0, "updated_count": len(results), "error_count": len(errors), "results": results}
            if errors:
                response_data["errors"] = errors
            return Response(response_data, status=status.HTTP_200_OK if not errors else status.HTTP_207_MULTI_STATUS)

        # Single update
        result = _process_single_bill(request.data)
        if "error" in result:
            return Response({"success": False, "message": result["error"]}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "success": True,
                "message": f'{result["mode"].capitalize()} bill tally sync status updated successfully',
                "data": {"id": result["id"], "tally_synced": result["tally_synced"], "mode": result["mode"]},
            },
            status=status.HTTP_200_OK,
        )

    except Exception as e:
        return Response(
            {"success": False, "message": f"Error updating bill tally sync status: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
