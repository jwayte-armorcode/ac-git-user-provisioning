"""Cross-run cache of what was already provisioned, to skip no-op API calls.

The apply phase is idempotent: re-running it re-derives every team's desired
state and GET-merges, so a second run against an unchanged tenant makes the
same calls and changes nothing. Correct, but on a large tenant that's
thousands of paced calls to confirm nothing happened.

This cache records the desired state of each team and user AFTER it was
successfully provisioned. On the next run, a team whose desired state is
byte-identical to the cached entry is skipped without calling ArmorCode.

  Why this is a FAST PATH and not a source of truth
  -------------------------------------------------
  The cache says "last time we provisioned X, ArmorCode ended up matching
  X". It cannot know whether ArmorCode still matches X: an admin can remove
  a user from a team in the UI, delete a team, or edit scope by hand, and
  none of that touches this file. A skipped team therefore stays drifted
  until something forces a reconcile.

  Three guards keep that bounded:

  1. Entries are written ONLY after that team's or user's API calls actually
     succeeded — never in bulk at the end. A partially failed run cannot
     record work it didn't do.
  2. The file is stamped with the tenant URL and a schema version. A cache
     from another tenant, or from an older layout, is rejected wholesale
     rather than half-applied.
  3. --full ignores the cache and reconciles everything. Run it
     periodically (e.g. weekly) so hand edits and drift are repaired.

Dry runs never write the cache: nothing was provisioned, so there is
nothing to record.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = 2


def _team_fingerprint(info: dict, default_role: str) -> dict:
    """The desired state of one team, reduced to what apply actually writes.

    Deliberately excludes "repos": which repos contributed a team is
    irrelevant to the ArmorCode calls made. Including it would force a
    pointless re-provision every time an untagged repo was renamed or a
    second repo joined a team that already had the same scope.
    """
    return {
        "members": sorted(info["members"]),
        "sub_product_ids": sorted(str(s) for s in info["sub_product_ids"]),
        "role": default_role,
    }


class ApplyCache:
    """Loads/stores per-team and per-user provisioning fingerprints."""

    def __init__(self, path: str, tenant_url: str, default_role: str):
        self.path = Path(path)
        self.tenant_url = tenant_url
        self.default_role = default_role
        self._prev_teams: dict = {}
        self._prev_users: dict = {}
        self._teams: dict = {}
        self._users: dict = {}
        self.loaded = False
        self.reject_reason: str | None = None

    def load(self) -> None:
        """Read a previous run's cache, rejecting it if it can't be trusted."""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            self.reject_reason = f"unreadable ({e})"
            return

        if data.get("schema") != SCHEMA_VERSION:
            self.reject_reason = (f"schema {data.get('schema')!r} != {SCHEMA_VERSION} "
                                 f"(cache layout changed)")
            return
        if data.get("tenant") != self.tenant_url:
            self.reject_reason = (f"written for tenant {data.get('tenant')!r}, "
                                 f"this run targets {self.tenant_url!r}")
            return

        self._prev_teams = data.get("teams") or {}
        self._prev_users = data.get("users") or {}
        self.loaded = True

    # -- lookups ----------------------------------------------------------

    def team_unchanged(self, team_name: str, info: dict) -> bool:
        return (self.loaded
                and self._prev_teams.get(team_name)
                == _team_fingerprint(info, self.default_role))

    def user_unchanged(self, email: str) -> bool:
        """A cached user was created with this role, so it exists. Users are
        only ever created here (never modified), so existence is the whole
        fingerprint."""
        return self.loaded and self._prev_users.get(email) == self.default_role

    # -- recording (only after success) -----------------------------------

    def record_team(self, team_name: str, info: dict) -> None:
        self._teams[team_name] = _team_fingerprint(info, self.default_role)

    def carry_team(self, team_name: str) -> None:
        """Keep a skipped team's previous entry, so skipping doesn't drop it
        from the cache and force a re-provision next run."""
        if team_name in self._prev_teams:
            self._teams[team_name] = self._prev_teams[team_name]

    def record_user(self, email: str) -> None:
        self._users[email] = self.default_role

    def save(self) -> None:
        self.path.write_text(json.dumps({
            "schema": SCHEMA_VERSION,
            "tenant": self.tenant_url,
            "teams": self._teams,
            "users": self._users,
        }, indent=2, sort_keys=True) + "\n")
