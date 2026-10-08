"""Thin wrapper around Places API (New). Field masks are kept minimal on
purpose -- businessStatus/displayName/formattedAddress stay in the cheap
Essentials/Pro tier, so a weekly check of a few hundred places costs cents.
Avoid adding rating/hours/photos/phone fields here; those bump every call
to the Enterprise SKU.
"""
import logging
from urllib.parse import quote

from config import env_float, env_str
from http_session import retrying_session
from statuses import ALL_STATUSES, OPERATIONAL, UNSPECIFIED

logger = logging.getLogger(__name__)

PLACES_BASE = "https://places.googleapis.com/v1"

MAX_BIAS_RADIUS_M = 50_000.0

_session = retrying_session(("GET", "POST"))


class PlaceNotFound(Exception):
    """Places answered 404 for a place_id: Google no longer recognises it.

    Distinct from a transient failure because retrying never helps -- place
    ids can be retired or merged, and the row has to be found again by hand.
    """


class PlacesAuthError(Exception):
    """Places refused the API key (401/403): bad, restricted or over quota.
    Every remaining request in a run would fail the same way."""


def _api_key() -> str:
    key = env_str("GOOGLE_PLACES_API_KEY")
    if not key:
        raise RuntimeError("Set GOOGLE_PLACES_API_KEY in your environment / .env")
    return key


def _raise_for_status(resp) -> None:
    if resp.status_code in (401, 403):
        raise PlacesAuthError(f"Places API answered {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()


def _location_bias() -> dict | None:
    """A circle around SEARCH_BIAS_LAT/LNG, or None when it isn't configured.

    A bias, not a restriction: a name typed with an explicit address elsewhere
    still resolves there. Places caps the radius at 50 km.
    """
    lat = env_float("SEARCH_BIAS_LAT")
    lng = env_float("SEARCH_BIAS_LNG")
    if lat is None or lng is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        logger.warning("SEARCH_BIAS_LAT/LNG (%s, %s) is out of range -- ignoring it.", lat, lng)
        return None
    radius = env_float("SEARCH_BIAS_RADIUS_M")
    if radius is None or not 0 < radius <= MAX_BIAS_RADIUS_M:
        radius = MAX_BIAS_RADIUS_M
    return {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius}}


def find_place_id(name: str, address_hint: str = "") -> dict | None:
    """Resolve a restaurant name (+ optional address/neighborhood) to a place_id
    via Text Search. Used by seed.py and the dashboard's add form."""
    url = f"{PLACES_BASE}/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.googleMapsUri",
    }
    query = f"{name} {address_hint}".strip()
    body: dict = {"textQuery": query, "includedType": "restaurant"}
    bias = _location_bias()
    if bias:
        body["locationBias"] = bias
    resp = _session.post(url, headers=headers, json=body, timeout=15)
    _raise_for_status(resp)
    places = resp.json().get("places", [])
    return places[0] if places else None


def place_summary(place: dict, fallback_name: str = "") -> dict:
    """Flatten a Text Search result into the fields db.add_restaurant() takes.

    Shared by seed.py and the dashboard's add flow so the two can't disagree
    about what gets stored -- `displayName` is Google's canonical name for
    the place ("Lilia" for a query of "lilia brooklyn"), with the text the
    caller searched for as the fallback if the field is missing.
    """
    return {
        "name": (place.get("displayName") or {}).get("text") or fallback_name,
        "place_id": place["id"],
        "address": place.get("formattedAddress"),
        "maps_url": place.get("googleMapsUri"),
    }


def get_business_status(place_id: str) -> str:
    """Cheap status check: id + businessStatus + displayName only.

    A value outside statuses.ALL_STATUSES (Google adding one) comes back as
    UNSPECIFIED, logged: the database would refuse to store it, and the row
    would then fail every run.
    """
    url = f"{PLACES_BASE}/places/{quote(place_id, safe='')}"
    headers = {
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": "id,businessStatus,displayName",
    }
    resp = _session.get(url, headers=headers, timeout=15)
    if resp.status_code == 404:
        raise PlaceNotFound(place_id)
    _raise_for_status(resp)
    status = resp.json().get("businessStatus", OPERATIONAL)
    if status not in ALL_STATUSES:
        logger.warning("Places returned an unknown businessStatus %r for %s -- "
                       "recording it as %s", status, place_id, UNSPECIFIED)
        return UNSPECIFIED
    return status
