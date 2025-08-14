from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework_api_key.models import APIKey  # noqa: F401
from .models import (
    Organization,
    OrgMembership,
    OrganizationAPIKey,
    Module,
    OrganizationModule,
)

User = get_user_model()


class NullablePKRelatedField(serializers.PrimaryKeyRelatedField):
    def to_internal_value(self, data):
        if data in (None, "", "0", 0):
            return None
        return super().to_internal_value(data)


class OrganizationSerializer(serializers.ModelSerializer):
    owner = NullablePKRelatedField(queryset=User.objects.all(), required=False, allow_null=True)
    owner_email = serializers.EmailField(write_only=True, required=False)

    class Meta:
        model = Organization
        fields = [
            "id",
            "name",
            "slug",
            "status",
            "owner",
            "created_by",
            "owner_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "slug", "created_by", "created_at", "updated_at"]

    def validate(self, attrs):
        owner = attrs.get("owner")
        owner_email = attrs.get("owner_email")
        if not owner and not owner_email:
            raise serializers.ValidationError({"owner_email": "Provide either 'owner' (user id) or 'owner_email'."})
        return attrs

    def create(self, validated_data):
        owner_email = validated_data.pop("owner_email", None)
        request = self.context.get("request")

        if request and request.user and not validated_data.get("created_by"):
            validated_data["created_by"] = request.user

        if owner_email and not validated_data.get("owner"):
            owner, _ = User.objects.get_or_create(email=owner_email, defaults={"is_active": True})
            validated_data["owner"] = owner

        return super().create(validated_data)


class OrgMembershipSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(write_only=True, required=False)

    class Meta:
        model = OrgMembership
        fields = ["id", "organization", "user", "user_email", "role", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        user_email = validated_data.pop("user_email", None)
        if user_email and not validated_data.get("user"):
            user, _ = User.objects.get_or_create(email=user_email, defaults={"is_active": True})
            validated_data["user"] = user
        return super().create(validated_data)


class APIKeyIssueSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)


class APIKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = OrganizationAPIKey
        fields = ["id", "name", "organization", "created_by", "created_at", "updated_at"]
        read_only_fields = ["id", "organization", "created_by", "created_at", "updated_at"]


class APIKeyRevokeSerializer(serializers.Serializer):
    revoked = serializers.BooleanField(default=True)


# ----- Modules -----

class ModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Module
        fields = ["id", "code", "name", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class OrganizationModuleSerializer(serializers.ModelSerializer):
    module = serializers.SlugRelatedField(queryset=Module.objects.all(), slug_field="code")

    class Meta:
        model = OrganizationModule
        fields = ["id", "organization", "module", "is_enabled", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        # upsert behavior: toggle if exists
        org = validated_data["organization"]
        mod = validated_data["module"]
        is_enabled = validated_data.get("is_enabled", True)
        obj, created = OrganizationModule.objects.get_or_create(
            organization=org, module=mod, defaults={"is_enabled": is_enabled}
        )
        if not created and obj.is_enabled != is_enabled:
            obj.is_enabled = is_enabled
            obj.save(update_fields=["is_enabled"])
        return obj
