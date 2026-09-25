#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml


ROOT = Path(__file__).resolve().parent
STOP = False

AGENT_LABELS = {
    "agent:ready": "0E8A16",
    "agent:running": "1D76DB",
    "agent:done": "5319E7",
    "agent:failed": "D93F0B",
}

SECRET_ENV_KEYS = {
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
}

FORBIDDEN_PATTERNS = [
    r"(^|[\s;&|])(shutdown|reboot|poweroff|halt)([\s;&|]|$)",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+[^;\n]*\bof=/dev/",
    r"\b(wipefs|fdisk|parted)\b",
    r"\brm\s+(-[A-Za-z]*r[A-Za-z]*f[A-Za-z]*|--recursive[^;\n]*--force|--force[^;\n]*--recursive)\s+/(?:\s|$|\*)",
    r"\b(iptables|ip6tables|nft)\b",
    r"\bufw\s+(disable|reset)\b",
    r"/etc/(shadow|gshadow|sudoers)",
    r"/etc/my-agent/",
    r"/root/\.ssh/",
    r"/home/[^/\s]+/\.ssh/",
    r"/proc/[^/\s]+/environ",
    r"\b(systemctl|service)\s+(stop|disable|mask)\s+(ssh|sshd|docker)\b",
    r"(curl|wget)[^;\n|]*\|\s*(sh|bash)\b",
    r"\b(chmod|chown)\s+-R\s+[^;\n]*\s+/(?:\s|$)",
]

SUSPICIOUS_SECRET_COMMANDS = [
    r"(^|[\s;&|])env([\s;&|]|$)",
    r"\bprintenv\b",
    r"\bset\s*(?:$|[;&|])",
    r"git-askpass\.sh",
]


@dataclass
class CmdResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


class AgentError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def load_config() -> dict[str, Any]:
    path = Path(os.environ.get("MY_AGENT_CONFIG", "/etc/my-agent/config.yaml"))
    if not path.exists():
        path = ROOT / "config.example.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalize_repo(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^git@github\.com:", "", value)
    value = re.sub(r"^https?://github\.com/", "", value)
    value = re.sub(r"\.git$", "", value)
    return value.strip("/").lower()


def sanitize_shell_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in SECRET_ENV_KEYS:
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    return env


class GitHub:
    def __init__(self, token: str, control_repo: str):
        self.token = token
        self.control_repo = control_repo
        self.base = "https://api.github.com"
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "my-agent-v1",
        })

    def req(self, method: str, path: str, **kwargs) -> Any:
        r = self.s.request(method, self.base + path, timeout=30, **kwargs)
        if r.status_code >= 400:
            raise AgentError(f"GitHub {method} {path}: {r.status_code} {r.text[:1200]}")
        if not r.content:
            return None
        return r.json()

    def ensure_labels(self) -> None:
        owner, repo = self.control_repo.split("/", 1)
        existing = self.req("GET", f"/repos/{owner}/{repo}/labels?per_page=100")
        names = {x["name"] for x in existing}
        for name, color in AGENT_LABELS.items():
            if name not in names:
                self.req("POST", f"/repos/{owner}/{repo}/labels", json={
                    "name": name, "color": color, "description": "Managed by my-agent"
                })

    def issues_with_label(self, label: str) -> list[dict[str, Any]]:
        owner, repo = self.control_repo.split("/", 1)
        enc = quote(label, safe="")
        items = self.req(
            "GET",
            f"/repos/{owner}/{repo}/issues?state=open&labels={enc}&sort=created&direction=asc&per_page=20",
        )
        return [x for x in items if "pull_request" not in x]

    def comment(self, number: int, body: str) -> None:
        owner, repo = self.control_repo.split("/", 1)
        self.req("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body})

    def set_status(self, issue: dict[str, Any], status: str) -> None:
        owner, repo = self.control_repo.split("/", 1)
        kept = [
            x["name"] for x in issue.get("labels", [])
            if not x["name"].startswith("agent:")
        ]
        labels = kept + [status]
        self.req("PATCH", f"/repos/{owner}/{repo}/issues/{issue['number']}", json={"labels": labels})
        issue["labels"] = [{"name": x} for x in labels]

    def close(self, issue: dict[str, Any]) -> None:
        owner, repo = self.control_repo.split("/", 1)
        self.req("PATCH", f"/repos/{owner}/{repo}/issues/{issue['number']}", json={"state": "closed"})

    def repo_info(self, full_name: str) -> dict[str, Any]:
        owner, repo = full_name.split("/", 1)
        return self.req("GET", f"/repos/{owner}/{repo}")


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None):
        self.token = token or ""
        self.chat_id = chat_id or ""

    def send(self, text: str) -> None:
        if not self.token or not self.chat_id:
            return
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text[:3900],
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            if r.status_code >= 400:
                log(f"telegram failed: {r.status_code} {r.text[:300]}")
        except Exception as exc:
            log(f"telegram exception: {exc}")


