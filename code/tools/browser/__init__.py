"""A real browser for the agent on vault-host: a Kernel cloud browser driven by Playwright MCP.

How it works, why, and the enable steps live in the vault at `_system/agent-browser.md`.
Code lives here.

    kernel.py   the Kernel REST API (browsers, profiles), stdlib only, a mockable transport
    state.py    per-conversation state on disk: which profile, which Kernel browser, the refs
    proxy.py    the MCP server the bridge starts: Playwright MCP behind a stdio filter and a
                local /json/version shim that hands it the Kernel browser's CDP URL
    __main__.py `vt browser use|login|close|status|profiles|mcp`

One conversation (one Claude session, `BROWSER_SESSION`) has at most one Kernel browser,
loaded with one named profile per site (`speechify`, `spokeo`). Logins happen once, by
the owner, in Kernel's live view; no password or card number is ever stored here.
"""
