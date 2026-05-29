# SOCKS Proxy Configuration

Route web tool traffic through a SOCKS proxy for privacy. All web tools (search, page fetch, wikipedia, research, site links, images) and network tools (IP/connectivity checks) use the proxy. LLM connections, model downloads, and TTS/STT do not.

If the proxy is enabled but broken, tools fail-secure rather than leaking to direct connections.

## Configuration

Open Settings → Network tab.

<img width="50%" alt="sapphire-network" src="https://github.com/user-attachments/assets/44d18b5c-2f92-4b06-acd6-b6ba26bc6f0f" />


1. Enable SOCKS proxy toggle
2. Set host and port (e.g., `127.0.0.1:9050` for Tor)
3. Click **Test Connection** to verify
4. **Enter a username and password** — required whenever SOCKS is enabled (web/network tools error if either is missing). For an auth-less proxy like Tor, enter any non-empty placeholder; Tor ignores it.

## Credentials

**Credentials are mandatory when SOCKS is enabled** — both username and password must be set, or every web/network tool fails (fail-secure, no direct-connection leak). For auth-less proxies (e.g. Tor on `127.0.0.1:9050`), enter any non-empty placeholder values. Sapphire checks for credentials in this order:

1. **Credential Manager** (checked first)
   - Enter in Settings → Network
   - Stored in `~/.config/sapphire/credentials.json` (not in Sapphire's user directory)
   - Not included in backups for security
   - Use the **Clear** button to remove stored credentials

2. **Environment variables** (fallback)
   - `SAPPHIRE_SOCKS_USERNAME`
   - `SAPPHIRE_SOCKS_PASSWORD`

## Verify It Works

1. Enable web tools in a chat's toolset
2. Ask the AI to fetch your IP via https://icanhazip.com/
3. Should show your proxy's IP, not your real IP

## Reference for AI

Route web tools through SOCKS5 proxy for privacy.

WHAT IT DOES:
- Affects ALL web tools (search, fetch, wikipedia, research, site links, images) and network tools (IP/connectivity checks)
- Does NOT affect: LLM connections, model downloads, TTS/STT
- Fail-secure: if proxy broken, tools error instead of leaking

SETUP:
1. Settings → Network → Enable SOCKS
2. Set host/port (e.g., 127.0.0.1:9050 for Tor)
3. Click Test Connection
4. Add credentials — REQUIRED when SOCKS is on (use any placeholder for auth-less proxies like Tor)

CREDENTIALS PRIORITY:
1. Credential Manager in Settings (stored in ~/.config/sapphire/)
2. Env vars: SAPPHIRE_SOCKS_USERNAME, SAPPHIRE_SOCKS_PASSWORD

VERIFY: Ask AI to fetch https://icanhazip.com/ - should show proxy IP

TROUBLESHOOTING:
- Tools error right after enabling: set BOTH username and password (required even for auth-less proxies — use any placeholder)
- Tools failing: Check proxy is running, verify host/port
- Auth errors: Check credentials in env vars or Settings
- Tor: Use port 9050 (SOCKS) not 9051 (control)
