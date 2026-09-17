"""Tests for the one part of places_client.py that isn't an HTTP call.

`place_summary()` is shared by seed.py and the dashboard's add flow, so it
decides what actually lands in the `restaurants` row in both. The HTTP
functions around it stay untested on purpose -- they're a request and a
field mask, and faking `requests` would only assert the mock.
"""
from places_client import place_summary


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
