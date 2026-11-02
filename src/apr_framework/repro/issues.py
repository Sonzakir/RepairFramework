"""Issue intake: find, fetch, cache and leak-screen the bug report.

Only pointers are shipped: ``data/bugsinpy_issues.json`` maps ``project:bug`` to
the GitHub issue URL (from SOAR-Lab's BugsInPy issue links). The title and body are
fetched at run time and cached under ``.workspace/repro_issues/``, which git
ignores, so no issue text is redistributed. Comments are never fetched.

The leak screen removes what could reveal the fix to the generator: links to
commits, pull requests, compare views and ``.diff``/``.patch`` files, and diff
hunks pasted into the body. An issue whose body contained a diff or the fix commit
id is flagged; the flag is reported next to every result for that bug.
"""

import json
import os
import re
import urllib.error
import urllib.request
from importlib import resources
from pathlib import Path

from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import BugIdentifier
from apr_framework.repro.models import ReproIssue

ISSUE_CACHE_DIR = Path(".workspace") / "repro_issues"
GITHUB_API_ROOT = "https://api.github.com"
_GITHUB_ISSUE_URL = re.compile(
    r"^https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/(?P<number>\d+)/?$"
)
# Links that point at the change itself rather than the problem.
_FIX_REVEALING_LINK = re.compile(
    r"https?://\S*?(?:/commit/|/commits/|/pull/|/compare/|\.diff\b|\.patch\b)\S*",
    re.IGNORECASE,
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCED_BLOCK = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_DIFF_LINE = re.compile(
    r"^(?:diff --git |@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@|--- a/|\+\+\+ b/)"
)
_COMMIT_ID = re.compile(r"\b[0-9a-f]{40}\b")
_REQUEST_TIMEOUT_SECONDS = 30


def load_bugsinpy_issue_links() -> dict[str, str]:
    """Return the shipped ``{"project:bug": issue URL}`` map."""
    data = resources.files("apr_framework.repro").joinpath("data/bugsinpy_issues.json")
    return json.loads(data.read_text(encoding="utf-8"))


def find_bugsinpy_issue_url(bug: BugIdentifier) -> str:
    """Return the issue URL for a BugsInPy bug, or raise if it has none."""
    links = {key.lower(): url for key, url in load_bugsinpy_issue_links().items()}
    url = links.get(f"{bug.project}:{bug.bug_id}".lower())
    if url is None:
        raise ConfigurationError(
            f"No linked GitHub issue for {bug.project}:{bug.bug_id}. Strict Repro "
            "covers the pytest-based BugsInPy bugs with an issue link "
            "(src/apr_framework/repro/data/bugsinpy_issues.json)."
        )
    return url


def fetch_and_screen_issue(
    url: str, project_root: Path, fix_commit_id: str = ""
) -> ReproIssue:
    """Fetch a GitHub issue (cached) and pass it through the leak screen."""
    title, body = fetch_github_issue_cached(url, project_root)
    return screen_issue_for_leaks(url, title, body, fix_commit_id)


def read_and_screen_issue_file(path: Path) -> ReproIssue:
    """Read a local issue file (first non-empty line is the title) and screen it."""
    text = path.read_text(encoding="utf-8")
    lines = text.strip().splitlines()
    if not lines:
        raise ConfigurationError(f"Issue file {path} is empty.")
    title = lines[0].lstrip("#").strip()
    body = "\n".join(lines[1:]).strip()
    return screen_issue_for_leaks(str(path), title, body)


def fetch_github_issue_cached(url: str, project_root: Path) -> tuple[str, str]:
    """Return (title, body) of a GitHub issue, from the cache when present."""
    owner, repo, number = parse_github_issue_url(url)
    cache_path = (
        project_root / ISSUE_CACHE_DIR / f"{owner}__{repo}__{number}.json".lower()
    )
    if cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        return cached["title"], cached["body"]
    payload = request_github_issue(owner, repo, number)
    if "pull_request" in payload:
        raise ConfigurationError(f"{url} is a pull request, not an issue.")
    title, body = payload.get("title") or "", payload.get("body") or ""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"url": url, "title": title, "body": body}, indent=1) + "\n",
        encoding="utf-8",
    )
    return title, body


