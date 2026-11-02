"""Compare observations: noise calibration, divergences, behaviour classes.

A test's *signature* in one version is its outcome, the first line of its failure
message, and every recorded call of a focus function that all compared versions
define (arguments, result or raised exception, state after the call), plus the
number of calls that went over the recording cap.

- **Unstable tests.** The buggy version is observed twice. A test whose signature
  differs between those two runs depends on something other than the code (time,
  randomness, ordering) and is left out of every comparison; the report lists it.
- **Divergence.** For one test, two versions' signatures differ. The report shows
  the first difference: a different outcome, or the first differing call.
- **Behaviour classes.** Versions with identical signatures on every compared test
  form one class. Two versions in different classes behave differently on at
  least one concrete test; two in one class agree on everything observed, which
  is evidence of equivalence on those tests only.
"""

from dataclasses import dataclass

from apr_framework.behaviour.amplify import Variant, variant_of_test_id
from apr_framework.behaviour.observe import Observation, RecordedTest

MISSING = "missing"


@dataclass(frozen=True)
class Divergence:
    """The first difference between two versions on one test.

    Fields:
        test_id:     The test (an original test or an amplified variant).
        kind:        ``outcome``, ``call``, ``call_count`` or ``missing``.
        left/right:  What each side did (outcome, or the differing call's result).
        call:        For ``call``: the function and the arguments it was called with
                     (left side; ``right_call`` when the right side's differ).
        variant_id:  The amplified variant the test is, if any.
    """

    test_id: str
    kind: str
    left: str
    right: str
    call: str = ""
    right_call: str = ""
    variant_id: str | None = None

    def to_json(self) -> dict:
        return {
            "test_id": self.test_id,
            "kind": self.kind,
            "left": self.left,
            "right": self.right,
            "call": self.call,
            "right_call": self.right_call,
            "variant_id": self.variant_id,
        }


def common_focus(observations: list[Observation]) -> frozenset[str]:
    """Focus functions defined in every observed version."""
    observed = [observation for observation in observations if observation.observed]
    if not observed:
        return frozenset()
    common = set(observed[0].focus_ids)
    for observation in observed[1:]:
        common &= set(observation.focus_ids)
    return frozenset(common)


def call_signature(call: dict) -> tuple:
    return (
        call.get("fn"),
        tuple(tuple(pair) for pair in call.get("args") or []),
        call.get("result"),
        call.get("raised"),
        tuple(tuple(pair) for pair in call.get("after") or []),
    )


def signature_of(record: RecordedTest | None, focus: frozenset[str]) -> tuple:
    if record is None:
        return (MISSING,)
    calls = tuple(
        call_signature(call) for call in record.calls if call.get("fn") in focus
    )
    return (record.outcome, record.detail, calls, record.dropped_calls)


def unstable_tests(
    first: Observation, second: Observation, focus: frozenset[str]
) -> list[str]:
    """Tests whose signatures differ between two runs of the same version."""
    test_ids = list(dict.fromkeys([*first.tests, *second.tests]))
    return [
        test_id
        for test_id in test_ids
        if signature_of(first.tests.get(test_id), focus)
        != signature_of(second.tests.get(test_id), focus)
    ]


def compared_tests(observations: list[Observation], unstable: set[str]) -> list[str]:
    """Every test seen in an observed version, minus the unstable ones, in order."""
    ordered: dict[str, None] = {}
    for observation in observations:
        if observation.observed:
            ordered.update(dict.fromkeys(observation.tests))
    return [test_id for test_id in ordered if test_id not in unstable]


def diverge(
    left: Observation,
    right: Observation,
    test_ids: list[str],
    focus: frozenset[str],
    variants: dict[str, Variant],
) -> list[Divergence]:
    """The first difference on every test where ``left`` and ``right`` disagree."""
    divergences: list[Divergence] = []
    for test_id in test_ids:
        left_record = left.tests.get(test_id)
        right_record = right.tests.get(test_id)
        if signature_of(left_record, focus) == signature_of(right_record, focus):
            continue
        variant = variant_of_test_id(test_id, variants)
        divergences.append(
            _first_difference(
                test_id,
                left_record,
                right_record,
                focus,
                variant.variant_id if variant else None,
            )
        )
    return divergences


def _first_difference(
    test_id: str,
    left: RecordedTest | None,
    right: RecordedTest | None,
    focus: frozenset[str],
    variant_id: str | None,
) -> Divergence:
    if left is None or right is None:
        return Divergence(
            test_id=test_id,
            kind=MISSING,
            left=describe_outcome(left),
            right=describe_outcome(right),
            variant_id=variant_id,
        )
    if (left.outcome, left.detail) != (right.outcome, right.detail):
        return Divergence(
            test_id=test_id,
            kind="outcome",
            left=describe_outcome(left),
            right=describe_outcome(right),
            variant_id=variant_id,
        )
    left_calls = [call for call in left.calls if call.get("fn") in focus]
    right_calls = [call for call in right.calls if call.get("fn") in focus]
    for left_call, right_call in zip(left_calls, right_calls, strict=False):
        if call_signature(left_call) == call_signature(right_call):
            continue
        same_arguments = left_call.get("fn") == right_call.get("fn") and left_call.get(
            "args"
        ) == right_call.get("args")
        return Divergence(
            test_id=test_id,
            kind="call",
            left=describe_call_result(left_call),
            right=describe_call_result(right_call),
            call=describe_call(left_call),
            right_call="" if same_arguments else describe_call(right_call),
            variant_id=variant_id,
        )
    return Divergence(
        test_id=test_id,
        kind="call_count",
        left=f"{len(left_calls) + left.dropped_calls} focus call(s)",
        right=f"{len(right_calls) + right.dropped_calls} focus call(s)",
        variant_id=variant_id,
    )


def describe_outcome(record: RecordedTest | None) -> str:
    if record is None:
        return "not run"
    return f"{record.outcome}: {record.detail}" if record.detail else record.outcome


def describe_call(call: dict) -> str:
    function = str(call.get("fn", "?")).split("::", 1)[-1]
    arguments = ", ".join(f"{name}={value}" for name, value in call.get("args") or [])
    return f"{function}({arguments})"


def describe_call_result(call: dict) -> str:
    if call.get("raised") is not None:
        text = f"raised {call['raised']}"
    else:
        text = f"returned {call.get('result')}"
    after = call.get("after") or []
    if after:
        text += "; after: " + ", ".join(f"{name}={value}" for name, value in after)
    return text


def behaviour_classes(
    observations: list[Observation], test_ids: list[str], focus: frozenset[str]
) -> list[list[str]]:
    """Group observed versions with identical signatures on every compared test."""
    classes: dict[tuple, list[str]] = {}
    for observation in observations:
        if not observation.observed:
            continue
        signature = tuple(
            signature_of(observation.tests.get(test_id), focus) for test_id in test_ids
        )
        classes.setdefault(signature, []).append(observation.label)
    return list(classes.values())
