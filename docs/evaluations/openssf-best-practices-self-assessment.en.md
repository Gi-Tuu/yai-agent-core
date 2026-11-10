> English translation of [中文](openssf-best-practices-self-assessment.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# OpenSSF Best Practices Badge (formerly CII) Self-Assessment Checklist — YAI Agent Core

**English** · [简体中文](openssf-best-practices-self-assessment.md)

> Assessed against the official OpenSSF Best Practices **Passing**-level criteria (https://www.bestpractices.dev/en/criteria/passing )
> Item by item for this repository's current main (**v0.27.1**, reviewed 2026-11-05). Status: **Met (satisfied) / Unmet (with reason) / N/A (with reason)**.
> Review highlights are in "8. Review Record (2026-10-25)" at the end; the in-repository text and the **platform record** were both updated on
> 2026-10-25 (the platform side was submitted after logging in with the project owner's own GitHub account — see Section 8).
>
> **How to amend an existing self-assessment (web UI, about 5 minutes)**
> 1. Open https://www.bestpractices.dev and log in at the top right with a **GitHub account (OAuth)** (it must be the project owner's account);
> 2. Go to the project page https://www.bestpractices.dev/projects/15360 and click **Edit** → choose the **Passing** section
>    (direct link: `/en/projects/15360/passing/edit`);
> 3. Change the status / explanation item by item per this table; entries marked `{Met URL}` require pasting the GitHub web URL of the corresponding file;
> 4. Click **Submit (and exit)** — the whole page is submitted together, and **unchanged entries keep their original values**;
> 5. After submitting, verify the public JSON at `https://www.bestpractices.dev/projects/15360.json`
>    for `*_status` / `*_justification` / `updated_at`, not just the page.
>
> **Result: the self-assessment was accepted on 2026-10-10 and the Passing badge was obtained** (project [#15360](https://www.bestpractices.dev/projects/15360)); all six areas — Basics/Change Control/Reporting/Quality/Security/Analysis — are satisfied; the English front door has been completed and the `english` item was changed to Met; only 2 SUGGESTED items (dedicated dynamic-analysis fuzzer/DAST) are Unmet, which does not block Passing.
>
> **2026-10-25 review**: badge status unchanged (Passing). The repository-side evidence has been updated — the test count grew from the then ~1,584 items to
> **2,075 items (2,070 passed + 5 skipped)**, line coverage **95.6%**; the two previously Unmet dynamic-analysis
> SUGGESTED items **now have usable tooling** (atheris fuzzing with 4 targets + a CI `fuzz` job, see the
> `dynamic_analysis*` rows in Section 5), and this checklist was therefore re-judged as Met.
> **Platform side synced**: submitted on 2026-10-25 13:58 UTC after the project owner logged in personally; five updates (`maintained`'s version string,
> `version_tags`'s example tags, `test_most`'s coverage, and the two `dynamic_analysis*` items changed from Unmet to Met);
> reading back the public JSON confirmed `badge_level=passing`, 100% Passing completion, and that `updated_at` had advanced.
>
> **2026-11-05 review**: badge status unchanged (Passing). Test count **2,386 items** (full offline pass, 5 skipped for environment reasons),
> kernel line coverage **93.12%** (CI gate 90); current version v0.27.1. No status change this round (judgements identical to 2026-10-25), and the
> platform record was not resubmitted (to sync the latest numbers to the platform, follow the web steps at the top of this document).

Status legend: ✅ Met · ⚠️ Unmet (reason required) · ➖ N/A (reason required)

---

## 1. Basics

| ID | Requirement (level) | Status | Evidence / what to fill in |
|---|---|---|---|
| description_good | The project site must briefly state what the software does and what problem it solves (MUST) | ✅ | README first sentence: "in-process embedded adaptive Agent kernel". URL: `.../blob/main/README.md` |
| interact | The project site explains how to obtain, give feedback on, and contribute to it (MUST) | ✅ | README (install/Quickstart) + CONTRIBUTING.md + Issues entry point |
| contribution | Explains the contribution process (whether PRs are used) (MUST, Met URL) | ✅ | `CONTRIBUTING.md`: fork → branch → PR, using Conventional Commits |
| contribution_requirements | Requirements/coding standards for acceptable contributions (SHOULD, Met URL) | ✅ | CONTRIBUTING: must pass `ruff` and `pytest`, add tests and walkthrough notes in sync |
| floss_license | Released as FLOSS (MUST) | ✅ | MIT (`pyproject.toml` PEP 639 SPDX) |
| floss_license_osi | License is OSI-approved (SUGGESTED) | ✅ | MIT is an OSI-approved license |
| license_location | License is in the repository's standard location (MUST, Met URL) | ✅ | root `LICENSE` |
| documentation_basics | Provides basic documentation (MUST, may be N/A) | ✅ | README + `docs/` + `docs/walkthrough/00–56` (56 line-by-line walkthroughs + 00-index) |
| documentation_interface | Describes the inputs/outputs of external interfaces (MUST, may be N/A) | ✅ | `docs/architecture.md`, SPI contracts, FastAPI endpoints, walkthrough |
| sites_https | Sites (website/repository/download) support HTTPS/TLS (MUST) | ✅ | GitHub / Gitee / PyPI are all HTTPS |
| discussion | A searchable, URL-addressable discussion mechanism that newcomers can join and that needs no proprietary software (MUST) | ✅ | GitHub Issues / PR (mirrored on Gitee) |
| english | English documentation, accepts English feedback (SHOULD) | ✅ | **Met**: README.en.md, CONTRIBUTING.en.md, SECURITY.md, docs/architecture.en.md, docs/comparison.en.md are provided, and English issues / code review are accepted; checked Met on 2026-10-10 with the Passing self-assessment (#15360). |
| maintained | The project is maintained (MUST) | ✅ | Continuous commits (repo created 2026-09-09, 539 commits, still merging in 2026-11), active ROADMAP; GitHub open issue/PR count is 0 |

## 2. Change Control

| ID | Requirement (level) | Status | Evidence / what to fill in |
|---|---|---|---|
| repo_public | Publicly readable repository with a URL (MUST) | ✅ | Public repositories on GitHub + Gitee |
| repo_track | Records what changed, by whom, and when (MUST) | ✅ | git history |
| repo_interim | Contains intermediate versions between releases, not only final releases (MUST) | ✅ | continuous commits on the mainline |
| repo_distributed | Uses a distributed VCS (SUGGESTED) | ✅ | git |
| version_unique | Each user-facing release has a unique version identifier (MUST) | ✅ | 0.7.0 → 0.27.1 unique (24 release tags) |
| version_semver | Uses SemVer / CalVer (SUGGESTED) | ✅ | SemVer (currently `0.27.1`) |
| version_tags | Releases are tagged in the VCS (SUGGESTED) | ✅ | tags `v0.7.0` … `v0.27.1` (including `v0.8.1`) |
| release_notes | Each release has human-readable release notes, not a git log (MUST, Met URL) | ✅ | `CHANGELOG.md` |
| release_notes_vulns | Lists publicly known runtime vulnerabilities with assigned CVEs fixed in this release (MUST, may be N/A) | ➖ | **N/A reason**: "The project's own artifacts have no public vulnerabilities with an assigned CVE; there are therefore no such entries." |
| report_process | Provides a process for submitting bugs (MUST, Met URL) | ✅ | GitHub Issues + `.github/ISSUE_TEMPLATE/` |
| report_tracker | Uses an issue tracker to track individual issues (SHOULD) | ✅ | GitHub Issues |
| report_responses | Responds to most bugs in the last 2–12 months (MUST) | ✅ | no unanswered/backlogged bugs (0 open issues) |
| enhancement_responses | Responds to most enhancement requests in the last 2–12 months (SHOULD) | ✅ | no backlogged enhancement requests |
| report_archive | Reports and responses have a publicly searchable archive (MUST, Met URL) | ✅ | public GitHub Issues archive |
| vulnerability_report_process | Publishes the vulnerability reporting process on the site (MUST, Met URL) | ✅ | `SECURITY.md` |
| vulnerability_report_private | Supports private vulnerability reports and a confidential channel (MUST, may be N/A, Met URL) | ✅ | SECURITY.md: GitHub Private Security Advisories / email |
| vulnerability_report_response | First response to vulnerabilities within 14 days over the last 6 months (MUST, may be N/A) | ➖ | **N/A reason**: "No vulnerability reports were received during that period." |

## 3. Quality

| ID | Requirement (level) | Status | Evidence / what to fill in |
|---|---|---|---|
| build | If a build is needed, provides a working build system that can rebuild automatically (MUST, may be N/A) | ✅ | hatchling build backend, `uv build` / pip install |
| build_common_tools | Uses common build tools (SUGGESTED, may be N/A) | ✅ | standard Python packaging (hatchling + uv) |
| build_floss_tools | Can be built using only FLOSS tools (SHOULD, may be N/A) | ✅ | the toolchain is entirely FLOSS |
| test | At least one FLOSS automated test suite, with instructions on how to run it (MUST) | ✅ | pytest suite, **2,386 items (full offline pass, 5 skipped for environment reasons, no network or key dependency)**; CI and README/CONTRIBUTING document `uv run pytest` |
| test_invocation | The test suite can be invoked the standard way for the language (SHOULD) | ✅ | `pytest` / `uv run pytest` |
| test_most | The suite covers most branches/inputs/features (SUGGESTED) | ✅ | kernel line coverage **93.12%** (measured 2026-11-05; CI gate 90) |
| test_continuous_integration | Continuous integration is in place (SUGGESTED) | ✅ | GitHub Actions (multi-version matrix) |
| test_policy | There is a policy that "new features must add tests" (MUST) | ✅ | CONTRIBUTING / AGENTS discipline: "changing behavior requires adding/updating tests in sync" |
| tests_are_added | Recent significant changes show evidence of following the add-tests policy (MUST) | ✅ | recent compaction / sandbox / dynamic discovery sources all came with corresponding tests |
| tests_documented_added | The add-tests policy is written into change proposals (SUGGESTED) | ✅ | CONTRIBUTING.md |
| warnings | Enables warning flags/safe mode/linter (MUST, may be N/A) | ✅ | ruff + mypy |
| warnings_fixed | Addresses warnings (MUST, may be N/A) | ✅ | ruff clean across the whole tree, mypy zero errors |
| warnings_strict | As strict as possible (SUGGESTED, may be N/A) | ✅ | ruff across the whole tree + mypy + coverage gate + CodeQL |

## 4. Security

| ID | Requirement (level) | Status | Evidence / what to fill in |
|---|---|---|---|
| know_secure_design | At least one primary developer knows secure software design (MUST) | ✅ | self-assessment statement: the developer designed and implemented three-tier permissions, sandbox isolation, no secrets in the repository, parameterized SQL, `hmac.compare_digest`, and the SECURITY policy (i.e. concrete evidence of secure design) |
| know_common_errors | The primary developer knows common vulnerabilities in this class of software and at least one mitigation for each (MUST) | ✅ | the project mitigates injection (parameterized queries), privilege escalation (permission gate/allowlist), credential leakage (.env not committed/secret scanning), and code execution (sandbox SPI) |
| crypto_published | Uses only publicly reviewed cryptographic protocols/algorithms by default (MUST, may be N/A) | ➖ | **N/A**: this software does not implement or negotiate cryptographic protocols; it performs HMAC verification only via the standard library |
| crypto_call | Calls dedicated crypto libraries rather than implementing crypto itself (SHOULD, may be N/A) | ➖ | **N/A**: the core purpose is not cryptography; where needed it calls the Python standard library `hmac`/`secrets` |
| crypto_floss | Features depending on cryptography can be implemented with FLOSS (MUST, may be N/A) | ➖ | **N/A**: no cryptography features are built in; the Python standard library itself is FLOSS |
| crypto_keylength | Default key lengths satisfy NIST through 2030 (MUST, may be N/A) | ➖ | **N/A**: no key-length negotiation or self-managed key-length scenarios |
| crypto_working | Does not rely on broken algorithms or improper modes by default (MUST, may be N/A) | ➖ | **N/A**: no cryptographic algorithms are used; HMAC comparison does not rely on broken hashes |
| crypto_weaknesses | Does not rely on algorithms/modes with known severe weaknesses (SHOULD, may be N/A) | ➖ | **N/A**: SHA-1/CRC etc. are not used |
| crypto_pfs | Key agreement implements forward secrecy (SHOULD, may be N/A) | ➖ | **N/A**: no key agreement |
| crypto_password_storage | If passwords are stored for external users, they must be salted and iteratively hashed (MUST, may be N/A) | ➖ | **N/A**: user login passwords are not stored; API Keys are compared via HMAC, not stored as password hashes |
| crypto_random | Uses a CSPRNG to generate keys/nonces (MUST, may be N/A) | ✅ | random identifiers via `uuid.uuid4` (backed by `os.urandom`, a CSPRNG) |
| delivery_mitm | The delivery mechanism resists MITM (MUST) | ✅ | PyPI over HTTPS + OIDC Trusted Publishing; repository over HTTPS |
| delivery_unsigned | Must not fetch hashes over HTTP without verifying signatures (MUST NOT) | ✅ | no such practice |
| vulnerabilities_fixed_60_days | No public medium/high-severity vulnerability left unfixed for over 60 days (MUST) | ✅ | CodeQL 0 alerts, no unfixed medium/high-severity findings in dependency audits |
| vulnerabilities_critical_fixed | Fixes critical vulnerabilities quickly (SHOULD) | ✅ | no known critical vulnerabilities |
| no_leaked_credentials | The public repository does not leak valid private keys/passwords (MUST) | ✅ | repository secret scanning found only test fake keys (`test-key`); `.env` is not committed |

## 5. Analysis

| ID | Requirement (level) | Status | Evidence / what to fill in |
|---|---|---|---|
| static_analysis | Applies static analysis tools (beyond compiler/linter) before major releases (MUST, may be N/A/Met with reason) | ✅ | CodeQL (SAST) + mypy; part of the release process |
| static_analysis_common_vulnerabilities | The tooling includes rules for finding common vulnerabilities (SUGGESTED, may be N/A) | ✅ | CodeQL security rule set |
| static_analysis_fixed | Medium/high-severity findings from static analysis are fixed promptly (MUST, may be N/A) | ✅ | CodeQL 0 alerts, nothing pending a fix |
| static_analysis_often | Static analysis runs on every commit or daily (SUGGESTED, may be N/A) | ✅ | CodeQL triggered on push/PR + a weekly scheduled run |
| dynamic_analysis | Uses dynamic analysis tools before major releases (SUGGESTED) | ✅ | **Met**: 4 atheris (libFuzzer) targets under `fuzzing/` — `fuzz_features.py`, `fuzz_schema.py`, `fuzz_compaction.py`, `fuzz_sandbox_gate.py`; a CI `fuzz` job (Ubuntu / py3.12) on every run, 45 seconds per target, covering pure functions in the kernel that face untrusted input directly plus the sandbox AST gate shipped with the package. |
| dynamic_analysis_unsafe | Memory-unsafe languages use dynamic tools to find memory problems (SUGGESTED, may be N/A) | ➖ | **N/A**: Python is a memory-safe language; there are no C/C++ artifacts |
| dynamic_analysis_enable_assertions | Dynamic analysis configuration enables a large number of assertions (SUGGESTED) | ✅ | **Met**: each target simultaneously asserts a set of invariants (bucket values valid, `key` is a fixed 9-tuple, root node type and shape preserved, ranges satisfy `0 <= start < end <= n`, etc.), and a violation errors out and exits. |
| dynamic_analysis_fixed | Medium/high-severity issues found by dynamic analysis are fixed promptly (MUST, may be N/A) | ✅ | **Met**: fuzzing actually found that a too-deeply nested attribute chain let `RecursionError` escape the code-tool sandbox gate; that is now converted to a denial inside the gate and pinned as a test case (`tests/test_sandbox_static_gate.py`); v0.21.0 also fixed an oracle NaN identity trap found by fuzzing. |

---

## 6. Silver / Gold Gap (not applying for now, for the record only)

- **Silver** adds on top of Passing: important changes must be reviewed by a **second person** (`silver`-level code_review: at least one non-author reviewer), stricter crypto requirements, mandatory use of static analysis that can find common vulnerabilities, etc. A single-person project is structurally stuck on "multi-person review"; **we do not recommend recruiting a reviewer in name only just to obtain Silver**.
- **Gold** further requires: two-person review with at least one external reviewer, security hardening, full replay/rollback, stricter test coverage, etc.
- Recommendation: **Passing was obtained on 2026-10-10**; Silver/Gold will be reached naturally once real external contributors appear — no process theater.

## 7. Post-Application Actions

1. After obtaining the Passing Badge, put the official badge Markdown/URL into the badge area at the top of `README.md`;
2. Record "obtained the OpenSSF Best Practices Passing Badge" in `CHANGELOG.md`;
3. This item also improves the OpenSSF Scorecard **CII-Best-Practices** score item: after obtaining the Passing badge, the 2026-10-10 Scorecard run (commit f052359) moved from 0 points to **5 points** (the Passing level scores 5 points; only Silver/Gold correspond to higher scores — this is not a full score); in the same run the total score rose from 6.1 to **6.5**, and the **CI-Tests** item also moved to 10 points as the CI on merged PRs turned fully green.

---

## 8. Review Record (2026-10-25)

| Item | Before review (at the 2026-10-10 self-assessment) | After review (measured 2026-10-25) |
|---|---|---|
| Self-assessment baseline | main v0.8.0 | main v0.25.0 (only the version string changed after the release commit `6e66b6e`) |
| Test count | ~1,584 items | **2,075 items** (2,070 passed + 5 skipped), exit 0 |
| Line coverage | about 92.6% | **95.65%** (8,029 statements / 349 uncovered; CI gate 90) |
| Walkthrough size | `docs/walkthrough/00–27` | `00–56` (56 line-by-line walkthroughs + 00-index) |
| Release tags | v0.7.0, v0.8.0 | **21 tags**: v0.7.0 … v0.25.0 (including v0.8.1, plus 1 milestone tag that was kept at the time and has since been removed) |
| Dynamic analysis (SUGGESTED) | ⚠ Unmet: no fuzzer/DAST deployed | ✅ Met: atheris fuzzing with 4 targets + CI `fuzz` job (Ubuntu/py3.12, 45 seconds per target); there are already instances of fuzzing finding defects that were then fixed |
| Commit activity | repo created 2026-09-09 | **414 commits**, still merging on 2026-10-25; GitHub open issue/PR count 0 |

Judgements and boundaries that did not change:

- **Badge status unchanged**: Passing (#15360, first obtained 2026-10-10). On 2026-10-25 the **platform record** was synced to the current state
  (submitted after the project owner logged in with their own GitHub account; five updates; reading back the public JSON confirmed `badge_level=passing`,
  100% Passing completion, `updated_at` = 2026-10-25 13:58 UTC).
- **Silver / Gold are still blocked on multi-person review**: a single-person project lacks a second reviewer, and we do not recruit a reviewer in name only to obtain a badge.
- **Discussions remains disabled**: Issue + PR already satisfy the `discussion` item; whether to enable Discussions is a maintenance decision,
  and this round explicitly does not do it.
- **This document constitutes no new external commitment**: third-party scoring is governed by the platform's re-scan results; the self-assessment conclusion does not replace a re-scan.

## 9. Review Record (2026-11-05)

| Item | 2026-10-25 review | 2026-11-05 review |
|---|---|---|
| Self-assessment baseline | main v0.25.0 | main v0.27.1 |
| Test count | 2,075 items (2,070 passed + 5 skipped) | **2,386 items** (full offline pass, 5 skipped for environment reasons), exit 0 |
| Line coverage | 95.65% (2026-10-25, whole-project statement scope) | **93.12%** (kernel line-coverage scope, CI gate 90; for the whole-project scope, the CI coverage job output is authoritative) |
| Release tags | 21 tags (including 1 milestone tag that was kept at the time and has since been removed): v0.7.0 … v0.25.0 | **24 tags**: v0.7.0 … v0.27.1 |
| Commit activity | 414 commits | **539 commits** (as of 2026-11-05); GitHub open issue/PR count 0 |

Judgements and badge status unchanged (Passing); the platform-side record was not resubmitted this round, and the latest numbers are governed by this table and
`docs/IMPLEMENTATION_STATUS.md`.
