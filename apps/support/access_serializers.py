"""Serializers for the access-grant and support-desk surfaces.

The grant serializers never carry ``token_hash`` and never carry the code.
The plaintext appears in exactly one response body — the mint response,
built by hand in the view — and nowhere else, so there is no field here
that a future ``fields = "__all__"`` could widen into a leak.
"""
from rest_framework import serializers

from .models import (
    SupportAccessEvent,
    SupportAccessGrant,
    SupportAccessSession,
    SupportAgent,
    SupportTicket,
)


class SupportAgentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportAgent
        fields = ["display_name", "is_active", "max_open_tickets"]
        read_only_fields = fields


class AccessGrantMintSerializer(serializers.Serializer):
    """``POST support/tickets/{id}/access-grants/`` body."""

    scope = serializers.ChoiceField(
        choices=SupportAccessGrant.SCOPE_CHOICES,
        default=SupportAccessGrant.SCOPE_READ_ONLY,
    )
    ttl_minutes = serializers.IntegerField(
        required=False,
        min_value=5,
        # The hard cap is also enforced on the model's constant, so a
        # second caller cannot talk its way past the serializer.
        max_value=SupportAccessGrant.MAX_TTL_MINUTES,
        default=SupportAccessGrant.DEFAULT_TTL_MINUTES,
    )


class SupportAccessSessionSerializer(serializers.ModelSerializer):
    agent_name = serializers.SerializerMethodField()
    is_live = serializers.BooleanField(read_only=True)
    grant_label = serializers.CharField(source="grant.code_label", read_only=True)
    scope = serializers.CharField(source="grant.scope", read_only=True)
    # The banner has to say when this ends, and the member reading it is
    # often not the admin who minted the grant — so they cannot look the
    # expiry up in the grant list, which is admin-only. Without it the
    # bar can only say "until the ticket closes", which is true and
    # useless: the whole point of a TTL is that the client can see the
    # clock running down.
    expires_at = serializers.DateTimeField(source="grant.expires_at", read_only=True)

    class Meta:
        model = SupportAccessSession
        fields = [
            "id",
            "grant",
            "grant_label",
            "scope",
            "agent_name",
            "started_at",
            "expires_at",
            "ended_at",
            "end_reason",
            "is_live",
            "ip",
        ]
        read_only_fields = fields

    def get_agent_name(self, obj):
        # The client sees the agent's display name, never the staff email.
        agent_row = getattr(obj.agent, "support_agent", None)
        return agent_row.display_name if agent_row else "Bill Munshi support"


class SupportAccessGrantSerializer(serializers.ModelSerializer):
    sessions = SupportAccessSessionSerializer(many=True, read_only=True)
    redeemed_by_name = serializers.SerializerMethodField()
    is_usable = serializers.SerializerMethodField()
    ticket_subject = serializers.CharField(source="ticket.subject", read_only=True)

    class Meta:
        model = SupportAccessGrant
        fields = [
            "id",
            "ticket",
            "ticket_subject",
            "organization",
            "code_label",
            "scope",
            "status",
            "is_usable",
            "expires_at",
            "created_at",
            "created_by",
            "redeemed_at",
            "redeemed_by_name",
            "revoked_at",
            "revoke_reason",
            "sessions",
        ]
        read_only_fields = fields

    def get_redeemed_by_name(self, obj):
        if not obj.redeemed_by_id:
            return None
        agent_row = getattr(obj.redeemed_by, "support_agent", None)
        return agent_row.display_name if agent_row else "Bill Munshi support"

    def get_is_usable(self, obj):
        # Computed, not read off ``status`` — the column lags the clock.
        return obj.is_usable()


class AccessRedeemSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=40, trim_whitespace=True, write_only=True)


class AccessRevokeSerializer(serializers.Serializer):
    reason = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )


class SupportAccessEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportAccessEvent
        fields = ["method", "path", "status_code", "at"]
        read_only_fields = fields


class DeskTicketSerializer(serializers.ModelSerializer):
    """Queue row. Carries the assignment, which the client serializer
    does not — who is working a ticket is desk bookkeeping."""

    assigned_to_name = serializers.SerializerMethodField()
    organization_name = serializers.CharField(
        source="organization.name", read_only=True, default=None
    )
    submitter_email = serializers.SerializerMethodField()
    access_live = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicket
        fields = [
            "id",
            "subject",
            "category",
            "status",
            "priority",
            "organization",
            "organization_name",
            "submitter_email",
            "assigned_to",
            "assigned_to_name",
            "assigned_at",
            "created_at",
            "updated_at",
            "access_live",
        ]
        read_only_fields = fields

    def get_assigned_to_name(self, obj):
        if not obj.assigned_to_id:
            return None
        agent_row = getattr(obj.assigned_to, "support_agent", None)
        return agent_row.display_name if agent_row else obj.assigned_to.email

    def get_submitter_email(self, obj):
        return obj.user.email if obj.user_id else (obj.contact_email or None)

    def get_access_live(self, obj):
        return any(
            grant.is_usable() and grant.sessions.filter(ended_at__isnull=True).exists()
            for grant in obj.access_grants.all()
        )


class DeskTicketUpdateSerializer(serializers.ModelSerializer):
    """``PATCH support-desk/tickets/{id}/`` — status and priority only.

    Listing the two fields rather than excluding the rest: an agent must
    not be able to rewrite a client's subject, message or organization,
    and a field added to the model later must not quietly become writable
    from the desk.
    """

    class Meta:
        model = SupportTicket
        fields = ["status", "priority"]


class DeskReplySerializer(serializers.Serializer):
    body = serializers.CharField(max_length=10000, trim_whitespace=True)
    is_internal = serializers.BooleanField(required=False, default=False)


class DeskAssignSerializer(serializers.Serializer):
    agent_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Omit to take the ticket yourself; null to unassign.",
    )
