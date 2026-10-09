---
name: dia-sib-kb
description: Search and answer questions from the DIA SI B Confluence space (SIDCRSS). Use when someone asks about team processes, reports, tools, data flows, onboarding, or any DIA SI B topic. Delegates to the dia-sib-kb agent which searches Confluence directly.
---

# DIA SI B Knowledge Base — Skill Entry Point

This skill delegates all work to the **dia-sib-kb** agent.

## When invoked

The user typed `/dia-sib-kb` optionally followed by a question or topic.

## What to do

1. Extract the user's question from `args` (if provided). If `args` is empty, ask: "What would you like to know about DIA SI B?"
2. Spawn the `dia-sib-kb` agent with the question and any conversation context.
3. The agent searches Confluence, reads relevant pages, and returns a structured answer.
4. Present the answer directly — do not add preamble like "The agent found..." — just give the result.

## Usage examples

```
/dia-sib-kb what is the DIA SI B team responsible for?
/dia-sib-kb how does the Forecast report work?
/dia-sib-kb where can I find the onboarding documentation?
/dia-sib-kb explain the data flow from Snowflake to Power BI
```

## Sharability notes

This skill + its paired agent (`~/.claude/agents/dia-sib-kb.md`) are the two files needed. To share with a team member:
- Copy both files to their `~/.claude/` directory (agents/ and skills/dia-sib-kb/)
- Ensure they have the `confluence` MCP configured (`claude mcp list`)
- The Confluence token in their MCP config needs to be their own personal API token
