"""Capture teams and memberships before anything is written.

The circuit breaker in reconcile.py stops the mass removals we can predict.
This covers the ones we can't: a bug, a mistaken --force, an API change, or
a failure mode nobody modelled. Every run that can write takes a snapshot
first, so there is always something to rebuild from.

    snapshots/2026-08-05T02-40-00Z/
        teams.json    id, name, description, scope, members (with role)
        users.json    userId, email, name, tenantRole, teamInfo (with role)
        meta.json     tenant, timestamp, command, counts, consistency check

Two things make this a real backup rather than a comforting file:

  1. PER-TEAM ROLE IS CAPTURED. A user's teamInfo carries a `role` that
     varies per team — a live tenant had one user who is "Read Only" on one
     team and "Developer" on another. Restoring membership without it would
     silently change people's access level, which is a security change
     disguised as a recovery.

  2. MEMBERSHIP IS CAPTURED FROM BOTH DIRECTIONS. The user record's
     teamInfo is what a restore writes back (PUT /user/update/user); the
     team record's members list is an independent view of the same
     relationship. They are compared at snapshot time, and any disagreement
     is recorded in meta.json — a snapshot that isn't self-consistent
     should not be trusted for recovery without a look.

`userId` is the real key on a user record; `id` is present but null.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1

TEAMS_FILE = "teams.json"
USERS_FILE = "users.json"
META_FILE = "meta.json"

# Colons are illegal in filenames on Windows and awkward everywhere, so the
# directory name uses a filesystem-safe variant of ISO 8601.
_DIR_FMT = "%Y-%m-%dT%H-%M-%SZ"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path, obj) -> None:
    """Atomic write: temp file in the same directory, then rename.

    A snapshot half-written when the disk filled would be worse than none at
    all — it would look like a backup and fail at restore time.
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


@dataclass
class Snapshot:
    """A snapshot on disk. Load with load_snapshot()."""

    dir: Path
    meta: dict
    teams: list
    users: list

    @property
    def tenant(self) -> str:
        return self.meta.get("tenant", "")

    @property
    def taken_at(self) -> str:
        return self.meta.get("taken_at", "")

    def memberships(self) -> dict:
        """team_id -> {email: role} as recorded, from the USER side.

        The user side is authoritative for a restore because that's the
        record the write goes to. Emails are lowercased, since that is how
        users are keyed everywhere else.
        """
        out: dict[int, dict] = {}
        for u in self.users:
            email = (u.get("email") or "").strip().lower()
            if not email:
                continue
            for ti in u.get("teamInfo") or []:
                tid = ti.get("teamId")
                if tid is None:
                    continue
                out.setdefault(int(tid), {})[email] = ti.get("role")
        return out

    def team_names(self) -> dict:
        """team_id -> name, for readable output."""
        return {int(t["id"]): t.get("name", "") for t in self.teams
                if t.get("id") is not None}


def _slim_team(detail: dict) -> dict:
    """Keep the fields a restore or an audit actually needs.

    The full team payload carries risk-score caches and nested duplicates of
    the same sub-product; storing all of it would bloat every snapshot for
    no recovery value.
    """
    return {
        "id": detail.get("id"),
        "name": detail.get("name"),
        "description": detail.get("description"),
        "emailAlias": detail.get("emailAlias"),
        "lead": detail.get("lead"),
        "tags": detail.get("tags"),
        "properties": detail.get("properties"),   # the scope, in READ shape
        "members": [
            {
                "user_id": (m.get("user") or {}).get("id"),
                "user_name": (m.get("user") or {}).get("name"),
                "role": (m.get("role") or {}).get("name"),
            }
            for m in detail.get("members") or []
        ],
    }


def _slim_user(u: dict) -> dict:
    """Keep identity plus the full teamInfo, including per-team role."""
    return {
        "userId": u.get("userId") or u.get("id"),
        "email": u.get("email"),
        "name": u.get("name"),
        "tenantRole": u.get("tenantRole"),
        "disableLogin": u.get("disableLogin"),
        "teamInfo": [
            {
                "teamId": ti.get("teamId"),
                "teamName": ti.get("teamName"),
                "role": ti.get("role"),
            }
            for ti in u.get("teamInfo") or []
        ],
    }


def _consistency_check(teams: list, users: list) -> dict:
    """Compare the team-side and user-side views of membership.

    Both come from the same tenant moments apart, so they should agree. A
    mismatch means either a concurrent change during the snapshot or an API
    inconsistency, and either way the operator should know before relying on
    this file to restore access.
    """
    from_users: set = set()
    for u in users:
        uid = u.get("userId")
        for ti in u.get("teamInfo") or []:
            if ti.get("teamId") is not None and uid is not None:
                from_users.add((int(ti["teamId"]), int(uid)))

    from_teams: set = set()
    for t in teams:
        tid = t.get("id")
        for m in t.get("members") or []:
            if m.get("user_id") is not None and tid is not None:
                from_teams.add((int(tid), int(m["user_id"])))

    only_users = from_users - from_teams
    only_teams = from_teams - from_users
    return {
        "memberships_from_users": len(from_users),
        "memberships_from_teams": len(from_teams),
        "only_in_user_records": len(only_users),
        "only_in_team_records": len(only_teams),
        "consistent": not only_users and not only_teams,
    }


