"""ArmorCode client and state for team_sync.py.

The client is an inlined subset of ac-sdk-v2's armorcode/client.py, kept
here so this tool has no dependency on the external SDK package. If you
need other endpoints, port the corresponding method from
ac-sdk-v2/armorcode/client.py rather than reintroducing that dependency.

Several methods carry hard-won notes about API shape traps (read vs write
shapes, membership living on the user record, endpoints that 500 on
self-update). Read those docstrings before changing a request body.
"""

from __future__ import annotations

import configparser
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests


# ArmorCode's published rate limits (as of 2026-07):
#
#   Per API token          2,000 RPM
#   Per endpoint (token)     100 RPM   <-- the binding constraint here
#   User session, write      100 RPM
#   User session, read       200 RPM
#
# The per-endpoint limit is what this tool actually hits: a long run calls
# the same few endpoints over and over (get_sub_product once per matched
# sub-product, update_user_team_info once per user), so it can exceed 100
# RPM on a single endpoint while nowhere near the 2,000 RPM token budget.
#
# 100 RPM = one request every 0.6s. The default pacing below is per-endpoint
# rather than global, so unrelated endpoints don't slow each other down.
PER_ENDPOINT_RPM = 100
MIN_ENDPOINT_INTERVAL = 60.0 / PER_ENDPOINT_RPM  # 0.6s


class _ThrottledRetrySession(requests.Session):
    """A ``requests.Session`` that paces requests per endpoint and waits out
    rate limits.

    Based on ac-sdk-v2's armorcode/client.py, with rate-limit handling
    reworked for long runs:

    - Pacing is PER ENDPOINT (path), not global, because ArmorCode's tight
      limit is per-endpoint. A global interval would either be too slow
      (pacing every call at the per-endpoint rate) or too permissive
      (hammering one endpoint while the token budget looks fine).
    - A 429 is transient by definition — the limit resets. So 429s retry
      effectively indefinitely (bounded by max_429_wait as a safety net)
      rather than giving up after N attempts and killing the run.
    - 5xx keeps a bounded retry count, since a 500 can be permanent (e.g.
      "User Can Not Update Him/Her Self") and must not spin forever.
    """

    def __init__(self, *args, min_interval=MIN_ENDPOINT_INTERVAL, max_retries=8,
                 backoff_base=2.0, backoff_cap=60.0, max_429_wait=3600.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._max_429_wait = max_429_wait
        # Per-endpoint last-request timestamps, keyed by URL path.
        self._last_request_ts: dict[str, float] = {}

    @staticmethod
    def _endpoint_key(url: str) -> str:
        """Bucket requests by URL path, ignoring the query string, so
        /api/sub-product/1 and /api/sub-product/2 share one bucket — the
        server's per-endpoint limit treats them as the same endpoint."""
        path = urlsplit(url).path
        # Collapse trailing numeric id segments so /api/team/123 and
        # /api/team/456 land in the same bucket.
        parts = [p for p in path.split("/") if p]
        return "/".join("{id}" if p.isdigit() else p for p in parts)

    def _sleep_to_throttle(self, key: str):
        if self._min_interval <= 0:
            return
        last = self._last_request_ts.get(key)
        if last is None:
            return
        wait = self._min_interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)

    def request(self, method, url, **kwargs):
        key = self._endpoint_key(url)
        delay = self._backoff_base
        server_errors = 0
        waited_on_429 = 0.0
        attempt = 0

        while True:
            attempt += 1
            self._sleep_to_throttle(key)
            self._last_request_ts[key] = time.monotonic()
            resp = super().request(method, url, **kwargs)

            if resp.status_code != 429 and resp.status_code < 500:
                return resp

            retry_after = resp.headers.get("Retry-After")
            if retry_after and str(retry_after).strip().isdigit():
                wait = float(str(retry_after).strip())
            else:
                wait = min(delay, self._backoff_cap)
                delay = min(delay * 2, self._backoff_cap)

            if resp.status_code == 429:
                # Rate limited: keep waiting. The limit resets, so giving up
                # here would abort a run for a purely temporary condition.
                # max_429_wait only guards against a limit that never clears.
                if waited_on_429 + wait > self._max_429_wait:
                    print(f"    [rate-limit] {method} {key} still 429 after "
                          f"{waited_on_429:.0f}s of waiting (limit "
                          f"{self._max_429_wait:.0f}s) — giving up")
                    return resp
                waited_on_429 += wait
                print(f"    [rate-limit] {method} {key} -> 429, waiting {wait:.0f}s "
                      f"for the limit to reset (waited {waited_on_429:.0f}s total, "
                      f"attempt {attempt}); the run continues automatically")
                time.sleep(wait)
                continue

            # 5xx: bounded, because it may be permanent.
            server_errors += 1
            if server_errors > self._max_retries:
                return resp
            # A blind retry-on-5xx can silently sit for minutes on a
            # PERMANENT error (e.g. a 500 the server will never stop
            # returning, like "User Can Not Update Him/Her Self") — that
            # looks indistinguishable from a real hang unless it's logged.
            print(f"    [retry] {method} {url} -> {resp.status_code} "
                  f"(server error {server_errors}/{self._max_retries}, "
                  f"waiting {wait:.0f}s): {resp.text[:200]}")
            time.sleep(wait)


