# apps/users/serializers.py

from typing import Optional

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.http import urlsafe_base64_decode

from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from drf_spectacular.utils import extend_schema_field, OpenApiTypes

from apps.organizations.models import Organization, OrgMembership

User = get_user_model()


# -------------------- Organization info nested under User --------------------

class OrganizationInfoSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "status", "role"]
        # Avoid component-name collisions in OpenAPI
        ref_name = "OrganizationInfo"

    @extend_schema_field(OpenApiTypes.STR)  # (or use a ChoiceField schema if you prefer)
    def get_role(self, obj) -> Optional[str]:
        """
        Returns the calling user's role within this organization.
        Safe even if 'request' wasn't provided in context.
        """
        request = self.context.get("request")
        user = getattr(request, "user", None) or self.context.get("user")
        if not user or not getattr(user, "is_authenticated", False):
            return None

        membership = obj.memberships.filter(user=user, is_active=True).only("role").first()
        return membership.role if membership else None


class UserSerializer(serializers.ModelSerializer):
    organizations = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "last_name",
            "is_active", "is_staff", "is_superuser",
            "organizations",
        ]
        read_only_fields = ["id", "is_active", "is_staff", "is_superuser"]

    def get_organizations(self, user):
        """
        List orgs where this user has an active membership.
        (If your view prefetches memberships->organization, this will be hot.)
        """
        qs = (
            Organization.objects
            .filter(memberships__user=user, memberships__is_active=True)
            .only("id", "name", "slug", "status")
            .distinct()
        )
        # Pass through the same context so get_role() can see request/user
        return OrganizationInfoSerializer(qs, many=True, context=self.context).data


# -------------------- Auth & account flows --------------------

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ["email", "password", "first_name", "last_name"]

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        # Let create_user handle hashing & defaults
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, **validated_data)
        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")

        user = User.objects.filter(email__iexact=email).first()
        if not user or not user.is_active or not user.check_password(password):
            raise serializers.ValidationError("Invalid email or password")

        refresh = RefreshToken.for_user(user)
        return {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            # IMPORTANT: pass serializer context so nested OrganizationInfoSerializer
            # can access request/user for get_role()
            "user": UserSerializer(user, context=self.context).data,
        }


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uidb64 = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        validate_password(value)
        return value

    def validate(self, attrs):
        uidb64 = attrs.get("uidb64")
        token = attrs.get("token")
        new_password = attrs.get("new_password")

        try:
            uid = urlsafe_base64_decode(uidb64).decode()
            user = User.objects.get(pk=uid)
        except Exception:  # noqa: BLE001
            raise serializers.ValidationError("Invalid reset link")

        if not PasswordResetTokenGenerator().check_token(user, token):
            raise serializers.ValidationError("Invalid or expired reset token")

        attrs["user_obj"] = user
        attrs["new_password"] = new_password
        return attrs
