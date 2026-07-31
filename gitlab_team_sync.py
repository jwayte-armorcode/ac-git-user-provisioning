#!/usr/bin/env python3
"""
gitlab_team_sync: Read ArmorCode team ownership from GitLab project topics
and provision the matching teams/users/scope in ArmorCode.

Self-contained — does not depend on a separate ac-sdk-v2 checkout. The
ArmorCode API methods it needs are inlined below (ported from ac-sdk-v2's
armorcode/client.py) so this script only needs `requests` and `python-gitlab`.

For each GitLab project:
  1. Read topics of the form "armorcode-team:<name>" -> one or more team names.
  2. Read project members (direct + inherited group members).
  3. For each team name:
       - Create the ArmorCode team if it doesn't exist.
       - Find every ArmorCode sub-product whose name matches the GitLab
         project name (there may be more than one — e.g. same repo name
         under different products). Merge all of them into the team's
         scope (GET current team -> merge new product/sub-product entries
         into properties -> PUT back). Never drops existing scope.
  4. For each GitLab member with a resolvable email:
       - Create the ArmorCode user if they don't exist.
       - Add them to the team (GET current members -> merge -> add via API).

Matching GitLab project name -> ArmorCode sub-product name is by exact
name equality (case-insensitive). If zero sub-products match, the project
is skipped with a warning (nothing to scope the team to). If multiple
match, all of them are added to the team's scope.

Usage:
    python gitlab_team_sync.py --env env_gitlab --ac-env /path/to/ac/env [--dry-run] [--rows N] [--repo NAME]

Dry run is the default. Pass --apply to write changes to ArmorCode.
"""

from __future__ import annotations

import argparse
import configparser
import sys
import time
from datetime import date, datetime
from pathlib import Path

import gitlab
import requests
from gitlab.exceptions import GitlabError

import email_exceptions
import sync_checkpoint


TEAM_TOPIC_PREFIX = "armorcode-team:"
MIN_ACCESS_LEVEL = 20  # Reporter+


# ---------------------------------------------------------------------------
# ArmorCode client — inlined subset of ac-sdk-v2's armorcode/client.py.
# Ported (not imported) so this script has no dependency on a separate SDK
# checkout. Only the methods this script actually calls are included.
# ---------------------------------------------------------------------------

class _ThrottledRetrySession(requests.Session):
    """A ``requests.Session`` that paces requests and retries on throttling.

    Ported verbatim from ac-sdk-v2's armorcode/client.py. Retries 429/5xx
    with exponential backoff (honoring Retry-After); other statuses (incl.
    4xx) are returned immediately for raise_for_status() to handle.
    """

    def __init__(self, *args, min_interval=0.0, max_retries=8,
                 backoff_base=2.0, backoff_cap=60.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._last_request_ts = 0.0

    def _sleep_to_throttle(self):
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)

    def request(self, method, url, **kwargs):
        delay = self._backoff_base
        last_resp = None
        for attempt in range(self._max_retries + 1):
            self._sleep_to_throttle()
            self._last_request_ts = time.monotonic()
            resp = super().request(method, url, **kwargs)
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            last_resp = resp
            if attempt >= self._max_retries:
                break
            retry_after = resp.headers.get("Retry-After")
            if retry_after and str(retry_after).isdigit():
                wait = float(retry_after)
            else:
                wait = delay
                delay = min(delay * 2, self._backoff_cap)
            wait = min(wait, self._backoff_cap)
            # A blind retry-on-5xx can silently sit for minutes on a
            # PERMANENT error (e.g. a 500 the server will never stop
            # returning, like "User Can Not Update Him/Her Self") — that
            # looks indistinguishable from a real hang unless it's logged.
            print(f"    [retry] {method} {url} -> {resp.status_code} "
                  f"(attempt {attempt + 1}/{self._max_retries + 1}, "
                  f"waiting {wait:.0f}s): {resp.text[:200]}")
            time.sleep(wait)
        return last_resp


