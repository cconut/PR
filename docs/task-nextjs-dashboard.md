# Task: Next.js Dashboard Migration

## Goal
Replace the static dashboard with a Next.js App Router application and a quieter engineering-operations visual system.

## Required
- Preserve review submission, task inspection, repair creation, skill reload, prompt evolution, login, and GitHub App flows.
- Proxy existing Python API routes through the Next.js server.
- Support desktop, tablet, and mobile layouts.
- Provide local and Docker startup paths.

## Done Means
- The Next.js production build passes.
- Existing API routes are still called with their current payloads.
- The dashboard renders useful loading, empty, error, and authenticated states.
- Python `8080` redirects its root page to the Next.js console on `3000`.

## Risks
- Authentication state is browser-local and must only be accessed on the client.
- GitHub redirects and webhook routes must continue to reach the Python service.

## Out Of Scope
- Backend API contract changes.
- New Agent, evaluation, or persistence behavior.
