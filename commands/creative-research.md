# /creative-research — 3-Stage Creative Feature Discovery

Run a divergent ideation pipeline to discover novel features for the current project. Unlike convergent research commands (`/research-perplexity`, `/export-to-council`), this command brainstorms **new ideas the user hasn't thought of**, then scores viability and generates implementation blueprints.

**3 sequential Perplexity queries via Playwright** — ~5-7 min total, $0/query.

**CRITICAL: Do NOT ask the user questions before completing Step 0 and Stage 1. Compile context silently, build all queries, and execute. Only interact after Stage 3 results are ready.**

## Input

`$ARGUMENTS` = Optional focus area for creativity (e.g., "AI-powered features", "developer experience", "monetization"). If empty, brainstorm broadly across all dimensions.

## Workflow

### Step 0: Compile Session Context — MANDATORY, SILENT

**Before doing ANYTHING else**, compile the current session state. Do NOT ask the user any questions during this step — proceed silently and autonomously.

1. **Read project memory**: Read the project's `MEMORY.md` from the auto-memory directory to understand what's been worked on, recent patterns, and known issues
2. **Recent commits**: Run `git log --oneline -10` to see recent work
3. **Uncommitted work**: Run `git diff --stat` to see what's in progress
4. **Active tasks**: Check `TaskList` for any active/pending tasks
5. **Synthesize**: Form a 1-paragraph internal "current state" summary — do NOT output this to the user, just hold it in context

Do NOT present findings. Do NOT ask questions. Proceed directly to Step 0.5.

### Step 0.5: Explore Codebase — MANDATORY, SILENT

After compiling session context (Step 0), explore the actual codebase:

1. **Find key files**: Use `Glob` for main source files (*.py, *.ts, *.js) in project root and src/
2. **Read recently modified**: Run `git diff --name-only HEAD~5 HEAD`, read up to 10 files (first 100 lines each)
3. **Read structural files**: README.md, pyproject.toml, package.json, CLAUDE.md if they exist
4. **Synthesize**: Form internal "codebase summary" — key files, purposes, architecture, tech stack

Do NOT present findings. Do NOT ask questions. Proceed directly to Step 1.

### Step 1: Close Browser Bridge Sessions — MANDATORY

**Before launching any Playwright-based query**, close active browser-bridge sessions to prevent DevTools Protocol collisions:

1. Call `mcp__browser-bridge__browser_close_session` to release all browser-bridge tab connections
2. Wait 2 seconds (`sleep 2` via Bash) for Chrome DevTools to fully detach
3. Then proceed to Stage 1

**Why:** The `research_query` tool launches Playwright. If `browser-bridge` has active Chrome DevTools connections, the two systems collide — causing tab detachment errors, empty results, and `"Debugger is not attached"` failures.

---

## Stage 1: Creative Ideation (Query 1/3)

Build and execute the divergent brainstorming query.

### Query Construction

Using the compiled context from Step 0/0.5, compose the ideation query:

```
You are an innovation consultant specializing in software product development. You've been given full context about a project (architecture, tech stack, recent work, current state).

FOCUS AREA: {$ARGUMENTS or "all dimensions — features, UX, integrations, developer experience, performance, monetization, novel interactions"}

Your task is DIVERGENT THINKING — creativity over feasibility. Generate 10-15 UNIQUE, NOVEL feature ideas that the development team likely hasn't considered. Avoid obvious or incremental improvements.

For each idea, provide:
1. **Name**: A catchy, descriptive feature name
2. **Elevator Pitch**: 1-2 sentences explaining the feature to a non-technical stakeholder
3. **Innovation Angle**: What makes this genuinely novel? Why hasn't it been done before in this context?
4. **User Impact**: Who benefits and how? What problem does it solve or what delight does it create?
5. **Technical Hooks**: Which existing parts of the codebase could this connect to? What APIs, libraries, or patterns would be involved?

Push boundaries. Think cross-domain. Consider: AI/ML augmentation, unconventional UX patterns, data-driven insights, automation opportunities, ecosystem integrations, and emerging tech applications.
```

### Execution

