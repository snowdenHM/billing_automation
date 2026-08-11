from rest_framework import serializers

from .models import SupportTicket, SupportTicketMessage


class SupportTicketMessageSerializer(serializers.ModelSerializer):
    author_email = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicketMessage
        fields = [
            "id",
            "ticket",
            "author_user",
            "author_email",
            "body",
            "is_internal",
            "created_at",
        ]
        read_only_fields = ["id", "ticket", "author_user", "created_at"]

    def get_author_email(self, obj):
        if obj.author_user_id:
            return obj.author_user.email
        return obj.author_email


class SupportTicketCreateSerializer(serializers.ModelSerializer):
    """Used for ``POST /support/tickets/``. ``organization`` and ``user``
    are derived from the request in the view, not accepted from the client."""

    class Meta:
        model = SupportTicket
        fields = [
            "id",
            "subject",
            "message",
            "category",
            "priority",
            "page_url",
            "browser",
        ]
        read_only_fields = ["id"]


class SupportTicketSerializer(serializers.ModelSerializer):
    """Read serializer — nests the message thread."""

    messages = serializers.SerializerMethodField()
    submitter_email = serializers.SerializerMethodField()
    organization_name = serializers.CharField(
        source="organization.name", read_only=True, default=None
    )

    class Meta:
        model = SupportTicket
        fields = [
            "id",
            "organization",
            "organization_name",
            "user",
            "submitter_email",
            "subject",
            "message",
            "category",
            "status",
            "priority",
            "page_url",
            "browser",
            "created_at",
            "updated_at",
            "resolved_at",
            "messages",
        ]
        read_only_fields = fields

    def get_submitter_email(self, obj):
        return obj.user.email if obj.user_id else None

    def get_messages(self, obj):
        request = self.context.get("request")
        is_staff = bool(request and request.user and request.user.is_staff)
        qs = obj.messages.all()
        if not is_staff:
            qs = qs.filter(is_internal=False)
        return SupportTicketMessageSerializer(qs, many=True, context=self.context).data
