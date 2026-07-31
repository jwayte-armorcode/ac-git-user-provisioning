#!/usr/bin/env python3
"""Sync SCM repo topics into ArmorCode teams, users, and scope.

One entry point for both GitHub and GitLab — pick with --source.

Repos declare their owning ArmorCode team via a topic:

  GitHub   armorcode-team-<name>   (topics are lowercase-alphanumeric-and
                                    -hyphens only, so no colon and the topic
                                    IS the slugified team identifier)
  GitLab   armorcode-team:<Name>   (topics allow mixed case and colons, so
                                    the team name survives as typed)

A repo may carry more than one team topic; each becomes a separate team
that the repo's sub-product is scoped into.

Flow, per repo:
  1. Read the repo's topics -> one or more team names.
  2. Read its members, split into those with a resolvable email and those
     without.
  3. Create the ArmorCode team (scope-only) if missing, or GET the existing
     team and merge in newly matched product/sub-product scope without
     dropping anything already scoped.
  4. Create any missing ArmorCode user, then add every member to the team
     via a GET-merge on the user's teamInfo (team membership lives on the
     user record, not the team).
  5. Members with no resolvable email are appended to email_exceptions.csv
     rather than silently dropped. Once an admin fills in the email column,
     --reprocess-from-exceptions provisions them.

Dry run is the default; nothing is written without --apply.

Usage:
    python team_sync.py --source github [--rows N] [--repo owner/name]
    python team_sync.py --source gitlab --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

import email_exceptions
import sync_checkpoint
from armorcode import (
    ArmorCodeClient,
    ArmorCodeState,
    add_user_to_team,
    load_default_role,
    load_env_file,
    merge_scope_into_team,
    user_label,
)
from scm_readers import GitHubTeamReader, GitLabTeamReader


def build_reader(source: str, env: dict):
    """Construct the reader for --source, failing loudly on a missing token."""
    if source == "github":
        pat = env.get("GITHUB_PAT") or env.get("token")
        if not pat:
            print("[error] no GitHub token found (expected 'GITHUB_PAT')")
            sys.exit(1)
        return GitHubTeamReader(pat)

    pat = env.get("GITLAB_PAT") or env.get("token")
    if not pat:
        print("[error] no GitLab token found (expected 'GITLAB_PAT')")
        sys.exit(1)
    url = env.get("GITLAB_URL") or env.get("url") or "https://gitlab.com"
    return GitLabTeamReader(pat, url)


def sync(reader, state: ArmorCodeState, rows: int | None, dry_run: bool,
         default_role: str, repo: str | None, exceptions_file: str, today: str,
         checkpoint_file: str, sparse: bool = False):
    ac = state.ac
    source = reader.source
    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"\n{'='*70}\n  {source} -> ArmorCode team sync ({mode})\n{'='*70}\n")
    if repo:
        print(f"[filter] restricting to single repo: {repo!r}\n")

    print(f"[{source}] Fetching and sorting full repo list (by id, for a stable resume order)...")
    all_repos = reader.load_repos(repo=repo)
    total_count = len(all_repos)
    print(f"[{source}] {total_count} repo(s) visible to this token")

    # Checkpointing is always on — every run checks for a prior checkpoint
    # and resumes from it automatically, no flag required. A run that
    # completes in full (no --repo/--rows filter) clears the checkpoint at
    # the end, so a plain first-ever run just proceeds normally: no
    # checkpoint file exists yet, so after_id is None and nothing is skipped.
    after_id = sync_checkpoint.load_checkpoint(checkpoint_file, source)
    if after_id is not None:
        print(f"[resume] Checkpoint found: resuming after repo id {after_id}")
    else:
        print(f"[resume] No checkpoint found for {source} — starting from the beginning")

    repos_seen = 0
    repos_with_teams = 0

    for scm_repo in reader.iter_repos(all_repos, rows=rows, after_id=after_id):
        repos_seen += 1
        repo_id = reader.repo_id(scm_repo)
        full_name = reader.repo_full_name(scm_repo)
        repo_name = reader.repo_name(scm_repo)  # short name, matched to AC sub-products
        team_names = reader.get_team_names(scm_repo)

        def checkpoint():
            if not dry_run:
                sync_checkpoint.save_checkpoint(
                    checkpoint_file, source, repo_id, repos_seen, total_count,
                    datetime.now().isoformat(),
                )

        if not team_names:
            checkpoint()
            continue

        repos_with_teams += 1
        print(f"\n[repo] {full_name}  (teams: {', '.join(team_names)})")

        members = reader.get_members(scm_repo)
        members_with_email = [m for m in members if m["email"]]
        members_missing_email = [m for m in members if not m["email"]]
        print(f"    members: {len(members)} total, {len(members_with_email)} with email, "
              f"{len(members_missing_email)} without (cannot provision without email)")

        sub_products = state.find_matching_sub_products(repo_name)
        if not sub_products:
            print(f"    [warn] no ArmorCode sub-product named {repo_name!r} found — "
                  f"team scope cannot be set for this repo (team/users still processed)")
        elif len(sub_products) > 1:
            names = ", ".join(f"{sp['name']}(id={sp['id']})" for sp in sub_products)
            print(f"    [info] {len(sub_products)} matching sub-products found — scoping team to all: {names}")
        else:
            print(f"    [info] matched sub-product: {sub_products[0]['name']} (id={sub_products[0]['id']})")

        scope_entries = state.build_scope_entries(sub_products) if sub_products else []

        # ---- Ensure ArmorCode users exist for all members with email ----
        # Keep full user records (not just ids) — add_user_to_team() needs
        # each user's cached teamInfo to GET-merge team membership correctly.
        user_records_for_team: list[dict] = []
        # Dry run can't create users, so a would-be-created user never lands in
        # user_records_for_team and would be missing from the membership
        # preview. Track them separately so the preview reflects everyone who
        # would end up on the team, not just those who already exist.
        pending_user_labels: list[str] = []
        for m in members_with_email:
            email_lower = m["email"].lower()
            existing = state.users_by_email.get(email_lower)
            if existing:
                user_records_for_team.append(existing)
                continue

            print(f"    [create-user] {m['name']} <{m['email']}>")
            if dry_run:
                pending_user_labels.append(f"{m['name']} <{m['email']}> (would be created)")
                continue
            try:
                created = ac.create_user(name=m["name"], email=m["email"], tenant_role=default_role)
                created.setdefault("teamInfo", [])
                state.users_by_email[email_lower] = created
                user_records_for_team.append(created)
            except Exception as e:
                print(f"      [error] failed to create user {m['email']}: {e}")

        for team_name in team_names:
            team = state.find_team(team_name)

            if team is None:
                print(f"    [create-team] {team_name}")
                if dry_run:
                    team = {"id": None, "name": team_name}  # placeholder for dry-run logging
                else:
                    try:
                        group_scopes = [(e["product"], e["subProduct"]) for e in scope_entries]
                        team = ac.create_team_scoped(
                            name=team_name,
                            group_scopes=group_scopes,
                            business_unit_id=state.business_unit_id,
                            business_unit_name=state.business_unit_name,
                        )
                        state.register_team(team)
                    except Exception as e:
                        print(f"      [error] failed to create team {team_name!r}: {e}")
                        continue
            else:
                print(f"    [team] {team['name']} exists (id={team['id']})")
                if scope_entries:
                    if dry_run:
                        print(f"      [dry_run] would merge scope entries: {scope_entries}")
                    else:
                        try:
                            body, changed = merge_scope_into_team(
                                ac, team, scope_entries,
                                business_unit_id=state.business_unit_id,
                                business_unit_name=state.business_unit_name,
                            )
                            if changed:
                                ac.put_team(body)
                                print(f"      [update] scope merged for team {team_name!r}")
                                if not sparse:
                                    print(f"        scope entries: {scope_entries}")
                            else:
                                print("      [noop] scope already covers these sub-products")
                        except Exception as e:
                            print(f"      [error] failed to merge scope for team {team_name!r}: {e}")

            # Membership: always add via add_user_to_team, whether the team
            # was just created (scope-only, no members yet) or already existed.
            if user_records_for_team or pending_user_labels:
                if dry_run:
                    labels = [user_label(r) for r in user_records_for_team] + pending_user_labels
                    print(f"      [dry_run] would ensure {len(labels)} member(s) on team"
                          f"{'' if sparse else ':'}")
                    if not sparse:
                        for label in labels:
                            print(f"        - {label}")
                else:
                    # Track who was actually added vs already present, so apply
                    # can name them the way dry run does — a bare "added 2"
                    # leaves no record of WHICH members changed.
                    added, already = [], []
                    for record in user_records_for_team:
                        try:
                            if add_user_to_team(ac, record, team["id"], default_role):
                                added.append(user_label(record))
                            else:
                                already.append(user_label(record))
                        except Exception as e:
                            uid = record.get("userId") or record.get("id")
                            print(f"      [error] failed to add user {uid} to team {team_name!r}: {e}")
                    if added:
                        print(f"      [update] added {len(added)} new member(s) to team {team_name!r}:")
                        if not sparse:
                            for label in added:
                                print(f"        - {label}")
                        if already and not sparse:
                            print(f"      [noop] {len(already)} member(s) already on team:")
                            for label in already:
                                print(f"        - {label}")
                    else:
                        print(f"      [noop] all {len(already)} member(s) already on team"
                              f"{':' if already and not sparse else ''}")
                        if already and not sparse:
                            for label in already:
                                print(f"        - {label}")

            if members_missing_email and not dry_run:
                email_exceptions.log_exceptions(
                    exceptions_file, source, full_name, team_name, members_missing_email, today,
                )

        if members_missing_email:
            names = ", ".join(f"{m['name']} ({m['username']})" for m in members_missing_email)
            print(f"    [warn] skipped (no public email, cannot provision): {names}")

        checkpoint()

    print(f"\n{'='*70}")
    print(f"  Done. {repos_seen} repo(s) scanned, {repos_with_teams} had armorcode-team topics.")
    print(f"{'='*70}\n")

    # A full, unfiltered, non-dry-run pass reached the end without being
    # killed — clear the checkpoint so a later fresh run doesn't skip
    # anything because a stale "last completed" position is still on disk.
    if not dry_run and repo is None and rows is None:
        sync_checkpoint.clear_checkpoint(checkpoint_file)
        print("[resume] Full run completed — checkpoint cleared")


def reprocess_exceptions(state: ArmorCodeState, exceptions_file: str, dry_run: bool,
                         default_role: str) -> None:
    """Provision contributors whose email exception row now has an email filled in.

    Does NOT re-touch team scope (that's driven by the repo's topics, which
    this mode doesn't re-read) — it only ensures the user exists in
    ArmorCode and is added to the team named in their exception row. Rows
    are only marked "reprocessed" after their team add succeeds.
    """
    ac = state.ac
    candidates = email_exceptions.load_reprocess_candidates(exceptions_file)
    if not candidates:
        print(f"[reprocess] No pending rows with an email filled in, in {exceptions_file}")
        return

    print(f"[reprocess] {len(candidates)} row(s) with an email now filled in\n")
    succeeded = []

    for row in candidates:
        email = row["email"].strip().lower()
        name = row.get("name") or row["username"]
        team_name = row["team"]
        print(f"  {name} <{email}> -> team {team_name!r} (from {row['source']} repo {row['repo']!r})")

        team = state.find_team(team_name)
        if team is None:
            print(f"    [error] team {team_name!r} no longer exists in ArmorCode — skipping, "
                  f"re-run the normal sync first so the team exists")
            continue

        if dry_run:
            print("    [dry_run] would ensure user exists and is added to this team")
            continue

        try:
            user_record = state.users_by_email.get(email)
            if user_record is None:
                user_record = ac.create_user(name=name, email=row["email"].strip(), tenant_role=default_role)
                user_record.setdefault("teamInfo", [])
                state.users_by_email[email] = user_record
                print("    [create-user] created")

            if add_user_to_team(ac, user_record, team["id"], default_role):
                print(f"    [update] added to team {team_name!r}")
            else:
                print("    [noop] already on team")
            succeeded.append(row)
        except Exception as e:
            print(f"    [error] failed to provision: {e}")

    if succeeded:
        email_exceptions.mark_reprocessed(exceptions_file, succeeded)
        print(f"\n[reprocess] {len(succeeded)} row(s) marked reprocessed in {exceptions_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Sync SCM repo topics (armorcode-team-*) into ArmorCode teams/users/scope",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python team_sync.py --source github\n"
            "  python team_sync.py --source github --rows 10\n"
            "  python team_sync.py --source github --repo owner/ac-sdk-v2 --apply\n"
            "  python team_sync.py --source gitlab --repo juice-shop --apply\n"
            "  python team_sync.py --source gitlab --apply\n"
        ),
    )
    parser.add_argument("--source", required=True, choices=["github", "gitlab"],
                        help="Which SCM to sync from. Determines the topic convention read "
                             "(armorcode-team-<name> on GitHub, armorcode-team:<Name> on "
                             "GitLab) and which token is used from the env file.")
    parser.add_argument("--env", default="envfile",
                        help="Path to the env file (default: envfile). Holds the SCM token "
                             "(GITHUB_PAT / GITLAB_PAT) and the ArmorCode tenant credentials "
                             "(TENANT_URL / API_TOKEN) — see env.example.")
    parser.add_argument("--ac-env", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--rows", type=int, default=None,
                        help="Process at most N repos. Upper bound, not a requirement — good "
                             "for a small-blast-radius smoke test before a full run.")
    parser.add_argument("--repo", default=None,
                        help="Restrict to a single repo. GitHub: 'owner/name'. GitLab: either "
                             "the short path ('juice-shop') or 'namespace/path'.")
    parser.add_argument("--config", default="team_sync.ini",
                        help="Path to the ini file holding default_role (default: team_sync.ini). "
                             "Looked up in the [<source>] section first, then [armorcode].")
    parser.add_argument("--default-role", default=None,
                        help="tenantRole for newly-created ArmorCode users. Overrides the ini "
                             "file. Must be a valid role in the tenant (e.g. Developer, Admin, "
                             "Security Engineer) — a wrong value returns 400 'Provided Tenant "
                             "Role Not Found'.")
    parser.add_argument("--exceptions-file", default=None,
                        help="CSV of members with no resolvable email (default: "
                             "email_exceptions_<source>.csv). They're logged here rather than "
                             "dropped; once an admin fills in the email column, re-run with "
                             "--reprocess-from-exceptions to provision them. Per-source by "
                             "default because the file is rewritten whole on each update — "
                             "pointing a concurrent GitHub run and GitLab run at one path "
                             "would let them silently drop each other's rows.")
    parser.add_argument("--reprocess-from-exceptions", action="store_true",
                        help="Instead of a normal sync, read --exceptions-file and provision "
                             "any row where the email column has been filled in (user created "
                             "if needed, added to the row's team). Does not re-scope teams.")
    parser.add_argument("--checkpoint-file", default=None,
                        help="Path to the resume checkpoint (default: "
                             "sync_checkpoint_<source>.json, so a GitHub run and a GitLab run "
                             "never clobber each other's progress). Every --apply run writes "
                             "progress here after each repo and checks it at startup: if it "
                             "exists, the run resumes after that repo id instead of starting "
                             "over. A full run that completes with no --repo/--rows filter "
                             "clears it. Dry runs never write it.")
    parser.add_argument("--sparse", action="store_true",
                        help="Condense logging to counts and summary lines, omitting the "
                             "per-user names and scope payloads printed by default. Applies "
                             "to both dry runs and --apply. Useful on very large tenants "
                             "where full per-user output is too verbose; the default "
                             "(non-sparse) output is what you want for an auditable record "
                             "of exactly which users and scope changed.")

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", default=None,
                            help="Print what would happen without writing anything (default)")
    mode_group.add_argument("--apply", action="store_true", default=None,
                            help="Write changes to ArmorCode")

    args = parser.parse_args()
    dry_run = not args.apply  # default True unless --apply passed
    source = args.source

    # Per-source state files by default: with one entry point for both SCMs,
    # a shared default path would let a GitHub run and a GitLab run clobber
    # each other. The checkpoint would resume from the wrong position; the
    # exceptions CSV is rewritten whole on every update, so two interleaved
    # runs would silently drop each other's rows.
    checkpoint_file = args.checkpoint_file or f"sync_checkpoint_{source}.json"
    exceptions_file = args.exceptions_file or f"email_exceptions_{source}.csv"

    # One env file holds everything: the qualified key names (GITHUB_PAT vs
    # API_TOKEN/TENANT_URL) don't collide. The legacy bare "token"/"url"
    # keys ARE ambiguous across services, so they're checked last, after
    # every qualified key — that way a combined file's "token=" (meant for
    # one service) can't be misread as the other's credential.
    # --ac-env is a hidden escape hatch for split-credential setups.
    ac_env_path = args.ac_env or args.env

    scm_env = load_env_file(args.env)
    reader = build_reader(source, scm_env)

    ac_env = load_env_file(ac_env_path)
    ac_token = ac_env.get("API_TOKEN") or ac_env.get("token")
    # No default tenant — an unset TENANT_URL must fail loudly rather than
    # silently targeting some other tenant than the operator intended.
    ac_url = ac_env.get("TENANT_URL") or ac_env.get("url")
    if not ac_token:
        print(f"[error] no ArmorCode token found in {ac_env_path}")
        sys.exit(1)
    if not ac_url:
        print(f"[error] no ArmorCode tenant URL found in {ac_env_path} "
              f"(expected 'TENANT_URL', e.g. TENANT_URL=https://xxxx.armorcode.xxx)")
        sys.exit(1)
    ac_url = ac_url.replace("https://", "").replace("http://", "")

    default_role = load_default_role(args.config, args.default_role, source)
    print(f"[config] new ArmorCode users will be created with role: {default_role!r} "
          f"(from {'--default-role' if args.default_role else args.config})")

    ac = ArmorCodeClient(tenant_url=ac_url, token=ac_token)
    state = ArmorCodeState(ac)

    if args.reprocess_from_exceptions:
        reprocess_exceptions(state, exceptions_file, dry_run, default_role)
        return

    sync(reader, state, rows=args.rows, dry_run=dry_run, default_role=default_role,
         repo=args.repo, exceptions_file=exceptions_file,
         today=date.today().isoformat(), checkpoint_file=checkpoint_file,
         sparse=args.sparse)


if __name__ == "__main__":
    main()