1. Call `mcp__browser-bridge__research_query` with:
   - `query`: The prompt above (with context baked in)
   - `includeContext`: `true`
2. **On success**: Save raw response to `~/.claude/council-logs/{YYYY-MM-DD_HHmm}-creative-stage1-{projectName}.md`
3. **On failure**: Wait 5 seconds (`sleep 5`), retry once. If second attempt fails → **STOP entirely** and report error to user. Stage 1 is the foundation — cannot proceed without ideas.

Proceed to Stage 2.

---

## Stage 2: Viability Analysis (Query 2/3)

Score and rank the ideas from Stage 1.

### Query Construction

Include the FULL Stage 1 response as input:

```
You are a technical product strategist. You've been given a list of feature ideas for a software project, along with full project context (architecture, tech stack, codebase).

Your task is CONVERGENT ANALYSIS — evaluate each idea rigorously.

For EACH idea from the brainstorming phase, score on these dimensions (1-10 scale):
- **Feasibility**: How realistic is implementation given the current tech stack and architecture? (10 = trivial, 1 = requires fundamental rewrite)
- **Effort**: How much development time is needed? (10 = <1 day, 1 = months of work)
- **Impact**: How much value does this deliver to users? (10 = transformative, 1 = negligible)
- **Uniqueness**: How differentiated is this from what competitors/similar projects offer? (10 = never seen before, 1 = table stakes)

For each idea, provide:
1. Scores for all 4 dimensions
2. **Composite Score**: Calculate (Impact x Uniqueness) / Effort
3. **Verdict**: HIGHLY_VIABLE | VIABLE | CHALLENGING | NOT_VIABLE
4. **Rationale**: 1-2 sentences explaining the verdict

Then provide:
- **TOP 5 RANKED**: The 5 highest composite-score ideas, ordered by rank
- **Quick Wins**: Ideas that are HIGHLY_VIABLE with Effort >= 7 (easy to build)
- **Moonshots**: Ideas that are CHALLENGING but have Impact >= 8 and Uniqueness >= 8 (hard but transformative)

{FULL STAGE 1 RESPONSE INSERTED HERE}
```

### Execution

1. Call `mcp__browser-bridge__research_query` with:
   - `query`: The prompt above (with Stage 1 results embedded)
   - `includeContext`: `true`
2. **On success**: Save raw response to `~/.claude/council-logs/{YYYY-MM-DD_HHmm}-creative-stage2-{projectName}.md`
3. **On failure**: Wait 5 seconds, retry once. If second attempt fails → present Stage 1 results as **partial success** and skip to Step 4 (present what we have).

Proceed to Stage 3.

---

## Stage 3: Blueprint Generation (Query 3/3)

Generate detailed implementation blueprints for the TOP 5 features.

### Query Construction

Include the TOP 5 from Stage 2 + actual code snippets from Step 0.5:

```
You are a senior software architect. You've been given the TOP 5 ranked feature ideas for a project, along with full project context including actual source code.

For EACH of the TOP 5 features, build a detailed implementation blueprint:

1. **Implementation Phases**: Break into 2-4 sequential phases with clear deliverables
2. **Files to Create/Modify**: Specific file paths based on the existing project structure
3. **APIs & Libraries**: External dependencies needed (with version recommendations)
4. **Data Models**: New models, schema changes, or data structures required
5. **Edge Cases**: What could go wrong? Input validation, error states, race conditions
6. **Testing Strategy**: Unit tests, integration tests, and manual verification steps
7. **Effort Estimate**: Time estimate per phase (hours/days)
8. **Risk Mitigation**: Top 2-3 risks and how to address them
9. **Integration Points**: How this connects to existing code — specific functions, classes, or modules

Be concrete and specific — reference actual file paths, function names, and patterns from the codebase context.

{TOP 5 IDEAS WITH VIABILITY SCORES FROM STAGE 2}
```

### Execution

1. Call `mcp__browser-bridge__research_query` with:
   - `query`: The prompt above (with Stage 2 TOP 5 + code context)
   - `includeContext`: `true`
