from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from supabase import Client

from .clients.supabase import make_anon_client
from .config import ApiSettings, get_settings

bearer_scheme = HTTPBearer(auto_error=False)


def get_anon_client(settings: Annotated[ApiSettings, Depends(get_settings)]) -> Client:
    return make_anon_client(settings)


async def get_current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    anon_client: Annotated[Client, Depends(get_anon_client)],
) -> UUID:
    """Verify the Supabase user JWT and return the authenticated user id."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        result = anon_client.auth.get_user(credentials.credentials)
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from exc
    if result is None or result.user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return UUID(result.user.id)


def ensure_owner(resource_user_id: UUID, user_id: UUID) -> None:
    """Second ownership guard on top of RLS. 404 to avoid leaking existence."""
    if resource_user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
