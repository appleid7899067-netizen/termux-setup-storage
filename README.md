# Termux Agent project

A small Python/HTML web UI that uses the Codex CLI locally, with optional Puter bridge scripts. This repository is prepared for editing with Codex Cloud.

## Contents
- `web_ui.py` and `web/index.html`: web interface and local backend
- `agent.py`, Puter bridge/proxy files, and theme

## Notes
- Local runtime state, credentials, terminal history, caches, and core dumps are intentionally excluded.
- The local web UI expects the Codex CLI and binds to localhost; the Cloud environment is for editing the source, not for exposing the local UI as a public service.
- Review changes in Codex Cloud before merging or downloading them back to Termux.
