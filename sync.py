#!/usr/bin/env python3
"""
ac-git-rbac: Sync GitHub/GitLab users into ArmorCode and assign them
to the repos (sub-products) they have access to.

Usage:
    python sync.py --source github [--dry-run]   # default: dry run
    python sync.py --source gitlab [--apply]      # write to ArmorCode
    python sync.py --source all   [--env env]

Dry run is the default. Pass --apply to write changes to ArmorCode.
SYNC_MODE=apply in the env file also enables apply mode (--dry-run flag overrides it).

By default this script NEVER creates new ArmorCode sub-products — it only
updates membership tags on sub-products that already exist (matched to a
repo by name). This is deliberate: most tenants already have their own
product/subgroup structure and don't want new sub-products invented for
every repo that doesn't happen to match. Pass --create-missing-subproducts
to opt into creating sub-products (and the GitRBAC-* holding product) for
unmatched repos.
"""

import argparse
import os
import sys
from pathlib import Path

from armorcode_client import ArmorCodeClient
from github_fetcher import GitHubFetcher
from gitlab_fetcher import GitLabFetcher


def load_env(env_path: str) -> None:
    path = Path(env_path)
    if not path.exists():
        print(f"[warn] env file not found: {env_path}, relying on shell env vars")
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        print(f"[error] Missing required env var: {key}")
        sys.exit(1)
    return val


def build_armorcode_client() -> ArmorCodeClient:
    tenant_url = require_env("TENANT_URL")
    # SDK prepends https:// itself, so strip the scheme if present
    tenant_url = tenant_url.replace("https://", "").replace("http://", "")
    token = require_env("API_TOKEN")
    return ArmorCodeClient(tenant_url=tenant_url, token=token)


