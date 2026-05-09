# Security Policy

## Scope

The main security-relevant surface of latex2word is API key handling: keys are read from `secrets.env` at runtime and passed to LLM provider clients. They are never logged, written to output files, or included in intermediate JSON artifacts.

## Reporting a Vulnerability

If you discover a security issue (e.g., API key leakage, path traversal in input handling, unsafe deserialization), please **do not open a public GitHub issue**.

Instead, report it by emailing the maintainer directly or using [GitHub's private vulnerability reporting](https://github.com/KLGR123/latex2word/security/advisories/new).

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a minimal proof-of-concept
- The version or commit you tested against

You can expect an acknowledgement within 72 hours and a fix or mitigation plan within 14 days for confirmed issues.

## Out of Scope

- Bugs in third-party LLM provider APIs
- Issues arising from users committing `secrets.env` to public repositories (do not do this)
