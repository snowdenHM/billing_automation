# apps/users/views.py

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.encoding import force_bytes
from django.db.models import Prefetch
from django.utils.http import urlsafe_base64_encode
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import OrgMembership
from apps.common.utils import send_simple_email
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    UserSerializer,
)

User = get_user_model()


class RegisterView(APIView):
    permission_classes = [AllowAny]
    serializer_class = RegisterSerializer

    @extend_schema(request=RegisterSerializer, responses=UserSerializer, tags=["Auth"])
    def post(self, request, *args, **kwargs):
        serializer = RegisterSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {"user": UserSerializer(user, context={"request": request}).data},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    permission_classes = [AllowAny]
    serializer_class = LoginSerializer

    @extend_schema(request=LoginSerializer, responses=LoginSerializer, tags=["Auth"])
    def post(self, request, *args, **kwargs):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    serializer_class = PasswordResetRequestSerializer

    @extend_schema(request=PasswordResetRequestSerializer, responses={"200": None}, tags=["Auth"])
    def post(self, request, *args, **kwargs):
        serializer = PasswordResetRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user:
            token = PasswordResetTokenGenerator().make_token(user)
            uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
            message = (
                "Use the following credentials to reset your password:\n"
                f"uidb64: {uidb64}\n"
                f"token: {token}\n"
            )
            send_simple_email("Password Reset", message, to_email=user.email)
        return Response({"detail": "If the email exists, a reset link was sent."})


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    serializer_class = PasswordResetConfirmSerializer

    @extend_schema(request=PasswordResetConfirmSerializer, responses={"200": None}, tags=["Auth"])
    def post(self, request, *args, **kwargs):
        serializer = PasswordResetConfirmSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user_obj"]
        new_password = serializer.validated_data["new_password"]
        user.set_password(new_password)
        user.save(update_fields=["password"])
        return Response({"detail": "Password has been reset."})


class MeView(RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    @extend_schema(responses=UserSerializer, tags=["Users"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(request=UserSerializer, responses=UserSerializer, tags=["Users"])
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)

    @extend_schema(request=UserSerializer, responses=UserSerializer, tags=["Users"])
    def patch(self, request, *args, **kwargs):
        return super().patch(request, *args, **kwargs)

    def get_object(self):
        # Prefetch to avoid N+1 when serializing organizations + role
        return (
            self.request.user.__class__.objects
            .filter(pk=self.request.user.pk)
            .prefetch_related(
                Prefetch(
                    "memberships",
                    queryset=OrgMembership.objects.select_related("organization").only(
                        "role", "is_active",
                        "organization__id", "organization__name",
                        "organization__slug", "organization__status",
                    )
                )
            )
            .get()
        )
