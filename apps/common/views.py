"""
Common views for the application.
"""

import os
from django.http import FileResponse, Http404
from django.conf import settings
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.static import serve as django_serve


@xframe_options_exempt
@csrf_exempt
def serve_bill_file(request, path):
    """
    Serve media files (PDF/images) with headers that allow iframe embedding.
    This view explicitly allows files to be displayed in an iframe.
    """
    print(f"🔵 Serving file: {path}")  # Debug log
    
    # Use Django's built-in serve function which handles everything properly
    response = django_serve(request, path, document_root=settings.MEDIA_ROOT)
    
    # Add headers to allow iframe embedding
    # Don't set X-Frame-Options (xframe_options_exempt decorator handles this)
    # Don't set CSP to allow embedding from any origin in development
    
    # Allow CORS
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response['Access-Control-Allow-Headers'] = '*'
    
    # Set cache control
    response['Cache-Control'] = 'public, max-age=3600'
    
    print(f"✅ File served successfully: {path}")  # Debug log
    
    return response
