"""REST API over the web endpoints behind instantdomainsearch.com."""

from .instant import InstantClient, InstantError

__all__ = ["InstantClient", "InstantError"]
