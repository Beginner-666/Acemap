"""Single urllib surface for every network client.

Keeping the symbols in one module lets verification and tests forbid all
outbound traffic by patching ``src.net.urlopen`` once.
"""

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

__all__ = ["HTTPError", "Request", "URLError", "urlopen"]