2. **On success**: Save raw response to `~/.claude/council-logs/{YYYY-MM-DD_HHmm}-creative-stage3-{projectName}.md`
3. **On failure**: Wait 5 seconds, retry once. If second attempt fails → present Stage 1 + Stage 2 results as **partial success** and proceed to Step 4 without blueprints.

---

## Step 4: Present Results

Present a structured summary to the user. Format:

```markdown
## Creative Research Results

**Project**: {projectName}
**Focus**: {$ARGUMENTS or "broad exploration"}
**Ideas Generated**: {count from Stage 1}
**Stages Completed**: {1, 2, or 3 of 3}

### Executive Summary
- {X} ideas generated, {Y} scored as HIGHLY_VIABLE or VIABLE
- **Quick Wins**: {list names} — high impact, low effort
- **Moonshots**: {list names} — transformative but challenging

### TOP 5 Features (Ranked)

| Rank | Feature | Impact | Effort | Uniqueness | Composite | Verdict |
|------|---------|--------|--------|------------|-----------|---------|
| 1 | {name} | {score} | {score} | {score} | {composite} | {verdict} |
| ... | ... | ... | ... | ... | ... | ... |

For each TOP 5 feature, show:
- **Elevator Pitch**: {from Stage 1}
- **Blueprint Summary**: {key phases from Stage 3, if available}
- **Estimated Effort**: {from Stage 3, if available}

### Stage Logs
- Stage 1 (Ideation): `council-logs/{filename}`
- Stage 2 (Viability): `council-logs/{filename}`
- Stage 3 (Blueprints): `council-logs/{filename}`
```

**CRITICAL: End with an explicit user selection prompt:**

> Which feature(s) would you like to implement? You can:
> - Pick one (e.g., "Feature #2")
> - Pick multiple (e.g., "Features #1 and #4")
> - Ask for more detail on any feature
> - Request a new round of ideation with a different focus

**Do NOT enter plan mode automatically. Do NOT create tasks. Wait for user response.**

---

## Step 5: User Selection → Plan Mode

**Only execute this step after the user responds with their selection.**

1. Read the Stage 3 blueprint(s) for the selected feature(s) from the saved log files
2. Call `EnterPlanMode`
3. In plan mode, build a master plan + sub-plans:
   - Use the Stage 3 blueprints as the foundation
   - Cross-reference against current codebase (re-read key files if needed)
   - Structure as numbered Phases with dependencies
   - Include acceptance criteria per phase
   - **Second-to-last phase**: Update project memory (MEMORY.md + topic files)
   - **Final phase**: Commit & Push

4. **Plan Verification** — MANDATORY (1 pass max):
   - Call `mcp__browser-bridge__research_query` with critique-focused prompt
   - Include: complete plan + Stage 3 blueprint + key codebase files
   - Ask for: logical errors, missing edge cases, dependency ordering, feasibility
   - Revise if needed, then call `ExitPlanMode`

5. After user approves the plan:
   - `TaskCreate` per Phase with full sub-plan in description
   - Set `addBlockedBy` for dependencies
   - Begin executing first unblocked task

---

## Error Handling

| Error | Action |
|-------|--------|
| Stage 1 fails (both attempts) | STOP — report error, suggest `/cache-perplexity-session` |
| Stage 2 fails (both attempts) | Present Stage 1 as partial results, skip to Step 4 |
| Stage 3 fails (both attempts) | Present Stage 1 + 2, skip blueprints in Step 4 |
| Browser collision / empty results | Close browser-bridge, wait 2s, retry once |
| Session expired | Report "run `/cache-perplexity-session` to refresh" |

## Key Differences from Other Research Commands

| Aspect | /creative-research | /research-perplexity | /export-to-council |
|--------|-------------------|---------------------|-------------------|
| **Purpose** | Divergent ideation | Convergent analysis | Multi-model analysis |
| **Queries** | 3 sequential | 1 | 1 (multi-model) |
| **Output** | Ranked features + blueprints | Strategic next steps | Synthesized recommendations |
| **Plan mode** | User-triggered | Automatic | Automatic |
| **Focus** | What COULD we build? | What SHOULD we do next? | What do experts think? |
| **Cost** | $0 | $0 | $0 |
