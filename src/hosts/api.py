import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.exc import SQLAlchemyError

from hosts.auth import require_service_auth
from hosts.deps import get_host_service
from hosts.exceptions import HostTeardownError
from hosts.models import Host
from hosts.schemas import HostCreate, HostOut, HostRenew
from hosts.service import HostService
from networking.tailscale import NetworkError
from providers.exceptions import ProviderError, UnknownProviderError, UnsupportedSizingError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hosts", tags=["hosts"], dependencies=[Depends(require_service_auth)])

HostServiceDep = Annotated[HostService, Depends(get_host_service)]


@router.post("", response_model=HostOut, status_code=status.HTTP_201_CREATED)
async def create_host(
    service: HostServiceDep,
    payload: HostCreate | None = None,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            pattern=r"^[A-Za-z0-9_\-:.]+$",
            description=(
                "Caller-supplied retry key. Same key within "
                "IDEMPOTENCY_KEY_TTL_HOURS returns the same host. Charset: "
                "A-Z a-z 0-9 _ - : . (length 1-255)."
            ),
        ),
    ] = None,
) -> Host:
    host_create = payload or HostCreate()
    # An omitted expires_at gets the default lease; an explicit null is the
    # caller's deliberate opt-in to a permanent host.
    expires_at = host_create.expires_at if "expires_at" in host_create.model_fields_set else ...

    try:
        return await service.get_or_create_host(
            env=host_create.env,
            secrets={name: entry.to_storage() for name, entry in host_create.secrets.items()},
            image=host_create.image,
            template=host_create.template,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            provider=host_create.provider,
            instance_type=host_create.instance_type,
            disk_gb=host_create.disk_gb,
        )
    except (UnknownProviderError, UnsupportedSizingError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        logger.exception("unexpected database error during host provisioning")
        raise HTTPException(
            status_code=503,
            detail="host provisioning could not be completed",
        ) from exc


@router.get("", response_model=list[HostOut])
async def list_hosts(service: HostServiceDep) -> list[Host]:
    return await service.list_hosts()


@router.get("/{host_id}", response_model=HostOut)
async def get_host(host_id: uuid.UUID, service: HostServiceDep) -> Host:
    if host := await service.get_host(host_id):
        return host
    raise HTTPException(status_code=404, detail="host not found")


@router.post("/{host_id}/renew", response_model=HostOut)
async def renew_host(
    host_id: uuid.UUID,
    service: HostServiceDep,
    payload: HostRenew | None = None,
) -> Host:
    host_renew = payload or HostRenew()
    return await service.renew_host(host_id, expires_at=host_renew.expires_at)


@router.delete("/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_host(host_id: uuid.UUID, service: HostServiceDep) -> Response:
    try:
        await service.delete_host(host_id)
    except (ProviderError, NetworkError) as exc:
        logger.exception("unexpected error deleting sandbox host")
        raise HostTeardownError("host teardown could not be completed") from exc
    except SQLAlchemyError as exc:
        logger.exception("unexpected database error during host teardown")
        raise HostTeardownError("host teardown could not be completed") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
