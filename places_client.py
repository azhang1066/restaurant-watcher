"""Thin wrapper around Places API (New). Field masks are kept minimal on
purpose -- businessStatus/displayName/formattedAddress stay in the cheap
Essentials/Pro tier, so a weekly check of a few hundred places costs cents.
Avoid adding rating/hours/photos/phone fields here; those bump every call
to the Enterprise SKU.
"""
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PLACES_BASE = "https://places.googleapis.com/v1"

_session = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET", "POST"),
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))


def _api_key():
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        raise RuntimeError("Set GOOGLE_PLACES_API_KEY in your environment / .env")
    return key


def find_place_id(name, address_hint=""):
    """Resolve a restaurant name (+ optional address/neighborhood) to a place_id
    via Text Search. Used once, at seed time, per restaurant."""
    url = f"{PLACES_BASE}/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.googleMapsUri",
    }
    query = f"{name} {address_hint}".strip()
    resp = _session.post(url, headers=headers, json={"textQuery": query}, timeout=15)
    resp.raise_for_status()
    places = resp.json().get("places", [])
    return places[0] if places else None


def place_summary(place, fallback_name=""):
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


def get_business_status(place_id):
    """Cheap status check: id + businessStatus + displayName only."""
    url = f"{PLACES_BASE}/places/{place_id}"
    headers = {
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": "id,businessStatus,displayName",
    }
    resp = _session.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("businessStatus", "OPERATIONAL")
