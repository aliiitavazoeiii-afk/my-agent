# Shared Chat → VPS Agent Protocol

This repository is the control plane for Ali's VPS coding/deployment agent.

## When a ChatGPT chat is told to "use my-agent", "با API روی سرور انجامش بده", or equivalent

1. Inspect the target project's GitHub repository and read its continuation/context docs first.
2. Think through architecture and produce a self-contained implementation brief.
3. Create one issue in `aliiitavazoeiii-afk/my-agent`.
4. The issue title MUST start with `[AGENT]`.
5. The issue body MUST contain exactly one fenced YAML block like this:

```yaml
version: 1
project: kharj
repository: aliiitavazoeiii-afk/darma-expense
request: |
  Implement the requested change here, preserving existing project decisions.
deploy: true
allow_infra: false
verify:
  - docker compose -f compose.expense.yml config
  - docker compose -f compose.expense.yml exec -T expense-web python manage.py check
rollback:
  - docker compose -f compose.expense.yml up -d --build
notes: |
  Read project docs/history before changing code.
```

6. Add label `agent:ready`.
7. Do not tell Ali to SSH or copy/paste deployment commands for normal tasks. The VPS agent owns implementation, testing and deployment.
8. For destructive/host-level changes (`firewall`, SSH, deleting databases/volumes, changing global networking), do NOT queue them as autonomous tasks. Explain what needs approval.
9. The VPS agent posts progress/result comments to the issue and sends Telegram on completion/failure.

## Project resolution

`repository` is the canonical identifier. The VPS agent scans allowed roots for an existing git checkout whose `origin` matches it. If none exists, it creates a workspace clone under `/srv/my-agent/workspaces/<repo-name>`.

## Verification

Always include deterministic `verify` commands when you know them. The executor will run them after the model finishes. A failed verification triggers escalation to the stronger API model.

## Rollback

For production tasks, include deterministic `rollback` commands whenever the project's deployment method is known. The controller first resets the working tree to the original HEAD and then runs these commands if the task fails.

## New sites

For a new site, create the GitHub repository first when appropriate and set:

```yaml
project: new-site-slug
repository: OWNER/REPO
request: |
  Build the site...
deploy: true
allow_infra: true
verify:
  - docker compose config
```

Host-level Caddy changes must use the dedicated `/etc/caddy/my-agent-sites/` directory and the approved reload helper. Never overwrite the main Caddyfile from an agent task.