def parse_task(body: str) -> dict[str, Any]:
    match = re.search(r"```ya?ml\s*(.*?)```", body or "", re.I | re.S)
    raw = match.group(1) if match else body
    task = yaml.safe_load(raw) or {}
    if not isinstance(task, dict):
        raise AgentError("Task body must parse to a YAML mapping")
    if int(task.get("version", 1)) != 1:
        raise AgentError("Unsupported task version")
    repo = str(task.get("repository", "")).strip()
    req = str(task.get("request", "")).strip()
    if not repo or "/" not in repo:
        raise AgentError("Task requires repository: OWNER/REPO")
    if not req:
        raise AgentError("Task requires request")
    task["repository"] = repo
    task["deploy"] = bool(task.get("deploy", False))
    task["allow_infra"] = bool(task.get("allow_infra", False))
    task["verify"] = [str(x) for x in (task.get("verify") or [])]
    task["rollback"] = [str(x) for x in (task.get("rollback") or [])]
    return task


def list_git_roots(allowed_roots: list[str]) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for root_s in allowed_roots:
        root = Path(root_s)
        if not root.exists() or not root.is_dir():
            continue
        candidates = [root]
        try:
            candidates += [p for p in root.iterdir() if p.is_dir()]
        except PermissionError:
            pass
        for p in candidates:
            if (p / ".git").exists():
                rp = str(p.resolve())
                if rp not in seen:
                    found.append(p.resolve())
                    seen.add(rp)
    return found


def git_output(cwd: Path, args: list[str], env: dict[str, str] | None = None, timeout: int = 60) -> str:
    p = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if p.returncode != 0:
        raise AgentError(f"git {' '.join(args)} failed: {p.stderr[-2000:]}")
    return p.stdout.strip()


