# /raken-perplexity — Raken API Research-Grounded Implementation via Perplexity

Run a Raken API-focused research query using Perplexity's research mode. **Every query is grounded in the actual Raken Public API 3.0 documentation** — endpoints, OAuth flow, response schemas, rate limits, and existing integration scripts are loaded and injected as mandatory context before any research or implementation planning begins.

**MANDATORY: You MUST read the Raken API documentation directory BEFORE building any query, designing any approach, or planning any implementation. Skipping the documentation read is a protocol violation. NO EXCEPTIONS.**

**CRITICAL — TWO-PASS VERIFICATION IS MANDATORY EVERY INVOCATION. After receiving Perplexity results and synthesizing a plan (Step 5), you MUST send that plan back to Perplexity for critique (Step 6) BEFORE calling ExitPlanMode. NEVER call ExitPlanMode without completing Step 6. Skipping verification is a protocol violation. NO EXCEPTIONS.**

**CRITICAL: Do NOT ask the user questions before completing Step 0 through Step 1. Compile context silently, build the query, and execute. Only ask questions if ARGUMENTS is empty AND you cannot determine a useful research focus from the compiled context.**

**MANDATORY CONTEXT PREAMBLE — EVERY QUERY, NO EXCEPTIONS:** Every single query sent to Perplexity (Step 2 AND Step 6.3) MUST begin with the following preamble block. This is a hard rule — never omit it, never paraphrase it, never move it to a footnote. It goes at the TOP of every query, before any other content:

```
[ENVIRONMENT CONTEXT — READ FIRST]
This project is being developed using Claude Code, Anthropic's official CLI tool for Claude (claude.ai/claude-code). The developer uses a Claude Max subscription and works entirely in the terminal via the `claude` CLI command. Claude Code is an agentic coding assistant that reads/writes files, runs terminal commands, searches codebases, and executes multi-step development tasks autonomously. All code generation, refactoring, debugging, and project management happens through Claude Code's conversation interface — there is no IDE or GUI involved. Responses should account for this workflow: recommend CLI-compatible tools, terminal-based solutions, and approaches that work well with an AI coding agent operating in a command-line environment.
[END ENVIRONMENT CONTEXT]
```

## Input

`$ARGUMENTS` = The Raken API research question, integration task, or implementation goal. Examples:
- "extract historical time cards for certified payroll reports"
- "build automated token refresh flow"
- "sync Raken members with Foundation Software workers"
- "design daily report extraction pipeline"
- Empty = defaults to general Raken integration strategy analysis

## Raken API Documentation Directory

All documentation lives at:
```
$RAKEN_REFERENCE_DIR
```

### Key Files (MUST be read in Step 0)

| File | Purpose |
|------|---------|
| README_API_TESTING.md | Quick start guide, OAuth credentials, endpoint summary, test workflow |
| Raken_API_SOP.html | Full OAuth 2.0 SOP — setup, flow, troubleshooting, production deployment |
| Raken+Public+API+3.csv | Complete API reference — all endpoints, parameters, response schemas, paging, rate limits |
| raken_make_calls.py | Working Python implementation — API call patterns, token loading, error handling |
| raken_get_token.py | OAuth code-to-token exchange implementation |
| raken_auth_url.py | Authorization URL generation |
| raken_token.json | Current token state (if exists) — check expiry |

### Raken API Quick Reference (baked into every query)

**Base URL**: https://developer.rakenapp.com/api/
**Auth URL**: https://app.rakenapp.com/oauth/authorize
**Token URL**: https://app.rakenapp.com/oauth/token
**Token lifetime**: 36,000 seconds (10 hours)
**OAuth grant types**: authorization_code, refresh_token
**Paging**: offset/limit approach (max 1000 per page)
**Rate limiting**: Per-second rate limit, burst limit, daily quota (plan-dependent)

**Available Endpoints**:
- /userinfo — authenticated user details
- /projects — list/create/update projects (ACTIVE, INACTIVE, DELETED)
- /members — team members with roles and classifications
- /timeCards — time entries by project, date range, worker
- /costCodes — job cost codes by project
- /payTypes — pay type definitions (REG, OT, etc.)
- /shifts — work shift configurations
- /classifications — worker trade classifications
- /budgets — project budgets
- /certificationTypes, /certifications — worker certifications
- /checklistTypes, /checklists — inspection checklists
- /dailyReports — daily field reports
- /equipment, /equipmentLogs — equipment tracking
- /groups — organizational groups
- /materialLogs, /materialUnits, /materials — material tracking
- /observations — safety observations
- /toolboxTalks — safety meeting records
- /workLogs — work activity logs

## Workflow

### Step 0: Load Raken API Documentation — MANDATORY, SILENT, FIRST

**This step runs BEFORE everything else. No exceptions.**

Read the following files from the Raken API documentation directory. Do NOT skip any file. Do NOT ask the user questions during this step.

