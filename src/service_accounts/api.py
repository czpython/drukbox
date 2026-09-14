from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.exceptions import ResourceNotFoundError
from hosts.auth import require_admin_auth
from service_accounts.exceptions import ServiceAccountExistsError, ServiceAccountStateError
from service_accounts.models import ServiceAccount

SERVICE_ACCOUNT_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"

router = APIRouter(
    prefix="/service-accounts",
    tags=["service-accounts"],
    dependencies=[Depends(require_admin_auth)],
)

ServiceAccountName = Annotated[str, Path(pattern=SERVICE_ACCOUNT_NAME_PATTERN)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_service_account(
    name: Annotated[str, Body(embed=True, pattern=SERVICE_ACCOUNT_NAME_PATTERN)],
    session: SessionDep,
    response: Response,
) -> dict[str, str]:
    account = ServiceAccount(name=name)
    token = account.issue_token()
    session.add(account)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ServiceAccountExistsError(f"service account {name} already exists") from exc

    response.headers["Cache-Control"] = "no-store"
    return {"name": name, "token": token}


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_service_account(name: ServiceAccountName, session: SessionDep) -> Response:
    if account := await session.get(ServiceAccount, name):
        if account.name == ServiceAccount.ADMIN:
            raise ServiceAccountStateError("the admin account has no token to revoke")
        await session.delete(account)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    raise ResourceNotFoundError("service account not found")
