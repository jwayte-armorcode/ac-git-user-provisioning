"""Configuration for the extract / provision / reconcile pipeline.

One ini file holds everything: the ArmorCode tenant, every SCM, and the
safety thresholds for reconciliation.

    [armorcode]
    url   = xxxx.armorcode.xxx
    token = ...

    [scm.gh-main]
    type  = github
    url   = https://api.github.com
    token = ghp_...

    [scm.gitlab-prod]
    type  = gitlab
    url   = https://gitlab.example.com
    token = glpat-...

    [reconcile]
    max_removal_pct   = 25
    max_removal_floor = 5
    max_tripped_teams = 3

    [snapshots]
    dir                     = snapshots
    snapshot_retention_days = 90

Why ini rather than the old KEY=VALUE format: an arbitrary number of SCMs
each need their own type/url/token, and flat keys can't express repetition
without inventing a naming convention (GITHUB_PAT_2, ...) that the code
then has to parse anyway.

The text after "scm." is the MNEMONIC. It names the SCM on the command
line (--scm gh-main) and is also the directory its extract is written to,
which is why it's validated as a filesystem-safe token rather than taken
on trust.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from pathlib import Path

SCM_SECTION_PREFIX = "scm."
VALID_SCM_TYPES = ("github", "gitlab")

# Mnemonics become directory names, so they're restricted to a safe token
# rather than sanitised. Silently rewriting "a/b" to "a_b" would make two
# distinct config sections collide on one output directory.
MNEMONIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Reconciliation safety thresholds. See ReconcileLimits for what each does.
DEFAULT_MAX_REMOVAL_PCT = 25
DEFAULT_MAX_REMOVAL_FLOOR = 5
DEFAULT_MAX_TRIPPED_TEAMS = 3

DEFAULT_SNAPSHOT_DIR = "snapshots"
DEFAULT_SNAPSHOT_RETENTION_DAYS = 90

DEFAULT_ROLE = "Developer"


class ConfigError(Exception):
    """Raised for a malformed or incomplete config file.

    Callers print this and exit non-zero; it always carries a message that
    names the offending section or key, since the whole point is to tell an
    operator what to fix.
    """


@dataclass(frozen=True)
class ScmConfig:
    """One source-control system to extract from."""

    mnemonic: str
    type: str          # "github" | "gitlab"
    url: str
    token: str

    @property
    def output_dir(self) -> Path:
        """Where this SCM's extract is written. Named for the mnemonic."""
        return Path(self.mnemonic)


@dataclass(frozen=True)
class ReconcileLimits:
    """Circuit breaker for the weekly reconcile job.

    The strict-mirror rule ("remove anyone not in a repo under this Group")
    is only as good as the extract feeding it. A refuse-on-missing-extract
    check catches an SCM that failed LOUDLY; these thresholds catch one that
    succeeded with bad data — e.g. a token that quietly loses group-read
    permission still authenticates and still lists repos, but reports zero
    members, which the strict-mirror rule faithfully turns into "remove
    everyone".

    max_removal_pct   per-team ceiling, as a percentage of current members
    max_removal_floor never trip below this many removals, so a 3-person
                      team losing 1 (33%) isn't flagged as a mass removal
    max_tripped_teams more teams than this tripping the per-team tier aborts
                      the whole run — one team over the line is plausible
                      attrition, several at once is a bad extract
    """

    max_removal_pct: int = DEFAULT_MAX_REMOVAL_PCT
    max_removal_floor: int = DEFAULT_MAX_REMOVAL_FLOOR
    max_tripped_teams: int = DEFAULT_MAX_TRIPPED_TEAMS

    def trips(self, removing: int, current_total: int) -> bool:
        """Whether removing `removing` of `current_total` members trips the
        per-team tier.

        Both conditions must hold: over the percentage AND over the absolute
        floor. Percentage alone cries wolf on small teams; an absolute count
        alone would wave through a 90% removal from a large one.
        """
        if removing <= self.max_removal_floor:
            return False
        if current_total <= 0:
            return False
        return (removing / current_total) * 100 > self.max_removal_pct


@dataclass(frozen=True)
class SnapshotConfig:
    """Where pre-write snapshots live, and how long they're kept."""

    dir: str = DEFAULT_SNAPSHOT_DIR
    retention_days: int = DEFAULT_SNAPSHOT_RETENTION_DAYS

    @property
    def path(self) -> Path:
        return Path(self.dir)


@dataclass(frozen=True)
class Config:
    """The whole configuration."""

    armorcode_url: str
    armorcode_token: str
    scms: dict[str, ScmConfig] = field(default_factory=dict)
    reconcile: ReconcileLimits = field(default_factory=ReconcileLimits)
    snapshots: SnapshotConfig = field(default_factory=SnapshotConfig)
    default_role: str = DEFAULT_ROLE

    def scm(self, mnemonic: str) -> ScmConfig:
        """Look up one SCM, failing with the valid list rather than a KeyError."""
        try:
            return self.scms[mnemonic]
        except KeyError:
            known = ", ".join(sorted(self.scms)) or "(none configured)"
            raise ConfigError(
                f"unknown SCM {mnemonic!r}. Configured: {known}"
            ) from None

    def select_scms(self, selector: str | None) -> list[ScmConfig]:
        """Resolve a --scm value to SCM configs.

        None or "all" means every configured SCM, in sorted order so runs
        are deterministic.
        """
        if selector is None or selector == "all":
            return [self.scms[m] for m in sorted(self.scms)]
        return [self.scm(selector)]