1. **Read README_API_TESTING.md** — extract OAuth credentials (client_id, client_secret, redirect_uri), endpoint list, test workflow
2. **Read Raken+Public+API+3.csv** (first 300 lines) — extract endpoint definitions, query parameters, response field schemas, paging rules, rate limits
3. **Read raken_make_calls.py** — extract existing API call patterns, base URL, error handling approach, token loading pattern
4. **Read Raken_API_SOP.html** — extract OAuth flow steps, troubleshooting patterns, security practices, production checklist
5. **Check raken_token.json** (if exists) — note token expiry status
6. **Synthesize Raken API Context Block** — compile a structured summary of:
   - All available endpoints with their HTTP methods and key parameters
   - OAuth flow state (credentials, token status)
   - Existing implementation patterns from the Python scripts
   - Rate limiting and paging constraints
   - Known troubleshooting patterns

Hold this as the **RAKEN_CONTEXT** block for injection into every Perplexity query.

### Step 0.5: Compile Session Context — MANDATORY, SILENT

After loading Raken docs (Step 0), compile the current session state:

1. **Read project memory**: Read the project MEMORY.md from the auto-memory directory
2. **Recent commits**: Run `git log --oneline -10` to see recent work
3. **Uncommitted work**: Run `git diff --stat` to see what's in progress
4. **Active tasks**: Check TaskList for any active/pending tasks
5. **Synthesize**: Form a 1-paragraph internal "current state" summary

Do NOT present findings. Do NOT ask questions. Proceed to Step 0.75.

### Step 0.75: Explore Codebase — MANDATORY, SILENT

1. **Find key files**: Use Glob for main source files in the current project
2. **Read recently modified**: Run `git diff --name-only HEAD~5 HEAD`, read up to 10 files (first 100 lines each)
3. **Read structural files**: README.md, pyproject.toml, package.json, CLAUDE.md if they exist
4. **Synthesize**: Form internal "codebase summary"

Do NOT present findings. Proceed to Step 1.

### Step 1: Build the Research Query

Using the RAKEN_CONTEXT from Step 0 + session context from Step 0.5 + codebase from Step 0.75, build the research query.

**Start with the MANDATORY CONTEXT PREAMBLE**, then append:

```
[RAKEN API DOCUMENTATION CONTEXT — AUTHORITATIVE REFERENCE]
The following is extracted directly from the Raken Public API 3.0 documentation and existing integration code. All recommendations MUST be compatible with these API capabilities and constraints. Do NOT suggest endpoints, parameters, or behaviors that are not documented here.

{INSERT RAKEN_CONTEXT BLOCK FROM STEP 0}
[END RAKEN API CONTEXT]

You are a construction technology integration specialist analyzing a Raken API integration task. Given the API documentation context and project state, provide strategic analysis and concrete implementation steps.

FOCUS AREA: {ARGUMENTS or "general Raken integration strategy — what should be the priority?"}

PROJECT CONTEXT:
{Session context from Step 0.5}

CODEBASE CONTEXT:
{Key code snippets from Step 0.75}

Please analyze and respond with:
1. API CAPABILITY CHECK: Which Raken endpoints are needed for this task? Are there any gaps where the API does not support required functionality?
2. OAUTH FLOW STATUS: Is the current auth setup sufficient, or does the token refresh flow need implementation?
3. DATA MODEL MAPPING: How do Raken's response schemas map to the target data model? Include field-level mappings.
4. IMPLEMENTATION APPROACH: Step-by-step plan with specific API calls, parameters, and expected response handling
5. RATE LIMIT STRATEGY: How to handle paging and rate limits for bulk data extraction
6. ERROR HANDLING: Common failure modes for each endpoint and recovery strategies
7. INTEGRATION RISKS: What could go wrong — API limitations, data quality issues, missing fields
8. EXISTING CODE REUSE: Which existing scripts/patterns from the Raken API directory can be reused or extended
```

### Step 2: Run Research Query

Call the research_query MCP tool with:
- query: The prompt from Step 1
- includeContext: true

This runs Playwright browser automation with Perplexity research mode.

### Step 3: Read Results

Present the key findings to the user in a concise summary, organized by the 8 analysis dimensions from Step 1.

### Step 4: Persist Results

Save output to the council-logs directory: `{YYYY-MM-DD_HHmm}-raken-{topic-slug}.md`

### Step 5: Synthesize Plan — MANDATORY

**IMMEDIATELY after receiving research results, enter plan mode using EnterPlanMode.** Do not ask the user — go straight into plan mode.

**CRITICAL: Cover ALL recommendations from the research. Never filter or skip — build the complete plan automatically.**

In plan mode, create a two-tier plan structure:

#### Tier 1: Master Plan

1. Read relevant project files identified in the research findings
2. Cross-reference ALL recommendations against the Raken API documentation (re-read specific CSV sections if needed to validate endpoint parameters)
3. List every priority as a numbered Phase in execution order:
   - Phase ordering: OAuth/auth first, then data layer, then extraction logic, then integration, then testing
   - Each Phase gets: title, 1-line goal, estimated complexity (S/M/L), prerequisite phases
   - Group related API endpoints into the same phase when they serve the same business function
