"""Portal API routers.

Each submodule exposes an ``APIRouter`` named ``router`` that ``portal.main``
includes defensively. All ``/api/*`` routers except the auth login/logout
endpoints attach :func:`portal.deps.require_user` as a router-level dependency,
so authentication is enforced uniformly.
"""

from __future__ import annotations
