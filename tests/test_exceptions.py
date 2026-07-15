import pathlib
import tomllib

from skyportalai._exceptions import (
    APIConnectionError,
    APIError,
    APIStatusError,
    AuthenticationError,
    SkyportalError,
)
from skyportalai._version import __version__


def test_version_is_a_string():
    assert isinstance(__version__, str)
    assert __version__.count(".") >= 1


def test_version_matches_pyproject():
    data = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    assert data["project"]["version"] == __version__


def test_hierarchy():
    assert issubclass(APIConnectionError, SkyportalError)
    assert issubclass(APIStatusError, SkyportalError)
    assert issubclass(AuthenticationError, APIStatusError)
    assert issubclass(APIError, APIStatusError)


def test_status_error_carries_status_and_body():
    err = APIError("boom", status_code=503, body={"detail": "down"})
    assert err.status_code == 503
    assert err.body == {"detail": "down"}
    assert str(err) == "boom"


def test_status_error_status_is_optional():
    err = AuthenticationError("nope")
    assert err.status_code is None
    assert err.body is None
