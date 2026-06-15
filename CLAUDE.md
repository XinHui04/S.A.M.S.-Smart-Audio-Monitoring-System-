# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Mandatory Pre-Change Workflow

**IMPORTANT: Before making ANY code changes, you MUST follow these steps in order. No exceptions.**

### Step 1 — Explain what the affected code does
Read all relevant files and explain to the user what the existing code does, how it works, and what role it plays in the system. Do not assume — read first.

### Step 2 — Present your plan
Describe how you intend to make the change: which files will be touched, what will be added/modified/removed, and why. Be specific (file paths, function names, layer responsibilities).

### Step 3 — Review the plan
Critically assess your own plan. Call out risks, edge cases, assumptions, or anything that could go wrong. Flag any uncertainty.

### Step 4 — Re-plan if needed
Revise the plan based on the review. If the original plan was sound, confirm it explicitly. If not, present a revised plan.

### Step 5 — Verify with the user before writing any code
Do NOT write any code yet. Ask the user clarifying questions to confirm: scope, intent, constraints, and acceptance criteria. Only proceed once the user has explicitly approved the plan.

### Step 6 — Agent execution model
Use an **orchestrator agent** (no coding — planning and coordination only) to break the work into discrete tasks. Spawn a **separate subagent per task** to implement each piece. The orchestrator must not write code itself.

### Step 7 — Apply secure coding practices
Every code change MUST follow secure coding practices. This is non-negotiable. At minimum, consider and defend against:
- **Injection** — SQL injection, command injection, LDAP injection (use parameterized queries, never concatenate user input)
- **XSS** — escape/sanitize any user-controlled content rendered in the UI
- **SSRF** — validate outbound URLs against the existing whitelist in `src/services/core/provider.ts`; never bypass it
- **Authentication & Authorization** — verify user identity and role/permission checks on every protected endpoint and route
- **Input validation** — validate and sanitize all inputs at trust boundaries (controllers, API handlers, form submissions)
- **Secrets** — never hardcode credentials, API keys, or tokens; use environment variables / configuration
- **Encryption** — respect the existing `Crypto.encrypt()` / `DecryptionMiddleware` contract; do not weaken or bypass it
- **Error handling** — do not leak stack traces, SQL errors, or internal paths to clients
- **Dependencies** — avoid introducing unvetted packages; prefer existing libraries already in the project

If a change touches security-sensitive code (auth, encryption, file upload, SQL, outbound HTTP), explicitly call out the security considerations in Step 2 (plan) and Step 3 (review).

### Step 8 — Test after making changes
After any code change, testing is mandatory. Do not consider the task complete until changes have been verified:
- **Frontend** — run `npm run build` to ensure the project compiles without TypeScript errors. Manually exercise the affected UI flows in `npm run dev` where applicable.
- **Backend** — run `dotnet build AgileAPServer.sln` to verify compilation, then `dotnet test AgileAPServer.sln` to run the test suite. For targeted changes, use `dotnet test --filter "FullyQualifiedName~SomeTestClass"`.
- **Cross-cutting** — if the change spans frontend + backend (e.g., new API endpoint), verify the end-to-end flow works against a running backend.
- Report test results to the user. If any test or build fails, diagnose and fix before declaring the task done.

---
