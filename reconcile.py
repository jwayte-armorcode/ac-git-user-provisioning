#!/usr/bin/env python3
"""Remove users from teams when they're no longer in any of the Group's repos.

    python reconcile.py                    # dry run (default) — a report
    python reconcile.py --apply
    python reconcile.py --apply --limit 5
    python reconcile.py --report-only

This is the weekly job, and the ONLY code here that takes access away.
Everything else in this tool is additive.

The rule is a strict mirror: for each ArmorCode Group that has matching
repos, the team's membership should be exactly the union of those repos'
members. Anyone on the team who is not in that union is removed — including
someone added by hand in the ArmorCode UI. That was a deliberate choice; it
makes team membership predictable and derived, rather than an accumulation
of whatever anyone ever did.

That rule is only as good as the extracts feeding it, so it is wrapped in
three independent safeguards:

  1. REFUSE UNUSABLE EXTRACTS. Every configured SCM must have a complete,
     non-partial extract. A missing SCM and an SCM with no members produce
     identical input, and acting on that difference removes real access.

  2. CIRCUIT BREAKER. Per-team, a removal of more than max_removal_pct of
     current members (and more than max_removal_floor people) is skipped.
     Whole-run, more than max_tripped_teams teams tripping aborts
     everything. This is what catches an extract that SUCCEEDED with bad
     data — e.g. a token that quietly lost group-read permission still
     authenticates and lists repos, but reports zero members.

  3. SNAPSHOT FIRST. Nothing is removed until the current state is captured;
     restore.py rebuilds from it.

Teams with NO matching repos are never touched. Absence of a Group in the
extract is not evidence that its team should be emptied — it usually means
no repo carries that sub-product's URL. Only teams this tool can positively
account for are reconciled.
"""

from __future__ import annotations

import argparse
import csv
import sys

import config
import extract_store
import model
import snapshot as snapshot_mod
from armorcode import (
    ArmorCodeClient,
    LastTeamError,
    remove_user_from_team,
)
from matching import SubProductIndex

REPORT_COLUMNS = ["team", "group", "email", "current_role", "reason", "action"]


def write_report(path: str, rows: list[dict]) -> None:
    """Write the reconciliation report, overwriting any previous one.

    Always written — including when empty — so a previous week's rows can't
    be mistaken for this week's findings.
    """
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in REPORT_COLUMNS})


def build_actual(ac) -> tuple[dict, dict, dict]:
    """Current membership from the tenant.

    Returns (by_team_id, users_by_email, team_ids_by_name) where by_team_id
    is team_id -> {email: role}.
    """
    users: dict[str, dict] = {}
    actual: dict[int, dict] = {}
    for u in ac.get_users():
        email = (u.get("email") or "").strip().lower()
        if not email:
            continue
        users[email] = u
        for ti in u.get("teamInfo") or []:
            tid = ti.get("teamId")
            if tid is not None:
                actual.setdefault(int(tid), {})[email] = ti.get("role")

    ids_by_name: dict[str, int] = {}
    for t in ac.get_teams():
        if t.get("id") is not None:
            ids_by_name[t["name"]] = int(t["id"])
            ids_by_name.setdefault(t["name"].lower(), int(t["id"]))
    return actual, users, ids_by_name


