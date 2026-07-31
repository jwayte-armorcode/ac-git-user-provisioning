"""
Shared resume-checkpoint mechanism for gitlab_team_sync.py and
github_team_sync.py, for tenants with very large repo counts (tens of
thousands+) where a killed process shouldn't mean starting over.

Design:
  - Before processing, fetch a lightweight list of ALL repos (id + name
    only — cheap, no per-repo detail calls) and sort it by the SCM's own
    numeric id, ascending. Ids are assigned once at repo creation and never
    change, so this order is stable across separate runs even if repos are
    added, removed, or renamed in between — unlike sorting by name/path,
    where a rename or namespace move could shift a repo's position.
  - After each repo finishes processing (successfully or with a logged
    per-repo error — anything short of the whole process dying), write its
    id to the checkpoint file as the new "last completed" position.
  - On restart with --resume, read the checkpoint, skip every repo whose id
    is <= the checkpoint, and continue from there.

The checkpoint is a single small JSON file, written after every repo (not
batched) so a hard kill loses at most the repo that was in flight — that
one will simply be reprocessed on resume, which is safe because the sync
is idempotent (confirmed: re-running is a no-op for anything already done).
"""

from __future__ import annotations

import json
from pathlib import Path


def load_checkpoint(path: str, source: str) -> int | None:
    """Return the last-completed repo id for `source`, or None if no
    checkpoint exists yet (or it's for a different source — a stale
    gitlab checkpoint should never be used to resume a github run)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("source") != source:
        return None
    return data.get("last_completed_id")


def save_checkpoint(path: str, source: str, last_completed_id: int,
                    processed_count: int, total_count: int, timestamp: str) -> None:
    """Overwrite the checkpoint file with the new position.

    Called after every repo, not batched — a hard kill mid-run should lose
    at most the one repo that was in flight, not a whole batch.
    """
    data = {
        "source": source,
        "last_completed_id": last_completed_id,
        "processed_count": processed_count,
        "total_count": total_count,
        "updated_at": timestamp,
    }
    Path(path).write_text(json.dumps(data, indent=2) + "\n")


def clear_checkpoint(path: str) -> None:
    """Remove the checkpoint file — call this after a full run completes
    with no --repo/--rows filter, so a later fresh run doesn't accidentally
    skip repos because a stale checkpoint from a previous full run is still
    sitting there."""
    p = Path(path)
    if p.exists():
        p.unlink()
