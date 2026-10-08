"""The Places `businessStatus` values this project branches on, in one place.

They are compared in main/checker, rendered in app, matched in SQL in db and
defaulted in places_client; spelled as bare strings in each, a typo in one is
a silent mismatch rather than an error. The database enforces the same set
with a CHECK constraint (see db.SCHEMA), built from ALL_STATUSES.
"""
OPERATIONAL = "OPERATIONAL"
CLOSED_TEMPORARILY = "CLOSED_TEMPORARILY"
CLOSED_PERMANENTLY = "CLOSED_PERMANENTLY"
# Places can answer with this instead of omitting the field.
UNSPECIFIED = "BUSINESS_STATUS_UNSPECIFIED"

CLOSED_STATUSES = (CLOSED_PERMANENTLY, CLOSED_TEMPORARILY)
ALL_STATUSES = (OPERATIONAL, CLOSED_TEMPORARILY, CLOSED_PERMANENTLY, UNSPECIFIED)
