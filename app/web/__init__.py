"""Server-rendered web interface (Phase 6) infrastructure.

Templates live in ``app/templates``, static assets in ``app/static``, and the
routes in ``app/routers/ui``. This package holds what those routes share:
template rendering, CSRF protection, bounded form parsing, response security
headers, HTML error pages, and presentation labels.

The UI calls the same services as the JSON API. It adds no business rules:
every eligibility, approval, and delivery check stays in ``app/services``.
"""
