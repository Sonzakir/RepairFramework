"""Deterministic evidence about fixes: the parts the evidence commands share.

Behaviour Diff, Tamper Check and Fix Grip all need the same things:

- a *target*: a place where a version of the project can be applied and its tests
  run, either a compiled BugsInPy checkout (tests run in the bug's own Python in
  the executor container) or a local project with its own interpreter;
- the *versions* being compared: the buggy code, the reference fix, and candidate
  fixes from patch files or from the plausible patches of earlier runs;
- a way to apply a version, run something, and always get the checkout back
  byte for byte.

None of them asks a model for a verdict.
"""
