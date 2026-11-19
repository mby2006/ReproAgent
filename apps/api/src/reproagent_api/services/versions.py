"""Retry versioned inserts on unique-constraint races.

Append-only version chains compute the next number as latest+1 then insert. Two
concurrent writers can read the same latest and collide on the (owner, version)
unique constraint; PostgREST surfaces that as APIError code 23505. Re-reading the
latest version and retrying keeps the chain monotonic without dropping a write.
"""

from collections.abc import Callable

from postgrest.exceptions import APIError

_UNIQUE_VIOLATION = "23505"


def insert_versioned[T](
    next_version: Callable[[], int],
    insert: Callable[[int], T],
    *,
    max_attempts: int = 5,
) -> T:
    version = next_version()
    for attempt in range(max_attempts):
        try:
            return insert(version)
        except APIError as exc:
            if exc.code != _UNIQUE_VIOLATION or attempt == max_attempts - 1:
                raise
            version = next_version()
    raise RuntimeError("insert_versioned exhausted attempts")