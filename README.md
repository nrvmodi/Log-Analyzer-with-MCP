# Log Analyzer with MCP (Remote HTTP Edition)

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that gives AI assistants access to AWS CloudWatch Logs for search, analysis, and cross-service correlation.

This fork extends the original [awslabs/Log-Analyzer-with-MCP](https://github.com/awslabs/Log-Analyzer-with-MCP) so it can run as a **remote HTTP MCP server** (for example on EC2) and connect from **Cursor Remote Agents**, Claude Remote, and other HTTP MCP clients.

---

## What changed vs the original AWS CloudWatch MCP

| Area | Original AWS CW MCP | This version |
|------|---------------------|--------------|
| Transport | Primarily **stdio** (local process started by the client) | **stdio** (local) **and** **streamable-http** (remote URL) |
| Client connection | Client launches `uvx` / `python -m ...` locally | Client can connect to a **URL** like `http://host:8000/mcp` |
| Remote agents | Not designed for Cursor/Claude remote agents | Designed for remote agents that need a reachable HTTP endpoint |
| Authentication | Relies on local process / AWS credentials | Optional shared-secret header: `x-mcp-token` |
| Host protection | Basic FastMCP defaults | Trusted-host middleware + configurable allowlist |
| Stateless HTTP | Optional (`--stateless`) | Supported and recommended for remote MCP clients |
| Deployment model | Desktop / local machine | Desktop **or** EC2 / VM with a public or internal URL |

### In plain language

- **Original**: the AI tool starts the MCP server as a local child process (stdio).
- **This version**: you can still do that locally, **or** run the server once on a machine (EC2) and give clients a URL + token.

CloudWatch tools/resources (list groups, search, summarize, find errors, correlate) stay the same. The main upgrade is **how clients connect**.

---

## Features

- Browse and search CloudWatch Log Groups
- Search with CloudWatch Logs Insights
- Summarize activity and find error patterns
- Correlate events across log groups
- Local **stdio** mode (same idea as original)
- Remote **streamable-http** mode for EC2 / remote agents
- Optional **token auth** via `x-mcp-token`

More detail: [docs/features.md](./docs/features.md)

---

## Prerequisites

- [uv](https://github.com/astral-sh/uv) (Python package runner)
- Python 3.12+
- AWS credentials with CloudWatch Logs permissions
- For remote mode: a host that can listen on a port (example: EC2 with security group allowing the MCP port)

AWS setup guide: [docs/aws-config.md](./docs/aws-config.md)

---

## Quick Start (Remote HTTP — recommended for Cursor Remote Agents)

### 1) Start the server on EC2 (or any host)

```bash
cd Log-Analyzer-with-MCP
uv sync

export MCP_AUTH_TOKEN="REPLACE_WITH_LONG_RANDOM_SECRET"

uv run python -m cw_mcp_server.server \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8000 \
  --streamable-http-path /mcp \
  --stateless \
  --trusted-host "*"
```

Notes:

- Auth is enabled only when `MCP_AUTH_TOKEN` (or the env named by `--auth-token-env`) is set.
- `--trusted-host "*"` is convenient for first setup. For production, list exact hosts (DNS and IP).
- Open inbound TCP `8000` in the EC2 security group (or put HTTPS/Nginx in front and keep `8000` private).

Endpoint example:

```text
http://YOUR_EC2_PUBLIC_DNS:8000/mcp
```

### 2) Configure Cursor MCP (`mcp.json`)

```json
{
  "mcpServers": {
    "http-cloudwatch-logs-analyzer": {
      "type": "http",
      "url": "http://YOUR_EC2_PUBLIC_DNS:8000/mcp",
      "headers": {
        "x-mcp-token": "REPLACE_WITH_LONG_RANDOM_SECRET"
      }
    }
  }
}
```

Replace:

- the URL with your EC2 DNS/IP
- `REPLACE_WITH_LONG_RANDOM_SECRET` with the **same** value as `MCP_AUTH_TOKEN` on the server

### 3) Verify

Without token (expect `401`):

```bash
curl -i -X POST "http://YOUR_EC2_PUBLIC_DNS:8000/mcp" \
  -H "content-type: application/json" \
  -H "accept: application/json, text/event-stream" \
  -d "{}"
```

With token (expect `200` + MCP initialize response):

```bash
curl -i -X POST "http://YOUR_EC2_PUBLIC_DNS:8000/mcp" \
  -H "content-type: application/json" \
  -H "accept: application/json, text/event-stream" \
  -H "x-mcp-token: REPLACE_WITH_LONG_RANDOM_SECRET" \
  -d '{"jsonrpc":"2.0","id":"1","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

Then reload MCP in Cursor and confirm the server connects.

---

## Local stdio mode (same style as original AWS MCP)

If you only need a local desktop client (Claude Desktop / local Cursor), you can still use stdio:

```json
{
  "mcpServers": {
    "cloudwatch-logs-analyzer": {
      "command": "uv",
      "args": [
        "run",
        "python",
        "-m",
        "cw_mcp_server.server"
      ]
    }
  }
}
```

Or via package script after install:

```bash
uv run python -m cw_mcp_server.server --profile YOUR_PROFILE --region us-east-1
```

This mode does **not** need `type: http` or `x-mcp-token`.

---

## Server CLI options (new / important)

| Flag | Default | Purpose |
|------|---------|---------|
| `--transport` | `stdio` | `stdio`, `streamable-http`, or `sse` |
| `--host` | `0.0.0.0` | Bind address for HTTP modes |
| `--port` | `8000` | HTTP listen port |
| `--streamable-http-path` | `/mcp` | MCP route path |
| `--stateless` | off | Stateless HTTP (recommended for remote MCP) |
| `--trusted-host` | `*` | Allowed Host header values (repeatable) |
| `--auth-token-env` | `MCP_AUTH_TOKEN` | Env var that stores the shared secret |
| `--auth-header-name` | `x-mcp-token` | Header clients must send |
| `--profile` / `--region` | unset | AWS credentials/region override |

---

## Why security is required

This deployment is intentionally an **open network endpoint** so that **Cursor Cloud AI agents** (and other remote MCP clients) can reach it from outside your laptop.

That design choice is powerful — and it is exactly why security is mandatory.

### Why the endpoint must be reachable

Cursor Cloud agents do not run as a local process on your machine. They run in Cursor’s cloud environment and must call your MCP server over HTTP using a URL such as:

```text
http://YOUR_EC2_PUBLIC_DNS:8000/mcp
```

Because of that:

- The MCP server cannot stay limited to local `stdio` only.
- The endpoint must be reachable from the public internet (or a broad network path).
- The same URL that enables your AI employee also becomes visible to anyone who discovers it.

In short: **open access for Cursor Cloud agents also means open exposure unless you protect the endpoint.**

### What changes compared to the original local MCP

| Local stdio (original AWS CW MCP style) | Remote HTTP (this fork) |
|------------------------------------------|-------------------------|
| Client starts MCP as a local child process | MCP runs on a host (for example EC2) and listens on a URL |
| Only your local machine can talk to it | Cursor Cloud agents can talk to it from the internet |
| Exposure is limited to your desktop session | Exposure is network-wide unless authenticated |
| Token auth is usually unnecessary | Token auth is required for safe operation |

### What an attacker can do without protection

If `/mcp` is reachable and has no token:

1. **Anyone who finds the URL can call CloudWatch tools**  
   List log groups, search logs, pull recent errors, and correlate services — using *your* AWS permissions attached to the EC2 role/credentials.

2. **Sensitive operational data can leak**  
   Logs often contain request IDs, customer identifiers, stack traces, internal URLs, and error payloads. That is confidential business data.

3. **Abuse can create AWS cost and noise**  
   CloudWatch Insights queries and repeated API calls can generate unexpected cost and operational noise.

4. **Host discovery is easy**  
   Open ports, public DNS, and internet scanners make unauthenticated MCP endpoints discoverable over time.

5. **Cursor Cloud access increases blast radius**  
   Making the endpoint reachable for remote AI agents is required for automation. Without auth, that same open path is also usable by unauthorized callers.

### What security must provide (minimum)

| Control | Why it matters |
|---------|----------------|
| Shared token (`x-mcp-token`) | Lets Cursor Cloud agents authenticate while blocking anonymous callers |
| Trusted hosts | Reduces Host-header abuse / DNS rebinding style attacks |
| Least-privilege IAM | Limits what CloudWatch data the server can read even if compromised |
| HTTPS (recommended) | Protects token and payload in transit |
| Restricted network access where possible | Reduces exposure beyond auth |

### Important mindset

- **Open endpoint ≠ unauthenticated endpoint.**  
  The URL can be reachable for Cursor Cloud agents, but every request must still prove it holds a valid secret.
- **`--trusted-host` alone is not enough** — it only checks the Host header, not who the caller is.
- **Security Group IP allowlists alone are not enough for Cursor Cloud agents** — agent egress IPs are not a stable public whitelist.
- **Token auth is the practical minimum** for a remote MCP used by Cursor Cloud AI agents.

That is why this fork requires (or strongly recommends) `MCP_AUTH_TOKEN` + `x-mcp-token` for any internet-facing deployment used by Cursor Cloud agents.

---

## How authentication works

1. Set `MCP_AUTH_TOKEN` on the server host.
2. Server enables `TokenAuthMiddleware`.
3. Every HTTP request to `/mcp` must include:
   ```http
   x-mcp-token: <same-secret>
   ```
4. Missing/wrong token → `401 Unauthorized`.
5. Valid token → request continues into FastMCP streamable HTTP.

This is intentionally simple (shared secret). It does **not** require Cognito/OAuth.

For stronger production hardening, also:

- Prefer HTTPS (Nginx/ALB) in front of the app
- Restrict EC2 security group where possible
- Use least-privilege IAM for CloudWatch Logs
- Avoid exposing port `8000` publicly if you terminate TLS elsewhere

---

## Why not API Gateway by default?

MCP streamable-http often responds with `text/event-stream` (SSE/chunked). API Gateway HTTP APIs are a poor fit for that streaming model and can return opaque errors even when auth succeeds.

Recommended remote path:

1. Run MCP on EC2 with token auth (this README)
2. Optionally put Nginx/ALB + HTTPS in front later

---

## Architecture (remote)

```text
Cursor / Claude Remote Agent
        |
        | HTTPS or HTTP + x-mcp-token
        v
EC2 MCP Server (:8000/mcp)
        |
        | boto3 + IAM credentials
        v
AWS CloudWatch Logs
```

For ticket RCA automation (Slack + Zendesk + CloudWatch), see:

- [docs/ai-rca-automation-overview.md](./docs/ai-rca-automation-overview.md)

---

## Documentation

- [Usage Guide](./docs/usage.md)
- [AI Integration](./docs/ai-integration.md)
- [AWS Configuration](./docs/aws-config.md)
- [Architecture](./docs/architecture.md)
- [Features](./docs/features.md)
- [Troubleshooting](./docs/troubleshooting.md)
- [AI RCA Automation Overview](./docs/ai-rca-automation-overview.md)

---

## Security

Security is **required** for remote HTTP mode because the endpoint can read CloudWatch Logs with your AWS credentials. See [Why security is required](#why-security-is-required).

Operational checklist:

- Always set `MCP_AUTH_TOKEN` for internet-facing deployments
- Never commit real tokens, AWS keys, or live EC2 URLs to git
- Rotate `MCP_AUTH_TOKEN` periodically
- Prefer HTTPS in production
- Use least-privilege CloudWatch IAM permissions
- Treat the MCP URL as sensitive infrastructure
- See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for vulnerability reporting

---

## License

Apache-2.0 (same as upstream awslabs project).
