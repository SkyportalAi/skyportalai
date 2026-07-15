from dataclasses import FrozenInstanceError

import pytest

from skyportalai.types import User


def test_from_dict_extracts_name_and_keeps_raw():
    user = User.from_dict({"name": "Henrique", "id": 7})
    assert user.name == "Henrique"
    assert user.raw == {"name": "Henrique", "id": 7}


def test_from_dict_defaults_missing_name_to_empty():
    user = User.from_dict({})
    assert user.name == ""
    assert user.raw == {}


def test_user_is_frozen():
    user = User.from_dict({"name": "x"})
    with pytest.raises(FrozenInstanceError):
        user.name = "y"
