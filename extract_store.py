"""Read and write per-SCM extract directories.

Each SCM writes into a directory named for its mnemonic:

    gh-main/
        repos.json          the extracted repos and their members
        extract_meta.json   status, timings, counts, partial flag, errors

The meta file is the contract between the extract phase and everything
downstream. Provision and reconcile MUST NOT run against an extract that
is missing, still running, failed, or partial — under the strict-mirror
reconciliation rule, "this SCM returned no members" and "this SCM was never
read" produce identical input, and the second one would silently remove
everyone. Only the meta file distinguishes them.

Statuses:

    running     started, not finished (a crashed run leaves this behind)
    complete    finished, saw every repo the token can list
    partial     finished, but deliberately truncated (--limit / --repo /
                --changed-since), so absence of a repo proves nothing
    failed      aborted with an error; whatever is in repos.json is suspect

`partial` is a first-class status rather than a warning because it is the
normal result of a testing run. Making it a status means the dangerous
downstream stages refuse it by default instead of relying on an operator
noticing a log line.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPOS_FILE = "repos.json"
META_FILE = "extract_meta.json"

SCHEMA_VERSION = 1

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

# Statuses a provision/reconcile run may consume without --force.
USABLE_STATUSES = (STATUS_COMPLETE,)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ExtractMeta:
    """What a run recorded about itself."""

    mnemonic: str
    scm_type: str
    scm_url: str
    status: str = STATUS_RUNNING
    schema: int = SCHEMA_VERSION
    started_at: str = field(default_factory=_utc_now)
    finished_at: str | None = None
    repos_listed: int = 0        # repos the token could see
    repos_written: int = 0       # repos actually written to repos.json
    members_total: int = 0
    members_with_email: int = 0
    partial: bool = False
    partial_reason: str | None = None
    limit: int | None = None
    errors: list = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """Whether a provision/reconcile run may consume this extract."""
        return self.status in USABLE_STATUSES and not self.partial

    def describe_problem(self) -> str | None:
        """One line explaining why this extract is not usable, or None."""
        if self.status == STATUS_RUNNING:
            return (f"{self.mnemonic}: extract still marked 'running' "
                    f"(started {self.started_at}) — a previous run probably "
                    f"crashed; re-run the extract")
        if self.status == STATUS_FAILED:
            first = f" ({self.errors[0]})" if self.errors else ""
            return f"{self.mnemonic}: last extract FAILED{first} — re-run the extract"
        if self.partial or self.status == STATUS_PARTIAL:
            reason = self.partial_reason or "truncated"
            return (f"{self.mnemonic}: extract is PARTIAL ({reason}) — it saw only "
                    f"{self.repos_written} repo(s), so a missing member proves "
                    f"nothing. Re-run the extract without limits")
        if self.status != STATUS_COMPLETE:
            return f"{self.mnemonic}: unexpected extract status {self.status!r}"
        return None


@dataclass
class RepoRecord:
    """One extracted repo. Members are split by whether we have an email.

    ArmorCode identifies users by email, so a member without one cannot be
    provisioned — but they must still be recorded, both for the exceptions
    CSV and so reconciliation knows they exist rather than treating them as
    absent.
    """

    scm: str
    repo_id: int
    full_name: str
    name: str
    url: str
    members: list = field(default_factory=list)          # [{username,name,email}]
    members_missing_email: list = field(default_factory=list)


class ExtractStore:
    """One SCM's extract directory."""

    def __init__(self, base_dir: Path | str, mnemonic: str):
        self.mnemonic = mnemonic
        self.dir = Path(base_dir) / mnemonic if base_dir else Path(mnemonic)

    @property
    def repos_path(self) -> Path:
        return self.dir / REPOS_FILE

    @property
    def meta_path(self) -> Path:
        return self.dir / META_FILE

    def exists(self) -> bool:
        return self.meta_path.exists()

    # -- writing ----------------------------------------------------------

    def begin(self, scm_type: str, scm_url: str, limit: int | None,
              partial_reason: str | None) -> ExtractMeta:
        """Create the directory and write a 'running' meta file.

        Written BEFORE any repos are read, so a crashed run leaves evidence
        that it started. Without this an interrupted extract is
        indistinguishable from one that never ran, and downstream would
        happily consume a truncated repos.json from the previous week.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        meta = ExtractMeta(
            mnemonic=self.mnemonic, scm_type=scm_type, scm_url=scm_url,
            status=STATUS_RUNNING, limit=limit,
            partial=bool(partial_reason), partial_reason=partial_reason,
        )
        self._write_meta(meta)
        return meta

    def write(self, meta: ExtractMeta, repos: list) -> None:
        """Write repos.json and finalise the meta file.

        repos.json is written first: if the process dies between the two,
        the meta stays 'running' and the extract is correctly refused, which
        is the safe direction. The reverse order could leave a 'complete'
        meta pointing at a stale or missing repos.json.
        """
        self._write_json(self.repos_path, [asdict(r) if isinstance(r, RepoRecord)
                                           else r for r in repos])
        meta.repos_written = len(repos)
        meta.members_total = sum(
            len(r.members) + len(r.members_missing_email)
            if isinstance(r, RepoRecord)
            else len(r.get("members", [])) + len(r.get("members_missing_email", []))
            for r in repos
        )
        meta.members_with_email = sum(
            len(r.members) if isinstance(r, RepoRecord) else len(r.get("members", []))
            for r in repos
        )
        meta.finished_at = _utc_now()
        if meta.status == STATUS_RUNNING:
            meta.status = STATUS_PARTIAL if meta.partial else STATUS_COMPLETE
        self._write_meta(meta)

    def fail(self, meta: ExtractMeta, error: str) -> None:
        """Mark the extract failed, preserving whatever was collected."""
        meta.status = STATUS_FAILED
        meta.finished_at = _utc_now()
        meta.errors.append(str(error)[:500])
        self._write_meta(meta)

    def _write_meta(self, meta: ExtractMeta) -> None:
        self._write_json(self.meta_path, asdict(meta))

    @staticmethod
    def _write_json(path: Path, obj) -> None:
        """Atomic write: temp file in the same directory, then rename.

        A half-written repos.json on a full disk or a kill would otherwise
        be unparseable, and the meta file might still say 'complete'.
        rename() within a directory is atomic on POSIX, so a reader sees
        either the old file or the new one.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(obj, f, indent=2, sort_keys=True, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # -- reading ----------------------------------------------------------

    def load_meta(self) -> ExtractMeta | None:
        """Read the meta file, or None if this SCM has never been extracted."""
        if not self.meta_path.exists():
            return None
        try:
            raw = json.loads(self.meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        known = {f for f in ExtractMeta.__dataclass_fields__}
        return ExtractMeta(**{k: v for k, v in raw.items() if k in known})

    def load_repos(self) -> list[dict]:
        """Read repos.json. Returns [] if absent or unreadable."""
        if not self.repos_path.exists():
            return []
        try:
            data = json.loads(self.repos_path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []


def check_all_usable(base_dir, mnemonics: list[str]) -> tuple[bool, list[str]]:
    """Verify every configured SCM has a usable extract.

    This is the precondition for provision and reconcile. Returns
    (ok, problems) where problems are operator-readable lines.

    Every SCM is checked and every problem collected before returning,
    rather than failing on the first: an operator fixing a broken pipeline
    wants the whole list, not one item per run.
    """
    problems: list[str] = []
    for mnemonic in mnemonics:
        store = ExtractStore(base_dir, mnemonic)
        meta = store.load_meta()
        if meta is None:
            problems.append(
                f"{mnemonic}: no extract found at {store.meta_path} — "
                f"run: python extract.py --scm {mnemonic}"
            )
            continue
        problem = meta.describe_problem()
        if problem:
            problems.append(problem)
    return (not problems), problems


def load_all_repos(base_dir, mnemonics: list[str]) -> list[dict]:
    """Concatenate every SCM's repos into one list for aggregation."""
    out: list[dict] = []
    for mnemonic in mnemonics:
        out.extend(ExtractStore(base_dir, mnemonic).load_repos())
    return out