def _clean(value: str | None) -> str:
    """Strip whitespace and the surrounding quotes people habitually add."""
    if value is None:
        return ""
    return value.strip().strip('"').strip("'")


def _get_int(parser: configparser.ConfigParser, section: str, key: str,
             default: int, *, minimum: int | None = None) -> int:
    """Read an int setting, defaulting when absent and erroring when invalid.

    An absent key is fine — every threshold has a working default, so an
    older or minimal config file keeps running. A PRESENT but unparseable
    key is an error rather than a silent fallback: someone wrote
    max_removal_pct = fifty intending a limit, and quietly substituting 25
    would apply a safety margin they didn't ask for.
    """
    if not parser.has_option(section, key):
        return default
    raw = _clean(parser.get(section, key))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"[{section}] {key} = {raw!r} is not a whole number"
        ) from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"[{section}] {key} must be >= {minimum}, got {value}")
    return value


def _normalise_tenant_url(raw: str) -> str:
    """Reduce a tenant URL to a bare host.

    The client builds https://<host> itself, so a scheme here would produce
    https://https://... — accept either form and store the host.
    """
    url = _clean(raw)
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    return url.rstrip("/")


def _parse_scm_section(parser: configparser.ConfigParser, section: str) -> ScmConfig:
    """Build one ScmConfig, validating the mnemonic and type."""
    mnemonic = section[len(SCM_SECTION_PREFIX):].strip()

    if not MNEMONIC_RE.match(mnemonic):
        raise ConfigError(
            f"[{section}]: mnemonic {mnemonic!r} is not usable as a directory "
            f"name. Use letters, digits, dot, dash or underscore (max 64), "
            f"starting with a letter or digit."
        )

    scm_type = _clean(parser.get(section, "type", fallback="")).lower()
    if scm_type not in VALID_SCM_TYPES:
        raise ConfigError(
            f"[{section}]: type must be one of {', '.join(VALID_SCM_TYPES)}, "
            f"got {scm_type or '(missing)'!r}"
        )

    token = _clean(parser.get(section, "token", fallback=""))
    if not token:
        raise ConfigError(f"[{section}]: token is required")

    url = _clean(parser.get(section, "url", fallback=""))
    if not url:
        # Sensible public defaults; a self-hosted instance must say so.
        url = ("https://api.github.com" if scm_type == "github"
               else "https://gitlab.com")

    return ScmConfig(mnemonic=mnemonic, type=scm_type, url=url.rstrip("/"),
                     token=token)


def load_config(path: str) -> Config:
    """Read and validate the ini file.

    Raises ConfigError with an operator-readable message on anything wrong;
    never returns a partially-valid Config.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"config file not found: {path} (copy env.example and fill it in)"
        )

    parser = configparser.ConfigParser()
    # Keep key case as written; values are what matter and this avoids
    # surprises if a future key is case-sensitive.
    parser.optionxform = str
    try:
        parser.read(p)
    except configparser.Error as e:
        raise ConfigError(f"could not parse {path}: {e}") from None

    if not parser.has_section("armorcode"):
        raise ConfigError(f"{path} has no [armorcode] section")

    # No default tenant: an unset URL must fail loudly rather than silently
    # targeting somewhere the operator didn't intend.
    url = _normalise_tenant_url(parser.get("armorcode", "url", fallback=""))
    if not url:
        raise ConfigError(
            "[armorcode] url is required, e.g. url = xxxx.armorcode.xxx"
        )
    token = _clean(parser.get("armorcode", "token", fallback=""))
    if not token:
        raise ConfigError("[armorcode] token is required")

    default_role = _clean(
        parser.get("armorcode", "default_role", fallback="")) or DEFAULT_ROLE

    scms: dict[str, ScmConfig] = {}
    for section in parser.sections():
        if not section.startswith(SCM_SECTION_PREFIX):
            continue
        scm = _parse_scm_section(parser, section)
        if scm.mnemonic in scms:
            raise ConfigError(f"duplicate SCM mnemonic {scm.mnemonic!r}")
        scms[scm.mnemonic] = scm

    if not scms:
        raise ConfigError(
            f"{path} configures no SCMs — add at least one "
            f"[{SCM_SECTION_PREFIX}<mnemonic>] section"
        )

    limits = ReconcileLimits(
        max_removal_pct=_get_int(parser, "reconcile", "max_removal_pct",
                                 DEFAULT_MAX_REMOVAL_PCT, minimum=0),
        max_removal_floor=_get_int(parser, "reconcile", "max_removal_floor",
                                   DEFAULT_MAX_REMOVAL_FLOOR, minimum=0),
        max_tripped_teams=_get_int(parser, "reconcile", "max_tripped_teams",
                                   DEFAULT_MAX_TRIPPED_TEAMS, minimum=0),
    )

    snapshots = SnapshotConfig(
        dir=_clean(parser.get("snapshots", "dir",
                              fallback=DEFAULT_SNAPSHOT_DIR)) or DEFAULT_SNAPSHOT_DIR,
        retention_days=_get_int(parser, "snapshots", "snapshot_retention_days",
                                DEFAULT_SNAPSHOT_RETENTION_DAYS, minimum=0),
    )

    return Config(armorcode_url=url, armorcode_token=token, scms=scms,
                  reconcile=limits, snapshots=snapshots,
                  default_role=default_role)
