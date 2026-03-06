# apps/module/tally/views/master.py
"""
Master API views – processes incoming data from Tally (STOCKITEM, etc.).
Currently MasterAPIView – kept as APIView because it receives raw JSON payloads.
"""
import json
import logging

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import Organization

from ..models import StockItem

logger = logging.getLogger(__name__)


@extend_schema(tags=["Tally TCP"])
class MasterAPIView(APIView):
    """
    Master API View for processing incoming data from Tally.
    Processes STOCKITEM data and saves directly to database.
    OPEN FOR TESTING – NO AUTHENTICATION REQUIRED.
    """

    permission_classes = [AllowAny]

    def _get_organization(self):
        org_id = self.kwargs.get("org_id")
        if org_id:
            try:
                return Organization.objects.get(id=org_id)
            except Organization.DoesNotExist:
                return None
        if hasattr(self.request, "auth") and self.request.auth:
            from apps.organizations.models import OrganizationAPIKey
            try:
                return OrganizationAPIKey.objects.get(api_key=self.request.auth).organization
            except OrganizationAPIKey.DoesNotExist:
                pass
        if hasattr(self.request, "organization"):
            return self.request.organization
        if hasattr(self.request.user, "memberships") and self.request.user.is_authenticated:
            membership = self.request.user.memberships.first()
            if membership:
                return membership.organization
        return None

    # Alias
    get_organization = _get_organization

    def dispatch(self, request, *args, **kwargs):
        logger.info(f"MasterAPIView - {request.method} {request.get_full_path()}")
        return super().dispatch(request, *args, **kwargs)

    @extend_schema(
        summary="Fetch Master Data / Organization Info",
        description="Returns basic organization details and optionally stock items.",
        responses={200: {"description": "Data retrieved successfully"}},
    )
    def get(self, request, *args, **kwargs):
        try:
            organization = self._get_organization() or Organization.objects.first()
            if not organization:
                return Response({"success": False, "error": "No organization found"}, status=status.HTTP_404_NOT_FOUND)

            stock_items_qs = StockItem.objects.filter(organization=organization).all()
            stock_items_data = [
                {
                    "id": str(item.id),
                    "master_id": item.master_id,
                    "alter_id": item.alter_id,
                    "name": item.name,
                    "parent": item.parent,
                    "unit": item.unit,
                    "category": item.category,
                    "gst_applicable": item.gst_applicable,
                    "item_code": item.item_code,
                    "alias": item.alias,
                    "company": item.company,
                }
                for item in stock_items_qs
            ]

            return Response(
                {
                    "success": True,
                    "message": "Data retrieved successfully",
                    "organization": {"id": str(organization.id), "name": organization.name},
                    "stock_items": stock_items_data,
                    "stock_items_count": stock_items_qs.count(),
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.error(f"Error in MasterAPIView GET: {str(e)}")
            return Response({"success": False, "error": f"Failed to retrieve data: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        summary="Process Master Data from Tally",
        description="Receives raw data from Tally and processes it directly to database. Currently supports STOCKITEM.",
        responses={200: {"description": "Data processed successfully"}},
    )
    def post(self, request, *args, **kwargs):
        try:
            organization = self._get_organization()
            if not organization:
                try:
                    organization = Organization.objects.first()
                except Exception:
                    organization = None

            raw_data = request.body.decode("utf-8")

            try:
                parsed_data = json.loads(raw_data) if raw_data else {}
                data_keys = list(parsed_data.keys()) if isinstance(parsed_data, dict) else []

                stockitem_processing_result = None
                if isinstance(parsed_data, dict) and "STOCKITEM" in parsed_data:
                    stockitem_processing_result = self._process_stockitem_data(parsed_data["STOCKITEM"], organization)

            except json.JSONDecodeError:
                return Response({"error": "Invalid JSON data provided", "success": False}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.error(f"Error parsing data: {str(e)}")
                return Response({"error": f"Error processing data: {str(e)}", "success": False}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            response_data = {
                "success": True,
                "message": "Data processed successfully",
                "organization": {
                    "id": str(organization.id) if organization else None,
                    "name": organization.name if organization else "TEST MODE",
                },
                "data_length": len(raw_data),
                "processed_data_types": data_keys,
            }
            if stockitem_processing_result:
                response_data["stockitem_processing"] = stockitem_processing_result

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error in MasterAPIView: {str(e)}")
            return Response({"error": f"Failed to process incoming data: {str(e)}", "success": False}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _process_stockitem_data(self, stockitem_data, organization):
        """Process STOCKITEM data and save to database."""
        if not stockitem_data or not isinstance(stockitem_data, list):
            return {"success": False, "error": "No valid STOCKITEM data provided"}
        if not organization:
            return {"success": False, "error": "No organization available for processing"}

        created_items, failed_items, updated_items = [], [], []

        try:
            with transaction.atomic():
                for i, entry in enumerate(stockitem_data):
                    try:
                        company = entry.get("Company", "").strip().replace("\r\n", "").replace("\n", "")
                        item_data = {
                            "master_id": entry.get("Master_Id", ""),
                            "alter_id": entry.get("Alter_id", ""),
                            "name": entry.get("Name", ""),
                            "parent": entry.get("Parent", ""),
                            "unit": entry.get("Unit", ""),
                            "category": entry.get("Category", ""),
                            "gst_applicable": entry.get("GstApplicable", ""),
                            "item_code": entry.get("Item_Code", ""),
                            "alias": entry.get("ALIAS", ""),
                            "company": company,
                            "organization": organization,
                        }

                        master_id = entry.get("Master_Id", "")
                        if master_id and organization:
                            stock_item, created = StockItem.objects.update_or_create(
                                master_id=master_id, organization=organization, defaults=item_data
                            )
                        else:
                            stock_item = StockItem.objects.create(**item_data)
                            created = True

                        item_response = {
                            "id": str(stock_item.id),
                            "master_id": stock_item.master_id,
                            "alter_id": stock_item.alter_id,
                            "name": stock_item.name,
                            "parent": stock_item.parent,
                            "unit": stock_item.unit,
                            "category": stock_item.category,
                            "gst_applicable": stock_item.gst_applicable,
                            "item_code": stock_item.item_code,
                            "alias": stock_item.alias,
                            "company": stock_item.company,
                        }
                        (created_items if created else updated_items).append(item_response)

                    except Exception as item_error:
                        failed_items.append(
                            {"index": i + 1, "name": entry.get("Name", "Unknown"), "error": str(item_error), "data": entry}
                        )
                        continue

            return {
                "success": True,
                "created_count": len(created_items),
                "updated_count": len(updated_items),
                "failed_count": len(failed_items),
                "total_processed": len(stockitem_data),
                "created_items": created_items[:5],
                "updated_items": updated_items[:5],
                "failed_items": failed_items[:3] if failed_items else [],
            }

        except Exception as e:
            logger.error(f"Error in STOCKITEM bulk processing: {str(e)}")
            return {
                "success": False,
                "error": f"STOCKITEM bulk processing failed: {str(e)}",
                "created_count": len(created_items),
                "updated_count": len(updated_items),
                "failed_count": len(failed_items),
            }
