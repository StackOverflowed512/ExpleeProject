"""Server-rendered web interface routes, mounted under ``/ui``.

Every route here is thin: it parses and validates browser input, calls the
same services as the JSON API, and renders a template. State-changing routes
are POST-only and CSRF-protected (``app.web.forms.read_protected_form``).
Nothing here approves or sends on page load, and approving and sending are
separate explicit requests.
"""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import RedirectResponse

from app.routers.ui import campaigns, deliveries, drafts, explorer, prospects, system

router = APIRouter(prefix="/ui", include_in_schema=False)


@router.get("")
@router.get("/")
def ui_home() -> RedirectResponse:
    return RedirectResponse("/ui/explorer", status_code=303)


for module in (explorer, prospects, campaigns, drafts, deliveries, system):
    router.include_router(module.router)
