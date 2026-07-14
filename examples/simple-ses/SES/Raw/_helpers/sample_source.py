"""Built-in sample source data for the simple-ses example.

In a real SES a Folder object fetches from an external source — a public API, an
SFTP drop, a cloud bucket — and lands whatever it finds into staging. To keep
this example runnable offline and completely deterministic, the "source" is a
small fixed set of CSV snapshots emitted from here. The Folder objects land
these exactly as they would land real downloads.

Keeping the mechanical source work in a helper (rather than in the object body)
is the normal Weaver shape: the object stays a short, readable statement of
*what it produces*, and the helper carries the *how*.
"""

from __future__ import annotations

# One current snapshot of the customer master. Re-landed on every run: the
# customer table downstream is a full snapshot of whoever is current.
CUSTOMER_SNAPSHOT: dict[str, str] = {
    "customers.csv": (
        "customer_id,customer_name,segment,signup_date\n"
        "C001,Acme Industries,Enterprise,2025-11-03\n"
        "C002,Blue Fox Studio,SMB,2026-01-12\n"
        "C003,Cedar Analytics,Enterprise,2025-09-21\n"
        "C004,Dovetail Design,SMB,2026-02-02\n"
    ),
}

# Order extracts arrive one file per month and accumulate. A snapshot filename
# is landed exactly once; downstream the order table is loaded incrementally.
ORDER_SNAPSHOTS: dict[str, str] = {
    "orders-2026-01.csv": (
        "order_id,customer_id,order_date,amount\n"
        "O-1001,C001,2026-01-05,1200.00\n"
        "O-1002,C002,2026-01-09,340.50\n"
        "O-1003,C001,2026-01-22,880.00\n"
    ),
    "orders-2026-02.csv": (
        "order_id,customer_id,order_date,amount\n"
        "O-1004,C003,2026-02-03,5400.00\n"
        "O-1005,C004,2026-02-14,150.00\n"
        "O-1006,C002,2026-02-27,720.25\n"
    ),
}
