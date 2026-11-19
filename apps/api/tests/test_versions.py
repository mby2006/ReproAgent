import pytest
from postgrest.exceptions import APIError
from reproagent_api.services.versions import insert_versioned


def _conflict() -> APIError:
    return APIError({"code": "23505", "message": "duplicate key", "details": "", "hint": ""})


def test_insert_versioned_retries_on_unique_conflict():
    latest = {"v": 1}
    seen = []

    def next_version() -> int:
        return latest["v"] + 1

    def insert(v: int) -> dict:
        seen.append(v)
        if len(seen) == 1:
            # a concurrent writer claimed version 2 before us
            latest["v"] = 2
            raise _conflict()
        return {"version": v}

    result = insert_versioned(next_version, insert)
    assert result == {"version": 3}
    assert seen == [2, 3]


def test_insert_versioned_reraises_non_conflict():
    def insert(v: int) -> dict:
        raise APIError({"code": "42501", "message": "rls", "details": "", "hint": ""})

    with pytest.raises(APIError):
        insert_versioned(lambda: 1, insert)


def test_insert_versioned_gives_up_after_max_attempts():
    calls = {"n": 0}

    def insert(v: int) -> dict:
        calls["n"] += 1
        raise _conflict()

    with pytest.raises(APIError):
        insert_versioned(lambda: 1, insert, max_attempts=3)
    assert calls["n"] == 3