# Security Policy

## Supported versions

Security fixes are applied to the latest release and the `main` branch.

## Report a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/ferretgeek/llm-api-benchmark/security/advisories/new). Do not open a public issue containing a secret, private endpoint, exploit detail, or personal data.

Include the affected version, a minimal reproduction, impact, and any suggested mitigation. Remove real credentials and use reserved example domains in evidence.

## Security boundaries

- The dashboard is local-first and should be reached remotely only through an SSH tunnel or loopback-only port mapping.
- WebSocket control requires both same-origin and the high-entropy session token printed by the launcher; do not forward that URL.
- Keys and Base URLs are memory-only. If a crash dump, proxy, browser extension, or upstream service captures process memory or traffic, that is outside the persistence guarantee.
- Model output bodies and raw upstream errors are excluded from persistent previews, reports, history, and logs.
- A successful benchmark sends prompts and credentials to the configured upstream. Users are responsible for trusting that endpoint and understanding its billing and data policy.
- HTTPS is required for remote upstreams; loopback HTTP is allowed for local compatible gateways.
