import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status

from hosts.auth import require_service_auth
from http_proxies.deps import get_http_proxy_service
from http_proxies.schemas import (
    HTTP_PROXY_NAME_PATTERN,
    HTTPProxyAttachmentOut,
    HTTPProxyCreate,
    HTTPProxyOut,
)
from http_proxies.service import HTTPProxyService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/http-proxies", tags=["http-proxies"], dependencies=[Depends(require_service_auth)]
)

HTTPProxyServiceDep = Annotated[HTTPProxyService, Depends(get_http_proxy_service)]
ProxyName = Annotated[str, Path(pattern=HTTP_PROXY_NAME_PATTERN)]


@router.post("", response_model=HTTPProxyOut, status_code=status.HTTP_201_CREATED)
async def create_http_proxy(
    payload: HTTPProxyCreate,
    service: HTTPProxyServiceDep,
) -> HTTPProxyOut:
    await service.create_http_proxy(
        name=payload.name,
        target=str(payload.target),
        headers=payload.headers,
    )
    return HTTPProxyOut(name=payload.name, status="created")


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_http_proxy(name: ProxyName, service: HTTPProxyServiceDep) -> Response:
    await service.delete_http_proxy(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/hosts/{host_id}", response_model=HTTPProxyAttachmentOut)
async def attach_http_proxy(
    name: ProxyName,
    host_id: uuid.UUID,
    service: HTTPProxyServiceDep,
) -> HTTPProxyAttachmentOut:
    await service.attach_http_proxy(name, host_id)
    return HTTPProxyAttachmentOut(name=name, host_id=host_id, status="attached")


@router.delete("/{name}/hosts/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_http_proxy(
    name: ProxyName,
    host_id: uuid.UUID,
    service: HTTPProxyServiceDep,
) -> Response:
    await service.detach_http_proxy(name, host_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
