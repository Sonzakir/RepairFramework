"""Terminal report for Tamper Check."""

from apr_framework.tamper.models import SEVERITY_ORDER, TamperReport

_SHOWN_PER_SEVERITY = {
    "weakening": 50,
    "suspicious": 50,
    "strengthening": 12,
    "neutral": 8,
}


def format_report(report: TamperReport, *, title: str) -> str:
    counts = report.counts()
    lines = [
        f"Tamper Check · {title}",
        f"Verdict: {report.verdict.upper()}  ("
        + ", ".join(f"{counts[severity]} {severity}" for severity in SEVERITY_ORDER)
        + ")",
        f"Changed files: {len(report.changed_files)}; source files outside the tests: "
        + (", ".join(report.source_files_changed) or "none"),
    ]
    for severity in SEVERITY_ORDER:
        findings = [
            finding for finding in report.findings if finding.severity == severity
        ]
        if not findings:
            continue
        lines.append("")
        lines.append(f"{severity.upper()}")
        limit = _SHOWN_PER_SEVERITY[severity]
        for finding in findings[:limit]:
            where = finding.file + (f"::{finding.test}" if finding.test else "")
            where += f" (line {finding.line})" if finding.line else ""
            lines.append(f"  {finding.kind:<26} {where}")
            lines.append(f"    {finding.message}")
            for prefix, text in (("-", finding.before), ("+", finding.after)):
                for text_line in text.splitlines()[:4]:
                    lines.append(f"    {prefix} {text_line}")
        if len(findings) > limit:
            lines.append(f"  ... {len(findings) - limit} more (see the JSON report)")
    if report.dynamic_note:
        lines.append("")
        lines.append(report.dynamic_note)
    if report.dynamic:
        lines.append("")
        lines.append("Fail-before / pass-after (run in the target):")
        for result in report.dynamic:
            original = result["original_test_on_change"]
            lines.append(
                f"  {result['selector']}: with the change {result['with_change']}, "
                f"tests-only {result['tests_only']}"
                + (f", original test on the change {original}" if original else "")
                + f" → {result['verdict']}"
            )
    return "\n".join(lines)
