# AGENTS.md — Klipper for Mac

## Project premise (READ THIS FIRST)
Klipper for Mac makes Klipper work on macOS without modifying upstream projects.
This is the ENTIRE point of the project. If you break this, you break the project.

## Non-negotiable rules

### 1. Upstream is sacred
- Klipper, Moonraker, Mainsail, Fluidd source checkouts MUST remain unmodified
- `git status` inside any upstream/ directory MUST always be clean
- macOS compatibility comes ONLY from:
  - C header stubs (injected via CPATH)
  - Python venv shims (sitecustomize.py, ip shim)
  - Wrapper scripts and CLI commands
  - Config generation (moonraker.conf, printer.cfg snippets)
  - Separate tools that talk to upstream via standard APIs

### 2. Never do these things
- Do NOT patch/edit/monkey-patch Klipper or Moonraker source files
- Do NOT "fix" upstream Python by editing it directly
- Do NOT add pip packages that conflict with Moonraker's pinned dependencies
- Do NOT change printer.cfg semantics silently
- Do NOT enable LAN/external network access by default
- Do NOT disable tests to make them pass
- Do NOT force-skip linting or type checking

### 3. When you're unsure
- Pick the simplest safe option
- Document your assumption in the PR summary
- Only stop and ask if: credentials are missing, DB/files are missing AND required, or action is destructive/irreversible

### 4. Verification before claiming "done"
- shell scripts: `bash -n`
- Python: `python -m py_compile`
- Swift: `swiftc -parse` (or full build if possible)
- `klipperformac doctor` must pass
- `klipperformac selftest` must pass (if touching install/setup)
- `git status` in upstream/ dirs must be clean — PROVE THIS in your summary

### 5. macOS-specific awareness
- Respect camera/microphone privacy permissions
- Don't prevent system sleep unless user explicitly asked
- Use launchd or nohup for background processes, not systemd
- Homebrew paths vary (arm64 vs x86_64) — detect dynamically
- Python version matters: 3.12 for main stack, 3.14 for shaper only

### 6. PR format required
Every PR/change summary must include:
- Files changed (list)
- How verified (commands run + output)
- Assumptions made
- `git status` output for upstream/ directories
- Manual test steps for the human

## Project structure (for context)
- `src/` or `lib/` — klipperformac CLI and helpers
- `upstream/` — pristine checkouts of Klipper, Moonraker, web UIs
- `shims/` — C headers, Python shims, wrapper scripts
- `presets/` — printer config templates
- `specs/` — feature specifications
- `.app/` or `KlipperForMac.app/` — SwiftUI application bundle
- `install.sh` — main installer
- `uninstall.sh` — cleanup

## Definition of done
build passes + lint passes + doctor passes + selftest passes (if applicable) +
upstream git status clean + PR summary written