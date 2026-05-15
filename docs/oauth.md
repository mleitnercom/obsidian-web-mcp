# OAuth

Back to [README](../README.md).

The server exposes MCP over Streamable HTTP and protects it with OAuth 2.0-style connector flows plus bearer-token enforcement.

## Flow

1. Client discovers OAuth metadata through the well-known endpoints.
2. Client registers dynamically when supported.
3. Client starts an authorization-code flow with PKCE.
4. Server validates local login settings when configured.
5. Client exchanges the code for a token.
6. Client calls `/mcp` with bearer authorization.

Dynamic client registrations can be persisted so reconnects survive normal service restarts. Configure:

```env
VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS=true
VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH=/path/to/oauth_registered_clients.json
VAULT_REGISTERED_CLIENT_TTL_SECONDS=0
```

`0` disables automatic registration expiry.

## Public URLs Behind Tunnels

Set `VAULT_PUBLIC_BASE_URL` when running behind a reverse proxy or tunnel so OAuth metadata emits public HTTPS URLs rather than local HTTP URLs.

```env
VAULT_PUBLIC_BASE_URL=https://REPLACE_WITH_PUBLIC_HOSTNAME
VAULT_ALLOWED_HOSTS=127.0.0.1:*,localhost:*,[::1]:*,REPLACE_WITH_PUBLIC_HOSTNAME
VAULT_TRUSTED_PROXY_IPS=127.0.0.1,::1
```

## Connector Behavior

- **Claude:** Works with the `/mcp` connector URL and the standard OAuth flow.
- **ChatGPT:** Requires HTTPS metadata to match the public URL. `VAULT_PUBLIC_BASE_URL` is important behind tunnels.
- **Codex:** Discovery may succeed while the token exchange or final resource binding still depends on connector-side behavior. Check server and proxy logs before assuming an MCP server bug.

## Troubleshooting

### Connector gets 404

Use the full MCP endpoint URL ending in `/mcp`.

### Connector reports unsafe or HTTP URL

Set `VAULT_PUBLIC_BASE_URL` to the external HTTPS origin and restart the service.

### Auth works until service restart

Enable persistent registered clients and use a stable `VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH`.

### Token exchange fails after restart

Authorization codes are short-lived and in-memory. Re-run the connector flow after the service is stable.

### Dynamic client registrations pile up

Set `MAX_REGISTERED_CLIENTS` and optionally `VAULT_REGISTERED_CLIENT_TTL_SECONDS` for retention.

## Security Notes

Use real login credentials for `/oauth/authorize` when exposing the server outside loopback:

```env
VAULT_OAUTH_AUTH_USERNAME=REPLACE_WITH_USERNAME
VAULT_OAUTH_AUTH_PASSWORD=REPLACE_WITH_PASSWORD
VAULT_OAUTH_REQUIRE_APPROVAL=true
```

Do not store tokens or secrets in the vault, README, release notes, or smoke docs.
