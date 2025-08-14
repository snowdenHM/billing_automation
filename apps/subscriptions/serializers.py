from rest_framework import serializers
from .models import Plan, Subscription


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = ["id", "code", "name", "max_users", "features", "billing_cycle", "price", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class SubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subscription
        fields = [
            "id",
            "organization",
            "plan",
            "status",
            "starts_at",
            "ends_at",
            "assigned_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "assigned_by"]

    def validate(self, attrs):
        # enforce plan max_users
        instance = Subscription(**attrs)
        instance.clean()
        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        if request and request.user and not validated_data.get("assigned_by"):
            validated_data["assigned_by"] = request.user
        return super().create(validated_data)