from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.module.tally.models import TallySetupStep
from apps.module.tally.serializers import TallySetupStepSerializer


@extend_schema(
    operation_id="get_tally_setup_guide",
    tags=["Tally Config"],
    summary="Get the Tally setup guide steps",
    description=(
        "Returns the ordered list of active Tally setup steps. "
        "Each step contains a step number, title, description, optional image, "
        "image alt-text and a sort order. The frontend renders these as a "
        "timeline on the Account Info / Setup Guide page. "
        "Steps are configured by admins via the Django admin."
    ),
    responses={
        200: OpenApiResponse(
            response=TallySetupStepSerializer(many=True),
            description="List of active setup steps in display order.",
        ),
    },
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tally_setup_guide(request):
    """
    Return the active steps of the Tally setup guide, in display order.
    """
    steps = TallySetupStep.objects.filter(is_active=True).order_by("step_number", "order")
    serializer = TallySetupStepSerializer(steps, many=True, context={"request": request})
    return Response({"steps": serializer.data}, status=status.HTTP_200_OK)