def sync_source(source: str, ac: ArmorCodeClient, dry_run: bool, create_missing_subproducts: bool) -> None:
    print(f"\n{'='*60}")
    print(f"  Syncing source: {source.upper()}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'APPLY'}")
    print(f"{'='*60}\n")

    if source == "github":
        pat = require_env("GITHUB_PAT")
        repos_filter = [
            r.strip()
            for r in os.environ.get("GITHUB_REPOS", "").split(",")
            if r.strip()
        ]
        fetcher = GitHubFetcher(pat=pat, repos_filter=repos_filter)
    elif source == "gitlab":
        pat = require_env("GITLAB_PAT")
        gitlab_url = os.environ.get("GITLAB_URL", "https://gitlab.com").strip()
        projects_filter = [
            p.strip()
            for p in os.environ.get("GITLAB_PROJECTS", "").split(",")
            if p.strip()
        ]
        fetcher = GitLabFetcher(pat=pat, url=gitlab_url, projects_filter=projects_filter)
    else:
        raise ValueError(f"Unknown source: {source}")

    # 1. Fetch repo→members mapping from the SCM
    print(f"[{source}] Fetching repos and members...")
    repo_members = fetcher.get_repo_members()
    if not repo_members:
        print(f"[{source}] No repos found.")
        return

    all_scm_users: dict[str, dict] = {}  # email -> {name, email, username}
    for repo, members in repo_members.items():
        for m in members:
            if m["email"]:
                all_scm_users[m["email"]] = m

    print(f"[{source}] Found {len(repo_members)} repos, {len(all_scm_users)} unique users with email addresses")

    # 2. Get existing ArmorCode state
    print("\n[armorcode] Fetching existing users, repos, products...")
    ac_users = ac.get_users()
    # API returns "email" field (not "emailId")
    ac_user_by_email = {u.get("email", "").lower(): u for u in ac_users if u.get("email")}
    print(f"[armorcode] {len(ac_users)} existing users in tenant")

    ac_repos = ac.get_repos(states=["ACTIVE", "INACTIVE", "DORMANT"])
    ac_repo_by_name: dict[str, dict] = {}
    for r in ac_repos.get("content", []):
        name = r.get("name") or r.get("repoName", "")
        if name:
            ac_repo_by_name[name] = r
    print(f"[armorcode] {len(ac_repo_by_name)} repos visible in tenant")

    ac_products = ac.get_products()
    ac_product_by_name = {p["name"]: p for p in ac_products.get("content", [])}
    print(f"[armorcode] {len(ac_product_by_name)} products in tenant")

    ac_sub_products = ac.get_sub_products()
    ac_sub_by_name = {sp["name"]: sp for sp in ac_sub_products}
    print(f"[armorcode] {len(ac_sub_by_name)} sub-products in tenant")

    # 3. Invite missing users
    print("\n[sync] Checking users to invite...")
    invited = 0
    already_present = 0
    for email, user_info in all_scm_users.items():
        if email.lower() in ac_user_by_email:
            already_present += 1
            continue
        print(f"  [invite] {user_info['name']} <{email}> (username: {user_info['username']})")
        if not dry_run:
            try:
                ac.invite_user(
                    email=email,
                    name=user_info["name"],
                    role="USER",
                )
                invited += 1
            except Exception as e:
                print(f"    [error] Failed to invite {email}: {e}")
        else:
            invited += 1

    print(f"  -> {already_present} already in ArmorCode, {invited} to invite{'d' if not dry_run else ''}")

    # 4. For each repo, tag its matching sub-product with members.
    # Sub-product CREATION only happens when --create-missing-subproducts is
    # passed. Off by default: most customers already have their own
    # product/subgroup structure (mirroring their SCM groups) and only want
    # this script to update membership tags on what already exists, not
    # invent new sub-products for anything that doesn't match by name.
    print("\n[sync] Syncing repo memberships as sub-product tags...")
    product_name = f"GitRBAC-{source.capitalize()}"

    if create_missing_subproducts:
        if not dry_run:
            if product_name not in ac_product_by_name:
                print(f"  [create] Product: {product_name}")
                product = ac.create_product(
                    name=product_name,
                    description=f"Auto-created by ac-git-rbac for {source} repos",
                )
                ac_product_by_name[product_name] = product
            else:
                print(f"  [exists] Product: {product_name}")
        else:
            print(f"  [dry_run] Would ensure product exists: {product_name}")
    else:
        print("  [skip] Sub-product creation disabled (pass --create-missing-subproducts to enable). "
              "Only existing sub-products will have their membership tags updated.")

    for repo_full_name, members in repo_members.items():
        repo_short = repo_full_name.split("/")[-1]
        member_emails = [m["email"] for m in members if m["email"]]
        member_names = [m["username"] for m in members]

        # Check if this repo exists in ArmorCode
        ac_repo = ac_repo_by_name.get(repo_short) or ac_repo_by_name.get(repo_full_name)
        if ac_repo:
            repo_id = ac_repo.get("id") or ac_repo.get("repoId")
            source_tag = f"ac-repo-id:{repo_id}"
        else:
            source_tag = f"scm-repo:{repo_short}"

        members_tag = f"members:{','.join(sorted(member_names))}" if member_names else "members:none"

        print(f"\n  Repo: {repo_full_name} ({len(members)} members)")
        for m in members:
            status = "in AC" if m["email"] and m["email"].lower() in ac_user_by_email else "needs invite"
            print(f"    - {m['username']} <{m['email'] or 'no email'}> [{status}]")

        sub_product_exists = repo_short in ac_sub_by_name

        if not sub_product_exists and not create_missing_subproducts:
            print(f"    [skip] No existing sub-product named '{repo_short}' — "
                  f"not creating one (pass --create-missing-subproducts to enable)")
            continue

        if not dry_run:
            if sub_product_exists:
                sp = ac_sub_by_name[repo_short]
                ac.update_sub_product_set_tag(sp["id"], members_tag)
                ac.update_sub_product_set_tag(sp["id"], source_tag)
                print(f"    [update] Sub-product '{repo_short}' tags updated")
            else:
                sp = ac.create_sub_product(
                    name=repo_short,
                    product_name=product_name,
                    description=f"Repo: {repo_full_name} ({source})",
                    tags=[members_tag, source_tag, f"source:{source}"],
                )
                ac_sub_by_name[repo_short] = sp
                print(f"    [create] Sub-product '{repo_short}' created")
        else:
            if sub_product_exists:
                print(f"    [dry_run] Would update sub-product '{repo_short}' tags: {members_tag}")
            else:
                print(f"    [dry_run] Would create sub-product '{repo_short}' with tags: {members_tag}")

    print(f"\n[{source}] Sync complete.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync SCM users/repos into ArmorCode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python sync.py --source github              # dry run (default)\n"
            "  python sync.py --source github --apply      # write to ArmorCode\n"
            "  python sync.py --source all --dry-run       # explicit dry run\n"
            "  python sync.py --source gitlab --env ~/my-env --apply\n"
            "  python sync.py --source gitlab --apply --create-missing-subproducts\n"
            "                                                # also create sub-products\n"
            "                                                # for repos with no existing match\n"
        ),
    )
    parser.add_argument(
        "--source",
        choices=["github", "gitlab", "all"],
        default="all",
        help="Which SCM source to sync (default: all)",
    )
    parser.add_argument(
        "--env",
        default="envfile",
        help="Path to env file (default: envfile) — holds TENANT_URL/API_TOKEN plus "
             "GITHUB_PAT and/or GITLAB_PAT. See env.example.",
    )
    parser.add_argument(
        "--create-missing-subproducts",
        action="store_true",
        help="Create a new ArmorCode sub-product (and GitRBAC-* product, if needed) for any "
             "repo that doesn't already match an existing sub-product by name. OFF BY DEFAULT: "
             "most tenants already have their own product/subgroup structure and only want "
             "membership tags updated on what exists, not new sub-products invented. When this "
             "flag is off, unmatched repos are reported and skipped rather than created.",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Print what would happen without writing anything (default)",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        default=None,
        help="Write changes to ArmorCode (invite users, update tags on existing sub-products; "
             "add --create-missing-subproducts to also create sub-products for unmatched repos)",
    )

    args = parser.parse_args()

    load_env(args.env)

    # Flag takes precedence over env var; env var takes precedence over default
    if args.apply:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        mode = os.environ.get("SYNC_MODE", "dry_run").strip().lower()
        dry_run = mode != "apply"

    ac = build_armorcode_client()

    sources = ["github", "gitlab"] if args.source == "all" else [args.source]
    for source in sources:
        try:
            sync_source(source, ac, dry_run, args.create_missing_subproducts)
        except Exception as e:
            print(f"\n[error] {source} sync failed: {e}")
            if os.environ.get("DEBUG"):
                raise


if __name__ == "__main__":
    main()
