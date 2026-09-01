# LogicGate SPE form — field-by-field answers

Record: `Exception-1220` · https://ciena.logicgate.com/records/1L3Jb066

Source material: `ticket-body.txt` (full detail) and `security-allowlist-request.md` (evidence).
Attach `ticket-body.txt` to the Risk Mitigation Plan attachment field.

---

## ⚠️ First — you are probably in the wrong form

**Update 2026-09-01: the blocking product is confirmed as ThreatLocker**, running
application control in **default-deny** mode (`applicationControl: true`,
`defaultDeny: true` in `/Library/Application Support/ThreatLocker/AgentConfiguration.plist`).

That settles the channel question. Default-deny means the agent blocks anything not on the
approved list — it is **working exactly as designed**, and no security policy is being
violated or bent. What's needed is a **ThreatLocker application approval**, i.e. adding the
Python toolchain to the allowlist.

A Security Policy Exception is for "we cannot comply with policy X, please accept the
risk". Nothing here needs excepting. Filing an SPE would create a standing risk-acceptance
record against you and your director for what is really a routine allowlist addition.

**Recommended:** do not complete this form until whoever owns ThreatLocker approvals
confirms it is required. Ask them first — see the ready-to-paste message below. If they
say an SPE *is* also needed, everything below applies.

Also worth doing regardless: click **"Send a request"** on the ThreatLocker popup when it
fires. That is the product's native approval path and goes straight to the right queue.

### Ready-to-paste message for your security contact

**Answering "which application is blocking it?"** — use this:

```
It's ThreatLocker — the application control agent, running in default-deny
mode.

Confirmed from the agent's own config on my machine
(/Library/Application Support/ThreatLocker/AgentConfiguration.plist), which
has applicationControl: true and defaultDeny: true.

I also confirmed ThreatLocker has already inventoried the exact file it's
blocking — its baseline database lists
.../cpython-3.12.10-macos-aarch64-none/bin/python3.12 with SHA-256
62D77CE5...EFEC, which matches the binary that gets killed. It just isn't on
the approved list.

For completeness, the machine also runs CyberArk EPM, CrowdStrike Falcon,
Zscaler and Jamf — but the default-deny behaviour and the "request access"
popup point to ThreatLocker.

So I think what I actually need is a ThreatLocker approval for the Python
toolchain, not a policy exception. Can you confirm, and let me know who owns
ThreatLocker approvals? I have the full list of paths and hashes ready.
```

**Plain language — if you need a non-technical version**

```
It's ThreatLocker, the software-approval tool. It's set to block anything
that isn't on its approved list, and standard Python isn't on that list — so
it shuts Python down as soon as it starts. Because of that, five of our Blue
Planet projects won't run on my laptop.

It's the normal Python from the official site, nothing modified. So it just
needs adding to the approved list rather than anything being overridden.

Who looks after ThreatLocker approvals? I've got the full list of files ready
to send.
```

Whichever version you use, two details do the work: naming the **five blocked projects**
frames it as impact rather than inconvenience, and **"nothing modified, straight from the
official site"** frames it as a routine allowlist addition rather than a request to run
something risky.

---

## General Information (11 fields)

**Exception Name**
```
Endpoint agent exclusions for approved Python developer toolchain — Blue Planet development workstation (macOS arm64)
```

**Exception Requester** — Saurabh Brahmankar (self)

**Requested on behalf of** — leave blank (requesting for yourself)

**Management Owner** — your line manager. *You must supply this.*

**Director Level Owner** — your director or above. *You must supply this.* Worth giving
them a heads-up first; the form says this is for senior-level awareness, so an
out-of-the-blue approval request tends to stall.

**Responsible Business Unit** — Blue Planet (pick the closest option in the dropdown;
likely the Blue Planet software / R&D entry).

**Exception for Policy** — pick the endpoint-protection / anti-malware / host security
policy. If several look plausible, choose the one governing *endpoint protection agent
configuration*, not data classification or access control.

**Exception Request Description**
```
The host endpoint protection agent on my macOS (Apple silicon) development
workstation is terminating the Python interpreters used for Blue Planet
Advisor development, and is refusing to memory-map the compiled Python
extension modules those projects depend on.

Two behaviours are observed:
(A) Interpreters are SIGKILLed on launch — the shell reports "Killed: 9",
    exit code 137, with no output.
(B) Compiled .so extension modules fail to load with
    "dlopen(...): mmap(size=...) failed with errno=1".

Five Blue Planet repositories cannot currently be run or tested at all:
BP-AGENTS/bp-knowledge-retriever-agent, BP-AGENTS/bp-supervisor-agent,
bpa_langflow, bpa_ai_sdk/bpa-gen-ai and bpa_ai_sdk/bpa-base. Each of their
virtual environments symlinks to one of two blocked CPython interpreters.

I am requesting hash-based and directory-based exclusions for the affected
interpreters, developer tooling and Python package directories. Full paths,
SHA-256 values and diagnostic evidence are in the attached document.

The affected files are unmodified official releases from python.org,
Anaconda, PyPI and the GitHub Copilot CLI distribution. Diagnostic testing
indicates a per-file reputation decision rather than a genuine detection:
copying a blocked file to /tmp did not help, ad-hoc re-signing did not help,
a trivial C extension compiled locally on this machine was also refused, no
Gatekeeper quarantine attributes are present, and functionally identical
copies of the same tool receive different verdicts.
```