4. The master plan should read like a table of contents with dependency arrows

#### Tier 2: Sub-Plans

For each Phase, write a detailed sub-plan:
- Specific Raken API endpoints to call (with exact parameters)
- Response field mappings (Raken field name to target field name)
- Python code approach (extending existing patterns from raken_make_calls.py)
- Paging strategy for endpoints that return paginated results
- Error handling for each API call
- Acceptance criteria — how to verify this phase is done
- Risk mitigations from the research findings

#### Required final sections (in every plan):

- **Second-to-last phase: Update project memory** — follow these 6 rules:
  1. MEMORY.md stays under 150 lines — move implementation details to topic files
  2. No duplication between MEMORY.md and CLAUDE.md
  3. New session-learned patterns go in MEMORY.md; implementation details go to topic files
  4. Delete outdated entries rather than accumulating
  5. If adding a new topic file, add a 1-line entry to the Topic File Index in MEMORY.md
  6. Topic file naming: kebab-case.md
- **Final phase: Commit and Push** — commit all changes and push to remote

Write the full plan, then proceed to Step 6.

### Step 6: Verify Plan via Second Perplexity Pass — MANDATORY, NO EXCEPTIONS

**This step is the hard gate before ExitPlanMode. NEVER skip it.**

#### 6.1: Build the Verification Query

Construct a critique-focused query containing the complete plan, research summary, Raken API context, and codebase context.

**Start with the MANDATORY CONTEXT PREAMBLE**, then append:

```
[RAKEN API DOCUMENTATION CONTEXT — AUTHORITATIVE REFERENCE]
{RAKEN_CONTEXT BLOCK FROM STEP 0}
[END RAKEN API CONTEXT]

You are a senior construction technology architect reviewing a Raken API integration plan. Critically evaluate this plan for correctness, completeness, and feasibility against the documented API capabilities.

## Plan to Review
{complete plan text from Step 5}

## Research Context
{summary of findings from Step 3}

## Codebase Context
{key file snippets from Step 0.75}

Please evaluate:
1. API ACCURACY: Does the plan reference correct endpoints, parameters, and response fields per the Raken API docs?
2. MISSING ENDPOINTS: Are there Raken API capabilities the plan should use but does not?
3. OAUTH COMPLETENESS: Does the auth flow handle token expiry and refresh correctly?
4. PAGING CORRECTNESS: Will the paging strategy capture all records, or could data be missed?
5. RATE LIMIT COMPLIANCE: Will the implementation stay within Raken rate limits?
6. DEPENDENCY ORDERING: Are phases ordered correctly?
7. SCOPE CREEP: Does the plan include unnecessary work?
8. FEASIBILITY: Are estimated complexities realistic?
9. DATA INTEGRITY: Are there edge cases where Raken data might be incomplete or malformed?
10. VERDICT: APPROVED (proceed as-is) or REVISE (with specific changes needed)
```

#### 6.2: Run Verification

Call research_query MCP tool with the critique prompt and includeContext: true.

#### 6.3: Revise Plan (if needed)

- If critique identifies issues: revise the plan accordingly. **Maximum 1 revision pass.**
- If critique returns APPROVED: proceed as-is.

#### 6.4: Exit Plan Mode

**Only after completing 6.1-6.3**, call ExitPlanMode for user approval.

#### Error Handling for Step 6

If research_query fails in Step 6:
1. Retry once
2. If retry also fails: note the failure reason in the plan file, proceed to ExitPlanMode

### Step 7: Post-Approval Execution

After the user approves the plan:
- Use TaskCreate to create one task per Phase from the master plan
- Set dependencies with addBlockedBy matching the phase prerequisites
- Each task description should contain the full sub-plan for that phase
- Begin executing the first unblocked task

## Key Differences from Other Commands

| Aspect | /raken-perplexity | /research-perplexity | /implement-perplexity |
|--------|-------------------|---------------------|----------------------|
| **Purpose** | Raken API-grounded research and implementation | General strategic analysis | Blueprint to validated plan |
| **Mandatory docs** | YES — Raken API docs read before every query | No domain docs required | Creative-research logs |
| **Domain context** | Construction tech, certified payroll, Raken API | General | General |
| **API validation** | Checks plans against actual Raken endpoint docs | No API validation | No API validation |
| **Queries** | 2 (research + verify) | 2 (research + verify) | 2-4 targeted |
| **Cost** | Free (Perplexity login session) | Free | Free |

## Error Handling

- **Raken docs not found**: STOP. Report: "Raken API documentation directory not found at expected path. Verify the path exists."
- **Session expired**: Report "Run /cache-perplexity-session to refresh Perplexity login session"
- **Research mode not available**: Falls back to regular Perplexity query
- **Empty results**: Retry once. If still empty, report session may need refresh.
- **Token expired**: Note in research query that token refresh must be implemented before any API calls
