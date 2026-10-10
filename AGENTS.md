# Agent instructions

## Project scope
- This repository is a Termux-oriented Python/HTML UI that launches the locally installed Codex CLI and includes optional Puter bridge/proxy scripts.
- Treat Codex Cloud as a source-editing and review environment, not as a public deployment target for the local UI.
- Keep Termux-specific runtime behavior intact unless the task explicitly asks to change it.

## Safe-change rules
- Inspect the relevant files and callers before editing.
- Prefer small, focused changes. Do not rewrite large files or change unrelated behavior.
- Do not add dependencies, configuration files, or new subsystems unless required by the task.
- Never commit credentials, authentication tokens, local state, terminal history, caches, or generated runtime data.
- Do not expose local-only services or bind them to public interfaces without an explicit request and a security review.
- Preserve existing user changes. Ask before destructive or broad changes.

## Validation
- For Python changes, run `python -m py_compile agent.py web_ui.py puter_responses_proxy.py puter_model_check.py` where the available Python version supports the code.
- For JavaScript changes, run `node --check puter-login.cjs` and/or `node --check puter-sdk-bridge.cjs` for the files changed.
- Report checks that could not be run in the environment instead of claiming they passed.
- Do not assume Codex CLI, Termux, Puter authentication, or Android-specific behavior is available in a Cloud environment.
