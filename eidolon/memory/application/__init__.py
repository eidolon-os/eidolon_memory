"""Application layer package.

Import concrete modules instead of re-exporting them here.  Eager package-level
imports made ``application.query_embedding`` load ``livekit_recall`` which loads
``public_recall`` and then loops back through ``mempalace_fast_search``.  The
cycle was order-dependent: the full suite happened to pass while the focused
fast-search contract could not even be collected.
"""

__all__: list[str] = []
