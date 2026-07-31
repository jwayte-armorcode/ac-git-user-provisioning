"""Durable per-repo spool for the collect phase.

Why this exists
---------------
The collect phase reads every repo from an SCM and holds the result in
memory until the apply phase provisions it. On a 100,000-repo run that is
hours of reading, and if the process dies the work is gone.

A "last completed repo id" checkpoint alone is NOT enough, and is in fact
dangerous here: it records how far the READ got, but the collected data
lived only in memory. Resuming from it would skip those repos and then
provision from the small remainder — silently dropping every team and
member found before the crash, while looking like a successful run.

So the collected data itself is spooled to CSV, one row per repo, appended
and flushed as each repo is read. The spool is both the durable record and
the resume position: on restart the run reloads every row, skips those
repos, and continues from the highest id present.

Format (one row per repo that carries at least one armorcode-team topic):

    repo_id,repo,repo_name,teams,members,members_missing_email,sub_product_ids

  teams                 ; separated team names
  members               ; separated  name|email  pairs
  members_missing_email ; separated  name|username  pairs
  sub_product_ids       ; separated ArmorCode sub-product ids

Repos with no team topic are not spooled as data — there is nothing to
provision — but their id still advances the resume position, recorded in a
single trailing "progress" row so a long stretch of untagged repos isn't
re-read after a crash.

Chose CSV over JSON deliberately: a JSON array cannot be appended to
without rewriting the file, and a partially-written array is unparseable.
A CSV can be appended one line at a time, and a truncated final line from
a hard kill is detectable and skippable — the run simply re-reads that one
repo, which is safe because collect is idempotent.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

COLUMNS = [
    "repo_id", "repo", "repo_name", "teams",
    "members", "members_missing_email", "sub_product_ids",
]

# Marks the "no team topic, but read this far" rows.
PROGRESS_MARKER = "__progress__"


def _pack_members(members: list[dict]) -> str:
    """name|email pairs, ; separated. Both are free text, so strip the two
    delimiters rather than risk producing an unparseable row."""
    out = []
    for m in members:
        name = (m.get("name") or "").replace(";", ",").replace("|", "/")
        email = (m.get("email") or "").replace(";", ",").replace("|", "/")
        out.append(f"{name}|{email}")
    return ";".join(out)


def _unpack_members(raw: str) -> list[dict]:
    out = []
    for chunk in (raw or "").split(";"):
        if not chunk:
            continue
        name, _, email = chunk.partition("|")
        out.append({"name": name, "email": email})
    return out


def _pack_missing(members: list[dict]) -> str:
    out = []
    for m in members:
        name = (m.get("name") or "").replace(";", ",").replace("|", "/")
        username = (m.get("username") or "").replace(";", ",").replace("|", "/")
        out.append(f"{name}|{username}")
    return ";".join(out)


def _unpack_missing(raw: str) -> list[dict]:
    out = []
    for chunk in (raw or "").split(";"):
        if not chunk:
            continue
        name, _, username = chunk.partition("|")
        out.append({"name": name, "username": username, "email": None})
    return out


class RepoSpool:
    """Append-only CSV of collected repos, for one source.

    Open it, call load() to recover anything a previous run wrote, then
    append() per repo as they're read.
    """

    def __init__(self, path: str, source: str):
        self.path = Path(path)
        self.source = source
        self._fh = None
        self._writer = None

    # -- reading (resume) --------------------------------------------------

    def load(self) -> tuple[dict, int | None, int]:
        """Reload a previous run's rows.

        Returns (repos, after_id, data_rows) where repos is the same
        "<source>-<full_name>" -> entry mapping the collect phase builds,
        after_id is the highest repo id seen (the resume position, or None
        for a fresh run), and data_rows counts real repo rows.

        A truncated final line from a hard kill is skipped: csv will yield a
        short row, and any row missing repo_id is ignored. That repo gets
        re-read on resume, which is safe — collect is idempotent.
        """
        repos: dict = {}
        max_id: int | None = None
        data_rows = 0

        if not self.path.exists():
            return repos, None, 0

        with self.path.open(newline="") as f:
            for row in csv.DictReader(f):
                raw_id = (row.get("repo_id") or "").strip()
                if not raw_id.isdigit():
                    continue  # header echo or corrupt line

                # A hard kill can leave a partial final line. csv still parses
                # it, filling the absent trailing columns with None — which
                # would restore a repo with silently empty teams/members AND
                # advance the resume position past it, so it would never be
                # re-read. Require every column to be present before trusting
                # the row; an incomplete one is dropped so that repo is simply
                # collected again (collect is idempotent).
                if any(row.get(col) is None for col in COLUMNS):
                    continue

                rid = int(raw_id)
                max_id = rid if max_id is None else max(max_id, rid)

                if row.get("repo") == PROGRESS_MARKER:
                    continue  # position-only row, no data to restore

                # Ids must come back as ints. build_scope_entries() and the
                # scope merge compare them numerically, so a string id from
                # the spool would not match one read live from the tenant.
                sub_ids = [int(s) for s in (row.get("sub_product_ids") or "").split(";") if s]
                repos[f"{self.source}-{row['repo']}"] = {
                    "source": self.source,
                    "repo": row["repo"],
                    "repo_name": row["repo_name"],
                    "teams": [t for t in (row.get("teams") or "").split(";") if t],
                    "members": _unpack_members(row.get("members", "")),
                    "members_missing_email": _unpack_missing(
                        row.get("members_missing_email", "")),
                    "sub_product_ids": sub_ids,
                }
                data_rows += 1

        return repos, max_id, data_rows

    # -- writing ----------------------------------------------------------

    def open_append(self) -> None:
        """Open for appending, writing the header only on a new file."""
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        self._fh = self.path.open("a", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=COLUMNS)
        if is_new:
            self._writer.writeheader()
            self._flush()

    def _flush(self) -> None:
        """Flush to the OS and fsync, so a hard kill (or a killed container)
        can't lose rows sitting in a buffer. Without the fsync the whole
        point of spooling is defeated on an abrupt termination."""
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def append(self, repo_id: int, entry: dict) -> None:
        """Spool one collected repo and flush immediately."""
        self._writer.writerow({
            "repo_id": repo_id,
            "repo": entry["repo"],
            "repo_name": entry["repo_name"],
            "teams": ";".join(entry["teams"]),
            "members": _pack_members(entry["members"]),
            "members_missing_email": _pack_missing(entry["members_missing_email"]),
            "sub_product_ids": ";".join(str(s) for s in entry["sub_product_ids"]),
        })
        self._flush()

    def append_progress(self, repo_id: int) -> None:
        """Record that a repo with no team topic was read, so a long run of
        untagged repos isn't re-read after a crash. Position only, no data."""
        self._writer.writerow({
            "repo_id": repo_id, "repo": PROGRESS_MARKER, "repo_name": "",
            "teams": "", "members": "", "members_missing_email": "",
            "sub_product_ids": "",
        })
        self._flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None

    def discard(self) -> None:
        """Delete the spool. Called once the apply phase has fully succeeded,
        so the next run starts clean instead of resuming from stale data."""
        self.close()
        if self.path.exists():
            self.path.unlink()

    def __enter__(self):
        self.open_append()
        return self

    def __exit__(self, *exc):
        self.close()
        return False