def take_snapshot(ac, tenant_url: str, base_dir, command: str,
                  *, verbose: bool = True) -> Snapshot:
    """Capture the tenant's teams, scope and memberships.

    Called before the first write of any --apply run. Cost is one call for
    the user list, one for the team list, and one per team for its scope and
    members — the same team detail calls a provision run would make anyway.
    """
    taken_at = _utc_now()
    snap_dir = Path(base_dir) / taken_at.strftime(_DIR_FMT)

    if verbose:
        print(f"\n[snapshot] capturing teams and memberships -> {snap_dir}/")

    users = [_slim_user(u) for u in ac.get_users()]

    team_list = ac.get_teams()
    teams = []
    failures = []
    for entry in team_list:
        tid = entry.get("id")
        if tid is None:
            continue
        try:
            teams.append(_slim_team(ac.get_team(tid)))
        except Exception as e:
            # A team that can't be read is a hole in the backup. Recorded
            # rather than swallowed, so a restore doesn't quietly skip it.
            failures.append({"team_id": tid, "name": entry.get("name"),
                             "error": str(e)[:200]})

    check = _consistency_check(teams, users)

    meta = {
        "schema": SCHEMA_VERSION,
        "tenant": tenant_url,
        "taken_at": taken_at.isoformat(timespec="seconds"),
        "command": command,
        "teams": len(teams),
        "users": len(users),
        "teams_unreadable": failures,
        "consistency": check,
    }

    _write_json(snap_dir / TEAMS_FILE, teams)
    _write_json(snap_dir / USERS_FILE, users)
    # meta last: its presence is the marker that the snapshot is complete.
    _write_json(snap_dir / META_FILE, meta)

    if verbose:
        print(f"[snapshot] {len(teams)} team(s), {len(users)} user(s), "
              f"{check['memberships_from_users']} membership(s)")
        if failures:
            print(f"[snapshot] [warn] {len(failures)} team(s) could not be read "
                  f"and are NOT in this snapshot:")
            for f in failures:
                print(f"             {f['name']!r} (id={f['team_id']}): {f['error']}")
        if not check["consistent"]:
            print(f"[snapshot] [warn] team-side and user-side membership disagree "
                  f"({check['only_in_user_records']} only on users, "
                  f"{check['only_in_team_records']} only on teams). Probably a "
                  f"concurrent change; review before restoring from this one.")

    return Snapshot(dir=snap_dir, meta=meta, teams=teams, users=users)


def load_snapshot(path) -> Snapshot:
    """Read a snapshot directory. Raises ValueError if it isn't usable."""
    d = Path(path)
    meta_path = d / META_FILE
    if not meta_path.exists():
        raise ValueError(
            f"{d} is not a complete snapshot (no {META_FILE}) — it may be from "
            f"a run that died mid-capture"
        )
    try:
        meta = json.loads(meta_path.read_text())
        teams = json.loads((d / TEAMS_FILE).read_text())
        users = json.loads((d / USERS_FILE).read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"could not read snapshot {d}: {e}") from None

    if meta.get("schema") != SCHEMA_VERSION:
        raise ValueError(
            f"snapshot {d} has schema {meta.get('schema')!r}, expected "
            f"{SCHEMA_VERSION} — written by a different version of this tool"
        )
    return Snapshot(dir=d, meta=meta, teams=teams, users=users)


def list_snapshots(base_dir) -> list[Path]:
    """Snapshot directories, newest first. Incomplete ones are skipped."""
    base = Path(base_dir)
    if not base.exists():
        return []
    dirs = [d for d in base.iterdir() if d.is_dir() and (d / META_FILE).exists()]
    return sorted(dirs, key=lambda d: d.name, reverse=True)


def latest_snapshot(base_dir) -> Path | None:
    snaps = list_snapshots(base_dir)
    return snaps[0] if snaps else None


def prune(base_dir, retention_days: int, *, verbose: bool = True) -> int:
    """Delete snapshots older than retention_days. Returns how many went.

    retention_days = 0 disables pruning, so an operator who wants to keep
    everything doesn't have to pick a large number and hope.

    The newest snapshot is never pruned regardless of age: on a tenant that
    is provisioned rarely, expiring the only backup would leave nothing to
    restore from.
    """
    if retention_days <= 0:
        return 0
    snaps = list_snapshots(base_dir)
    if len(snaps) <= 1:
        return 0

    cutoff = _utc_now() - timedelta(days=retention_days)
    removed = 0
    for d in snaps[1:]:                     # keep the newest unconditionally
        try:
            when = datetime.strptime(d.name, _DIR_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue                        # not one of ours; leave it alone
        if when < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed and verbose:
        print(f"[snapshot] pruned {removed} snapshot(s) older than "
              f"{retention_days} day(s)")
    return removed