def plan_removals(agg, actual, ids_by_name, limits, protected_emails,
                  ignore_breaker=False) -> tuple[list, list, list]:
    """Work out who should be removed from which team.

    Returns (removals, tripped, skipped) where:
      removals  [{team, group, team_id, email, role}] — eligible to remove
      tripped   [{team, removing, total, pct}] — teams the breaker stopped
      skipped   [str] — informational notes

    Members whose email the SCM could not resolve are in `protected_emails`
    and never removed: they are invisible to the extract by definition, so
    treating them as "not in any repo" would remove exactly the people the
    email-exceptions workflow exists to onboard.
    """
    removals: list[dict] = []
    tripped: list[dict] = []
    skipped: list[str] = []

    for team_name in sorted(agg.teams):
        plan = agg.teams[team_name]
        team_id = ids_by_name.get(team_name) or ids_by_name.get(team_name.lower())
        if team_id is None:
            skipped.append(f"{team_name}: no such team in ArmorCode yet "
                           f"(run provision.py first)")
            continue

        current = actual.get(team_id, {})
        if not current:
            continue

        desired = set(plan.members)               # lowercased emails
        candidates = [e for e in sorted(current) if e not in desired]

        # Never remove someone the SCM couldn't see an email for.
        protected = [e for e in candidates if e in protected_emails]
        candidates = [e for e in candidates if e not in protected_emails]
        for e in protected:
            skipped.append(f"{team_name}: keeping {e} — appears in the extract "
                           f"without a resolvable email")

        if not candidates:
            continue

        if limits.trips(len(candidates), len(current)):
            pct = len(candidates) / len(current) * 100
            tripped.append({"team": team_name, "group": plan.group_name,
                            "removing": len(candidates), "total": len(current),
                            "pct": pct,
                            "emails": candidates})
            # Recorded as tripped either way, so the report and the log always
            # show which teams crossed the line. With --force-removals the
            # removals still go ahead.
            if not ignore_breaker:
                continue

        for email in candidates:
            removals.append({
                "team": team_name, "group": plan.group_name, "team_id": team_id,
                "email": email, "role": current.get(email),
            })

    return removals, tripped, skipped


