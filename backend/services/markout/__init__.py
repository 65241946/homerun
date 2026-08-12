"""Markout / adverse-selection instrumentation (Gap 3).

Pure-logic core for measuring how quotes fare after they are hit. Everything in
this package is intentionally free of DB, network and event-bus dependencies so
it can be unit tested and replayed offline; the ingest/persistence layers sit
above it.

See docs/repair/ for the full specification.
"""

from .curves import (
    Cohort,
    FillObservation,
    MarkoutStats,
    TtlBucket,
    aggregate_markouts,
    classify_cohort,
    markout_bps,
    ttl_bucket_for,
)

__all__ = [
    "Cohort",
    "FillObservation",
    "MarkoutStats",
    "TtlBucket",
    "aggregate_markouts",
    "classify_cohort",
    "markout_bps",
    "ttl_bucket_for",
]
