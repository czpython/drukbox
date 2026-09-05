import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status

from host_secrets.deps import get_host_secret_service
from host_secrets.schemas import SECRET_NAME_PATTERN, SecretRegistration
from host_secrets.service import HostSecretService
from hosts.auth import require_service_auth

router = APIRouter(
    prefix="/hosts/{host_id}/secrets",
    tags=["host-secrets"],
    dependencies=[Depends(require_service_auth)],
)


@router.put("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def register_secret(
    host_id: uuid.UUID,
    name: Annotated[str, Path(pattern=SECRET_NAME_PATTERN)],
    payload: SecretRegistration,
    service: Annotated[HostSecretService, Depends(get_host_secret_service)],
) -> Response:
    await service.register_secret(host_id=host_id, name=name, entry=payload.to_storage())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
