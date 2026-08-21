from fastapi import APIRouter, Depends

from inferna_server.auth import get_current_user
from inferna_server.models import User
from inferna_server.services.compatibility import ENGINE_VENDORS

router = APIRouter(prefix="/compatibility", tags=["compatibility"])


@router.get("")
async def compatibility(_: User = Depends(get_current_user)) -> dict:
    return {"engine_vendors": {k: sorted(v) for k, v in ENGINE_VENDORS.items()}}
