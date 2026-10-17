"""HTTPS opening with certificate checks and no redirects.

urllib follows redirects by default and copies request headers to the new
location, which would send an API key or webhook token wherever a redirect
points. Every outgoing request here goes through ``open_url`` instead.
"""

from __future__ import annotations

import ssl
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None  # a redirect is reported as an HTTP error, never followed


def open_url(request: urllib.request.Request, timeout: float):
    """Open with certificate checks and no redirects. Honors HTTPS_PROXY."""
    opener = urllib.request.build_opener(NoRedirect(),
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return opener.open(request, timeout=timeout)