class ArmorCodeClient:
    """Minimal ArmorCode REST client — teams, users, sub-products only.

    Ported from ac-sdk-v2's ArmorCodeClient. If you need other endpoints,
    pull the corresponding method from ac-sdk-v2/armorcode/client.py rather
    than reintroducing a dependency on the external SDK package.
    """

    def __init__(self, tenant_url, token, *, timeout=60,
                 min_request_interval=0.0, max_retries=8):
        self.base_url = f"https://{tenant_url.rstrip('/')}"
        self._session = _ThrottledRetrySession(
            min_interval=min_request_interval,
            max_retries=max_retries,
        )
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        self._timeout = timeout

    # -- Teams -------------------------------------------------------------

    def get_teams(self):
        """List all teams (id + name)."""
        resp = self._session.get(f"{self.base_url}/api/team/all-teams", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def get_team(self, team_id):
        """Get full detail for a specific team (members, properties/scope, lead)."""
        resp = self._session.get(f"{self.base_url}/api/team/{team_id}", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def create_team_scoped(self, name, group_scopes, *, description="",
                           members=None, business_unit_id, business_unit_name,
                           email_alias="", extra=None):
        """Create a new team scoped to specific products/sub-products.

        group_scopes: iterable of (product_id, [sub_product_ids]) tuples.
        An empty/None sub_product_ids list means whole-product access.
        """
        psp_map = []
        for entry in group_scopes:
            if isinstance(entry, (list, tuple)):
                pid, subgroups = entry[0], (entry[1] if len(entry) > 1 else None)
            else:
                pid, subgroups = entry, None
            subs = list(subgroups) if subgroups else []
            psp_map.append({
                "product": int(pid),
                "subProduct": subs,
                "accessOnAllSubProduct": not subs,
            })

        body = {
            "name": name,
            "description": description,
            "members": list(members) if members else [],
            "properties": [{
                "businessUnitId": business_unit_id,
                "businessUnitName": business_unit_name,
                "productSubProductMap": psp_map,
                "accessType": "individual",
                "groups": [],
            }],
            "emailAlias": email_alias,
            "accessOnAllBusinessUnits": False,
            "approvalWorkflow": {"approvers": []},
        }
        if extra:
            body.update(extra)
        resp = self._session.post(f"{self.base_url}/api/team", json=body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def put_team(self, body):
        """Raw PUT of a full team body (used for scope-merge updates)."""
        resp = self._session.put(f"{self.base_url}/api/team", json=body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def update_user_team_info(self, user_id, team_info):
        """Set a user's full team membership list via PUT /user/update/user.

        team_info: list of {"teamId": int, "role": <role name str>}. This
        REPLACES the user's entire teamInfo — callers must GET the user's
        current teamInfo and merge before calling, or existing memberships
        are lost. The API also rejects an empty list (a user must belong to
        at least one team), so never call this with team_info=[].

        Note: /api/v2/team/{id}/members (the more obvious-looking
        "add member to team" endpoint) was tried first but rejects every
        role value attempted ("Role not found: <value>") regardless of
        whether role is passed as a name, a /user/roles id, or a nested
        object — its expected role vocabulary could not be determined.
        This endpoint is the confirmed-working path (same one the ac-sdk-v2
        SDK's update_user() uses), verified end-to-end against JulianSandbox.
        """
        resp = self._session.put(
            f"{self.base_url}/user/update/user",
            json={"id": user_id, "teamInfo": list(team_info)},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        # Confirmed inconsistent: a single-team update returns the full
        # updated user object, but a multi-team update returns 200 with an
        # empty body. Don't rely on the response for confirmation either
        # way — treat any 2xx as success and return {} when there's no body.
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # -- Users ---------------------------------------------------------------

    def get_users(self):
        """List all users in the tenant."""
        resp = self._session.get(f"{self.base_url}/user/data/users", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def get_business_units(self):
        """List business units (organizations) in the tenant: [{id, name}, ...]."""
        resp = self._session.get(f"{self.base_url}/user/business-units/", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def create_user(self, name, email, tenant_role, *, disable_login=False,
                    team_info=None, extra=None):
        """Create a new user in the tenant."""
        body = {
            "name": name,
            "email": email,
            "tenantRole": tenant_role,
            "disableLogin": disable_login,
        }
        if team_info is not None:
            body["teamInfo"] = list(team_info)
        if extra:
            body.update(extra)
        resp = self._session.post(f"{self.base_url}/user/add/user", json=body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    # -- Sub-products (repos/components) --------------------------------------

    def get_sub_products(self):
        """List all sub-products (lightweight: id + name)."""
        resp = self._session.get(
            f"{self.base_url}/user/sub-product/elastic/short", timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_sub_product(self, sub_product_id):
        """Get full detail for a sub-product, including its parent `product`."""
        resp = self._session.get(
            f"{self.base_url}/api/sub-product/{sub_product_id}", timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

def load_env_file(path: str) -> dict:
    """Parse a simple KEY=VALUE env file (lowercase or uppercase keys)."""
    out = {}
    p = Path(path)
    if not p.exists():
        print(f"[error] env file not found: {path}")
        sys.exit(1)
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


DEFAULT_ROLE = "Developer"


def load_default_role(config_path: str, cli_override: str | None) -> str:
    """Resolve the ArmorCode tenantRole for newly-created users.

    Precedence: --default-role CLI flag > [armorcode] default_role in the
    ini file > built-in default ("Developer"). Missing ini file is not an
    error — it's an optional override, not a requirement.
    """
    if cli_override:
        return cli_override

    p = Path(config_path)
    if p.exists():
        parser = configparser.ConfigParser()
        parser.read(p)
        if parser.has_option("armorcode", "default_role"):
            return parser.get("armorcode", "default_role").strip()

    return DEFAULT_ROLE


# ---------------------------------------------------------------------------
# GitLab side
# ---------------------------------------------------------------------------

class GitLabTeamReader:
    def __init__(self, pat: str, url: str = "https://gitlab.com"):
        self._gl = gitlab.Gitlab(url=url, private_token=pat)
        self._gl.auth()

    def load_sorted_projects(self, repo: str | None = None):
        """Fetch all projects the token has membership on, sorted by
        project id ascending, and return the full list.

        Sorting by id (not name/path) gives a resume order that's stable
        across separate runs — ids never change once assigned, so a
        checkpoint recorded as "last completed id" always means the same
        resume point even if repos are added, removed, or renamed between
        runs.

        Materializes the full list up front rather than streaming
        page-by-page (needed to sort) — at 100K+ repos this is still only
        tens of MB (each project object is small), the same "keep
        preloading" tradeoff already made for ArmorCode's team/user/
        sub-product state.

        repo: optional filter to a single project, matched case-insensitively
              against either the short project name (project.path, e.g.
              "juice-shop") or the full "namespace/path" (e.g.
              "julianwayte/juice-shop"). Intended for one-off test runs
              against a single known repo rather than scanning everything.
        """
        projects = list(self._gl.projects.list(membership=True, all=True, iterator=True))
        projects.sort(key=lambda p: p.id)

        if repo is not None:
            repo_lower = repo.strip().lower()
            projects = [
                p for p in projects
                if p.path.lower() == repo_lower or p.path_with_namespace.lower() == repo_lower
            ]
        return projects

    def iter_projects(self, projects: list, rows: int | None = None,
                      after_id: int | None = None):
        """Yield from an already-sorted project list (see
        load_sorted_projects), applying resume-skip and a row cap.

        after_id: optional resume point — skip every project with
              id <= after_id (used by --resume).
        rows: optional cap on how many (post-skip) projects to yield.
        """
        count = 0
        for project in projects:
            if after_id is not None and project.id <= after_id:
                continue
            yield project
            count += 1
            if rows is not None and count >= rows:
                return

    def get_team_names(self, project) -> list[str]:
        """Extract ArmorCode team names from the project's topics."""
        topics = getattr(project, "topics", None) or getattr(project, "tag_list", None) or []
        names = []
        for t in topics:
            if t.startswith(TEAM_TOPIC_PREFIX):
                name = t[len(TEAM_TOPIC_PREFIX):].strip()
                if name:
                    names.append(name)
        return names

    def get_members(self, project) -> list[dict]:
        """Direct + inherited group members at or above MIN_ACCESS_LEVEL."""
        members_map: dict[int, dict] = {}
        try:
            member_list = project.members_all.list(all=True)
        except GitlabError:
            member_list = project.members.list(all=True)

        for m in member_list:
            if m.access_level < MIN_ACCESS_LEVEL:
                continue
            email = self._resolve_email(m)
            members_map[m.id] = {
                "username": m.username,
                "name": m.name,
                "email": email,
            }
        return list(members_map.values())

    def _resolve_email(self, member) -> str | None:
        email = getattr(member, "email", None)
        if email:
            return email
        try:
            user = self._gl.users.get(member.id)
            return getattr(user, "email", None) or getattr(user, "public_email", None) or None
        except GitlabError:
            return None


# ---------------------------------------------------------------------------
# ArmorCode side helpers
# ---------------------------------------------------------------------------

class ArmorCodeState:
    """Caches ArmorCode teams/users/sub-products so we don't re-fetch per project."""

    def __init__(self, ac: ArmorCodeClient):
        self.ac = ac
        print("[armorcode] Loading teams, users, sub-products, business units...")

        # Business unit ids are tenant-specific — never hardcode one. Resolve
        # the tenant's actual default org by name; fall back to whichever
        # business unit comes first if no "Default Organization" exists.
        business_units = ac.get_business_units()
        default_bu = next(
            (bu for bu in business_units if bu.get("name") == "Default Organization"),
            business_units[0] if business_units else None,
        )
        if default_bu is None:
            raise RuntimeError("No business units found in this tenant — cannot scope teams")
        self.business_unit_id = default_bu["id"]
        self.business_unit_name = default_bu["name"]
        print(f"[armorcode] Using business unit: {self.business_unit_name!r} (id={self.business_unit_id})")

        # Keyed by exact name AND lowercased name — see find_team()/register_team()
        # for why: a GitLab topic's team name should still match an existing
        # team of the same name in a different case (e.g. topic "web" vs.
        # team "Web"), rather than creating a case-variant duplicate.
        self.teams_by_name: dict[str, dict] = {}
        for t in ac.get_teams():
            self.register_team(t)

        self.users_by_email: dict[str, dict] = {}
        for u in ac.get_users():
            email = (u.get("email") or "").lower()
            if email:
                self.users_by_email[email] = u

        # sub-products: name (lowercased) -> list of {id, name} (may be >1)
        self.sub_products_by_name: dict[str, list[dict]] = {}
        for sp in ac.get_sub_products():
            key = sp["name"].strip().lower()
            self.sub_products_by_name.setdefault(key, []).append(sp)

        print(
            f"[armorcode] {len(self.teams_by_name)} teams, "
            f"{len(self.users_by_email)} users with email, "
            f"{sum(len(v) for v in self.sub_products_by_name.values())} sub-products"
        )

    # -- team lookup (case-insensitive) -----------------------------------

    def find_team(self, team_name: str) -> dict | None:
        """Look up a team by name, case-insensitively — a topic-derived
        team name should still match an existing team of the same name in
        a different case, rather than creating a duplicate."""
        return self.teams_by_name.get(team_name) or self.teams_by_name.get(team_name.lower())

    def register_team(self, team: dict) -> None:
        """Index a team (new or freshly fetched) under both its exact name
        and lowercased name, so find_team() can match it either way."""
        self.teams_by_name.setdefault(team["name"], team)
        self.teams_by_name.setdefault(team["name"].lower(), team)

    # -- sub-product matching --------------------------------------------

    def find_matching_sub_products(self, gitlab_project_name: str) -> list[dict]:
        """Return every AC sub-product whose name matches the GitLab project
        name exactly (case-insensitive). May be 0, 1, or many."""
        return list(self.sub_products_by_name.get(gitlab_project_name.strip().lower(), []))

    # -- team scope (product/sub-product map) -----------------------------

    def build_scope_entries(self, sub_products: list[dict]) -> list[dict]:
        """For each sub-product, resolve its parent product id and build a
        productSubProductMap entry {product, subProduct:[...], accessOnAllSubProduct: False}.

        Sub-products belonging to the same parent product are merged into a
        single entry with all their ids listed.
        """
        by_product: dict[int, set[int]] = {}
        for sp in sub_products:
            detail = self.ac.get_sub_product(sp["id"])
            product = detail.get("product") or {}
            product_id = product.get("id")
            if product_id is None:
                print(f"    [warn] sub-product {sp['name']!r} (id {sp['id']}) has no parent product, skipping")
                continue
            by_product.setdefault(product_id, set()).add(sp["id"])

        return [
            {"product": pid, "subProduct": sorted(sub_ids), "accessOnAllSubProduct": False}
            for pid, sub_ids in by_product.items()
        ]


# ---------------------------------------------------------------------------
# Merge helpers (GET -> merge -> PUT, never drop existing scope/members)
# ---------------------------------------------------------------------------

def merge_scope_into_team(ac: ArmorCodeClient, team: dict, new_entries: list[dict],
                          business_unit_id: int, business_unit_name: str) -> tuple[dict, bool]:
    """Merge new productSubProductMap entries into a team's existing scope,
    without dropping anything already scoped.

    GET /api/team/{id} and PUT /api/team use two different shapes for the
    same data:
      - READ  (GET response "properties"): businessUnit is a nested
        {id, name} object; productSubProductMap entries nest product as
        {id, name} and use "subProducts" (plural) as a list of {id, name}.
      - WRITE (PUT request body): businessUnitId is a flat int; product is
        a flat id; the sub-product list key is "subProduct" (singular) of
        flat ids.
    Sending the read shape back on a PUT causes a 400 (Jackson can't
    deserialize an object where it expects a plain id). So this function
    reads via get_team(), converts each existing property entry into the
    flat write shape, merges the new entries into that, and returns a body
    built entirely in write shape.

    Returns (updated_team_body_that_will_be_sent, changed).
    """
    current = ac.get_team(team["id"])
    read_properties = current.get("properties") or []

    # Convert every existing property entry (read shape) to write shape.
    write_properties = []
    for entry in read_properties:
        bu = entry.get("businessUnit") or {}
        psp_map = []
        for e in entry.get("productSubProductMap") or []:
            product = e.get("product") or {}
            sub_products = e.get("subProducts") or []
            psp_map.append({
                "product": int(product["id"]),
                "subProduct": [int(sp["id"]) for sp in sub_products],
                "accessOnAllSubProduct": bool(e.get("accessOnAllSubProduct")),
            })
        write_properties.append({
            "businessUnitId": bu.get("id"),
            "businessUnitName": bu.get("name"),
            "productSubProductMap": psp_map,
            "accessType": "individual",
            "groups": entry.get("groups") or [],
        })

    bu_entry = None
    for entry in write_properties:
        if entry["businessUnitId"] == business_unit_id:
            bu_entry = entry
            break

    if bu_entry is None:
        bu_entry = {
            "businessUnitId": business_unit_id,
            "businessUnitName": business_unit_name,
            "productSubProductMap": [],
            "accessType": "individual",
            "groups": [],
        }
        write_properties.append(bu_entry)

    psp_map = bu_entry["productSubProductMap"]
    by_product = {e["product"]: e for e in psp_map}
    changed = False

    for new_entry in new_entries:
        pid = new_entry["product"]
        if pid in by_product:
            existing = by_product[pid]
            if existing.get("accessOnAllSubProduct"):
                continue  # already has whole-group access; nothing to add
            existing_subs = set(existing.get("subProduct") or [])
            new_subs = set(new_entry.get("subProduct") or [])
            merged_subs = existing_subs | new_subs
            if merged_subs != existing_subs:
                existing["subProduct"] = sorted(merged_subs)
                changed = True
        else:
            psp_map.append(dict(new_entry))
            by_product[pid] = new_entry
            changed = True

    body = dict(current)
    body["id"] = team["id"]
    body["properties"] = write_properties
    return body, changed


def add_user_to_team(ac: ArmorCodeClient, user_record: dict, team_id: int, role: str) -> bool:
    """Ensure one user belongs to one team, without dropping their other
    team memberships.

    Team membership lives on the USER record (user_record["teamInfo"]), not
    on the team record — PUT /user/update/user replaces a user's entire
    teamInfo list, so this always GET-merges: read the user's current
    teamInfo (already cached on user_record from get_users()), skip if
    team_id is already present, otherwise append and write the full list
    back.

    Returns True if the user was newly added, False if they already had
    this team (no-op, no write performed).
    """
    user_id = user_record.get("userId") or user_record["id"]
    team_info = list(user_record.get("teamInfo") or [])

    if any(t.get("teamId") == team_id for t in team_info):
        return False

    team_info.append({"teamId": team_id, "role": role})
    updated = ac.update_user_team_info(user_id, team_info)
    user_record["teamInfo"] = updated.get("teamInfo", team_info)
    return True


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync(gl_reader: GitLabTeamReader, state: ArmorCodeState, rows: int | None, dry_run: bool,
         default_role: str = "Developer", repo: str | None = None,
         exceptions_file: str = "email_exceptions.csv", today: str = "",
         resume: bool = False, checkpoint_file: str = "sync_checkpoint.json"):
    ac = state.ac
    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"\n{'='*70}\n  GitLab -> ArmorCode team sync ({mode})\n{'='*70}\n")
    if repo:
        print(f"[filter] restricting to single repo: {repo!r}\n")

    print("[gitlab] Fetching and sorting full project list (by id, for a stable resume order)...")
    all_projects = gl_reader.load_sorted_projects(repo=repo)
    total_count = len(all_projects)
    print(f"[gitlab] {total_count} project(s) visible to this token")

    after_id = None
    if resume:
        after_id = sync_checkpoint.load_checkpoint(checkpoint_file, "gitlab")
        if after_id is not None:
            print(f"[resume] Checkpoint found: resuming after project id {after_id}")
        else:
            print("[resume] No checkpoint found for gitlab — starting from the beginning")

    projects_seen = 0
    projects_with_teams = 0
    last_completed_id = after_id

    for project in gl_reader.iter_projects(all_projects, rows=rows, after_id=after_id):
        projects_seen += 1
        path = project.path_with_namespace
        project_name = project.path  # short name, used to match AC sub-products
        team_names = gl_reader.get_team_names(project)

        if not team_names:
            last_completed_id = project.id
            if resume and not dry_run:
                sync_checkpoint.save_checkpoint(
                    checkpoint_file, "gitlab", last_completed_id, projects_seen, total_count,
                    datetime.now().isoformat(),
                )
            continue

        projects_with_teams += 1
        print(f"\n[project] {path}  (teams: {', '.join(team_names)})")

        members = gl_reader.get_members(project)
        members_with_email = [m for m in members if m["email"]]
        members_missing_email = [m for m in members if not m["email"]]
        print(f"    members: {len(members)} total, {len(members_with_email)} with email, "
              f"{len(members_missing_email)} without (cannot provision without email)")

        sub_products = state.find_matching_sub_products(project_name)
        if not sub_products:
            print(f"    [warn] no ArmorCode sub-product named {project_name!r} found — "
                  f"team scope cannot be set for this project (team/users still processed)")
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
        for m in members_with_email:
            email_lower = m["email"].lower()
            existing = state.users_by_email.get(email_lower)
            if existing:
                user_records_for_team.append(existing)
                continue

            print(f"    [create-user] {m['name']} <{m['email']}>")
            if dry_run:
                continue
            try:
                created = ac.create_user(name=m["name"], email=m["email"], tenant_role=default_role)
                created.setdefault("teamInfo", [])
                state.users_by_email[email_lower] = created
                user_records_for_team.append(created)
            except Exception as e:
                print(f"      [error] failed to create user {m['email']}: {e}")

        user_ids_for_team = [
            (r.get("userId") or r["id"]) for r in user_records_for_team
        ]

        for team_name in team_names:
            team = state.find_team(team_name)

            if team is None:
                print(f"    [create-team] {team_name}")
                if dry_run:
                    team = {"id": None, "name": team_name}  # placeholder for dry-run logging
                else:
                    try:
                        # Members are NOT passed here. POST /api/team rejects
                        # any member whose user already has "account level
                        # access" ("... cannot be added directly to Teams
                        # please update User access by updating user") —
                        # confirmed against JulianSandbox, and every user this
                        # script creates hits that case. So team creation is
                        # scope-only; membership is always added afterward via
                        # add_user_to_team() (PUT /user/update/user), the same
                        # path used for pre-existing teams below.
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
                            else:
                                print("      [noop] scope already covers these sub-products")
                        except Exception as e:
                            print(f"      [error] failed to merge scope for team {team_name!r}: {e}")

            # Membership: always add via add_user_to_team, whether the team
            # was just created (scope-only, no members yet) or already existed.
            if user_records_for_team:
                if dry_run:
                    print(f"      [dry_run] would ensure {len(user_records_for_team)} member(s) on team")
                else:
                    added_count = 0
                    for record in user_records_for_team:
                        try:
                            if add_user_to_team(ac, record, team["id"], default_role):
                                added_count += 1
                        except Exception as e:
                            uid = record.get("userId") or record.get("id")
                            print(f"      [error] failed to add user {uid} to team {team_name!r}: {e}")
                    if added_count:
                        print(f"      [update] added {added_count} new member(s) to team {team_name!r}")
                    else:
                        print("      [noop] all members already on team")

            if members_missing_email and not dry_run:
                email_exceptions.log_exceptions(
                    exceptions_file, "gitlab", path, team_name, members_missing_email, today,
                )

        if members_missing_email:
            names = ", ".join(f"{m['name']} ({m['username']})" for m in members_missing_email)
            print(f"    [warn] skipped (no public email, cannot provision): {names}")

        last_completed_id = project.id
        if resume and not dry_run:
            sync_checkpoint.save_checkpoint(
                checkpoint_file, "gitlab", last_completed_id, projects_seen, total_count,
                datetime.now().isoformat(),
            )

    print(f"\n{'='*70}")
    print(f"  Done. {projects_seen} project(s) scanned, {projects_with_teams} had armorcode-team topics.")
    print(f"{'='*70}\n")

    # A full, unfiltered, non-dry-run pass reached the end without being
    # killed — clear the checkpoint so a later fresh run doesn't skip
    # anything because a stale "last completed" position is still on disk.
    if resume and not dry_run and repo is None and rows is None:
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
        description="Sync GitLab project topics (armorcode-team:*) into ArmorCode teams/users/scope",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python gitlab_team_sync.py --env env_gitlab --ac-env /path/to/JulianSandbox/env\n"
            "  python gitlab_team_sync.py --env env_gitlab --ac-env ../JulianSandbox/env --rows 5\n"
            "  python gitlab_team_sync.py --env env_gitlab --ac-env ../JulianSandbox/env --repo juice-shop\n"
            "  python gitlab_team_sync.py --env env_gitlab --ac-env ../JulianSandbox/env --repo juice-shop --apply\n"
            "  python gitlab_team_sync.py --env env_gitlab --ac-env ../JulianSandbox/env --apply\n"
        ),
    )
    parser.add_argument("--env", default="env_gitlab", help="Path to GitLab token env file (default: env_gitlab)")
    parser.add_argument("--ac-env", required=True, help="Path to ArmorCode tenant env file (token/url)")
    parser.add_argument("--rows", type=int, default=None,
                        help="Limit to the first N GitLab projects (for testing)")
    parser.add_argument("--repo", default=None,
                        help="Restrict to a single GitLab project for a one-off test run. "
                             "Matches either the short project name (e.g. juice-shop) or "
                             "the full namespace/path (e.g. julianwayte/juice-shop), "
                             "case-insensitive.")
    parser.add_argument("--config", default="gitlab_team_sync.ini",
                        help="Path to config ini (default: gitlab_team_sync.ini). "
                             "Currently provides [armorcode] default_role.")
    parser.add_argument("--default-role", default=None,
                        help="ArmorCode tenantRole assigned to newly-created users. "
                             "Overrides the ini file's [armorcode] default_role. "
                             "Must be a valid role name from the tenant's GET /user/roles "
                             "list, e.g. Developer, Admin, DevOps, Security Engineer, Read Only.")
    parser.add_argument("--exceptions-file", default="email_exceptions.csv",
                        help="CSV path for contributors with no resolvable email "
                             "(default: email_exceptions.csv). Appended to on every run; "
                             "an admin fills in the email column by hand, then re-run with "
                             "--reprocess-from-exceptions to provision them.")
    parser.add_argument("--reprocess-from-exceptions", action="store_true",
                        help="Instead of a normal sync, read --exceptions-file and provision "
                             "any row where the email column has been filled in (user created "
                             "if needed, added to the row's team). Does not re-scope teams.")
    parser.add_argument("--resume", action="store_true",
                        help="Track progress in --checkpoint-file after every project, so a "
                             "killed run can be restarted with --resume and continue from where "
                             "it left off instead of reprocessing everything. Projects are "
                             "processed in a stable order (sorted by project id) so the resume "
                             "point is meaningful across separate runs. On a full run that "
                             "completes (no --repo/--rows), the checkpoint is cleared. Only "
                             "meaningful with --apply — dry runs don't write a checkpoint.")
    parser.add_argument("--checkpoint-file", default="sync_checkpoint.json",
                        help="Path to the resume checkpoint (default: sync_checkpoint.json — "
                             "same default name as github_team_sync.py's, but the file is "
                             "single-source: it's tagged with which source wrote it, and a run "
                             "for a DIFFERENT source ignores it as if no checkpoint existed. "
                             "This means the default path is NOT safe to share between a "
                             "concurrent GitLab run and GitHub run — each would overwrite the "
                             "other's progress. Pass distinct --checkpoint-file paths (e.g. "
                             "gitlab_checkpoint.json / github_checkpoint.json) if both sources "
                             "run around the same time. Only used with --resume.")

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", default=None,
                            help="Print what would happen without writing anything (default)")
    mode_group.add_argument("--apply", action="store_true", default=None,
                            help="Write changes to ArmorCode")

    args = parser.parse_args()
    dry_run = not args.apply  # default True unless --apply passed

    gl_env = load_env_file(args.env)
    gl_pat = gl_env.get("token2") or gl_env.get("GITLAB_PAT") or gl_env.get("token")
    if not gl_pat:
        print(f"[error] no GitLab token found in {args.env} (expected 'token2', 'token', or 'GITLAB_PAT')")
        sys.exit(1)
    gl_url = gl_env.get("GITLAB_URL") or gl_env.get("url") or "https://gitlab.com"

    ac_env = load_env_file(args.ac_env)
    ac_token = ac_env.get("token") or ac_env.get("API_TOKEN")
    ac_url = (ac_env.get("url") or ac_env.get("TENANT_URL") or "https://app.armorcode.com")
    ac_url = ac_url.replace("https://", "").replace("http://", "")
    if not ac_token:
        print(f"[error] no ArmorCode token found in {args.ac_env}")
        sys.exit(1)

    default_role = load_default_role(args.config, args.default_role)
    print(f"[config] new ArmorCode users will be created with role: {default_role!r} "
          f"(from {'--default-role' if args.default_role else args.config})")

    ac = ArmorCodeClient(tenant_url=ac_url, token=ac_token)
    state = ArmorCodeState(ac)

    if args.reprocess_from_exceptions:
        reprocess_exceptions(state, args.exceptions_file, dry_run, default_role)
        return

    gl_reader = GitLabTeamReader(pat=gl_pat, url=gl_url)
    sync(gl_reader, state, rows=args.rows, dry_run=dry_run, default_role=default_role, repo=args.repo,
         exceptions_file=args.exceptions_file, today=date.today().isoformat(),
         resume=args.resume, checkpoint_file=args.checkpoint_file)


if __name__ == "__main__":
    main()
