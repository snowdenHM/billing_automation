from rest_framework import serializers

from apps.module.tally.models import TallySetupStep


class TallySetupStepSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for a single Tally setup guide step.
    Returns absolute URL for the image so the frontend can render it directly.
    """

    image_url = serializers.SerializerMethodField()

    class Meta:
        model = TallySetupStep
        fields = (
            "id",
            "step_number",
            "title",
            "description",
            "image_url",
            "image_alt",
            "order",
        )

    def get_image_url(self, obj):
        if not obj.image:
            return None
        request = self.context.get("request")
        if request is not None:
            return request.build_absolute_uri(obj.image.url)
        return obj.image.url
