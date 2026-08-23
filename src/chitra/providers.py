"""Public import surface for the provider lifecycle seam.

The implementation lives in :mod:`chitra.provider_protocol` to make the
contract easy to find beside ``api_protocol``.  This plural alias keeps
provider-oriented call sites stable without adding a second implementation.
"""

from .provider_protocol import *  # noqa: F401,F403
