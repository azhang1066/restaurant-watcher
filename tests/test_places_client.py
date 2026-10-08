"""Tests for the one part of places_client.py that isn't an HTTP call.

`place_summary()` is shared by seed.py and the dashboard's add flow, so it
decides what actually lands in the `restaurants` row in both. The HTTP
functions around it stay untested on purpose -- they're a request and a
field mask, and faking `requests` would only assert the mock.
"""
import pytest

import places_client
from places_client import PlaceNotFound, place_summary


def test_place_summary_maps_a_search_hit_to_row_fields():
    fields = place_summary({
        "id": "place-lilia",
        "displayName": {"text": "Lilia", "languageCode": "en"},
        "formattedAddress": "567 Union Ave, Brooklyn, NY 11211",
        "googleMapsUri": "https://maps.google.com/?cid=1",
    })

    # The keys are add_restaurant()'s parameters -- both callers splat this.
    assert fields == {
        "name": "Lilia",
        "place_id": "place-lilia",
        "address": "567 Union Ave, Brooklyn, NY 11211",
        "maps_url": "https://maps.google.com/?cid=1",
    }


def test_place_summary_prefers_googles_name_over_the_query():
    """What was typed is a search query; what's stored should be the name the
    place actually goes by, or the dashboard and the alerts read like a
    search box."""
    fields = place_summary({"id": "x", "displayName": {"text": "Lilia"}},
                           fallback_name="lilia bklyn")

    assert fields["name"] == "Lilia"


def test_place_summary_falls_back_when_google_has_no_display_name():
    """Every field but `id` is optional in a Text Search response. A row with
    no name at all would render as a blank link on the dashboard."""
    fields = place_summary({"id": "x"}, fallback_name="Don Angie")

    assert fields["name"] == "Don Angie"
    assert fields["address"] is None
    assert fields["maps_url"] is None


def test_place_summary_falls_back_on_an_empty_display_name():
    """`{"text": ""}` is not a usable name either -- `or` rather than `.get`
    with a default is what makes that fall through."""
    fields = place_summary({"id": "x", "displayName": {"text": ""}},
                           fallback_name="Don Angie")

    assert fields["name"] == "Don Angie"


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


def test_a_404_for_a_place_id_is_place_not_found(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setattr(places_client._session, "get", lambda *a, **kw: _Resp(404))

    with pytest.raises(PlaceNotFound):
        places_client.get_business_status("gone")


def test_other_http_errors_are_not_place_not_found(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setattr(places_client._session, "get", lambda *a, **kw: _Resp(500))

    with pytest.raises(RuntimeError):
        places_client.get_business_status("p1")


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_key_is_an_auth_error(monkeypatch, code):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setattr(places_client._session, "get", lambda *a, **kw: _Resp(code))
    monkeypatch.setattr(places_client._session, "post", lambda *a, **kw: _Resp(code))

    with pytest.raises(places_client.PlacesAuthError):
        places_client.get_business_status("p1")
    with pytest.raises(places_client.PlacesAuthError):
        places_client.find_place_id("Lilia")


def test_an_unknown_business_status_is_recorded_as_unspecified(monkeypatch):
    """The database CHECK would refuse a value it has never heard of, and the
    row would then fail every run."""
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setattr(places_client._session, "get",
                        lambda *a, **kw: _Resp(200, {"businessStatus": "ON_FIRE"}))

    assert places_client.get_business_status("p1") == "BUSINESS_STATUS_UNSPECIFIED"


def test_the_place_id_is_quoted_into_the_url(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    urls = []
    monkeypatch.setattr(places_client._session, "get",
                        lambda url, **kw: urls.append(url) or _Resp(200, {}))

    places_client.get_business_status("a/b?c")

    assert urls == [places_client.PLACES_BASE + "/places/a%2Fb%3Fc"]
