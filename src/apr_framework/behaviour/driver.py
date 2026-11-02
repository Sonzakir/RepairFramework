"""Observation driver: runs a project's test suite and records what happened.

This file is copied next to a JSON spec and run with the *target's* interpreter,
from the project checkout:

    python driver.py spec.json

so it must work on Python 3.6 and later, with only the standard library (plus
pytest when the suite uses pytest). It is never imported by the framework.

It runs the suite in-process (``unittest`` or ``pytest``, as the project does) and
records, per test:

- the outcome: passed, failed, error, skipped, xfailed or xpassed, with the
  exception type and the first line of its message;
- when ``record_calls`` is set, every call of a *focus function* (named in the
  spec by file and definition line): the arguments at call time, the return value
  or the exception that propagated out, and the arguments (including ``self``)
  after the call when they changed;
- when ``watch`` lists lines, which of those lines ran (inside focus functions).

Values are rendered so that two runs of the same code agree: memory addresses and
temporary paths are masked, sets are sorted, objects without a ``__repr__`` are
shown by their attributes, and long renderings are cut with a checksum of the
rest. ``PYTHONHASHSEED`` is pinned to 0 by re-executing the interpreter.

The spec's ``output`` file is written even when the suite crashes; a killed
process leaves no output, which the caller reports as a failed observation.
"""

import contextlib
import inspect
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import unittest
import zlib

DRIVER_VERSION = 1
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")
# Objects from these modules carry scheduler, thread and I/O state whose values
# depend on timing; they are shown by their repr only, never by their attributes.
_OPAQUE_MODULES = (
    "threading",
    "asyncio",
    "concurrent",
    "multiprocessing",
    "queue",
    "socket",
    "selectors",
    "ssl",
    "subprocess",
    "logging",
    "weakref",
    "_weakrefset",
    "io",
    "_io",
    "tempfile",
    "unittest.mock",
)
_MISSING = object()
# Generators and coroutines "return" at every yield or await, and how often a
# coroutine suspends can depend on timing (an awaited thread-pool future may or
# may not be done). Only the arguments of their first call are recorded.
_SUSPENDABLE = (
    0x20 | 0x80 | 0x100 | 0x200
)  # GENERATOR, COROUTINE, ITERABLE_COROUTINE, ASYNC_GENERATOR
_OUTCOME_RANK = {
    "passed": 0,
    "xfailed": 0,
    "skipped": 0,
    "xpassed": 1,
    "failed": 2,
    "error": 3,
}


class Renderer:
    """Turns values into short, run-independent strings."""

    def __init__(self, max_length, max_items=12, max_depth=3):
        self.max_length = max_length
        self.max_items = max_items
        self.max_depth = max_depth
        temporary_root = re.escape(tempfile.gettempdir())
        self._temporary_path = re.compile(temporary_root + r"[^\s'\"(),\[\]{}]*")

    def render(self, value):
        try:
            text = self._render(value, self.max_depth)
        except Exception:  # noqa: BLE001 - a user __repr__ may raise anything
            text = "<unrepresentable " + type(value).__name__ + ">"
        return self.clip(self.mask(text))

    def mask(self, text):
        text = _ADDRESS.sub("0x?", text)
        return self._temporary_path.sub("<tmp>", text)

    def clip(self, text):
        if len(text) <= self.max_length:
            return text
        checksum = zlib.crc32(text.encode("utf-8", "surrogatepass")) & 0xFFFFFFFF
        return text[: self.max_length] + f"...<{len(text)} chars, crc {checksum:08x}>"

    def _render(self, value, depth):
        if value is None or isinstance(value, (bool, int, float, complex)):
            return repr(value)
        if isinstance(value, (str, bytes, bytearray)):
            return repr(value)
        if inspect.isroutine(value) or inspect.isclass(value):
            module = getattr(value, "__module__", None) or "?"
            name = getattr(value, "__qualname__", None) or getattr(
                value, "__name__", "?"
            )
            return f"<{type(value).__name__} {module}.{name}>"
        if inspect.ismodule(value):
            return f"<module {value.__name__}>"
        if isinstance(value, BaseException):
            return type(value).__name__ + self._render(tuple(value.args), depth)
        if depth <= 0:
            return "<" + type(value).__name__ + ">"
        value_type = type(value)
        if value_type in (list, tuple):
            items = [self._render(item, depth - 1) for item in value[: self.max_items]]
            if len(value) > self.max_items:
                items.append(f"...+{len(value) - self.max_items}")
            if value_type is tuple:
                return "(" + ", ".join(items) + ("," if len(value) == 1 else "") + ")"
            return "[" + ", ".join(items) + "]"
        if value_type in (set, frozenset):
            items = sorted(self._render(item, depth - 1) for item in value)
            shown = items[: self.max_items]
            if len(items) > self.max_items:
                shown.append(f"...+{len(items) - self.max_items}")
            return value_type.__name__ + "{" + ", ".join(shown) + "}"
        if value_type is dict:
            return self._render_mapping(value, depth)
        return self._render_object(value, depth)

    def _render_mapping(self, mapping, depth):
        items = []
        for index, (key, item) in enumerate(mapping.items()):
            if index >= self.max_items:
                items.append(f"...+{len(mapping) - self.max_items}")
                break
            items.append(
                self._render(key, depth - 1) + ": " + self._render(item, depth - 1)
            )
        return "{" + ", ".join(items) + "}"

    def _render_object(self, value, depth):
        value_type = type(value)
        if value_type.__repr__ is not object.__repr__:
            text = repr(value)
        else:
            text = "<" + value_type.__name__ + ">"
        module = getattr(value_type, "__module__", "") or ""
        if module.split(".")[0] in _OPAQUE_MODULES or module in _OPAQUE_MODULES:
            return text
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, dict) and attributes:
            ordered = dict(sorted(attributes.items(), key=lambda pair: str(pair[0])))
            text += " " + self._render_mapping(ordered, depth - 1)
        return text