def main():
    parser = argparse.ArgumentParser(
        description="Remove users from teams when no longer in any of the "
                    "Group's repos (strict mirror)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python reconcile.py                   # dry run report\n"
            "  python reconcile.py --apply --limit 5\n"
            "  python reconcile.py --apply\n"
            "\n"
            "Weekly cron should run:\n"
            "  python extract.py --scm all && python reconcile.py --apply\n"
            "so a failed extract stops the pipeline before anything is removed.\n"
        ),
    )
    parser.add_argument("--env", default="envfile",
                        help="Path to the ini config file (default: envfile).")
    parser.add_argument("--extract-dir", default=".",
                        help="Parent directory holding the per-SCM extracts.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Reconcile at most N teams (sorted order). Caps teams "
                             "TOUCHED, not the data reconciled from — each team's "
                             "desired membership is still computed from every "
                             "extract, so a limited run is correct as far as it "
                             "goes. Use it for a first --apply.")
    parser.add_argument("--report", default="reconcile_report.csv",
                        help="Where to write the report "
                             "(default: reconcile_report.csv).")
    parser.add_argument("--report-only", action="store_true",
                        help="Write the report and exit without removing anything, "
                             "even if --apply is passed.")
    parser.add_argument("--max-removal-pct", type=int, default=None,
                        help="Override [reconcile] max_removal_pct for this run.")
    parser.add_argument("--max-removal-floor", type=int, default=None,
                        help="Override [reconcile] max_removal_floor for this run.")
    parser.add_argument("--max-tripped-teams", type=int, default=None,
                        help="Override [reconcile] max_tripped_teams for this run.")
    # Two separate overrides, deliberately not one flag. A tenant whose
    # sub-products mostly lack a repoLink needs --force-extracts on every
    # run; if that also disabled the mass-removal guard, the protection
    # would be permanently off for exactly the tenants most likely to
    # mis-match repos.
    parser.add_argument("--force-extracts", action="store_true",
                        help="Proceed despite an unusable extract. Refused when an "
                             "extract is PARTIAL. Does NOT disable the circuit "
                             "breaker.")
    parser.add_argument("--force-removals", action="store_true",
                        help="Bypass the circuit breaker, for a genuine mass "
                             "departure (a group deleted in the SCM) after "
                             "reviewing the report. Does NOT disable the "
                             "extract checks.")
    parser.add_argument("--force", action="store_true",
                        help="Both --force-extracts and --force-removals. Prefer "
                             "the specific flag so you only disable the guard you "
                             "mean to.")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="Skip the pre-write snapshot. Strongly discouraged "
                             "here — this command removes access.")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=None,
                      help="Report what would be removed without writing (default)")
    mode.add_argument("--apply", action="store_true", default=None,
                      help="Actually remove the memberships")

    args = parser.parse_args()
    dry_run = not args.apply or args.report_only
    force_extracts = args.force or args.force_extracts
    force_removals = args.force or args.force_removals

    try:
        cfg = config.load_config(args.env)
    except config.ConfigError as e:
        print(f"[error] {e}")
        sys.exit(1)

    limits = config.ReconcileLimits(
        max_removal_pct=(args.max_removal_pct if args.max_removal_pct is not None
                         else cfg.reconcile.max_removal_pct),
        max_removal_floor=(args.max_removal_floor if args.max_removal_floor is not None
                           else cfg.reconcile.max_removal_floor),
        max_tripped_teams=(args.max_tripped_teams if args.max_tripped_teams is not None
                           else cfg.reconcile.max_tripped_teams),
    )

    mnemonics = sorted(cfg.scms)
    print(f"\n{'='*70}")
    print(f"  ArmorCode reconcile ({'DRY RUN' if dry_run else 'APPLY'})")
    print(f"{'='*70}")
    print(f"[config] tenant {cfg.armorcode_url}")
    print(f"[config] SCMs: {', '.join(mnemonics)}")
    print(f"[config] circuit breaker: skip a team removing >{limits.max_removal_pct}% "
          f"of members (min {limits.max_removal_floor + 1}); abort the run if "
          f">{limits.max_tripped_teams} team(s) trip")

    # Guard 1: every SCM must have a usable extract.
    ok, problems = extract_store.check_all_usable(args.extract_dir, mnemonics)
    if not ok:
        print("\n[error] not every SCM has a usable extract:")
        for p in problems:
            print(f"    - {p}")
        partial = any("PARTIAL" in p for p in problems)
        if partial:
            # No override for this one: a truncated extract makes every unseen
            # member look like a departure, which is precisely the input that
            # turns a strict mirror into a mass removal.
            print("\n  [refused] a PARTIAL extract cannot be reconciled from, with "
                  "or without\n            --force. It makes every unseen member "
                  "look like a departure.\n            Re-run the extract without "
                  "--limit/--repo/--changed-since.")
            sys.exit(1)
        if not force_extracts:
            print("\n  Reconciling from an incomplete view would remove people whose "
                  "repos\n  simply weren't read. Re-run the extracts, or pass "
                  "--force-extracts.")
            sys.exit(1)
        print("\n  [force-extracts] continuing despite the above")

    repos = extract_store.load_all_repos(args.extract_dir, mnemonics)
    print(f"[extract] {len(repos)} repo(s) across {len(mnemonics)} SCM(s)")
    if not repos:
        print("[error] no repos in the extracts — refusing to reconcile, since "
              "that would empty every managed team")
        sys.exit(1)

    ac = ArmorCodeClient(tenant_url=cfg.armorcode_url, token=cfg.armorcode_token)

    print("\n[armorcode] loading sub-products (1 call)...")
    index = SubProductIndex(ac.get_sub_products_full())
    agg = model.aggregate(repos, index)
    print(f"[match] {agg.matched_repos}/{agg.total_repos} repo(s) matched -> "
          f"{len(agg.teams)} Group-derived team(s)")

    if not agg.teams:
        print("\n[done] no Group-derived teams found — nothing to reconcile")
        return

    # Members the SCM saw but couldn't resolve an email for. They must never
    # count as departures; see plan_removals().
    protected: set = set()
    for plan in agg.teams.values():
        for miss in plan.members_missing_email:
            uname = (miss.get("member") or {}).get("username")
            if uname:
                protected.add(uname.strip().lower())
    # Match on email too, since an admin may have filled one in by hand.
    protected |= {
        (m.get("member") or {}).get("email", "").strip().lower()
        for plan in agg.teams.values() for m in plan.members_missing_email
        if (m.get("member") or {}).get("email")
    }
    protected.discard("")

    actual, users, ids_by_name = build_actual(ac)
    removals, tripped, skipped = plan_removals(agg, actual, ids_by_name, limits,
                                               protected,
                                               ignore_breaker=force_removals)

    # Report every intent, including what the breaker stopped, so the CSV is a
    # complete record of what the run wanted to do.
    rows = [
        {"team": r["team"], "group": r["group"], "email": r["email"],
         "current_role": r["role"], "reason": "not in any repo under this Group",
         "action": "would remove" if dry_run else "removed"}
        for r in removals
    ]
    if not force_removals:
        # With --force-removals these emails are already in `removals` above,
        # so adding them here too would double-report them.
        for t in tripped:
            for email in t["emails"]:
                rows.append({
                    "team": t["team"], "group": t["group"], "email": email,
                    "current_role": actual.get(
                        ids_by_name.get(t["team"], -1), {}).get(email, ""),
                    "reason": (f"not in any repo under this Group, but the team "
                               f"tripped the removal limit ({t['removing']}/"
                               f"{t['total']}, {t['pct']:.0f}%)"),
                    "action": "SKIPPED (circuit breaker)",
                })
    write_report(args.report, rows)
    print(f"[report] wrote {args.report} ({len(rows)} row(s))")

    for note in skipped:
        print(f"  [keep] {note}")

    # Guard 2, whole-run tier.
    if tripped:
        print(f"\n{'-'*70}")
        print(f"  CIRCUIT BREAKER: {len(tripped)} team(s) exceeded the removal limit")
        print(f"{'-'*70}")
        for t in tripped:
            print(f"  [skip] {t['team']}: would remove {t['removing']} of "
                  f"{t['total']} member(s) ({t['pct']:.0f}%)")
        print("\n  A team losing most of its members usually means an incomplete")
        print("  extract rather than that many people leaving at once.")

        if len(tripped) > limits.max_tripped_teams and not force_removals:
            print(f"\n[abort] {len(tripped)} team(s) tripped the limit, more than "
                  f"the {limits.max_tripped_teams} allowed.")
            print("        That pattern points at bad input, not real departures.")
            print(f"        NOTHING WAS REMOVED. Review {args.report}, then re-run "
                  f"with")
            print("        --force-removals if the removals are genuinely correct.")
            sys.exit(1)
        if force_removals:
            print("\n  [force-removals] bypassing the circuit breaker — the teams "
                  "above WILL be reconciled")

    if not removals:
        print(f"\n{'='*70}")
        print("  Nothing to remove — every team's membership already matches "
              "its repos.")
        print(f"{'='*70}\n")
        return

    by_team: dict[str, list] = {}
    for r in removals:
        by_team.setdefault(r["team"], []).append(r)

    names = sorted(by_team)
    total_teams = len(names)
    if args.limit is not None:
        names = names[:args.limit]

    print(f"\n{'-'*70}")
    print(f"  {sum(len(by_team[n]) for n in names)} removal(s) across "
          f"{len(names)} team(s)"
          f"{f' of {total_teams} (--limit {args.limit})' if args.limit is not None else ''}")
    print(f"{'-'*70}")

    if args.report_only:
        print(f"\n[report-only] not removing anything. See {args.report}.")
        return

    # Guard 3: snapshot before the first removal.
    if not dry_run and not args.no_snapshot:
        snapshot_mod.take_snapshot(ac, cfg.armorcode_url, cfg.snapshots.path,
                                   command="reconcile.py --apply")

    removed = 0
    errors = 0
    last_team = 0
    for team_name in names:
        items = by_team[team_name]
        print(f"\n[team] {team_name}  ({len(items)} removal(s))")
        for item in items:
            label = f"{item['email']} (role {item['role']!r})"
            if dry_run:
                print(f"    [dry_run] would remove {label}")
                continue
            record = users.get(item["email"])
            if record is None:
                print(f"    [skip] {item['email']}: no longer in the tenant")
                continue
            try:
                if remove_user_from_team(ac, record, item["team_id"]):
                    print(f"    [remove] {label}")
                    removed += 1
                else:
                    print(f"    [noop] {item['email']} was not on the team")
            except LastTeamError:
                # The API forbids an empty teamInfo, so this person cannot be
                # removed from their only team. Reported for a human, not
                # worked around.
                print(f"    [keep] {item['email']}: this is their only team — "
                      f"ArmorCode requires at least one. Remove the user, or "
                      f"add them to another team first.")
                last_team += 1
            except Exception as e:
                print(f"    [error] {item['email']}: {e}")
                errors += 1

    if args.limit is not None and total_teams > len(names):
        print(f"\n  [limit] processed {len(names)} of {total_teams} team(s) "
              f"(--limit {args.limit}); {total_teams - len(names)} left untouched")

    print(f"\n{'='*70}")
    if dry_run:
        print(f"  DRY RUN — nothing removed. {len(removals)} membership(s) would go.")
        print(f"  Review {args.report}, then re-run with --apply.")
    else:
        print(f"  Done. {removed} membership(s) removed, {last_team} kept as a "
              f"last team, {errors} error(s).")
        if removed:
            print("  To undo: python restore.py --apply")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
