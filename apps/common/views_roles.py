"""What the signed-in user may do in one organisation.

The roles matrix lives on the server, and the server enforces it. The
frontend still needs to read it: a Sync button that an Accountant can
press and that always answers 403 is worse than no button. One request
per organisation, answered from a single membership lookup.
"""

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from apps.common import roles
from apps.common.services import approval as approval_service
from apps.common.utils import get_organization_from_request


class MyPermissionsSerializer(serializers.Serializer):
    role = serializers.CharField(allow_null=True)
    permissions = serializers.ListField(child=serializers.CharField())
    require_bill_approval = serializers.BooleanField()


@extend_schema(responses={200: MyPermissionsSerializer}, tags=["Organizations"])
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_permissions(request, org_id):
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {"error": "Not Found", "message": "Organisation not found.",
             "error_code": "ORG_NOT_FOUND"},
            status=status.HTTP_404_NOT_FOUND,
        )

    role = roles.membership_role(request.user, organization.id)
    # Staff have no membership row but are let past every check, so the
    # answer they get has to match what the server will actually allow.
    allowed = (
        list(roles.ALL_ACTIONS)
        if (request.user.is_superuser or request.user.is_staff)
        else sorted(roles.permissions_for(role))
    )
    return Response({
        "role": role,
        "permissions": allowed,
        "require_bill_approval": approval_service.approval_required(organization),
    })


class RoleCatalogSerializer(serializers.Serializer):
    roles = serializers.ListField(child=serializers.DictField())
    actions = serializers.ListField(child=serializers.DictField())
    group_order = serializers.ListField(child=serializers.CharField())


@extend_schema(responses={200: RoleCatalogSerializer}, tags=["Organizations"])
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def org_roles(request, org_id):
    """Every role and what it can do, for the screen that assigns one.

    Somebody inviting a member picks between four words with no way to
    know what any of them grants. This is that answer, read from the
    same matrix the server enforces — so the screen cannot promise
    something the permission check will refuse.

    Readable by any member: knowing what a Consultant may do is not
    privileged information, and hiding it is what forced the frontend
    to keep its own copy.
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {"error": "Not Found", "message": "Organisation not found.",
             "error_code": "ORG_NOT_FOUND"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(roles.describe_roles())
