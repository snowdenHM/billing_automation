"""
Common views for the application.
"""
import logging

from django.conf import settings
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.static import serve as django_serve

logger = logging.getLogger(__name__)


@xframe_options_exempt
@csrf_exempt
def serve_bill_file(request, path):
    """
    Serve media files (PDF/images) and allow iframe embedding from the
    configured frontend origins. ``django_serve`` validates the path and
    raises Http404 on traversal attempts.
    """
    logger.debug("Serving bill file: %s", path)

    response = django_serve(request, path, document_root=settings.MEDIA_ROOT)
    response['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response['Cache-Control'] = 'public, max-age=3600'
    return response
