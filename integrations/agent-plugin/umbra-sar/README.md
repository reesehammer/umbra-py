# umbra-sar

Search, preview, and open Umbra open SAR (CC BY 4.0) through the community
umbra MCP host at `https://api.umbra-py.space/mcp`. The plugin enables only
the core tools and ships a short skill.

## Install

**Folder import.** Add this directory (`integrations/agent-plugin/umbra-sar/`).
It contains `plugin.json`, `mcp.json`, and `skills/umbra-sar/SKILL.md`.

**Claude Code.** Register this repository as a marketplace, then enable
`umbra-sar`:

```
/plugin marketplace add reesehammer/umbra-py
```

Claude Code reads `.claude-plugin/plugin.json` and `.mcp.json` in this
folder. Those files match `plugin.json` and `mcp.json`.