def controller_git_env(token: str) -> dict[str, str]:
    env = sanitize_shell_env()
    env["MY_AGENT_GH_TOKEN"] = token
    env["GIT_ASKPASS"] = str(ROOT / "scripts" / "git-askpass.sh")
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def find_or_clone_repo(task: dict[str, Any], cfg: dict[str, Any], gh_token: str) -> Path:
    wanted = normalize_repo(task["repository"])
    roots = [str(x) for x in cfg.get("allowed_roots", ["/opt", "/srv/my-agent/workspaces"])]
    for p in list_git_roots(roots):
        try:
            origin = git_output(p, ["remote", "get-url", "origin"])
        except Exception:
            continue
        if normalize_repo(origin) == wanted:
            return p

    workspace_root = Path("/srv/my-agent/workspaces")
    workspace_root.mkdir(parents=True, exist_ok=True)
    dest = workspace_root / task["repository"].split("/", 1)[1]
    if dest.exists() and any(dest.iterdir()):
        raise AgentError(f"Workspace exists but is not the requested git repo: {dest}")

    env = controller_git_env(gh_token)
    url = f"https://github.com/{task['repository']}.git"
    p = subprocess.run(
        ["git", "clone", url, str(dest)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if p.returncode != 0:
        raise AgentError(f"git clone failed: {p.stderr[-2000:]}")
    return dest.resolve()


def current_git_state(path: Path) -> tuple[str, str, str]:
    head = git_output(path, ["rev-parse", "HEAD"])
    branch = git_output(path, ["branch", "--show-current"])
    dirty = git_output(path, ["status", "--porcelain"])
    return head, branch, dirty


def command_allowed(command: str, cfg: dict[str, Any], task: dict[str, Any]) -> tuple[bool, str]:
    cmd = command.strip()
    if not cmd:
        return True, ""

    sec = cfg.get("security") or {}
    sudo_helpers = [str(x) for x in sec.get("allow_sudo_helpers", [])]

    if re.search(r"(^|[\s;&|])sudo([\s;&|]|$)", cmd):
        allowed_exact = {f"sudo {x}" for x in sudo_helpers}
        if cmd not in allowed_exact:
            return False, "sudo is blocked except approved exact helper commands"

    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, cmd, re.I | re.M):
            return False, f"blocked by security rule: {pattern}"

    for pattern in SUSPICIOUS_SECRET_COMMANDS:
        if re.search(pattern, cmd, re.I | re.M):
            return False, "commands that expose the agent process environment or credentials are blocked"

    if not task.get("allow_infra"):
        infra_patterns = [
            r"/etc/caddy/",
            r"\bcaddy\b",
            r"\bsystemctl\b",
            r"\bservice\b",
            r"\bdocker\s+(system|network|volume)\s+(prune|rm|create)\b",
        ]
        for pattern in infra_patterns:
            if re.search(pattern, cmd, re.I | re.M):
                return False, "host/infra command requires allow_infra: true"

    return True, ""


class LocalShell:
    def __init__(self, cwd: Path, cfg: dict[str, Any], task: dict[str, Any]):
        self.cwd = cwd
        self.cfg = cfg
        self.task = task
        self.timeout = int((cfg.get("security") or {}).get("max_command_seconds", 180))
        self.max_chars = int((cfg.get("security") or {}).get("max_output_chars", 24000))

    def run(self, command: str, timeout_ms: int | None = None) -> CmdResult:
        ok, reason = command_allowed(command, self.cfg, self.task)
        if not ok:
            return CmdResult("", f"MY_AGENT_SECURITY_BLOCK: {reason}\nCommand was not executed.", 126, False)

        timeout = self.timeout
        if timeout_ms:
            timeout = min(max(1, int(timeout_ms / 1000)), self.timeout)

        env = sanitize_shell_env()
        try:
            p = subprocess.Popen(
                ["bash", "-lc", command],
                cwd=str(self.cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                out, err = p.communicate(timeout=timeout)
                return CmdResult(out[-self.max_chars:], err[-self.max_chars:], p.returncode, False)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                out, err = p.communicate()
                return CmdResult(out[-self.max_chars:], err[-self.max_chars:], None, True)
        except Exception as exc:
            return CmdResult("", f"executor exception: {exc}", 127, False)


class OpenAIExecutor:
    def __init__(self, api_key: str, cfg: dict[str, Any]):
        self.key = api_key
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "my-agent-v1",
        })
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        r = self.s.post("https://api.openai.com/v1/responses", json=payload, timeout=180)
        if r.status_code >= 400:
            raise AgentError(f"OpenAI API error {r.status_code}: {r.text[:3000]}")
        data = r.json()
        usage = data.get("usage") or {}
        for k in self.usage:
            self.usage[k] += int(usage.get(k, 0) or 0)
        return data

    def run(
        self,
        task: dict[str, Any],
        cwd: Path,
        model: str,
        effort: str,
        max_turns: int,
        extra_context: str = "",
    ) -> str:
        shell = LocalShell(cwd, self.cfg, task)
        infra = "ALLOWED" if task.get("allow_infra") else "NOT ALLOWED"
        deploy = "YES" if task.get("deploy") else "NO"

        instructions = f"""
You are the execution engineer inside Ali's VPS. A stronger planner has already written the task.
Work autonomously until the requested change is implemented and verified.

Repository working directory: {cwd}
Repository: {task['repository']}
Production deployment requested: {deploy}
Host/infrastructure changes: {infra}

Rules:
- Inspect the repository, git status, documentation, continuation/history files, deployment docs, and existing conventions before editing.
- Preserve established architecture and project decisions. Do not rewrite unrelated code.
- Use the local shell to read/edit files, run tests, inspect Docker/logs, and deploy when requested.
- Never print, read, search for, or expose API keys, tokens, /etc/my-agent, SSH private keys, or process environment secrets.
- Never disable SSH, firewall protections, or delete databases/volumes.
- Do not use sudo except the exact approved helper exposed by the runtime.
- Do not commit or push; the controller will commit/push after deterministic verification.
- If a command fails, inspect stdout/stderr and fix the root cause. Do not just retry blindly.
- If deployment is requested, verify the live service after deployment using the project's established method.
- Keep the final response concise: what changed, what you tested, deployment/live state, and anything that still needs human review.
""".strip()

        user_input = f"""TASK:
{task['request']}

Planner notes:
{task.get('notes', '')}

Deterministic verification commands that the controller will run after you finish:
{json.dumps(task.get('verify') or [], ensure_ascii=False)}

{extra_context}
"""

        response = self.request({
            "model": model,
            "reasoning": {"effort": effort},
            "instructions": instructions,
            "input": user_input,
            "tools": [{"type": "shell", "environment": {"type": "local"}}],
            "tool_choice": "auto",
        })

        for _turn in range(max_turns):
            shell_calls = [x for x in response.get("output", []) if x.get("type") == "shell_call"]
            if not shell_calls:
                texts: list[str] = []
                for item in response.get("output", []):
                    if item.get("type") != "message":
                        continue
                    for c in item.get("content", []):
                        if c.get("type") == "output_text" and c.get("text"):
                            texts.append(c["text"])
                return "\n".join(texts).strip() or "(model finished without text output)"

            outputs: list[dict[str, Any]] = []
            for call in shell_calls:
                action = call.get("action") or {}
                commands = action.get("commands") or []
                max_output_length = int(action.get("max_output_length") or 12000)
                command_outputs: list[dict[str, Any]] = []
                for command in commands:
                    log(f"shell: {command[:500]}")
                    result = shell.run(str(command), action.get("timeout_ms"))
                    outcome = (
                        {"type": "timeout"}
                        if result.timed_out
                        else {"type": "exit", "exit_code": int(result.exit_code or 0)}
                    )
                    command_outputs.append({
                        "stdout": result.stdout[-max_output_length:],
                        "stderr": result.stderr[-max_output_length:],
                        "outcome": outcome,
                    })
                outputs.append({
                    "type": "shell_call_output",
                    "call_id": call["call_id"],
                    "max_output_length": max_output_length,
                    "output": command_outputs,
                })

            response = self.request({
                "model": model,
                "reasoning": {"effort": effort},
                "previous_response_id": response["id"],
                "input": outputs,
                "tools": [{"type": "shell", "environment": {"type": "local"}}],
                "tool_choice": "auto",
            })

        raise AgentError(f"Model exceeded max_turns={max_turns}")


def run_verify(commands: list[str], cwd: Path, cfg: dict[str, Any], task: dict[str, Any]) -> tuple[bool, str]:
    if not commands:
        return True, "No deterministic verify commands were supplied."
    shell = LocalShell(cwd, cfg, task)
    transcript: list[str] = []
    for cmd in commands:
        result = shell.run(cmd)
        transcript.append(
            f"$ {cmd}\nexit={result.exit_code} timeout={result.timed_out}\n"
            f"stdout:\n{result.stdout[-5000:]}\nstderr:\n{result.stderr[-5000:]}"
        )
        if result.timed_out or result.exit_code != 0:
            return False, "\n\n".join(transcript)
    return True, "\n\n".join(transcript)


def rollback(task: dict[str, Any], cwd: Path, original_head: str, cfg: dict[str, Any]) -> str:
    transcript: list[str] = []
    try:
        p = subprocess.run(
            ["git", "reset", "--hard", original_head],
            cwd=str(cwd), capture_output=True, text=True, timeout=60,
        )
        transcript.append(f"git reset --hard {original_head}: {p.returncode}\n{p.stdout}\n{p.stderr}")
    except Exception as exc:
        transcript.append(f"git reset failed: {exc}")

    if task.get("deploy") and task.get("rollback"):
        shell = LocalShell(cwd, cfg, {**task, "allow_infra": task.get("allow_infra", False)})
        for cmd in task["rollback"]:
            result = shell.run(cmd)
            transcript.append(
                f"$ {cmd}\nexit={result.exit_code}\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
            )
    return "\n\n".join(transcript)


def commit_and_push(cwd: Path, task: dict[str, Any], issue_number: int, token: str) -> str:
    status = git_output(cwd, ["status", "--porcelain"])
    if not status:
        return git_output(cwd, ["rev-parse", "HEAD"])

    subprocess.run(["git", "add", "-A"], cwd=str(cwd), check=True, timeout=60)
    message = f"agent: issue #{issue_number} - {str(task.get('project') or 'task')[:80]}"
    p = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(cwd), capture_output=True, text=True, timeout=60,
    )
    if p.returncode != 0:
        raise AgentError(f"git commit failed: {p.stderr[-2000:]}")

    branch = git_output(cwd, ["branch", "--show-current"])
    if not branch:
        branch = f"agent/issue-{issue_number}"
        git_output(cwd, ["switch", "-c", branch])

    env = controller_git_env(token)
    remote = f"https://github.com/{task['repository']}.git"
    p = subprocess.run(
        ["git", "push", remote, f"HEAD:{branch}"],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=180,
    )
    if p.returncode != 0:
        raise AgentError(f"git push failed: {p.stderr[-3000:]}")
    return git_output(cwd, ["rev-parse", "HEAD"])


