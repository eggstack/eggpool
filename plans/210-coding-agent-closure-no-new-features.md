# Plan 210: Coding-agent closure scope guard

> **Status:** READY FOR IMPLEMENTATION
>
> **Parent:** Plan 203
>
> **Priority:** Scope guard
>
> **Scope:** prevent the closure pass from accumulating unrelated feature work.

## Rule

During execution of Plans 203–209, do not add new coding-agent features unless a current supported client fails without them and the requirement cannot be addressed within an existing narrow compatibility boundary.

Examples that are **not** part of this closure pass by default:

- Responses WebSockets;
- persisted `previous_response_id` state;
- conversation/background APIs;
- OpenCodex-private history/notes/account-affinity surfaces;
- server-side tool execution;
- image/voice APIs;
- active provider probing from `status`;
- a new router or capability database solely for Codex/OpenCode.

If one of these becomes a real blocker, stop and write a new roadmap rather than extending Plan 203 silently.

This plan introduces no implementation work unless scope pressure appears during handoff.