def parse_github_issue_url(url: str) -> tuple[str, str, str]:
    match = _GITHUB_ISSUE_URL.match(url.strip())
    if match is None:
        raise ConfigurationError(
            f"Not a GitHub issue URL: {url!r} (expected https://github.com/OWNER/REPO/issues/N)"
        )
    return match.group("owner"), match.group("repo"), match.group("number")


def request_github_issue(owner: str, repo: str, number: str) -> dict:
    """GET the issue from the GitHub API; ``GITHUB_TOKEN`` is used when set."""
    request = urllib.request.Request(
        f"{GITHUB_API_ROOT}/repos/{owner}/{repo}/issues/{number}",
        headers=build_github_headers(),
    )
    try:
        with urllib.request.urlopen(
            request, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise ConfigurationError(
            f"GitHub returned HTTP {error.code} for {owner}/{repo}#{number}"
            + (" (rate limit? set GITHUB_TOKEN)" if error.code in (403, 429) else "")
        ) from error
    except urllib.error.URLError as error:
        raise ConfigurationError(
            f"Could not reach GitHub for {owner}/{repo}#{number}: {error.reason}"
        ) from error


def build_github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "apr-framework-strict-repro",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def screen_issue_for_leaks(
    url: str, title: str, body: str, fix_commit_id: str = ""
) -> ReproIssue:
    """Strip fix-revealing links and pasted diffs; flag what looked like a leak."""
    removed: list[str] = []
    flags: list[str] = []
    screened_body = _HTML_COMMENT.sub("", body.replace("\r\n", "\n"))
    screened_body = strip_diff_blocks(screened_body, removed, flags)
    screened_body = strip_fix_revealing_links(screened_body, removed)
    if fix_commit_id and fix_commit_id[:7].lower() in (title + body).lower():
        flags.append("the issue mentions the fix commit id")
    screened_body = strip_commit_ids(screened_body, removed)
    return ReproIssue(
        url=url,
        title=title.strip(),
        body=screened_body.strip(),
        leak_flags=tuple(flags),
        removed=tuple(removed),
    )


def strip_diff_blocks(body: str, removed: list[str], flags: list[str]) -> str:
    """Remove fenced blocks that hold a diff, and loose diff header/hunk lines."""
    removed_before = len(removed)

    def drop_diff_fence(match: re.Match) -> str:
        block = match.group(0)
        if any(_DIFF_LINE.match(line) for line in block.splitlines()):
            removed.append("a fenced diff block")
            return "[diff removed by the leak screen]"
        return block

    without_fenced_diffs = _FENCED_BLOCK.sub(drop_diff_fence, body)
    kept_lines = []
    for line in without_fenced_diffs.splitlines():
        if _DIFF_LINE.match(line):
            removed.append("a diff line")
            continue
        kept_lines.append(line)
    if len(removed) > removed_before:
        flags.append("the issue body contained a diff")
    return "\n".join(kept_lines)


def strip_fix_revealing_links(body: str, removed: list[str]) -> str:
    def drop_link(match: re.Match) -> str:
        removed.append(f"link {match.group(0)[:80]}")
        return "[link removed by the leak screen]"

    return _FIX_REVEALING_LINK.sub(drop_link, body)


def strip_commit_ids(body: str, removed: list[str]) -> str:
    def drop_commit_id(match: re.Match) -> str:
        removed.append(f"commit id {match.group(0)[:12]}")
        return "[commit id removed]"

    return _COMMIT_ID.sub(drop_commit_id, body)


def read_fix_commit_id(worktree: Path) -> str:
    """The fixed commit id from a BugsInPy checkout's ``bugsinpy_bug.info``."""
    bug_info = worktree / "bugsinpy_bug.info"
    if not bug_info.is_file():
        return ""
    match = re.search(r'fixed_commit_id="([0-9a-f]+)"', bug_info.read_text("utf-8"))
    return match.group(1) if match else ""
