import pytest
from pydantic import ValidationError

from api.profile_api import ProfileForm


def test_line_only_profile_accepts_missing_email():
    profile = ProfileForm(
        name="LINE 使用者",
        email="",
        lineBound=True,
    )

    assert profile.email is None
    assert profile.lineBound is True


def test_profile_still_accepts_a_valid_email():
    profile = ProfileForm(
        name="Google 使用者",
        email="member@example.com",
    )

    assert str(profile.email) == "member@example.com"


def test_profile_still_rejects_an_invalid_non_empty_email():
    with pytest.raises(ValidationError):
        ProfileForm(name="測試使用者", email="not-an-email")