class FocusIndex:
    """Recognises the code objects of the focus functions."""

    def __init__(self, focus, working_directory):
        self._by_file = {}
        for entry in focus:
            path = os.path.realpath(os.path.join(working_directory, entry["file"]))
            self._by_file.setdefault(path, []).append(entry)
        self._real_paths = {}
        self._codes = {}

    def identify(self, code):
        """The focus entry for ``code``, or None."""
        found = self._codes.get(code, _MISSING)
        if found is not _MISSING:
            return found
        found = None
        filename = code.co_filename
        real_path = self._real_paths.get(filename)
        if real_path is None:
            real_path = os.path.realpath(filename)
            self._real_paths[filename] = real_path
        for entry in self._by_file.get(real_path, ()):
            if code.co_name == entry["name"] and (
                entry["first"] <= code.co_firstlineno <= entry["def"]
            ):
                found = entry
                break
        self._codes[code] = found
        return found


class Recorder:
    """Collects outcomes, focus calls and watched lines per test."""

    def __init__(self, spec, working_directory):
        self.working_directory = working_directory
        self.record_calls = bool(spec.get("record_calls", True))
        # Without focus functions there is nothing to trace: outcomes only, full speed.
        self.tracing = bool(spec.get("focus"))
        self.max_calls_per_test = int(spec.get("max_calls_per_test", 200))
        self.max_calls_per_function = int(spec.get("max_calls_per_function", 40))
        self.renderer = Renderer(int(spec.get("max_repr", 200)))
        self.focus = FocusIndex(spec.get("focus", []), working_directory)
        self.watch = {}
        for relative_path, lines in (spec.get("watch") or {}).items():
            path = os.path.realpath(os.path.join(working_directory, relative_path))
            self.watch[path] = (relative_path, set(lines))
        self.tests = []
        self._by_id = {}
        self.current = None
        self._calls_per_function = {}
        self._suspendable_frames = {}

    # -- tests -------------------------------------------------------------
    def start_test(self, test_id, file=None, qualname=None, line=None):
        record = self._by_id.get(test_id)
        if record is None:
            record = {
                "id": test_id,
                "outcome": "passed",
                "detail": "",
                "file": file,
                "qualname": qualname,
                "line": line,
                "calls": [],
                "dropped_calls": 0,
                "lines": [],
            }
            self._by_id[test_id] = record
            self.tests.append(record)
        self.current = record
        self._calls_per_function = {}
        self._suspendable_frames = {}
        # A coverage plugin may have replaced the tracer since the last test.
        if self.tracing and sys.gettrace() != self.global_trace:
            sys.settrace(self.global_trace)

    def stop_test(self):
        self.current = None

    def set_outcome(self, test_id, outcome, detail=""):
        record = self._by_id.get(test_id)
        if record is None:
            self.start_test(test_id)
            self.stop_test()
            record = self._by_id[test_id]
        if _OUTCOME_RANK.get(outcome, 0) >= _OUTCOME_RANK.get(record["outcome"], 0):
            record["outcome"] = outcome
            record["detail"] = self.renderer.clip(self.renderer.mask(detail))

    # -- tracing -----------------------------------------------------------
    def global_trace(self, frame, event, arg):
        if event != "call" or self.current is None:
            return None
        entry = self.focus.identify(frame.f_code)
        if entry is None:
            return None
        return self._enter(frame, entry)

    def _enter(self, frame, entry):
        test = self.current
        watched = self.watch.get(os.path.realpath(frame.f_code.co_filename))
        call = None
        suspendable = frame.f_code.co_flags & _SUSPENDABLE
        resumed = suspendable and id(frame) in self._suspendable_frames
        if suspendable:
            # Keep the frame alive so its id is not reused within this test.
            self._suspendable_frames[id(frame)] = frame
        if self.record_calls and not resumed:
            call = self._open_call(frame, entry, test)
            if call is not None and suspendable:
                call.pop("_values", None)
                call["result"] = "<generator or coroutine>"
                call = None
        state = {"pending": None}

        def local_trace(local_frame, event, arg):
            if event == "line":
                state["pending"] = None
                if watched is not None and local_frame.f_lineno in watched[1]:
                    hit = watched[0] + ":" + str(local_frame.f_lineno)
                    if hit not in test["lines"]:
                        test["lines"].append(hit)
            elif event == "exception":
                state["pending"] = arg
            elif event == "return" and call is not None:
                self._close_call(call, state["pending"], arg)
            return local_trace

        return local_trace

    def _open_call(self, frame, entry, test):
        count = self._calls_per_function.get(entry["id"], 0) + 1
        self._calls_per_function[entry["id"]] = count
        if (
            count > self.max_calls_per_function
            or len(test["calls"]) >= self.max_calls_per_test
        ):
            test["dropped_calls"] += 1
            return None
        names = _argument_names(frame.f_code)
        values = [frame.f_locals.get(name, _MISSING) for name in names]
        arguments = [
            [name, self.renderer.render(value)]
            for name, value in zip(names, values)
            if value is not _MISSING
        ]
        call = {"fn": entry["id"], "args": arguments, "result": None, "raised": None}
        call["_values"] = [(name, value) for name, value in zip(names, values)]
        test["calls"].append(call)
        return call

    def _close_call(self, call, pending_exception, return_value):
        values = call.pop("_values", None)
        if values is None:
            return
        if pending_exception is not None:
            exception_type, exception_value = pending_exception[0], pending_exception[1]
            call["raised"] = (
                self.renderer.render(exception_value) or exception_type.__name__
            )
        else:
            call["result"] = self.renderer.render(return_value)
        before = {name: rendered for name, rendered in call["args"]}
        after = []
        for name, value in values:
            if value is _MISSING:
                continue
            rendered = self.renderer.render(value)
            if before.get(name) != rendered:
                after.append([name, rendered])
        if after:
            call["after"] = after

    def to_json(self):
        for test in self.tests:
            for call in test["calls"]:
                call.pop("_values", None)
        return self.tests


