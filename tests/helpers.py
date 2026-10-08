"""Test-only helpers."""
import db


def archive_restaurant(restaurant_id):
    """Archive a row directly. The app never archives except as part of
    recording a permanent closure, so there's no db function for it."""
    with db.get_conn() as conn:
        conn.execute("UPDATE restaurants SET archived = 1 WHERE id = ?", (restaurant_id,))
