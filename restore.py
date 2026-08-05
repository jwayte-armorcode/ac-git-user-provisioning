#!/usr/bin/env python3
"""Rebuild team memberships from a snapshot.

    python restore.py                              # dry run, latest snapshot
    python restore.py --apply
    python restore.py --snapshot snapshots/2026-08-05T02-46-01Z --apply
    python restore.py --teams payments,web --apply
    python restore.py --list

The recovery path for a bad removal. Every provision/reconcile --apply takes
a snapshot first, so there is always a point to come back to.

ADDITIVE ONLY, deliberately. Restore puts back memberships the snapshot had
and that are now missing; it never removes anything added since. The reason
is narrow but important: this exists to undo an over-removal, and a restore
that also removed things could turn one bad run into two. If you need an
exact point-in-time revert, do the removals by hand after reviewing the
diff this prints.

Roles are restored as recorded. A user's role varies per team, so putting
someone back with a default role would quietly change their access level —
that would be a security change wearing a recovery's clothes.
"""

from __future__ import annotations

import argparse
import sys

import config
import snapshot as snapshot_mod
from armorcode import ArmorCodeClient, add_user_to_team


def build_current(ac) -> tuple[dict, dict, dict]:
    """Read the tenant's present membership.

    Returns:
      current    team_id -> {email: role}
      users      lowercased email -> user record (with teamInfo, for merging)
      team_names team_id -> name
    """
    users = {}
    current: dict[int, dict] = {}
    for u in ac.get_users():
        email = (u.get("email") or "").strip().lower()
        if not email:
            continue
        users[email] = u
        for ti in u.get("teamInfo") or []:
            tid = ti.get("teamId")
            if tid is not None:
                current.setdefault(int(tid), {})[email] = ti.get("role")

    team_names = {int(t["id"]): t.get("name", "")
                  for t in ac.get_teams() if t.get("id") is not None}
    return current, users, team_names


