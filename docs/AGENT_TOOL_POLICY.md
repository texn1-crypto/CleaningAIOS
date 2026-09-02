# Agent read-only tool policy

## Default-deny gateway

Agent tasks may request tools through the optional `read_only_tools` array in
their payload. `AgentRuntime` resolves every request through one central registry.
There are no shell, browser, filesystem, arbitrary HTTP or write tools. The local
registry currently contains only bounded aggregate views:

- `workflow.status_counts`;
- `agent.slo_snapshot`;
- `system.integration_readiness` (configuration states with URL/source/credential
  fields removed).

Every tool has an exact agent allowlist. A batch also has a maximum call count,
per-call deadline, total deadline and serialized-result size limit. Policy denials,
timeouts, failures and successes are stored in `agent_tool_calls` with only an
argument digest and aggregate execution metadata. Raw arguments and results are
not copied to this audit table. Successful results are labelled `read_only` and
attached to the normal `AgentRun` evidence.

## Optional remote MCP

Remote MCP is disabled by default. `AGENT_MCP_READ_SERVERS_JSON` can declare at
most ten reviewed Streamable HTTP servers, each with:

- an explicit `mode: "read_only"`;
- an exact HTTPS endpoint in production;
- a finite tool list and agent allowlist;
- an optional environment-variable reference beginning with `MCP_READ_`;
- a bounded timeout.

The client uses the stateless MCP `2026-07-28` `tools/call` envelope and required
`MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` headers. It never follows
redirects, never discovers or invokes tools outside the configured list and does
not support asynchronous tasks or interactive input requests. This follows the
[official MCP 2026-07-28 transport release](https://blog.modelcontextprotocol.io/posts/2026-07-28/).

Remote arguments over 8 KiB, credential-like keys, email addresses, phone numbers
and card-like values are rejected before the network call. Returned content is
marked `untrusted_external_data`; prompt instructions must continue treating it as
data. Configure only a source whose terms and data classification have been
reviewed. The gateway cannot prove that a third-party tool is read-only, so its
name must not be added merely because the provider describes it that way.

Example (keep secrets out of this JSON):

```text
AGENT_MCP_READ_SERVERS_JSON=[{"name":"approved_public","url":"https://example.org/mcp","mode":"read_only","allowed_agents":["research"],"tools":["search"],"timeout_seconds":2,"secret_ref":"MCP_READ_APPROVED_PUBLIC_TOKEN"}]
```

`GET /api/agent-tools` is manager-only and exposes the effective limits and tool
names, but not endpoints, secret references, handlers or credentials.
