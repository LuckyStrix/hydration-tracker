"""Reading configuration the way a person actually writes it.

A `.env` file is edited by hand in a text editor, which means the values in it
arrive with whatever the editor and the shell left on them. A trailing space is
invisible in every editor there is, and a credential carrying one fails
somewhere far away with a message about authentication.
"""

from __future__ import annotations

import importlib

import pytest

from hydration import config


def _reread(monkeypatch, **environment):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return importlib.reload(config)


@pytest.fixture(autouse=True)
def _restore():
    yield
    importlib.reload(config)


def test_a_trailing_space_is_not_part_of_the_password(monkeypatch):
    """The failure mode without this is a 401 from Garmin that looks exactly
    like a wrong password, retried every fifteen minutes."""
    fresh = _reread(monkeypatch, GARMIN_PASSWORD="hunter2 ", GARMIN_EMAIL=" me@example.com")
    assert fresh.GARMIN_PASSWORD == "hunter2"
    assert fresh.GARMIN_EMAIL == "me@example.com"


def test_quotes_a_person_added_are_not_part_of_the_password(monkeypatch):
    """`GARMIN_PASSWORD="hunter 2"` is what you write when it has a space in
    it. Compose strips them now; `docker run -e` does not."""
    fresh = _reread(monkeypatch, GARMIN_PASSWORD='"hunter 2"')
    assert fresh.GARMIN_PASSWORD == "hunter 2"


def test_single_quotes_too(monkeypatch):
    assert _reread(monkeypatch, GARMIN_PASSWORD="'hunter2'").GARMIN_PASSWORD == "hunter2"


def test_a_quote_inside_the_password_is_left_alone(monkeypatch):
    """Only a matched outer pair is stripped."""
    assert _reread(monkeypatch, GARMIN_PASSWORD='hun"ter2').GARMIN_PASSWORD == 'hun"ter2'


def test_an_unmatched_quote_is_left_alone(monkeypatch):
    assert _reread(monkeypatch, GARMIN_PASSWORD='"hunter2').GARMIN_PASSWORD == '"hunter2'


def test_a_blank_value_reads_as_not_configured(monkeypatch):
    """`.env.example` ships with `GARMIN_EMAIL=` on it, and an empty string is
    not a credential -- `sync.start_sync` has to see None and stay idle."""
    fresh = _reread(monkeypatch, GARMIN_EMAIL="", GARMIN_PASSWORD="   ")
    assert fresh.GARMIN_EMAIL is None
    assert fresh.GARMIN_PASSWORD is None