def main():
    parser = argparse.ArgumentParser(
        description="Restore team memberships from a snapshot (additive only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python restore.py --list\n"
            "  python restore.py                      # dry run, latest snapshot\n"
            "  python restore.py --apply\n"
            "  python restore.py --teams payments --apply\n"
        ),
    )
    parser.add_argument("--env", default="envfile",
                        help="Path to the ini config file (default: envfile).")
    parser.add_argument("--snapshot", default=None,
                        help="Snapshot directory to restore from "
                             "(default: the most recent one).")
    parser.add_argument("--teams", default=None,
                        help="Restore only these teams, comma-separated by name. "
                             "Use this to undo one team's removals without "
                             "touching anything else.")
    parser.add_argument("--list", action="store_true",
                        help="List available snapshots and exit.")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=None,
                      help="Show what would be restored without writing (default)")
    mode.add_argument("--apply", action="store_true", default=None,
                      help="Write the restored memberships")

    args = parser.parse_args()
    dry_run = not args.apply

    try:
        cfg = config.load_config(args.env)
    except config.ConfigError as e:
        print(f"[error] {e}")
        sys.exit(1)

    base = cfg.snapshots.path

    if args.list:
        snaps = snapshot_mod.list_snapshots(base)
        if not snaps:
            print(f"no snapshots in {base}/")
            return
        print(f"{len(snaps)} snapshot(s) in {base}/ (newest first):\n")
        for d in snaps:
            try:
                s = snapshot_mod.load_snapshot(d)
            except ValueError as e:
                print(f"  {d.name}  [unusable: {e}]")
                continue
            mem = sum(len(v) for v in s.memberships().values())
            flag = "" if s.meta.get("consistency", {}).get("consistent", True) \
                   else "  [inconsistent]"
            print(f"  {d.name}  {s.meta.get('teams')} teams, "
                  f"{s.meta.get('users')} users, {mem} memberships{flag}")
            print(f"      {s.meta.get('command','')}")
        return

    snap_dir = args.snapshot or snapshot_mod.latest_snapshot(base)
    if snap_dir is None:
        print(f"[error] no snapshots found in {base}/ — nothing to restore from")
        sys.exit(1)

    try:
        snap = snapshot_mod.load_snapshot(snap_dir)
    except ValueError as e:
        print(f"[error] {e}")
        sys.exit(1)

    print(f"\n{'='*70}")
    print(f"  Restore memberships ({'DRY RUN' if dry_run else 'APPLY'})")
    print(f"{'='*70}")
    print(f"[snapshot] {snap.dir}")
    print(f"[snapshot] taken {snap.taken_at} by: {snap.meta.get('command','')}")

    # A snapshot from another tenant would map team ids onto entirely
    # different teams — the single most destructive mistake available here.
    if snap.tenant != cfg.armorcode_url:
        print(f"\n[error] snapshot is from tenant {snap.tenant!r} but this config "
              f"targets {cfg.armorcode_url!r}.")
        print("        Team ids are per-tenant, so restoring across tenants would "
              "write nonsense.")
        sys.exit(1)

    if not snap.meta.get("consistency", {}).get("consistent", True):
        print("[warn] this snapshot's team-side and user-side membership disagreed "
              "when taken — review the diff below carefully")

    ac = ArmorCodeClient(tenant_url=cfg.armorcode_url, token=cfg.armorcode_token)
    current, users, live_team_names = build_current(ac)

    snap_mem = snap.memberships()
    snap_names = snap.team_names()

    wanted_teams = None
    if args.teams:
        wanted_teams = {t.strip().lower() for t in args.teams.split(",") if t.strip()}

    # Work out what's missing relative to the snapshot.
    to_restore: list[dict] = []
    missing_teams: list[str] = []
    missing_users: list[str] = []

    for tid, members in sorted(snap_mem.items()):
        name = snap_names.get(tid, f"id={tid}")
        if wanted_teams is not None and name.lower() not in wanted_teams:
            continue
        if tid not in live_team_names:
            # The team itself is gone. Restore only rebuilds memberships, so
            # this needs the team recreated first — reported, not attempted.
            missing_teams.append(name)
            continue
        live = current.get(tid, {})
        for email, role in sorted(members.items()):
            if email in live:
                continue
            if email not in users:
                missing_users.append(f"{email} (team {name})")
                continue
            to_restore.append({"team_id": tid, "team_name": name,
                               "email": email, "role": role})

    if missing_teams:
        print(f"\n[warn] {len(missing_teams)} team(s) in the snapshot no longer "
              f"exist; their memberships cannot be restored until the team is "
              f"recreated (run provision.py):")
        for n in sorted(set(missing_teams)):
            print(f"    - {n}")

    if missing_users:
        print(f"\n[warn] {len(missing_users)} membership(s) reference a user who no "
              f"longer exists in the tenant:")
        for n in missing_users[:10]:
            print(f"    - {n}")

    if not to_restore:
        print("\n[done] nothing to restore — the tenant already has every "
              "membership in this snapshot")
        print(f"{'='*70}\n")
        return

    by_team: dict[str, list] = {}
    for item in to_restore:
        by_team.setdefault(item["team_name"], []).append(item)

    print(f"\n{'-'*70}")
    print(f"  {len(to_restore)} membership(s) to restore across "
          f"{len(by_team)} team(s)")
    print(f"{'-'*70}")

    restored = 0
    errors = 0
    for team_name, items in sorted(by_team.items()):
        print(f"\n[team] {team_name}")
        for item in items:
            label = f"{item['email']} as {item['role']!r}"
            if dry_run:
                print(f"    [dry_run] would add {label}")
                continue
            record = users[item["email"]]
            try:
                # Role comes from the snapshot, never a default — see the
                # module docstring.
                if add_user_to_team(ac, record, item["team_id"], item["role"]):
                    print(f"    [restore] added {label}")
                    restored += 1
                else:
                    print(f"    [noop] {item['email']} already on team")
            except Exception as e:
                print(f"    [error] {item['email']}: {e}")
                errors += 1

    print(f"\n{'='*70}")
    if dry_run:
        print(f"  DRY RUN — nothing written. {len(to_restore)} membership(s) "
              f"would be restored.")
        print("  Re-run with --apply to write.")
    else:
        print(f"  Done. {restored} membership(s) restored, {errors} error(s).")
    print("  Restore is additive: nothing added since the snapshot was removed.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