def _argument_names(code):
    count = code.co_argcount + getattr(code, "co_kwonlyargcount", 0)
    if code.co_flags & inspect.CO_VARARGS:
        count += 1
    if code.co_flags & inspect.CO_VARKEYWORDS:
        count += 1
    return list(code.co_varnames[:count])


def _describe_exception(exception_type, exception_value):
    message = str(exception_value).strip().splitlines()
    return exception_type.__name__ + (": " + message[0] if message else "")


# -- unittest ---------------------------------------------------------------
def _unittest_location(test, working_directory):
    method_name = getattr(test, "_testMethodName", None)
    function = getattr(type(test), method_name, None) if method_name else None
    if function is None:
        return None, None, None
    with contextlib.suppress(Exception):
        function = inspect.unwrap(function)
    code = getattr(function, "__code__", None)
    if code is None:
        return None, None, None
    qualname = type(test).__qualname__ + "." + method_name
    return (
        os.path.relpath(code.co_filename, working_directory),
        qualname,
        code.co_firstlineno,
    )


def run_unittest(arguments, recorder):
    class RecordingResult(unittest.TextTestResult):
        def startTest(self, test):
            file, qualname, line = _unittest_location(test, recorder.working_directory)
            recorder.start_test(test.id(), file, qualname, line)
            super().startTest(test)

        def stopTest(self, test):
            super().stopTest(test)
            recorder.stop_test()

        def addSuccess(self, test):
            super().addSuccess(test)
            recorder.set_outcome(test.id(), "passed")

        def addFailure(self, test, err):
            super().addFailure(test, err)
            recorder.set_outcome(
                test.id(), "failed", _describe_exception(err[0], err[1])
            )

        def addError(self, test, err):
            super().addError(test, err)
            recorder.set_outcome(
                test.id(), "error", _describe_exception(err[0], err[1])
            )

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            recorder.set_outcome(test.id(), "skipped", str(reason))

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            recorder.set_outcome(test.id(), "xfailed")

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            recorder.set_outcome(test.id(), "xpassed")

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                outcome = (
                    "failed" if issubclass(err[0], test.failureException) else "error"
                )
                recorder.set_outcome(
                    test.id(), outcome, _describe_exception(err[0], err[1])
                )

    runner = unittest.TextTestRunner(
        stream=sys.stderr, verbosity=1, resultclass=RecordingResult
    )
    unittest.main(
        module=None,
        argv=["python -m unittest", *arguments],
        testRunner=runner,
        exit=False,
    )


