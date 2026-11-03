# WeatherEdge — production access

## Session memory

At the start of the first WeatherEdge task in every new session, read
`docs/SESSION_MEMORY.md` and print its `Session Brief` to the user before doing
project work. Treat its production facts as a dated handoff that must be
revalidated when current operational state matters.

After a production incident, deployment, material audit, strategy-policy change,
or deliberate deferment, update that memory with the root cause, completed work,
verification evidence, safety state, and remaining work. Keep credentials, exact
access identifiers, key paths, and commands containing those sensitive values in
the gitignored `.local` directory. Sanitized access decisions and outcomes may
be recorded in the tracked memory.

## Authoritative runtime

The trading and forecasting runtime is the AWS EC2 deployment documented in
docs/aws_deployment.md. Do not diagnose production data from ignored local
runtime files.

Connection details, private keys, instance identifiers, and operator-specific
commands belong in the gitignored .local directory. Load .local/ec2.env and use
the variables defined there; never copy their values into tracked files,
commits, issues, pull requests, or generated artifacts.

The runtime application roots are under /opt/weatheredge, environment settings
are in /etc/weatheredge.env with root-only permissions, and the sfo-prefixed
systemd services and timers drive the unattended pipeline. Production remains
paper-only unless the owner separately authorizes a real-money change.

## Deployment and publishing

Use the scripts under trading/deploy/aws and follow docs/aws_deployment.md.
Preserve the runtime timer state, take a verified database backup, deploy a
clean exact Git SHA, seed the strategy/publication jobs, and restore the
freshness watchdog last.

GitHub Pages authentication is supplied through the production environment.
Never commit the deploy key path or key material. On rebuild, verify publishing
with a harmless dry run and confirm the public manifest hash before declaring
the deployment healthy.

## Archive layer

Nightly retention is archive-gated: complete UTC days are exported and verified
before pruning. Off-host upload is controlled by SFO_ARCHIVE_S3_BUCKET. When it
is unset, the archive is only a local ring buffer and is not a disaster-recovery
backup.

## The agent team

The main session runs a goal itself: it briefs agents with exact files and the
definition of done, checks what they return, and reports to Jaxson. When two or
more agents can work at the same time (different files, neither needs the
other's result, at most one uses a browser, none commits or switches branches),
it launches them in one message. Only work that splits into three or more
independent pieces goes to the `orchestrator`; its planning costs several
minutes, so it isn't worth it when the steps depend on each other.

| Agent | Where | Use it for |
|---|---|---|
| `orchestrator` | global (`~/develop/agent claude md/orchestrator.md`, symlinked into `~/.claude/agents/`) | Batches of independent work: audits across areas, several bugs or pages at once. Add "plan only" to see the waves first |
| `frontend-designer` | global (`~/develop/agent claude md/frontend-designer.md`, symlinked into `~/.claude/agents/`) | Any UI work in `src/`. The site is HeroUI v3 plus licensed HeroUI Pro; its knowledge pack is `~/develop/agent claude md/frontend-designer/heroui/GUIDE.md`. `AGENTS.md` "Design And Redesign Memory" binds it |
| `release-manager` | global (`~/.claude/agents/`) | Shipping: a PR with only that work's files, the ChatGPT review loop until approve, sign-off, squash-merge. Also "what's open, is anything stuck?" |

Backend work (`trading/`, `forecaster/`, `trading/deploy/aws/`) runs in the main
session or a general-purpose agent with targeted `pytest`. Production data and
heavy research run read-only: on AWS through bounded queries, or on the Mac
against a verified copy under `.local/`.

## Shipping: milestones go to the release-manager

When a verified slice of work is worth saving, or Jaxson asks to put work on
GitHub, hand it to `release-manager` with the exact files, what and why, and how
it was verified; don't edit those files while it works. Never commit straight to
`main`. Repo: `github.com/Jaxsonb04/weather_edge`.

Merging never deploys. AWS backend releases and GitHub Pages publication stay in
the main session, follow "Deployment and publishing" above, and need the owner's
authorization for each release. The release-manager never runs the scripts under
`trading/deploy/aws`.
