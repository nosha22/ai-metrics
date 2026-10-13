import pytest
from fastapi import HTTPException

from aimetrics.server import _check


@pytest.mark.parametrize("expected", ["teste-local", "teste-local\r", '"teste-local"', " teste-local "])
@pytest.mark.parametrize("header", ["Bearer teste-local", "bearer teste-local", "teste-local", "Bearer  teste-local "])
def test_token_variants_accepted(expected, header):
    _check(expected, header)


@pytest.mark.parametrize("header", [None, "", "Bearer outro", "Bearer teste-local-x"])
def test_wrong_token_rejected(header):
    with pytest.raises(HTTPException) as e:
        _check("teste-local", header)
    assert e.value.status_code == 401


def test_missing_server_token():
    with pytest.raises(HTTPException) as e:
        _check("", "Bearer x")
    assert e.value.status_code == 503
