# Task: Inner LangGraph Migration

## Goal
Replace the six-stage collaboration executor in `MultiAgentCoordinator` with a
LangGraph state graph while retaining the outer task lifecycle runtime.

## Required
- Preserve planner, specialists, deliberation, evidence, verifier and arbiter behavior.
- Keep AgentLoop, ToolRegistry, collaboration messages, memory and public reviewer APIs.
- Keep `ReviewHarness`, task checkpoints, task cancellation and API contracts unchanged.
- Add a graph-structure regression test and run the existing collaboration tests.

## Optional
- Report the inner graph name in the collaboration summary.

## Done Means
- Inner collaboration is invoked through a compiled LangGraph graph.
- The multi-agent, runtime-memory and full backend test suites pass.

## Risks
- LangGraph state updates must preserve all fields used by later collaboration nodes.
- A missing dependency must fail clearly in installation, not at import time.

## Out Of Scope
- Replacing the outer `ReviewHarness` runtime.
- Changing database schema, API routes, Next.js UI or evaluation behavior.
