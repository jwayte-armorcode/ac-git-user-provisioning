# ac-git-user-provisioning

Provisions ArmorCode **teams**, **users** and **product/sub-product scope** from repo membership across any number of GitHub and GitLab instances — and, weekly, removes people from teams once they're no longer in any of the group's repos.

Team names come from **ArmorCode itself**, not from source control. A repo is matched to an ArmorCode sub-product by repository URL; the **Group** that sub-product belongs to becomes the team:

```
repo (from any SCM)                    ArmorCode
  https://github.com/acme/api   ──►    sub-product  "api"      (repoLink matches)
                                       └─ Group     "payments" ──►  team "payments"
  repo members ──────────────────────────────────────────────────►  team members
```

Nothing in the SCM decides team naming — no topics, no labels, no conventions to maintain.

## Contents

- [Commands](#commands)
- [Quick start](#quick-start)
- [How matching works](#how-matching-works)
- [Configuration](#configuration)
- [The extract contract](#the-extract-contract)
- [Reconciliation](#reconciliation)
- [The circuit breaker](#the-circuit-breaker)
- [Snapshots and restore](#snapshots-and-restore)
- [ArmorCode API notes](#armorcode-api-notes)
- [Reference](#reference)

## Commands

| Command | Scope | Writes? | Purpose |
|---|---|---|---|
| `extract.py` | per-SCM (`--scm all\|<mnemonic>`) | local files only | Read repos + members from source control |
| `provision.py` | **all SCMs** | ArmorCode (additive) | Create users, create/scope teams, add members |
| `reconcile.py` | **all SCMs** | ArmorCode (**removes**) | Remove members no longer in any of the group's repos |
| `restore.py` | — | ArmorCode (additive) | Rebuild memberships from a snapshot |
| `run_all.py` | all SCMs | via the above | `extract` + `provision`, for interactive use |

Dry run is the default everywhere; `--apply` is required to write.

**`provision` and `reconcile` have no `--scm` flag, deliberately.** A Group can hold sub-products whose repos live in *different* SCMs, so a team's membership is the union across all of them. Running against one SCM would compute a partial member set and write it as though complete. Both commands therefore refuse to start unless every configured SCM has a complete extract.

## Quick start

```bash
pip install -r requirements.txt
cp env.example envfile        # then fill in the tokens; envfile is gitignored

# 1. Read source control. Writes ./<mnemonic>/repos.json, no ArmorCode calls.
python extract.py --scm all

# 2. See what would happen. Nothing is written.
python provision.py --dump-json

# 3. Write it — start small
python provision.py --apply --limit 5
python provision.py --apply

# 4. Later: see who would be removed. Still nothing written.
python reconcile.py

# 5. Remove them
python reconcile.py --apply
```

For the weekly job, chain the steps so a failed extract stops the pipeline **before** anything is removed:

```bash
python extract.py --scm all && python reconcile.py --apply
```

## How matching works

A repo's team is derived entirely inside ArmorCode:

1. The repo's web URL is matched against every sub-product's **Repository URL** (`repoLink` in the API).
2. The matched sub-product's **Group** (`parentName`) becomes the team name.
3. The sub-product is added to that team's scope.
4. The repo's members become team members.

All of it costs **one API call** — `GET /user/sub-product/elastic` returns `repoLink`, the parent product id and the Group name for every sub-product at once.

### URL normalisation

The two sides describe the same repo but rarely match byte-for-byte, so both are reduced to a canonical `host/path` key. All of these match:

```
https://github.com/Acme/Repo
https://github.com/acme/repo.git
git@github.com:acme/repo.git
https://gitlab.example.com:8443/group/sub/repo/
https://www.github.com/acme/repo
```

Scheme, `www.`, userinfo, port, trailing slash and `.git` are all stripped. Host is lowercased; **path case is preserved**, because GitLab paths are case-sensitive while GitHub's are not. A repo that matches only after case-folding is still matched, but reported — on GitLab that could mean two genuinely different projects.

### Repos that match nothing

**A repo with no matching sub-product provisions nothing at all** — no team, no users. Without a sub-product there is no Group, and without a Group there is no team name.

This is common rather than exceptional: in practice only sub-products created by ArmorCode's auto-onboarding runbook have `repoLink` populated. Hand-created ones usually don't. Use `--unmatched-csv` to get the list:

```csv
scm,repo,url,reason
gh-main,acme/orphan,https://github.com/acme/orphan,no sub-product with this repoLink
```

The fix is to set the Repository URL on the sub-product in ArmorCode, then re-run. **The tool never creates sub-products.**

If **more than half** of all repos match nothing, `provision.py` aborts:

```
[error] 940 of 1000 repo(s) (94%) matched no sub-product.
        That usually means the repoLink format differs from the SCM's web URL,
        not that most repos are genuinely un-onboarded.
```

At that rate the likely cause is a URL format the normaliser doesn't handle, not 940 onboarding gaps — and provisioning would create a handful of teams while silently ignoring the estate. `--force` overrides once you've confirmed the gaps are real.

### Team names

The Group name is used as-is, with one transformation: **angle brackets are stripped**, because `POST /api/team` rejects them. ArmorCode uses `<...>` for system-generated placeholder Groups, so `<DEFAULT>` becomes team `DEFAULT`. The mapping is logged whenever it changes a name.

Team lookup is case-insensitive, so a Group named `api` adopts an existing team `API` rather than creating a near-duplicate.

### Members

| | GitHub | GitLab |
|---|---|---|
| Members | Repo collaborators; falls back to the authors of the last 200 commits if the collaborators endpoint 403s | `members_all` (direct **plus** inherited group members), access level ≥ 30 (Reporter) |
| Email | Public profile email only | Member record email, else a profile fetch |

ArmorCode identifies users by email, so **a member whose email the token can't see cannot be provisioned.** They're written to `email_exceptions.csv` with a blank email column rather than dropped. Fill it in by hand and they'll be picked up on the next run.

Emails are resolved once per person per run and cached — including misses, since members with no visible email are the common case and recur on every repo they touch.

## Configuration

One ini file (`envfile` by default) holds the tenant, every SCM, and the safety thresholds:

```ini
[armorcode]
url   = xxxx.armorcode.xxx        # host only, no scheme; no default
token = ...
default_role = Developer          # role for users this tool creates

[scm.gh-main]
type  = github
token = ghp_...                   # needs repo, read:org, read:user

[scm.gitlab-prod]
type  = gitlab
url   = https://gitlab.example.com
token = glpat-...                 # needs read_api, read_user

[reconcile]
max_removal_pct   = 25
max_removal_floor = 5
max_tripped_teams = 3

[snapshots]
dir                     = snapshots
snapshot_retention_days = 90
```

Add as many `[scm.<mnemonic>]` sections as you need — two GitHub orgs, a self-hosted GitLab, a staging instance. The **mnemonic** names the SCM on the command line *and* is the directory its extract is written to, so it's validated as a filesystem-safe token (`a/b` and `..` are rejected rather than sanitised, since silently rewriting them would let two sections collide on one directory).

`url` is required and has no default: an unset value is a hard error, never a run against an unintended tenant. `default_role` is validated against the tenant at startup, so a typo aborts immediately with the valid list instead of failing per-user deep into a run.

## The extract contract

Each SCM writes its own directory:

```
gh-main/
    repos.json          repos, URLs, members split by whether an email resolved
    extract_meta.json   status, timings, counts, partial flag, errors
```

`extract_meta.json` is the contract between extract and everything downstream. Its status is one of:

| Status | Meaning | Usable downstream? |
|---|---|---|
| `running` | Started, never finished — a crashed run | No |
| `complete` | Saw every repo the token can list | **Yes** |
| `partial` | Deliberately truncated (`--limit`/`--repo`/`--changed-since`) | No |
| `failed` | Aborted with an error | No |

**`partial` is a first-class status, not a warning.** This is the crux of the whole design:

> Under strict-mirror reconciliation, *"this SCM returned no members"* and *"this SCM was never read"* produce **identical input** — and acting on that difference removes real people's access.

So `--limit 2` on an extract marks it partial, and `provision`/`reconcile` refuse it:

```
[error] not every SCM has a usable extract:
    - gh-main: extract is PARTIAL (--limit 2) — it saw only 2 repo(s), so a
      missing member proves nothing. Re-run the extract without limits
```

`extract.py` also exits non-zero if any SCM didn't complete, which is what makes `extract.py && reconcile.py` safe as a cron line.

### What `--limit` means per command

The same flag, but limiting *input* and limiting *output* are very different operations:

| Command | `--limit` caps | Safe? |
|---|---|---|
| `extract.py` | repos read per SCM | Marks the extract **partial**; downstream refuses it |
| `provision.py` | **teams written** | Safe — each team still gets its full membership |
| `reconcile.py` | **teams reconciled** | Safe — each team's desired set still comes from every extract |

Limiting output is the right blast-radius control for a first `--apply`: every team touched is fully correct, and the rest are simply left for a later run. Teams are processed in sorted order, so the same slice is taken each time, and what was skipped is always reported:

```
[limit] processed 5 of 213 team(s) (--limit 5); 208 left untouched
```

## Reconciliation

`reconcile.py` is the weekly job and **the only command that removes access.**

The rule is a **strict mirror**: for each Group with matching repos, team membership should be exactly the union of those repos' members. Anyone else is removed — *including someone added by hand in the ArmorCode UI*. That was a deliberate choice: it makes membership derived and predictable rather than an accumulation of whatever anyone ever did.

Three things are never touched:

- **Teams with no matching repos.** Absence of a Group from the extract is not evidence its team should be emptied — it usually just means no repo carries that sub-product's URL. Only teams the tool can positively account for are reconciled.
- **Members with no resolvable email.** They're invisible to the extract by definition, so treating them as departures would remove exactly the people the `email_exceptions.csv` workflow exists to onboard.
- **A user's last team.** ArmorCode rejects an empty `teamInfo`, so this is reported for a human rather than worked around:
  ```
  [keep] sam@acme.com: this is their only team — ArmorCode requires at least
         one. Remove the user, or add them to another team first.
  ```

Every run writes `reconcile_report.csv` recording every intent — including what the circuit breaker stopped — so a dry run is a complete answer to "what would this do".

## The circuit breaker

The strict-mirror rule is only as good as the extract feeding it. Refusing an incomplete extract catches an SCM that failed **loudly**; the circuit breaker catches one that succeeded with **bad data** — the failure mode that actually bites an unattended weekly job.

The scenario: a GitLab token silently loses group-read permission. It still authenticates, still lists projects, but returns zero members. The extract completes cleanly — fresh timestamp, non-partial, no errors. Reconcile then computes, correctly per the rule:

```
desired = {}                       # the extract says nobody is in any repo
actual  = {alice, bob, ...}        # 30 real members
remove  = actual - desired         # all 30
```

Every individual decision is right; the input was wrong. Unattended, over a weekend, that empties every GitLab-derived team.

Two tiers, both checked **before any write**:

| Tier | Trips when | Effect |
|---|---|---|
| Per-team | removals > `max_removal_pct` of members **and** > `max_removal_floor` people | Skip that team, continue |
| Whole-run | more than `max_tripped_teams` teams trip | **Abort everything**, exit non-zero |

```
  [skip] payments: would remove 28 of 30 member(s) (93%)

  A team losing most of its members usually means an incomplete
  extract rather than that many people leaving at once.

[abort] 7 team(s) tripped the limit, more than the 3 allowed.
        That pattern points at bad input, not real departures.
        NOTHING WAS REMOVED. Review reconcile_report.csv, then re-run with
        --force-removals if the removals are genuinely correct.
```

The absolute floor matters: a 3-person team losing 1 member is 33% but entirely normal, so percentage alone would cry wolf on small teams. The whole-run tier exists because one team over the line is plausible attrition, while several at once means the input is broken.

### Two force flags, not one

| Flag | Bypasses | Does **not** bypass |
|---|---|---|
| `--force-extracts` | The unusable-extract check | The circuit breaker |
| `--force-removals` | The circuit breaker | The extract checks |
| `--force` | Both | — |

These are separate for a concrete reason found during testing. A tenant whose sub-products mostly lack a Repository URL needs to force past the match guard on *every* run — and with a single combined flag that would silently disable mass-removal protection for exactly the tenant most likely to mis-match repos.

**A `partial` extract cannot be reconciled from at all**, with or without `--force`. There is no override, because truncation makes every unseen member look like a departure.

## Snapshots and restore

The circuit breaker stops the removals we can predict. Snapshots cover the ones we can't — a bug, a mistaken `--force`, an API change.

**Every `--apply` run captures the tenant's teams and memberships before its first write:**

```
snapshots/2026-08-05T02-47-49Z/
    teams.json    id, name, scope, members (with role)
    users.json    userId, email, name, tenantRole, teamInfo (with role)
    meta.json     tenant, timestamp, command, counts, consistency check
```

Two things make this a real backup rather than a comforting file:

- **Per-team role is captured.** A user's role varies *per team* — one live tenant has someone who is `Read Only` on one team and `Developer` on another. Restoring without it would silently change access levels: a security change disguised as a recovery.
- **Membership is captured from both directions** — the user's `teamInfo` and the team's `members` list — and compared. Any disagreement is recorded in `meta.json`, because a snapshot that isn't self-consistent shouldn't be trusted blindly.

To undo:

```bash
python restore.py --list                 # what's available
python restore.py                        # dry run against the latest
python restore.py --apply
python restore.py --teams payments --apply
```

Restore is **additive**: it puts back memberships the snapshot had and that are now missing, with their original roles, and never removes anything added since. Undoing one bad run shouldn't be able to cause a second. It refuses a snapshot from a different tenant — team ids are per-tenant, so that would write memberships to arbitrary unrelated teams.

Pruning honours `snapshot_retention_days` but **never deletes the newest snapshot**, so a rarely-provisioned tenant can't expire its only backup. Snapshots contain emails and are gitignored.

## ArmorCode API notes

Found by probing a live tenant. These are why `armorcode.py` looks more defensive than a thin HTTP wrapper needs to.

**`/user/sub-product/elastic` must be called unpaged.** With no `pageSize` it returns every sub-product as a bare list. With `pageSize` it returns a Spring page envelope — but caps at 100 *and ignores `page`*, so `page=0` and `page=1` return the same records. A paging loop would spin on page 0 and duplicate rows forever. The `/short` variant returns only `id` and `name`, with no `repoLink`.

**GET and PUT disagree about team scope.** The read shape nests `businessUnit`/`product` as objects and uses `subProducts` (plural, objects); the write shape wants flat ids and `subProduct` (singular). Sending the read shape back is a 400.

**Team membership lives on the user record.** `PUT /user/update/user` replaces the entire `teamInfo` list, so both adding and removing GET-merge. It rejects an empty list, and 500s with *"User Can Not Update Him/Her Self"* if the token's own user is the target.

**Team names reject angle brackets.** `POST /api/team` returns `400 "name Name should be alphanumeric"` for any name containing `<` or `>`. The message overstates it — hyphen, space, underscore, dot and slash are all accepted. It's an HTML/injection filter, not a charset rule.

**Ids change type between endpoints.** `id` is a **string** from `/user/sub-product/elastic` but an **int** from `/api/sub-product/{id}`. Scope comparison is numeric, so ids are coerced on the way in.

**Rate limits.** The binding constraint is 100 RPM *per endpoint*, not the 2,000 RPM token budget — a long run hammers the same few endpoints. Requests are paced per endpoint at one every 0.6s, bucketed by URL path with numeric ids collapsed. A 429 is waited out and retried indefinitely (limits are transient by definition, bounded by a one-hour safety net); a 5xx keeps a bounded retry count, since a 500 can be permanent.

## Reference

### Files

| File | Role |
|---|---|
| `extract.py` | Per-SCM extract entry point |
| `provision.py` | Global provision entry point |
| `reconcile.py` | Global strict-mirror removal entry point |
| `restore.py` | Rebuild memberships from a snapshot |
| `run_all.py` | `extract` + `provision` convenience wrapper |
| `config.py` | Ini config: tenant, SCM registry, thresholds |
| `scm_readers.py` | `GitHubTeamReader` / `GitLabTeamReader` behind one interface |
| `matching.py` | URL normalisation, sub-product index, team-name sanitising |
| `model.py` | Aggregate repos into users and teams (pure, no I/O) |
| `armorcode.py` | ArmorCode REST client, cached tenant state, merge helpers |
| `extract_store.py` | Per-SCM extract directories and the usability contract |
| `snapshot.py` | Pre-write capture, load, prune |
| `email_exceptions.py` | The no-resolvable-email CSV |

Generated files, all gitignored: `<mnemonic>/repos.json`, `<mnemonic>/extract_meta.json`, `snapshots/`, `email_exceptions.csv`, `unmatched_repos.csv`, `reconcile_report.csv`, `provision_plan.json`.

`armorcode.py` is an inlined subset of [ac-sdk-v2](https://github.com/jwayte-armorcode/ac-sdk-v2)'s client, kept local so this tool has no dependency on the SDK package. If you need another endpoint, port the method across rather than reintroducing that dependency.

### Safety summary

- Dry run by default on every command; `--apply` required to write.
- `provision` and `reconcile` refuse to run unless **every** SCM has a complete extract.
- A `partial` extract can never be reconciled from — no override.
- Snapshot taken before the first write of any `--apply` run; pruning never removes the newest.
- Two-tier circuit breaker on removals, with separate force flags so forcing past a match problem doesn't disable removal protection.
- `provision` and `restore` are additive: they never remove scope or membership. Every write is a GET-merge.
- Members without a resolvable email are logged, never dropped — and never removed by reconcile.
- A user's last team is never removed (the API forbids it); reported instead.
- Sub-products are never created; unmatched repos are reported and skipped.
- `url` has no default — an unset tenant is a hard error.
- `default_role` is validated against the tenant before anything is read or written.
- Requests are paced under the per-endpoint rate limit; 429s are waited out.

### Known limitations

- **Concurrent `provision` runs could double-create a team.** The team list is cached at run start, so two simultaneous runs can each decide a team is missing. Run one at a time.
- **`--changed-since` marks an extract partial**, so it can't feed a reconcile. Repo timestamps don't move when a member publishes an email or gains access via a group, so a filtered extract is not a valid basis for removal.
- **Teams with no backing Group are left alone entirely** — including any pre-existing teams from before this model. They're never reconciled, since there's nothing to mirror them against.
