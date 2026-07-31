"""
Shared "no resolvable email" exception log for gitlab_team_sync.py and
github_team_sync.py.

When a repo contributor has no email visible via the SCM API (GitLab: no
public email; GitHub: no public email), they can't be provisioned into
ArmorCode — ArmorCode identifies users by email. Rather than just print a
warning and forget it, every such contributor is appended as a row to a CSV
so an admin has a durable, actionable list: they can track down the real
email out-of-band (ask the person, check an internal directory, etc.), fill
it into the `email` column, and re-run with --reprocess-from-exceptions to
provision everyone who now has an email filled in — without needing a fresh
SCM API pull.

CSV columns: source,repo,team,username,name,email,first_seen,status

- source: "gitlab" or "github"
- repo: repo/project full name as seen by the source
- team: the armorcode-team topic value that repo was tagged with
- username: SCM username (stable identifier — used for de-duplication)
- name: display name, if known
- email: blank until an admin fills it in; once filled, --reprocess-from-exceptions
  will pick the row up
- first_seen: date the row was first written (informational only)
- status: "pending" (email still blank) or "reprocessed" (already provisioned
  via --reprocess-from-exceptions, so it won't be re-offered)
"""

from __future__ import annotations

import csv
from pathlib import Path

CSV_COLUMNS = ["source", "repo", "team", "username", "name", "email", "first_seen", "status"]


def log_exceptions(csv_path: str, source: str, repo: str, team: str,
                    members_missing_email: list[dict], today: str) -> None:
    """Append one row per contributor with no resolvable email.

    De-duplicates against existing rows on (source, repo, team, username) —
    re-running the sync repeatedly won't pile up duplicate rows for the same
    unresolved person on the same repo/team.
    """
    if not members_missing_email:
        return

    existing = _read_all(csv_path)
    existing_keys = {(r["source"], r["repo"], r["team"], r["username"]) for r in existing}

    new_rows = []
    for m in members_missing_email:
        key = (source, repo, team, m["username"])
        if key in existing_keys:
            continue
        new_rows.append({
            "source": source,
            "repo": repo,
            "team": team,
            "username": m["username"],
            "name": m.get("name") or "",
            "email": "",
            "first_seen": today,
            "status": "pending",
        })
        existing_keys.add(key)

    if not new_rows:
        return

    all_rows = existing + new_rows
    _write_all(csv_path, all_rows)
    print(f"    [exception-log] {len(new_rows)} unresolved-email contributor(s) "
          f"logged to {csv_path}")


def load_reprocess_candidates(csv_path: str) -> list[dict]:
    """Return pending rows that now have an email filled in.

    Callers should provision each returned row's user/team-membership, then
    call mark_reprocessed() with the rows that succeeded so they aren't
    offered again next time.
    """
    rows = _read_all(csv_path)
    return [r for r in rows if r["status"] == "pending" and r.get("email", "").strip()]


def mark_reprocessed(csv_path: str, reprocessed_rows: list[dict]) -> None:
    """Mark the given rows (by source,repo,team,username) as reprocessed."""
    if not reprocessed_rows:
        return
    done_keys = {(r["source"], r["repo"], r["team"], r["username"]) for r in reprocessed_rows}
    rows = _read_all(csv_path)
    for r in rows:
        key = (r["source"], r["repo"], r["team"], r["username"])
        if key in done_keys:
            r["status"] = "reprocessed"
    _write_all(csv_path, rows)


def _read_all(csv_path: str) -> list[dict]:
    p = Path(csv_path)
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def _write_all(csv_path: str, rows: list[dict]) -> None:
    p = Path(csv_path)
    with p.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({col: r.get(col, "") for col in CSV_COLUMNS})
