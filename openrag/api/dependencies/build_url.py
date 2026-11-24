from fastapi import Request

def build_url(request: Request, route_name: str, *, preferred_url_scheme: str | None = None, **path_params) -> str:
    """Build a URL using the preferred scheme if configured."""
    url = request.url_for(route_name, **path_params)
    if preferred_url_scheme:
        url = url.replace(scheme=preferred_url_scheme)
    return str(url)