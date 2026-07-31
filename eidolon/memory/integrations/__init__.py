"""Optional wiring to systems that embed this service.

Nothing in the service core may import from here, and nothing here is needed to
run it. Each subpackage adapts the service to one host system and depends on
that system's packages, which are declared as extras rather than requirements —
so a standalone deployment installs neither.

An import-boundary test enforces the one-way rule.
"""
