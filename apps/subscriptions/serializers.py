from rest_framework import serializers
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field, OpenApiTypes
from .models import Plan, Subscription


class PlanSerializer(serializers.ModelSerializer):
    """Serializer for Plan model with complete field representation"""

    class Meta:
        model = Plan
        fields = [
            "id",
            "code",
            "name",
            "description",
            "max_users",
            "features",
            "billing_cycle",
            "price",
            "is_active",
            "trial_days",
            "modules",
            "created_at",
            "updated_at"
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class SubscriptionSerializer(serializers.ModelSerializer):
    """Serializer for Subscription model with validation"""

    plan_details = PlanSerializer(source='plan', read_only=True)
    days_remaining = serializers.SerializerMethodField()
    is_valid = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            "id",
            "organization",
            "plan",
            "plan_details",
            "status",
            "starts_at",
            "ends_at",
            "canceled_at",
            "assigned_by",
            "auto_renew",
            "notes",
            "days_remaining",
            "is_valid",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "assigned_by",
            "days_remaining",
            "is_valid"
        ]

    @extend_schema_field(OpenApiTypes.INT)
    def get_days_remaining(self, obj) -> int:
        """Calculate days remaining in subscription"""
        if not obj.ends_at:
            return 0
        today = timezone.now().date()
        if obj.ends_at.date() < today:
            return 0
        return (obj.ends_at.date() - today).days

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_is_valid(self, obj) -> bool:
        """Check if subscription is currently valid"""
        today = timezone.now().date()
        return (
            obj.status == "active" and
            obj.starts_at.date() <= today and
            (not obj.ends_at or obj.ends_at.date() >= today)
        )

    def validate(self, attrs):
        """Validate subscription constraints including max users"""
        # We need to handle both create and update cases
        if self.instance:
            # Update case: merge with existing instance
            instance = self.instance
            for key, value in attrs.items():
                setattr(instance, key, value)
        else:
            # Create case: use provided attributes
            instance = Subscription(**attrs)

        # Run model validation
        instance.clean()
        return attrs

    def create(self, validated_data):
        """Create subscription with current user as assigner"""
        request = self.context.get("request")
        if request and request.user and not validated_data.get("assigned_by"):
            validated_data["assigned_by"] = request.user
        return super().create(validated_data)


class SubscriptionCancelSerializer(serializers.Serializer):
    """Serializer for canceling a subscription"""
    reason = serializers.CharField(required=False, allow_blank=True)


class SubscriptionRenewSerializer(serializers.Serializer):
    """Serializer for renewing a subscription"""
    days = serializers.IntegerField(required=False, allow_null=True)


class SubscriptionChangePlanSerializer(serializers.Serializer):
    """Serializer for changing subscription plan"""
    plan = serializers.PrimaryKeyRelatedField(queryset=Plan.objects.filter(is_active=True))
