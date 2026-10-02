# AGENTS.md

Strata runs the Qwen3.8-Flash-Next mixture-of-experts model (and its Coder, Swift 1.5 and Unsloth variants) on a
normal PC: one NVIDIA or AMD graphics card plus system RAM, on Windows or Linux. It has a C++/CUDA/HIP engine
(`src/`, `include/`), a Python server with an OpenAI- and Anthropic-compatible API and a web app (`serve/`), and a
one-click installer (`setup.py`, started by `START-HERE.bat` / `setup.sh`).

## Installing Strata for a user

Follow **[docs/AI_SETUP.md](docs/AI_SETUP.md)**: check the PC, pick the model by RAM, run setup non-interactively,
start and verify the server, and connect the user's apps. Never expose the server beyond `127.0.0.1` without
`--api-key`. As an alternative to shell commands, Strata's MCP server ([docs/MCP_SERVER.md](docs/MCP_SERVER.md))
offers the same steps as tools.

## Working on the code

- How the engine works, every measured number, the API and all settings: [docs/DETAILS.md](docs/DETAILS.md) and
  the [paper](docs/paper/Strata-Paper.pdf).
- AMD (HIP) build and validation: [docs/AMD_HIP.md](docs/AMD_HIP.md); multi-GPU: [docs/MULTI_GPU.md](docs/MULTI_GPU.md).
- Setup's own tests run without a GPU or downloads: `python tools/test_setup_<name>.py` (for example
  `tools/test_setup_amd.py`, `tools/test_setup_choices.py`).
- Keep the docs' style: plain words, measured numbers with what they were measured on, no claims without a
  measurement.

## Fork repository agent policy

## GitHub Issue creation and decision provenance

Before creating or materially rewriting a GitHub Issue, read this file, [`docs/DECISIONS.md`](docs/DECISIONS.md), the smallest current canonical document for the proposed change, and the relevant source/tests needed to avoid filing a stale or contradictory request.

Before doing architecture, runtime-structure, compatibility, migration, failure-boundary, or roadmap work — even when no Issue is being created — agents must read [`docs/DECISIONS.md`](docs/DECISIONS.md) first and then read the smallest current canonical document for the affected surface.

- Keep routine bug, task, and investigation Issues lightweight. Do not create a separate ADR document, `adr` label, lifecycle state, or machine-readable marker merely because an Issue exists.
- Treat an Issue as **decision-bearing** when it proposes or changes architecture, lifecycle semantics, protocol/schema/API behavior, ownership or source-of-truth boundaries, compatibility/migration policy, security/safety boundaries, cross-component contracts, or durable contributor/operations workflow.
- A decision-bearing Issue must record: **Context/problem**, **Proposed decision/contract**, **Rationale/evidence**, **Alternatives considered or rejected**, **Consequences/trade-offs/non-goals**, and **Acceptance/validation**. It must also identify the current canonical contract and the exact proposed delta when one already exists.
- Record decision status in ordinary prose. Default to **Proposed** unless the Issue is documenting a decision already explicitly accepted by the maintainer/user or current canonical contract.
- When a decision is replaced, link the superseding/superseded Issue or contract instead of leaving ambiguous competing guidance.
- A GitHub Issue is decision provenance, not automatically the current source of truth. Current canonical docs, repository contracts, source, and tests take precedence over stale or superseded Issue discussion.
- Once a decision is accepted and implemented, update the owning canonical documentation in the same change when the durable contract changed, and link the Issue/PR for provenance.
- Chat or agent conversation is not canonical repository memory by itself; durable conclusions that future contributors or agents must rely on belong in the Issue and/or the owning canonical document.
- Create a new GitHub Issue only when the user/maintainer explicitly requests Issue creation. A bare request to create an Issue grants Issue-creation authority only; it does not by itself accept an architectural or product decision.
- Never invent labels, states, branches, workflows, markers, or other repository objects while creating an Issue. Use only currently verified repository semantics.

Human-readable guidance and the decision-bearing Issue template live in [`docs/DECISIONS.md`](docs/DECISIONS.md).