def process_issue(
    issue: dict[str, Any],
    cfg: dict[str, Any],
    gh: GitHub,
    tg: Telegram,
    oa: OpenAIExecutor,
    gh_token: str,
) -> None:
    num = issue["number"]
    author = ((issue.get("user") or {}).get("login") or "").lower()
    allowed_users = {str(x).lower() for x in cfg.get("authorized_github_users", [])}
    if author not in allowed_users:
        gh.comment(num, f"⛔ Ignored: issue author `{author}` is not authorized for VPS execution.")
        gh.set_status(issue, "agent:failed")
        gh.close(issue)
        return

    task = parse_task(issue.get("body") or "")
    gh.set_status(issue, "agent:running")
    gh.comment(num, "🤖 VPS agent accepted this task and started execution.")
    start = time.time()
    cwd: Path | None = None
    original_head = ""
    try:
        cwd = find_or_clone_repo(task, cfg, gh_token)
        original_head, branch, dirty = current_git_state(cwd)
        if dirty:
            raise AgentError(
                "Target checkout has pre-existing uncommitted changes. "
                "Refusing autonomous execution until they are committed/stashed.\n" + dirty[:4000]
            )

        ex_cfg = cfg.get("executor") or {}
        model = str(ex_cfg.get("model", "gpt-6-luna"))
        effort = str(ex_cfg.get("reasoning_effort", "medium"))
        max_turns = int(ex_cfg.get("max_turns", 40))

        summary = oa.run(task, cwd, model, effort, max_turns)
        ok, verify_log = run_verify(task["verify"], cwd, cfg, task)

        if not ok:
            esc_model = str(ex_cfg.get("escalation_model", "gpt-6-sol"))
            esc_effort = str(ex_cfg.get("escalation_reasoning_effort", "medium"))
            esc_turns = int(ex_cfg.get("escalation_max_turns", 30))
            gh.comment(
                num,
                f"⚠️ Verification failed after `{model}`. Escalating to `{esc_model}`.\n\n"
                f"```text\n{verify_log[-5000:]}\n```",
            )
            summary = oa.run(
                task,
                cwd,
                esc_model,
                esc_effort,
                esc_turns,
                extra_context=(
                    "The first executor completed, but deterministic verification failed. "
                    "Inspect the current working tree and fix it.\n\nVERIFICATION FAILURE:\n"
                    + verify_log[-10000:]
                ),
            )
            ok, verify_log = run_verify(task["verify"], cwd, cfg, task)

        if not ok:
            raise AgentError("Verification still failing after escalation:\n" + verify_log[-9000:])

        commit = commit_and_push(cwd, task, num, gh_token)
        duration = int(time.time() - start)
        usage = oa.usage
        result = (
            f"✅ **DONE**\n\n"
            f"Project: `{task.get('project', task['repository'])}`\n"
            f"Repo: `{task['repository']}`\n"
            f"Path: `{cwd}`\n"
            f"Commit: `{commit}`\n"
            f"Deploy requested: `{task['deploy']}`\n"
            f"Duration: `{duration}s`\n"
            f"API usage: input `{usage['input_tokens']}`, output `{usage['output_tokens']}` tokens\n\n"
            f"### Executor summary\n{summary[:8000]}\n\n"
            f"### Verification\n```text\n{verify_log[-6000:]}\n```"
        )
        gh.comment(num, result)
        gh.set_status(issue, "agent:done")
        gh.close(issue)
        if (cfg.get("telegram") or {}).get("notify_on_success", True):
            tg.send(
                f"✅ پروژه انجام شد\n"
                f"{task.get('project', task['repository'])}\n"
                f"Commit: {commit[:12]}\n"
                f"برو نتیجه را چک کن."
            )

    except Exception as exc:
        err = str(exc)
        rb = ""
        if cwd is not None and original_head:
            rb = rollback(task if "task" in locals() else {}, cwd, original_head, cfg)
        try:
            gh.comment(
                num,
                f"❌ **FAILED**\n\n```text\n{err[-9000:]}\n```\n\n"
                + (f"### Rollback\n```text\n{rb[-5000:]}\n```" if rb else ""),
            )
            gh.set_status(issue, "agent:failed")
            gh.close(issue)
        except Exception as comment_exc:
            log(f"could not report issue failure: {comment_exc}")
        if (cfg.get("telegram") or {}).get("notify_on_failure", True):
            tg.send(
                f"❌ اجرای پروژه ناموفق بود\n"
                f"Issue #{num}\n"
                f"{err[:1200]}\n"
                f"برای جزئیات issue را ببین."
            )
        log(f"issue #{num} failed: {err}")


