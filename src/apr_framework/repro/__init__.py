"""Strict Repro: turn a bug report into a failing pytest test you can trust.

The test must fail on the current code for the reason the report describes, and
reject plausible-wrong fixes ("probes") that lax tests let through. See
``docs/repro.md``.
"""
