from pathlib import Path
from django.http import FileResponse
from django.views.decorators.http import require_safe
from django.views.decorators.csrf import ensure_csrf_cookie

@ensure_csrf_cookie
@require_safe
def public_homepage(request):
    path = Path(__file__).resolve().parent / "homepage" / "index.html"
    response = FileResponse(path.open("rb"), content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    return response
