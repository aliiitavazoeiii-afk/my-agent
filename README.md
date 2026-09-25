# my-agent

Shared control plane for Ali's autonomous VPS coding/deployment agent.

The normal workflow is:

`ChatGPT project chat → GitHub issue in this repo → VPS daemon → OpenAI Responses API → local shell → tests/deploy → Telegram notification`

See `AGENT_CHAT_PROTOCOL.md` for the protocol every project chat should follow.

## Security model

- The daemon accepts tasks only from configured GitHub users.
- Only issues with `agent:ready` are executed.
- Secrets live only in `/etc/my-agent/agent.env` on the VPS.
- OpenAI local-shell calls are filtered for destructive/secret-reading commands.
- Secret values are stripped from the model shell subprocess environment.
- `sudo` is denied except explicitly allowlisted helper commands.
- Caddy agent snippets live separately in `/etc/caddy/my-agent-sites/`.
- A private GitHub control repository is strongly recommended.

## Install

On the VPS:

```bash
sudo rm -rf /opt/my-agent
sudo git clone https://github.com/aliiitavazoeiii-afk/my-agent.git /opt/my-agent
cd /opt/my-agent
sudo bash install.sh
```

The installer asks interactively for:
- OpenAI API key
- GitHub token (or reuses `gh auth token` if available)
- Telegram bot token
- Telegram chat ID (can auto-detect after you send `/start` to the bot)

After install:

```bash
sudo systemctl status my-agent --no-pager
sudo journalctl -u my-agent -n 100 --no-pager
```

## Telegram

Create a bot with `@BotFather`, copy its token, open the bot and send `/start`.
The installer can then discover your private chat ID.

## Control issue format

See `AGENT_CHAT_PROTOCOL.md`.
