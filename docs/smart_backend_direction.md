# Smart Backend Direction

## Decision

PalletPro should be built as a smart, hardened backend-first system. The backend is the brain of the product. AI and local LLMs can be added later as an optional presentation layer, but they should not be required for the system to make correct decisions.

## Core Principle

Backend code owns:

- business rules
- permissions
- audit trails
- workflow state
- risk detection
- recommendations
- data validation
- user and organisation access boundaries

The frontend owns:

- clear workflows
- low-friction forms
- useful summaries
- obvious next actions
- simple explanations for field and admin users

Local LLMs, if added, own only:

- plain-English explanations
- drafting text
- summarising already-approved backend facts
- optional assistant-style interaction

## Why This Route

PalletPro should not depend on every user having an external LLM API key, and PalletPro should avoid carrying broad LLM API costs for normal product behavior.

The product value should come from code we control:

- predictable backend logic
- testable rules
- secure permission checks
- auditable decisions
- structured operational insight
- reliable user workflows

This keeps the system cheaper to run, easier to debug, easier to test, and safer for customer data.

## Engineering Direction

Build deterministic backend intelligence before adding model-based assistance.

Examples:

- detect failed offline uploads and return exact repair steps
- detect stocktake variances and explain why review is required
- detect duplicate or suspicious transactions
- detect missing depots, partners, resources, or required transaction fields
- detect unusual stock movement
- detect risky login/session behavior
- detect pending approvals and route them to the right role
- calculate the next safe action for each workflow
- provide admin summaries from structured backend data

The backend should return structured insight payloads such as:

- severity
- reason
- affected records
- recommended action
- allowed user actions
- audit context

The frontend should render those insights directly without needing AI.

## Later Local LLM Layer

If local LLMs are added later, they should sit behind `palletpro-core`, not directly inside the browser.

Target shape:

```text
palletpro-app
  user interface for summaries, guidance, assistant panels

palletpro-core
  permission checks, data fetching, prompt construction, validation, audit logging

local LLM service
  optional explanation and drafting layer, for example Ollama with Gemma or similar
```

The LLM should never be allowed to directly mutate the database.

Initial LLM use should be read-only:

- rewrite backend facts in plain English
- summarise daily activity
- draft correction notes
- explain failed uploads
- explain pending approvals
- produce admin-readable summaries

Any action should still be confirmed by a user and executed by deterministic backend code.

## Product Intent

The goal is to reduce user friction without making AI a dependency.

Users should feel that PalletPro helps them do work they previously had to manually investigate:

- what needs attention
- why something failed
- what the next action is
- what changed today
- what requires admin review

Where possible, this should be achieved with backend intelligence and clear frontend UI. Local LLMs can become the cherry on top once the underlying system is already smart.
