"""Values FastAPI hands to each endpoint: the runtime, a database session and the
calling tenant (found from the API key)."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from parley.db.models import Tenant
from parley.services.runtime import Runtime
from parley.services.tenants import find_tenant_by_api_key

_bearer = HTTPBearer(auto_error=False)


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def get_session(rt: Annotated[Runtime, Depends(get_runtime)]) -> Iterator[Session]:
    """One transaction per request: committed if the endpoint succeeds, rolled
    back if it raises."""
    with rt.session_factory.begin() as session:
        yield session


def get_tenant(
    session: Annotated[Session, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Tenant:
    """The API key decides the tenant for every request (header: Authorization: Bearer <key>)."""
    tenant = find_tenant_by_api_key(session, credentials.credentials) if credentials else None
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return tenant


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
SessionDep = Annotated[Session, Depends(get_session)]
TenantDep = Annotated[Tenant, Depends(get_tenant)]
