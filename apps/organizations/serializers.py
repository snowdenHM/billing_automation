from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework_api_key.models import APIKey  # noqa: F401
from drf_spectacular.utils import extend_schema_field
from django.core.mail import send_mail
from django.conf import settings
import secrets
import string

from .models import (
    Organization,
    OrgMembership,
    OrganizationAPIKey,
    Module,
    OrganizationModule,
)

User = get_user_model()


class UserDetailSerializer(serializers.ModelSerializer):
    """Serializer for detailed user information in organizations"""
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'email', 'full_name', 'is_active']

    @extend_schema_field(serializers.CharField())
    def get_full_name(self, obj) -> str:
        """Returns the user's full name"""
        return obj.get_full_name()


class NullablePKRelatedField(serializers.PrimaryKeyRelatedField):
    def to_internal_value(self, data):
        if data in (None, "", "0", 0):
            return None
        return super().to_internal_value(data)


class OrganizationSerializer(serializers.ModelSerializer):
    owner = UserDetailSerializer(read_only=True)
    created_by = UserDetailSerializer(read_only=True)
    owner_email = serializers.EmailField(write_only=True, required=False)
    unique_name = serializers.CharField(read_only=True)  # This is auto-generated

    class Meta:
        model = Organization
        fields = [
            "id",
            "name",
            "unique_name",
            "slug",
            "status",
            "owner",
            "created_by",
            "owner_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "slug", "unique_name", "created_by", "created_at", "updated_at"]

    def validate(self, attrs):
        if self.instance is None:  # Only for creation
            owner_email = attrs.get("owner_email")
            if not owner_email:
                raise serializers.ValidationError({"owner_email": "Provide owner_email for organization creation."})
        return attrs

    def create(self, validated_data):
        owner_email = validated_data.pop("owner_email", None)
        request = self.context.get("request")

        if request and request.user and not validated_data.get("created_by"):
            validated_data["created_by"] = request.user

        if owner_email:
            owner, _ = User.objects.get_or_create(email=owner_email, defaults={"is_active": True})
            validated_data["owner"] = owner

        return super().create(validated_data)


class OrgMembershipSerializer(serializers.ModelSerializer):
    user = UserDetailSerializer(read_only=True)
    user_email = serializers.EmailField(write_only=True, required=False)
    email = serializers.EmailField(write_only=True, required=False)  # Accept both field names
    organization = serializers.PrimaryKeyRelatedField(queryset=Organization.objects.all())

    class Meta:
        model = OrgMembership
        fields = ["id", "organization", "user", "user_email", "email", "role", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        """Ensure either user_email or email is provided for new user creation"""
        user_email = attrs.get("user_email") or attrs.get("email")
        organization = attrs.get("organization")

        # If no email is provided, we need to ensure a user can be identified
        if not user_email:
            raise serializers.ValidationError({
                "user_email": "Email is required to add a member to the organization."
            })

        # Check if user is already a member of this organization
        if organization and user_email:
            from django.contrib.auth import get_user_model
            User = get_user_model()

            try:
                existing_user = User.objects.get(email=user_email)
                if OrgMembership.objects.filter(
                    organization=organization,
                    user=existing_user,
                    is_active=True
                ).exists():
                    raise serializers.ValidationError({
                        "user_email": f"User {user_email} is already a member of this organization."
                    })
            except User.DoesNotExist:
                pass  # New user, this is fine

        return attrs

    def create(self, validated_data):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.tokens import default_token_generator

        User = get_user_model()
        # Accept both 'user_email' and 'email' field names
        user_email = validated_data.pop("user_email", None) or validated_data.pop("email", None)

        if user_email:
            # Check if user already exists
            try:
                user = User.objects.get(email=user_email)
                created_user = False
            except User.DoesNotExist:
                # Create new user with a random password
                password = self._generate_random_password()
                user = User.objects.create_user(
                    email=user_email,
                    password=password,
                    is_active=True
                )
                created_user = True

                # Send welcome email with login credentials
                self._send_welcome_email(user, password)

            validated_data["user"] = user

        membership = super().create(validated_data)

        # If we created a new user, send additional notification
        if user_email and created_user:
            self._send_organization_invite_email(membership)

        return membership

    def _generate_random_password(self, length=12):
        """Generate a secure random password"""
        import string
        import secrets
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        password = ''.join(secrets.choice(alphabet) for i in range(length))
        return password

    def _send_welcome_email(self, user, password):
        """Send welcome email with login credentials to new user"""
        subject = "Welcome to BillMunshi - Your Account Details"
        message = f"""
        Welcome to BillMunshi!
        
        An account has been created for you with the following credentials:
        
        Email: {user.email}
        Temporary Password: {password}
        
        Please log in to your account and change your password immediately for security.
        
        Login URL: {getattr(settings, 'FRONTEND_URL', 'https://billmunshi.com')}
        
        Best regards,
        The BillMunshi Team
        """

        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=False,
            )
        except Exception as e:
            # Log the error but don't fail the user creation
            print(f"Failed to send welcome email to {user.email}: {str(e)}")

    def _send_organization_invite_email(self, membership):
        """Send organization invitation email"""
        subject = f"You've been added to {membership.organization.name}"
        message = f"""
        Hello {membership.user.email},
        
        You have been added as a {membership.role.lower()} to the organization "{membership.organization.name}" on BillMunshi.
        
        You can now access the organization's features and collaborate with other team members.
        
        Login to get started: {getattr(settings, 'FRONTEND_URL', 'https://your-domain.com')}/login
        
        Best regards,
        The BillMunshi Team
        """

        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [membership.user.email],
                fail_silently=False,
            )
        except Exception as e:
            # Log the error but don't fail the membership creation
            print(f"Failed to send organization invite email to {membership.user.email}: {str(e)}")


class APIKeyIssueSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)


class APIKeyRevokeResponseSerializer(serializers.Serializer):
    """Serializer for API key revocation response."""
    id = serializers.UUIDField()
    name = serializers.CharField()
    prefix = serializers.CharField()
    created = serializers.DateTimeField()
    revoked = serializers.BooleanField()
    organization = serializers.PrimaryKeyRelatedField(queryset=Organization.objects.all())
    created_by = UserDetailSerializer(read_only=True)


class ModuleToggleSerializer(serializers.Serializer):
    """Serializer for toggling module status."""
    organization = serializers.PrimaryKeyRelatedField(queryset=Organization.objects.all())
    module = serializers.PrimaryKeyRelatedField(queryset=Module.objects.all())
    is_enabled = serializers.BooleanField(default=True)


class APIKeySerializer(serializers.ModelSerializer):
    created_by = UserDetailSerializer(read_only=True)
    organization = OrganizationSerializer(read_only=True)

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
    organization = OrganizationSerializer(read_only=True)
    is_enabled = serializers.BooleanField()

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


class OrgMembershipUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating organization membership details (like role)"""

    class Meta:
        model = OrgMembership
        fields = ["role", "is_active"]

    def validate_role(self, value):
        """Validate that the role is one of the allowed choices"""
        valid_roles = [choice[0] for choice in OrgMembership.ROLE_CHOICES]
        if value not in valid_roles:
            raise serializers.ValidationError(f"Invalid role. Must be one of: {valid_roles}")
        return value
