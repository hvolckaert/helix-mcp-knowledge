# Professional positioning kit: IntelliAgentia and the Helix MCP family

This copy deck provides one consistent public narrative for the IntelliAgentia website,
LinkedIn, presentations, project directories, and future publications. It describes two
independent open-source MCP servers; it does not claim official affiliation with BMC or
OpenAI, production suitability, or unattended authority over a Helix environment.

The video work remains deferred. Do not publish empty video placeholders or describe a
documented walkthrough as a recorded demonstration.

## Core message

**Brand line:** Knowledge you can trace. Actions you can control.

**Supporting line:** Evidence-grounded context and policy-controlled access for an
autonomous agent specialized in BMC Helix.

**Short family description:**

> IntelliAgentia develops two independent local MCP servers for specialized BMC Helix
> agents: Helix MCP Knowledge retrieves traceable documentary evidence, while Helix MCP
> Gateway provides policy-controlled access to authorized live environments.

**Long family description:**

> A useful Helix agent needs more than a capable model. It needs documentary context that
> can be traced to the right product, version, project and section, and it needs operational
> access that remains bounded by explicit targets, permissions, review and audit.
>
> IntelliAgentia addresses those responsibilities with two separate local MCP servers.
> Helix MCP Knowledge retrieves evidence from selected official BMC documentation and
> authorized project knowledge while preserving provenance and project isolation. Helix
> MCP Gateway exposes bounded reads, reviewable read-only SQL and human-approved single
> record changes through the permissions of an authorized Helix account. An agent may use
> both servers, compare documented expectations with observed state and stop when the
> evidence or authority is insufficient. Neither server calls the other, and neither
> expands the permissions it has been given.

## Product descriptions

### Helix MCP Knowledge

**One line:** Traceable documentary evidence for agents working across the BMC Helix
ecosystem.

**Short description:**

> Helix MCP Knowledge is a local MCP server that searches selected official BMC Helix
> documentation and authorized project knowledge with explicit product, version, source,
> section and project provenance.

**Boundary statement:**

> Knowledge explains what the selected evidence supports. It does not inspect or change a
> live Helix environment, embed an LLM, or allow one project's documents to silently enter
> another project's context.

### Helix MCP Gateway

**One line:** Policy-controlled MCP access to authorized BMC Helix environments through
the AR API.

**Short description:**

> Helix MCP Gateway is a local MCP server for bounded Helix reads, reviewable read-only SQL
> and human-approved single-record changes under explicit environment, form, field and
> approval policies.

**Boundary statement:**

> Gateway can narrow the permissions of the configured Helix account but cannot expand
> them. It does not expose deletion, bulk writes, direct database access, permission
> elevation or unattended production automation.

## LinkedIn profile draft

### Headline

> Enterprise AI and BMC Helix | Building IntelliAgentia | Helix MCP Knowledge + Gateway |
> Traceable evidence and policy-controlled action

### About

> I design and build agentic systems for enterprise environments where useful automation
> must coexist with permissions, evidence, review and operational accountability.
>
> Through IntelliAgentia, I am developing two independent open-source MCP servers for an
> autonomous agent specialized in BMC Helix.
>
> Helix MCP Knowledge gives the agent a local documentary layer. It retrieves selected
> official BMC documentation and authorized project knowledge with explicit provenance,
> version context and strict separation between projects.
>
> Helix MCP Gateway gives the agent a separately governed path to live Helix environments.
> It supports bounded reads, reviewable read-only SQL and human-approved single-record
> changes through explicit policy and the permissions of the configured Helix account.
>
> The central design principle is separation of authority: documentation is not live state,
> a plausible inference is not evidence, and a proposed action is not approval. The agent
> can combine both servers, but each retains its own responsibilities and controls.
>
> My work focuses on local-first integration, inspectable workflows, safe failure modes and
> practical tools that help qualified professionals make better decisions without hiding
> risk behind an AI interface.
>
> Explore the projects and reproducible cases at https://intelliagentia.com/.

### Featured section

Recommended order:

1. **IntelliAgentia — Knowledge you can trace. Actions you can control.**  
   `https://intelliagentia.com/`  
   Overview of the product family, architecture and public demonstrations.
2. **Integrated Knowledge + Gateway demonstrations.**  
   `https://intelliagentia.com/demos/`  
   Sanitised cases showing traceable evidence, bounded live observation and governed
   action without presenting private Helix data.
3. **Helix MCP Knowledge.**  
   `https://github.com/hvolckaert/helix-mcp-knowledge`  
   Local evidence retrieval with provenance, version context and project isolation.
4. **Helix MCP Gateway.**  
   `https://github.com/hvolckaert/helix-mcp-gateway`  
   Policy-controlled AR API access with bounded reads, reviewable SQL and approved writes.

Do not add a video entry until a recording, public transcript, thumbnail and stable URL
all exist.

## LinkedIn project entries

### IntelliAgentia

**Role:** Founder and builder

> Independent product practice focused on practical, controlled enterprise AI. I design
> systems in which evidence, permissions, review and recovery remain explicit outside the
> model prompt. The first product family provides complementary documentary and operational
> tools for agents specialized in BMC Helix.

Link: `https://intelliagentia.com/`

### Helix MCP Knowledge

**Role:** Creator and maintainer

> Designed and implemented a local MCP server for evidence-grounded retrieval across
> selected BMC Helix documentation and authorized project knowledge. The system preserves
> product, version, source, section and project provenance; uses SQLite/FTS5 as its base
> retrieval path; and keeps semantic search, reranking and project-PDF OCR optional.

Links:

- `https://helix-mcp-knowledge.intelliagentia.com/`
- `https://github.com/hvolckaert/helix-mcp-knowledge`

### Helix MCP Gateway

**Role:** Creator and maintainer

> Designed and implemented a local policy gateway between MCP-capable agents and authorized
> BMC Helix environments through the AR API. It makes target selection explicit, bounds
> form and SQL reads, requires plan/review/apply workflows for supported writes, and keeps
> audit output free of business payloads.

Links:

- `https://helix-mcp-gateway.intelliagentia.com/`
- `https://github.com/hvolckaert/helix-mcp-gateway`

## Publication checks

- Use `https://intelliagentia.com/` as the common family destination.
- Keep Knowledge and Gateway as separate products, repositories, runtimes and authorities.
- Link technical claims to the relevant repository, documentation or public case.
- Describe private-environment cases only through sanitized counts, decisions and control
  outcomes.
- Do not imply official endorsement by, or affiliation with, BMC or OpenAI.
- Do not describe a human-reviewed validation as unattended autonomous operation.
- Recheck release numbers and capability claims before each publication.
- Add articles or videos only after their public URLs and supporting materials are stable.
