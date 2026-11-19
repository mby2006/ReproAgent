import threading

import httpx

from supabase import Client, ClientOptions, create_client

from ..config import ApiSettings

# One client per (url, key) per process: each Client owns an httpx connection
# pool, so creating one per request leaks sockets/FDs. The lock guards the
# check-then-create against the sync-dependency threadpool.
_clients: dict[tuple[str, str], Client] = {}
_lock = threading.Lock()


def _options() -> ClientOptions:
    # trust_env=False: some hosts set NO_PROXY entries (e.g. "[::1]") that
    # httpx cannot parse; Supabase traffic never needs a system proxy here.
    return ClientOptions(httpx_client=httpx.Client(trust_env=False, timeout=30.0))


def _cached_client(url: str, key: str) -> Client:
    cache_key = (url, key)
    with _lock:
        client = _clients.get(cache_key)
        if client is None:
            client = create_client(url, key, _options())
            _clients[cache_key] = client
        return client


def make_service_client(settings: ApiSettings) -> Client:
    """Service-role client: bypasses RLS, so every query MUST enforce ownership
    (via the projects.user_id chain) in application code."""
    if not settings.supabase_service_role_key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not configured")
    return _cached_client(settings.supabase_url, settings.supabase_service_role_key)


def make_anon_client(settings: ApiSettings) -> Client:
    if not settings.supabase_anon_key:
        raise RuntimeError("SUPABASE_ANON_KEY is not configured")
    return _cached_client(settings.supabase_url, settings.supabase_anon_key)