**Business Justification**
```
Blue Planet Advisor development and testing. The affected repositories are
BP-AGENTS (bp-knowledge-retriever-agent, bp-supervisor-agent), the BPA AI SDK
(bpa-base, bpa-gen-ai), bpa_langflow, and the generative-AI and data-science
workstreams.

Impact: five BPA project environments cannot start, so local development,
debugging and pre-commit testing are blocked for those repositories. Work
must currently be pushed to CI or a jump host to be executed at all, which
lengthens every change cycle and removes local debugging.

The blocked components are standard, widely used, open-source developer
tooling — CPython, numpy, pydantic, lxml and ripgrep — which are direct or
transitive dependencies of the frameworks these projects are built on
(langflow, FastAPI, LangChain). They are not optional and cannot be
substituted.

All execution is local to the workstation. No production system, customer
environment or customer data is involved.
```

**What is the authoritative source for this exception?**

Ambiguous field — it usually means *which policy or standard is being excepted from*. Give
the policy, then the evidence:
```
Ciena endpoint protection / anti-malware policy (the policy selected in the
"Exception for Policy" field above).

Supporting evidence: attached document "ticket-body.txt", containing absolute
paths, SHA-256 values and reproduction steps for every affected binary,
re-verified 2026-09-01. Evidence is regenerated by the script
collect_blocked_binaries.sh, which re-tests each binary and prints current
hashes.
```
If the field turns out to mean something narrower in your workflow, adjust — but naming
both the policy and the evidence covers either reading.

**Risk Mitigation Plan**
```
Scope limitation
- Exclusions apply to a single named developer workstation, not to a fleet
  policy or a shared/production system.
- Requested directory exclusions are confined to developer toolchain
  locations: the uv interpreter store, the miniconda install, the local
  package cache, and site-packages directories beneath the Blue Planet
  workspace. No system directory, user document location or network share is
  in scope.

Compensating controls that remain in force
- Full-disk encryption, OS patching and MDM management remain unchanged.
- Network, proxy and DNS controls remain unchanged; the exclusions affect
  local execution only.
- All other endpoint agent detection capability remains active on this host;
  only the named paths and hashes are excepted.
- Source code is version-controlled and code-reviewed; CI runs independently
  of this workstation and is unaffected.

Provenance controls on the excluded content
- All excluded binaries are unmodified official releases (python.org,
  Anaconda, PyPI, GitHub Copilot CLI). SHA-256 values are recorded in the
  attached document and can be verified against VirusTotal or the publishers.
- Python dependencies are installed from PyPI via pinned project manifests
  and lockfiles, so the contents of the excluded site-packages directories
  are reproducible and auditable.

Ongoing review
- Hashes will be re-verified with the supplied collector script at each
  toolchain upgrade.
- I will withdraw this exception immediately if an approved/managed Python
  distribution or a standard developer-workstation policy profile is made
  available — that is the preferred end state, and I have asked for it in the
  request.
```

**Risk Mitigation Plan Supporting Documentation** — attach `ticket-body.txt`. Optionally
also `security-allowlist-request.md`.

**Requested Exception Duration** — pick **12 months**, or the longest standard option if 12
isn't offered. Shorter durations mean re-filing while nothing has changed. The mitigation
plan already commits to withdrawing early if a managed toolchain appears.

**More Actions** — leave blank. Only set Canceled/Resolved if you abandon the request.

---

## Risk Assessment (1 field)

**Is the exception for a user/group account or an information system?** → **Information
System**

The form's guidance sends software/EOL cases to Information System, and account, password,
permission and MFA cases to User/Group. This is a software execution issue on a host, so
Information System.

---

## Information System Assessment (6 fields)

> Verify each of these against your own knowledge of what is on the machine — I can see the
> workspace layout, not your full data footprint. The answers below are what a standard
> managed developer laptop should be, but you are the one signing them.

**Information System Type** → Endpoint / Workstation / Laptop (whichever wording the
dropdown uses). Not Server, not Application.

**Does the information system access, store, process, or transmit any of the following
data types?** → Expected answer **None / Not applicable**, on the basis that this is a
development workstation working against source code and synthetic or test data.

⚠️ Check before answering. Say so honestly if any of the following is true: you hold
customer configuration data, production exports, PII, or credentials for production
systems in local files. Understating this is the fastest way to have an exception revoked
later.

**Is the information system directly accessible from the Internet?** → **No**. It is a
laptop behind corporate network controls with no inbound exposure; local services bind to
localhost.

**Does the information system have regulatory or contractual requirements (SOX etc.)?** →
Expected **No** for a development workstation. Confirm against your BU's position — if
Blue Planet development is in SOX scope for change management, answer Yes and let the
reviewer weigh it.

**Other than in the context of the current exception request, does the information system
run only approved/supported versions of software?** → **Yes**. Note that the macOS build,
MDM agent and endpoint agent are all current and managed; the exception concerns
classification of specific developer binaries, not unsupported software.

**Is the information system in whole or in part managed by an external entity (e.g., SaaS
application)?** → **No**. It is a Ciena-managed corporate endpoint.

---

## Before you submit

- Confirm the channel question at the top — that is the single highest-value check.
- Re-run `bash collect_blocked_binaries.sh` and refresh the hashes in `ticket-body.txt`;
  they go stale at every toolchain upgrade, and a reviewer who cannot reproduce your hash
  will reject the request.
- Give the director-level owner advance notice rather than surfacing it via a workflow
  notification.
- Section 8 of `ticket-body.txt` covers the Docker Desktop `ciena` org sign-in issue. It is
  a different owner and does **not** belong in this exception — raise it separately, but
  mention it verbally if asked why containers aren't the workaround.