class ArmorCodeClient:
    """Minimal ArmorCode REST client — teams, users, sub-products only.

    Ported from ac-sdk-v2's ArmorCodeClient. If you need other endpoints,
    pull the corresponding method from ac-sdk-v2/armorcode/client.py rather
    than reintroducing a dependency on the external SDK package.
    """

    def __init__(self, tenant_url, token, *, timeout=60,
                 min_request_interval=MIN_ENDPOINT_INTERVAL, max_retries=8):
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

        Members are intentionally NOT wired to POST /api/team's `members`
        field by callers of this method — that field rejects any user with
        "account level access" ("... cannot be added directly to Teams
        please update User access by updating user"), which every user
        this script creates hits. Team creation here is scope-only;
        membership is always added afterward via add_user_to_team()
        (PUT /user/update/user). The `members` param is kept for API
        parity with ac-sdk-v2 but callers in this script always omit it.
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

        Also confirmed: this endpoint 500s with "User Can Not Update
        Him/Her Self" if the token's own user tries to update its own
        teamInfo — use a token belonging to someone other than the account
        you're provisioning, or provision other users only.
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

    def get_role_names(self):
        """Valid tenantRole names for this tenant, e.g. ['Admin', 'Developer',
        'Read Only', 'Custom_Developer', ...].

        Each entry from GET /user/roles is a full role object; only the
        "role" field is the name accepted by create_user's tenant_role.
        Custom roles appear here too (typically Custom_*), so the list is
        tenant-specific — never hardcode it.
        """
        resp = self._session.get(f"{self.base_url}/user/roles", timeout=self._timeout)
        resp.raise_for_status()
        return [r["role"] for r in resp.json() if r.get("role")]

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

    def get_sub_products_full(self):
        """Every sub-product WITH repoLink and parent product, in one call.

        This is the endpoint the whole matching model depends on. Unlike
        /user/sub-product/elastic/short (id + name only), the un-suffixed
        /user/sub-product/elastic returns the fields needed to:

          - match a repo by URL          -> repoLink, repoType
          - name the team from its Group -> parentName
          - scope the team               -> parent (product id)

        So the URL->sub-product map AND every team name cost one request
        rather than one per sub-product. On a 20,000 sub-product tenant,
        paced at 0.6s, the per-sub-product alternative would be over three
        hours before any real work started.

        Called with NO query parameters, deliberately. Probing a live tenant
        showed the endpoint has two modes, and only this one is usable:

          - no `pageSize`  -> returns a bare JSON list of EVERY sub-product,
                              ignoring `page`, `size` and `pageNumber`.
          - with `pageSize` -> returns a Spring page envelope, capped at
                              `pageSize <= 100` ... but `page` is IGNORED:
                              page=0 and page=1 both return the first N
                              records. Iterating it would loop on page 0 and
                              silently duplicate rows forever.

        Hence the unpaged form. A page envelope coming back anyway (should
        the API change) is unwrapped rather than mistaken for a list, and
        `content` alone would be a truncated view, so that case warns.

        Note ids come back as STRINGS here but as ints from
        /api/sub-product/{id}. Callers must coerce — matching.SubProductRef
        does.
        """
        resp = self._session.get(
            f"{self.base_url}/user/sub-product/elastic", timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            return data

        # Defensive: the API started paginating this response. Take what we
        # can and say so loudly — a silently truncated sub-product list means
        # repos stop matching, which under reconcile means people lose access.
        content = data.get("content") or []
        total = data.get("totalElements")
        if total is not None and len(content) < total:
            print(f"    [warn] /user/sub-product/elastic returned a paged response "
                  f"({len(content)} of {total}). The sub-product list is INCOMPLETE, "
                  f"so some repos will not match. Do not reconcile from this run.")
        return content


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


def load_default_role(config_path: str, cli_override: str | None,
                      source: str | None = None) -> str:
    """Resolve the ArmorCode tenantRole for newly-created users.

    Precedence: --default-role CLI flag > [<source>] default_role >
    [armorcode] default_role > built-in default ("Developer").

    The per-source section ([github] / [gitlab]) lets one config file give
    each SCM a different role; [armorcode] is the shared fallback for the
    common case where both should match. A missing ini file is not an
    error — it's an optional override, not a requirement.
    """
    if cli_override:
        return cli_override

    p = Path(config_path)
    if p.exists():
        parser = configparser.ConfigParser()
        parser.read(p)
        for section in ([source] if source else []) + ["armorcode"]:
            if parser.has_option(section, "default_role"):
                return parser.get(section, "default_role").strip()

    return DEFAULT_ROLE

class ArmorCodeState:
    """Caches ArmorCode teams/users/sub-products so we don't re-fetch per repo."""

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
        # for why: GitHub topics are lowercase-only, so a topic-derived team
        # name (e.g. "api") must still match an existing team named "API".
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
        """Look up a team by name, case-insensitively.

        GitHub topics are forced lowercase (e.g. "armorcode-team-api"), so
        the team name parsed from a topic must still match an existing
        team named e.g. "API" — exact-match lookup would miss it and
        create a duplicate "api" team instead.
        """
        return self.teams_by_name.get(team_name) or self.teams_by_name.get(team_name.lower())

    def register_team(self, team: dict) -> None:
        """Index a team (new or freshly fetched) under both its exact name
        and lowercased name, so find_team() can match it either way."""
        self.teams_by_name.setdefault(team["name"], team)
        self.teams_by_name.setdefault(team["name"].lower(), team)

    # -- sub-product matching --------------------------------------------

    def find_matching_sub_products(self, repo_name: str) -> list[dict]:
        """Return every AC sub-product whose name matches the repo name
        exactly (case-insensitive). May be 0, 1, or many."""
        return list(self.sub_products_by_name.get(repo_name.strip().lower(), []))

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


def user_label(user_record: dict) -> str:
    """Format a user for logging. Existing tenant users often have
    name == email, so only show the name when it adds something."""
    name, email = user_record.get("name"), user_record.get("email")
    return f"{name} <{email}>" if name and name != email else f"{email}"


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


class LastTeamError(Exception):
    """Raised when removing a team would leave a user with no teams at all.

    PUT /user/update/user rejects an empty teamInfo — a user must belong to
    at least one team — so this case has to be detected and reported rather
    than attempted. The user stays where they are and the operator decides:
    delete the user, or move them to another team first.
    """


def remove_user_from_team(ac: ArmorCodeClient, user_record: dict, team_id: int) -> bool:
    """Remove one user from one team, keeping every other membership.

    The mirror image of add_user_to_team(): membership lives on the USER
    record, and PUT /user/update/user replaces the whole teamInfo list, so
    this GET-merges by filtering rather than assigning.

    Returns True if a removal was written, False if the user was not on the
    team (no-op, no request made). Raises LastTeamError if this is the
    user's only team — the API forbids an empty teamInfo, so the caller must
    surface it instead of silently failing.
    """
    user_id = user_record.get("userId") or user_record["id"]
    team_info = list(user_record.get("teamInfo") or [])

    remaining = [t for t in team_info if t.get("teamId") != team_id]
    if len(remaining) == len(team_info):
        return False  # not a member; nothing to do

    if not remaining:
        raise LastTeamError(
            f"user {user_record.get('email') or user_id} is only on this team; "
            f"ArmorCode requires at least one team membership"
        )

    updated = ac.update_user_team_info(user_id, remaining)
    user_record["teamInfo"] = updated.get("teamInfo", remaining)
    return True
