"""Match SCM repos to ArmorCode sub-products on repository URL.

This is the join that the whole tool now turns on: a repo's team name comes
from the ArmorCode Group (`parentName`) of the sub-product whose `repoLink`
points at the same repository.

    repo.web_url  ==  sub_product.repoLink   ->  sub_product.parentName == team

The two URLs describe the same repository but rarely match byte-for-byte.
Observed in a live tenant and across both SCMs:

    https://github.com/Acme/Repo
    https://github.com/acme/repo.git
    git@github.com:acme/repo.git
    https://gitlab.example.com:8443/group/sub/repo/
    https://www.github.com/acme/repo

All of these are the same repo. So both sides are reduced to a canonical
"host/path" key before comparison.

Case handling is deliberately asymmetric, because the platforms are:

  - HOST is case-insensitive (DNS), so it's lowercased.
  - PATH is case-sensitive on GitLab — `Group/Repo` and `group/repo` can be
    two different projects — but case-insensitive on GitHub, which will
    redirect `/Acme/Repo` to `/acme/repo`.

Rather than pick one and be wrong half the time, each URL produces a primary
key (case preserved) and a folded key (lowercased). Matching tries exact
first and falls back to the folded form, reporting when only the fold
matched so a genuine GitLab case collision is visible rather than silent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# scp-style SSH remotes: git@host:owner/repo.git — not a URL urlsplit can
# parse, so it's converted to host/path before anything else runs.
_SSH_RE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")

# A scheme like "https://" or "ssh://". Used to tell a real URL from an
# scp-style remote, since "git@host:path" also contains a colon.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def normalise_repo_url(raw: str | None) -> str:
    """Reduce a repository URL to a canonical "host/path" key.

    Returns "" for anything unusable (None, blank, or a URL with no host or
    no path), which callers treat as "cannot match" rather than as a key —
    otherwise every unparseable URL would collide on the same empty key and
    match each other.

    >>> normalise_repo_url("https://github.com/Acme/Repo.git")
    'github.com/Acme/Repo'
    >>> normalise_repo_url("git@github.com:acme/repo.git")
    'github.com/acme/repo'
    >>> normalise_repo_url("https://www.gitlab.com:8443/g/s/r/")
    'gitlab.com/g/s/r'
    """
    if not raw:
        return ""
    url = raw.strip()
    if not url:
        return ""

    # scp-style SSH (git@host:owner/repo) has no scheme and can't be parsed
    # as a URL. Detect it by "has a colon but no scheme" and rewrite it.
    if not _SCHEME_RE.match(url):
        m = _SSH_RE.match(url)
        if m:
            url = f"//{m.group('host')}/{m.group('path')}"
        elif "/" in url:
            # Schemeless but slash-separated, e.g. "github.com/acme/repo".
            url = f"//{url}"
        else:
            return ""

    try:
        parts = urlsplit(url)
    except ValueError:
        return ""

    host = (parts.hostname or "").lower()
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]

    # Port is dropped: the same repo reached on :443 and :8443 is one repo,
    # and ArmorCode's repoLink and the SCM's web URL routinely disagree here.
    path = parts.path or ""
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    if not path:
        return ""

    return f"{host}/{path}"


def fold(key: str) -> str:
    """Case-folded form of a normalised key, for the fallback match."""
    return key.lower()


@dataclass(frozen=True)
class SubProductRef:
    """The bits of an ArmorCode sub-product this tool actually uses.

    Built from GET /user/sub-product/elastic, which returns every
    sub-product in one call including repoLink and the parent Group — so
    neither the URL map nor the team name costs a per-sub-product request.

    `id` and `product_id` are coerced to int on construction: the elastic
    endpoint returns them as STRINGS while /api/sub-product returns ints,
    and the team scope merge compares them numerically.
    """

    id: int
    name: str
    repo_link: str
    repo_type: str | None
    product_id: int | None
    group_name: str | None      # parentName — becomes the team name

    @classmethod
    def from_elastic(cls, raw: dict) -> "SubProductRef":
        def as_int(v):
            if v is None or v == "":
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        return cls(
            id=as_int(raw.get("id")),
            name=raw.get("name") or "",
            repo_link=raw.get("repoLink") or "",
            repo_type=raw.get("repoType"),
            product_id=as_int(raw.get("parent")),
            group_name=raw.get("parentName"),
        )


class SubProductIndex:
    """URL -> sub-product lookup, built once from the bulk elastic response.

    Sub-products with no repoLink are counted but not indexed: they can
    never match a repo, and in a real tenant they are the majority (29 of 36
    in the sandbox probed while designing this), so silently dropping them
    would misrepresent the match rate.
    """

    def __init__(self, raw_sub_products: list[dict]):
        self.total = len(raw_sub_products)
        self.without_link = 0
        self._exact: dict[str, list[SubProductRef]] = {}
        self._folded: dict[str, list[SubProductRef]] = {}

        for raw in raw_sub_products:
            ref = SubProductRef.from_elastic(raw)
            if ref.id is None:
                continue
            key = normalise_repo_url(ref.repo_link)
            if not key:
                self.without_link += 1
                continue
            self._exact.setdefault(key, []).append(ref)
            self._folded.setdefault(fold(key), []).append(ref)

    @property
    def indexed(self) -> int:
        """How many distinct repo URLs are matchable."""
        return len(self._exact)

    def lookup(self, repo_url: str) -> tuple[list[SubProductRef], bool]:
        """Find sub-products for a repo URL.

        Returns (matches, folded_only). `folded_only` is True when the URL
        matched only after case-folding, which is worth reporting: on GitLab
        that could mean two genuinely different projects, whereas on GitHub
        it's expected and harmless.

        A list is returned because two sub-products may legitimately carry
        the same repoLink, in which case the repo belongs to both Groups and
        contributes members to both teams.
        """
        key = normalise_repo_url(repo_url)
        if not key:
            return [], False
        exact = self._exact.get(key)
        if exact:
            return list(exact), False
        folded = self._folded.get(fold(key))
        if folded:
            return list(folded), True
        return [], False


# Team names reject angle brackets: POST /api/team returns
# 400 "name Name should be alphanumeric" for any name containing < or >.
# Confirmed by probing a live tenant — hyphen, space, underscore, dot and
# slash are all accepted, so this is an HTML/injection filter rather than a
# real charset rule. ArmorCode uses <...> for system-generated placeholder
# Groups such as <DEFAULT>, which is exactly what this has to survive.
_ANGLE_RE = re.compile(r"[<>]")
_WS_RE = re.compile(r"\s+")


def team_name_for_group(group_name: str | None) -> str:
    """Derive a usable ArmorCode team name from a Group (product) name.

    Strips angle brackets and collapses whitespace: "<DEFAULT>" -> "DEFAULT".
    Returns "" if nothing usable remains, which callers must skip and report
    rather than send to the API.
    """
    if not group_name:
        return ""
    name = _ANGLE_RE.sub("", group_name)
    name = _WS_RE.sub(" ", name).strip()
    return name
