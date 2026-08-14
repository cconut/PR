# Task: Next.js backend fusion

## Goal
Integrate the updated EvoAgent Python backend while retaining the existing Next.js control plane and its two-service deployment.

## Required
- Promote the updated Python package as the runtime implementation.
- Keep `web/` as the only user-facing console.
- Expose the new runtime, memory, context, and skill-evolution capabilities through the existing API and Next.js UI.
- Preserve existing comments and add concise Chinese comments for added modules and integration points.

## Done Means
- `python -m evoagent` resolves to the updated backend package.
- Next.js continues to proxy backend API routes.
- Backend tests and the Next.js type check/build pass in an environment with the required runtimes.

## Risks
- Nested duplicate packages can cause Python to import the stale implementation.
- Existing SQLite data needs additive schema migrations.
- The frontend must retain its current routes and proxy behavior.

## Out Of Scope
- Replacing the Next.js console with the bundled static frontend.
- Changing repository authentication or Git history.
