import pytest
from fastapi import HTTPException

from aimetrics.server import _check


@pytest.mark.parametrize("expected", ["local-test", "local-test\r", '"local-test"', " local-test "])
@pytest.mark.parametrize("header", ["Bearer local-test", "bearer local-test", "local-test", "Bearer  local-test "])
def test_token_variants_accepted(expected, header):
    _check(expected, header)


@pytest.mark.parametrize("header", [None, "", "Bearer other", "Bearer local-test-x"])
def test_wrong_token_rejected(header):
    with pytest.raises(HTTPException) as e:
        _check("local-test", header)
    assert e.value.status_code == 401


def test_missing_server_token():
    with pytest.raises(HTTPException) as e:
        _check("", "Bearer x")
    assert e.value.status_code == 503
