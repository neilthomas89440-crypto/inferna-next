"""REST API router aggregation under /api/v1."""

from __future__ import annotations

from fastapi import APIRouter

from inferna_server.api import (
    auth as auth_api,
)
from inferna_server.api import (
    clusters as clusters_api,
)
from inferna_server.api import (
    dashboard as dashboard_api,
)
from inferna_server.api import (
    instances as instances_api,
)
from inferna_server.api import (
    models as models_api,
)
from inferna_server.api import (
    users as users_api,
)
from inferna_server.api import (
    workers as workers_api,
)
from inferna_server.api import (
    compatibility as compatibility_api,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_api.router)
api_router.include_router(users_api.router)
api_router.include_router(clusters_api.router)
api_router.include_router(workers_api.router)
api_router.include_router(models_api.router)
api_router.include_router(instances_api.router)
api_router.include_router(dashboard_api.router)
api_router.include_router(compatibility_api.router)