def recover_running(gh: GitHub) -> None:
    for issue in gh.issues_with_label("agent:running"):
        try:
            gh.comment(issue["number"], "♻️ Agent daemon restarted; returning this task to the ready queue.")
            gh.set_status(issue, "agent:ready")
        except Exception as exc:
            log(f"recover issue #{issue.get('number')}: {exc}")


def handle_signal(_sig, _frame):
    global STOP
    STOP = True


def main() -> int:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    cfg = load_config()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    control_repo = str(cfg.get("control_repo", "aliiitavazoeiii-afk/my-agent"))
    if not openai_key:
        raise AgentError("OPENAI_API_KEY missing")
    if not github_token:
        raise AgentError("GITHUB_TOKEN missing")

    gh = GitHub(github_token, control_repo)
    gh.ensure_labels()
    tg = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
    recover_running(gh)

    interval = max(10, int(cfg.get("poll_interval_seconds", 20)))
    log(f"my-agent ready; control={control_repo}; poll={interval}s")

    while not STOP:
        try:
            issues = gh.issues_with_label("agent:ready")
            if issues:
                oa = OpenAIExecutor(openai_key, cfg)
                process_issue(issues[0], cfg, gh, tg, oa, github_token)
                continue
        except Exception as exc:
            log(f"poll error: {exc}")
        for _ in range(interval):
            if STOP:
                break
            time.sleep(1)

    log("my-agent stopped")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"fatal: {exc}")
        raise