# -- pytest -----------------------------------------------------------------
class PytestRecorderPlugin:
    def __init__(self, recorder):
        self.recorder = recorder

    def pytest_runtest_logstart(self, nodeid, location):
        file, line, domain = None, None, None
        if location:
            file, line, domain = location[0], location[1], location[2]
        qualname = domain.split("[")[0] if domain else None
        self.recorder.start_test(
            nodeid, file, qualname, None if line is None else line + 1
        )

    def pytest_runtest_logreport(self, report):
        outcome, detail = None, ""
        if hasattr(report, "wasxfail"):
            if report.when == "call":
                outcome = "xfailed" if report.skipped else "xpassed"
        elif report.when == "call":
            outcome = report.outcome
            if report.failed:
                detail = _pytest_crash_message(report)
        elif report.failed:
            outcome, detail = "error", _pytest_crash_message(report)
        elif report.skipped:
            outcome = "skipped"
            longrepr = report.longrepr
            if isinstance(longrepr, tuple) and len(longrepr) == 3:
                detail = str(longrepr[2])
        if outcome is not None:
            self.recorder.set_outcome(report.nodeid, outcome, detail)
        if report.when == "teardown":
            self.recorder.stop_test()

    def pytest_collectreport(self, report):
        if report.failed:
            self.recorder.set_outcome(
                "<collection>::" + report.nodeid, "error", _pytest_crash_message(report)
            )


def _pytest_crash_message(report):
    crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
    message = getattr(crash, "message", None)
    if message:
        return message.strip().splitlines()[0]
    text = str(getattr(report, "longrepr", "") or "").strip().splitlines()
    return text[-1] if text else ""


def run_pytest(arguments, recorder):
    import pytest

    pytest.main(
        [*arguments, "-p", "no:cacheprovider", "-q"],
        plugins=[PytestRecorderPlugin(recorder)],
    )


# -- entry point ------------------------------------------------------------
def main(argv):
    if os.environ.get("PYTHONHASHSEED") != "0" and sys.executable:
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = "0"
        os.execve(sys.executable, [sys.executable, *argv], environment)
    with open(argv[1], encoding="utf-8") as spec_file:
        spec = json.load(spec_file)
    working_directory = os.getcwd()
    # Like `python -m`: the project first on sys.path, never this script's folder.
    script_directory = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [
        entry
        for entry in sys.path
        if os.path.abspath(entry or os.curdir) != script_directory
    ]
    sys.path.insert(0, working_directory)

    recorder = Recorder(spec, working_directory)
    started = time.time()
    error = None
    if recorder.tracing:
        sys.settrace(recorder.global_trace)
        threading.settrace(recorder.global_trace)
    try:
        if spec["runner"] == "pytest":
            run_pytest(spec.get("arguments", []), recorder)
        else:
            run_unittest(spec.get("arguments", []), recorder)
    except BaseException:  # noqa: BLE001 - the output must be written whatever the suite does
        error = traceback.format_exc()
    finally:
        sys.settrace(None)
        threading.settrace(None)
    payload = {
        "driver": DRIVER_VERSION,
        "python": sys.version.split()[0],
        "runner": spec["runner"],
        "seconds": round(time.time() - started, 3),
        "error": error,
        "tests": recorder.to_json(),
    }
    with open(spec["output"], "w", encoding="utf-8") as output_file:
        json.dump(payload, output_file, ensure_ascii=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
