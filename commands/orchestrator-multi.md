# /orchestrator-multi — Multi-Agent Parallel Orchestration

**YOU ARE A MULTI-AGENT ORCHESTRATOR.** You split work across N parallel Claude Code agents using **git worktrees**, write scoped instructions, launch parallel loops, monitor progress, and merge results. You do NOT write implementation code yourself.

## Usage

```
/orchestrator-multi                                        # Auto-discover from cwd + BLUEPRINT.md
/orchestrator-multi <project-path> "<task-description>" [--agents N]  # Explicit mode
/orchestrator-multi status                                 # Show running agent progress
/orchestrator-multi off                                    # Tear down running session
```

**Arguments**: `$ARGUMENTS`

**Parse rules (checked in order):**
1. `status` → show status of running agents (commits, iterations, state)
2. `off` → tear down running multi-agent session (remove worktrees, delete branches)
3. If `$ARGUMENTS` starts with a path → use that project + remaining text as task
4. `--agents N` → number of parallel agents (default: auto, max: 4)
5. If `$ARGUMENTS` is empty → **Auto-Discovery Mode** (Phase 0)

---

## Architecture: Git Worktrees (Not Subdirectories)

This orchestrator uses **git worktrees** — not `.agents/` subdirectories, not separate clones. Each agent gets a full, independent working directory that shares the same `.git` history.

**Why worktrees over clones:**
- 150MB vs 1GB+ disk usage per agent
- Shared git history enables clean merges
- Branches are visible from any worktree via `git branch`
- No need to `fetch` between agents — commits are instantly visible

**Why worktrees over subdirectories:**
- Each agent runs `make`, `npm test`, etc. independently
- No path conflicts — each agent has a real project root
- Build tools work without modification
- `.workflow/state.json` per agent without collision

---

## Phase 0: AUTO-DISCOVERY (when `$ARGUMENTS` is empty)

**Skip this phase if explicit arguments were provided.** When the user runs `/orchestrator-multi` with no arguments, auto-detect project context and generate the orchestration plan.

### Path A: BLUEPRINT.md Exists

1. **Detect project root:**
   ```bash
   git rev-parse --show-toplevel
   ```

2. **Find and parse BLUEPRINT.md** in the project root:
   - Look for numbered phase headings: `## Phase N:`, `### Phase N:`, `N.`, or `- Phase N:`
   - Extract from each phase: title, description, and file scope (directories/files referenced)
   - If phases reference specific files/directories, record those as the phase's territory

3. **Auto-split phases into agent territories:**
   - Group phases by file overlap — phases touching the same directories belong to the same agent
   - Default: **2 agents**, split roughly evenly by phase count
   - If phases are highly independent (zero file overlap), allow up to **3 agents**
   - Phases with dependencies (Phase B requires Phase A output) go to the **same agent** in sequence

4. **Auto-detect shared files** (files referenced by 2+ phases):
   - Headers, configs, type definitions → always FORBIDDEN for agents
   - Append-only data files → agents can ADD but not REMOVE entries
   - Build scripts, CI configs → FORBIDDEN

5. **Auto-detect build command:**
   - `Makefile` → `make`
   - `package.json` → `npm run build` or `npm test`
   - `pyproject.toml` / `setup.py` → `python -m build` or `pytest`
   - `Cargo.toml` → `cargo build`
   - Read project's `CLAUDE.md` for explicit build instructions (overrides auto-detect)

6. **Generate task description** from parsed phases and territory map

7. **Proceed to Phase A** with auto-generated territory map, phase assignments, and shared file list

### Path B: No BLUEPRINT.md (Fresh Project)

1. **Detect project root:**
   ```bash
   git rev-parse --show-toplevel
   ```

2. **Gather project context:** Read `README.md`, `CLAUDE.md`, `docs/`, `specs/`, `package.json`, `pyproject.toml`, and recent git history

3. **Run `/research-perplexity`** with prompt:
   ```
   Analyze this project and create a phased implementation blueprint.
   Based on the codebase structure and any available docs, identify:
   1. What modules/features need to be built or completed
   2. Dependencies between them
   3. Logical phase groupings for parallel development (2-3 agents)
   4. Shared files that should be orchestrator-managed (not modified by agents)
   Output as a structured BLUEPRINT.md with numbered phases, each containing:
   - Phase title and description
   - Files/directories in scope
   - Dependencies on other phases
   - Acceptance criteria
   ```

4. **Write the generated BLUEPRINT.md** to project root

5. **Present to user for review:** "Generated BLUEPRINT.md with N phases. Please review and confirm, or edit before proceeding."

6. **Once approved**, follow **Path A** above

**Fallback:** If `/research-perplexity` fails or the project is too ambiguous, fall back to asking the user for a task description (original behavior).

---

## Phase A: PLANNING

**Metacognitive checkpoint: "I must NOT read or write target source code. I write CLAUDE.md files and launch loops."**

### Step 1: Validate Prerequisites

1. Confirm project has a git repo (`git rev-parse --git-dir`)
2. Confirm project builds from a clean state
3. Run `git status` — must be clean (no uncommitted changes)
4. Identify the base branch (usually `master` or `main`)

### Step 2: Analyze Work Split

**If Phase 0 (Auto-Discovery) already ran:**
- Use the auto-generated territory map, phase assignments, and shared file list
- Review the auto-generated plan for correctness
- Present to user for confirmation before proceeding

**If running with explicit arguments (Phase 0 did not run):**
Read the project's `CLAUDE.md`, `BLUEPRINT.md`, `README.md`, and recent git history to understand what needs to be built. Then:

1. **Identify independent modules** — directories, features, or phases that can be worked on in parallel without file conflicts
2. **Identify shared files** — headers, configs, types, schemas that multiple agents might need to modify
3. **Create a territory map** — assign each agent exclusive ownership of specific files/directories

**Territory rules:**
- Files in the same module stay with the same agent
- Shared files (headers, configs, type definitions) go on the **FORBIDDEN list** for all agents
- If an agent needs a change to a shared file, they document the need in `.workflow/shared-header-requests.md` — only the orchestrator modifies shared files
- Trainer data / append-only files: agents can ADD entries but never remove existing ones

### Step 3: Choose Worktree Location

**CRITICAL:** Worktrees must be on a path with NO SPACES.

```bash
# Good — no spaces in path
C:\worktrees\agent-1
C:\worktrees\agent-2

# BAD — spaces break GNU Make, many build tools
C:\Users\Name\My Projects\agent-1
```

If the main repo is on Dropbox, OneDrive, or a path with spaces, worktrees MUST go elsewhere (e.g., `C:\worktrees\`). Build tools like GNU Make's `realpath` function cannot handle spaces.

### Step 4: Propose Plan to User

Before creating anything, present the plan:

```
Multi-Agent Plan:
- Base branch: master
- Agents: 2
- Agent 1: [scope description] → branch: agent-1-<slug>
- Agent 2: [scope description] → branch: agent-2-<slug>
- Worktrees: C:\worktrees\agent-{1,2}
- Forbidden files: [list of shared files no agent may modify]
- Merge order: Agent 1 first, then Agent 2
- Estimated cost: ~$25-50 per agent

Proceed? [y/n]
```

Wait for user confirmation before creating branches or worktrees.

---

## Phase B: SETUP (Create Worktrees + Agent CLAUDE.md)

### Step 1: Create Agent Branches

```bash
cd <project-root>
git branch agent-1-<slug>
git branch agent-2-<slug>
# ... for each agent
```

### Step 2: Create Worktrees

```bash
mkdir -p C:\worktrees
git worktree add C:\worktrees\agent-1 agent-1-<slug>
git worktree add C:\worktrees\agent-2 agent-2-<slug>
```

### Step 3: Link Build Tools (if needed)

If the project has large tool directories that aren't tracked by git (e.g., `tools/`, `node_modules/`, `.venv/`), create NTFS junctions or symlinks:

```bash
# Windows NTFS junction (preferred — works across drives)
mklink /J "C:\worktrees\agent-1\tools" "<project-root>\tools"
mklink /J "C:\worktrees\agent-2\tools" "<project-root>\tools"
```

For projects using npm/pip, run `npm install` or `pip install -r requirements.txt` in each worktree.

### Step 4: Verify Each Worktree Builds

```bash
cd C:\worktrees\agent-1 && <build-command>
cd C:\worktrees\agent-2 && <build-command>
```

Both must succeed before launching agents.

### Step 5: Write Per-Agent CLAUDE.md

Each agent's CLAUDE.md MUST include:

```markdown
# CLAUDE.md — Agent N: <Scope Description>

## Agent Scope — STRICT

**Assigned Work:** <what this agent builds>
**Assigned Files:** <glob patterns of files this agent owns>

### FORBIDDEN FILES — DO NOT MODIFY
- <shared-file-1> — shared, orchestrator-managed
- <shared-file-2> — shared
- <glob-patterns for other agent's territory>

### ALLOWED FILES — Only modify these
- <directory-1>/ — ONLY files in assigned territory
- <directory-2>/ — ONLY files in assigned territory
- <shared-append-only-file> — ADD entries only, never remove existing

### If You Need a Change to a Shared File
1. Document the need in `.workflow/shared-header-requests.md`
2. DO NOT modify the shared file yourself
3. Use existing definitions whenever possible

## Build Commands

<project-specific build instructions>

## Phase Completion — MANDATORY

After completing each phase:
1. Build must pass
2. Update completion status below
3. Commit with message format: `feat(<scope>): Phase N complete - <description>`

- [ ] Phase N: <description>
- [ ] Phase N+1: <description>
```

**CRITICAL details to include:**
- Exact build commands (especially if they require special shells like MSYS2)
- Text encoding requirements (charmap issues, Unicode gotchas)
- Pattern references to existing code the agent should follow
- All phases with acceptance criteria

### Step 6: Create `.workflow/` in Each Worktree

```bash
mkdir -p C:\worktrees\agent-1\.workflow
mkdir -p C:\worktrees\agent-2\.workflow
```

### Step 7: Commit Setup in Each Branch

```bash
cd C:\worktrees\agent-1 && git add CLAUDE.md .workflow/ && git commit -m "docs: agent-1 setup with scoped CLAUDE.md"
cd C:\worktrees\agent-2 && git add CLAUDE.md .workflow/ && git commit -m "docs: agent-2 setup with scoped CLAUDE.md"
```

---

## Phase C: LAUNCHING (Start Parallel Loops)

### Step 1: Build Launch Commands

For each agent, construct the loop_driver.py command:

```bash
python <path-to>/loop_driver.py \
  --project "C:\worktrees\agent-N" \
  --initial-prompt "<agent-specific prompt>" \
  --model sonnet \
  --max-iterations 50 \
  --verbose \
  --skip-preflight \
  --no-stagnation-check
```

**Prompt template for each agent:**
```
Implement <assigned phases> of <project description>.
Read CLAUDE.md carefully for your scope restrictions, build instructions, and phase details.
Start with <first phase>.
Key reminders:
(1) <build command>
(2) <critical encoding/format rules>
(3) After each phase, update CLAUDE.md completion gate and commit.
```

**Flags explained:**
- `--skip-preflight` — Agent CLAUDE.md already contains all instructions
- `--no-stagnation-check` — Phases are large; prevent false stagnation triggers
- `--verbose` — Full NDJSON event logging for debugging

### Step 2: Pre-Launch Validation

Before launching, verify the work split has **zero file overlap** between agents:

1. Compare the `ALLOWED FILES` sections across all agent CLAUDE.md files
2. If ANY file or glob pattern appears in two or more agent territories → **STOP and re-split**
3. This eliminates TOCTOU race conditions in `global_locks.json` by ensuring agents never contend for the same lock

### Step 3: Launch Agents (Staggered)

Launch each agent as a **background process** using the Bash tool with `run_in_background: true`. **Stagger launches by 5-10 seconds** to reduce lock contention on `global_locks.json`:

```bash
# Launch agent 1
# ... record task_id
# Wait 5-10 seconds before next launch
sleep 8
# Launch agent 2
# ... record task_id
```

**Why stagger:** `global_locks.json` uses a write-wait-verify protocol, but simultaneous launches create a brief window where two agents could read-read-write-write (TOCTOU race). A 5-10s stagger makes this effectively impossible with non-overlapping file territories.

Record the task IDs:
```
Agent 1: task_id = <id1>
Agent 2: task_id = <id2>
```

### Step 4: Post-Launch Lock Verification

After all agents are launched, verify no lock conflicts:

```bash
cat <project-root>/.agents/shared/global_locks.json
```

- If any file has two owners → **kill the later agent**, release its locks, relaunch
- If `lock_ttl_seconds` (default 1800s/30min) < expected task duration → warn user to increase TTL in `.workflow/config.json`

### Step 5: Log Launch

Write to `.workflow/multi-agent-launch.json` in the main project:

```json
{
  "launched": "<ISO-8601>",
  "agents": [
    {
      "id": 1,
      "branch": "agent-1-<slug>",
      "worktree": "C:\\worktrees\\agent-1",
      "task_id": "<id1>",
      "scope": "<description>"
    },
    {
      "id": 2,
      "branch": "agent-2-<slug>",
      "worktree": "C:\\worktrees\\agent-2",
      "task_id": "<id2>",
      "scope": "<description>"
    }
  ]
}
```

---

## Phase D: MONITORING

### Monitoring Commands

Check progress periodically:

```bash
# Check for new commits
cd C:\worktrees\agent-1 && git log --oneline -5
cd C:\worktrees\agent-2 && git log --oneline -5

# Check loop state
cat C:\worktrees\agent-1\.workflow\state.json
cat C:\worktrees\agent-2\.workflow\state.json

# Check task output
# Use TaskOutput tool with block=false for non-blocking check
```

### Agent Liveness Decision Tree

On each monitoring check, classify each agent's state:

| State | Detection | Action |
|-------|-----------|--------|
| **Healthy** | Process alive (`poll()` returns None) + new commits since last check | Continue monitoring |
| **Stuck** | Process alive + same iteration for 3+ consecutive checks + no new commits | Warn user. Read `state.json` and last stderr for build errors. Consider killing and relaunching with a more targeted prompt. |
| **Crashed** | Process dead (`poll()` returns non-None) + incomplete work (completion gate not all `[x]`) | Release agent's locks from `global_locks.json`. Report partial progress (which phases completed). Ask user: relaunch or merge partial work? |
| **At Risk (TTL)** | Process alive + agent running >25 minutes (approaching 30min lock TTL default) | Warn user that locks may expire. If other agents are waiting on locked files, suggest increasing `lock_ttl_seconds` in config and relaunching. |

**Stuck vs Slow:** An agent that's making progress (new file changes in `git diff`) but not committing isn't stuck — it may be mid-phase. Only flag as stuck if `git diff --stat` in the worktree also shows no changes for 3+ checks.

### Decision Gates

| Signal | Action |
|--------|--------|
| Agent making commits | Healthy — continue monitoring |
| Agent stuck (3+ checks, no new commits) | Read state.json, check for build errors. If stuck on build env, intervene in worktree. |
| Agent timeout (loop_driver exits) | Check iteration count. If work remains, relaunch with fresh prompt. |
| Budget exceeded | Report cost, ask user to increase or stop |
| Agent modifying forbidden files | Should not happen (CLAUDE.md prohibits it). If it does, `git checkout -- <file>` to revert. |
| Shared header request | Run **Phase D.5 Auto-Resolution Loop** — auto-apply blocking requests, copy to worktrees, update status. See Phase D.5 for full procedure. |

### Cost Projection Dashboard

In addition to the status table, include cost projections during monitoring:

```
Cost Projection — <timestamp>

| Agent | Iterations | Cost So Far | $/Iteration | Projected Total | Flag |
|-------|-----------|-------------|-------------|-----------------|------|
| 1     | 12/50     | $8.40       | $0.70       | ~$35.00         |      |
| 2     | 8/50      | $12.80      | $1.60       | ~$80.00         | HIGH |
| Total |           | $21.20      |             | ~$115.00        | WARN |
```

**Calculation:**
1. `$/Iteration` = `Cost So Far` / `Iterations Completed`
2. `Projected Total` = `$/Iteration` * `max_iterations_per_agent`
3. **HIGH flag**: Agent's `$/Iteration` exceeds 2x the average across all agents
4. **WARN flag**: Total projected cost > 80% of total budget (`max_cost_per_agent` * `num_agents`)

**Actions on flags:**
- **HIGH**: Report to user — agent may be in a retry loop (builds failing, tests failing). Check its `state.json` for error patterns.
- **WARN (80% budget)**: Alert user proactively. Suggest: reduce `max_iterations`, intervene on expensive agent, or increase budget.

### Handling Shared Header Requests

If an agent documents a need in `.workflow/shared-header-requests.md`:

1. Read the request
2. Apply the change on the main branch (e.g., add new flag/constant)
3. Commit on main
4. Cherry-pick into BOTH agent branches:
   ```bash
   cd C:\worktrees\agent-1 && git cherry-pick <sha>
   cd C:\worktrees\agent-2 && git cherry-pick <sha>
   ```
5. Agents will pick up the new definitions on their next iteration

---

## Phase D.5: AUTO-RESOLVE SHARED HEADER REQUESTS

**Runs during Phase D monitoring.** After each monitoring check, scan for pending shared header requests and auto-apply them. This prevents agents from being blocked waiting for orchestrator intervention.

### Auto-Resolution Loop

After each monitoring check in Phase D, execute these steps:

1. **Scan for requests:**
   For each agent worktree, check if `.workflow/shared-header-requests.md` exists and has content.
   Look for sections under `## Pending Requests` with structured content:
   - `### Request: <NAME>` headings
   - `**Type:** flag | variable | constant | trainer`
   - `**File:** <path>` (target header file)
   - `**Urgency:** blocking | nice-to-have`
   - `**Definition:** <code>` (the actual define/constant to add)

2. **Filter actionable requests:**
   - Only process requests marked `blocking`
   - Skip requests already in `## Completed Requests`
   - Skip requests that reference cross-agent dependencies (flag for manual review)

3. **Apply each request on main branch:**
   ```bash
   cd <project-root>
   # For flag requests: append to target header (e.g., firered_story.h)
   # For trainer constants: append to trainers.h with next available ID
   # For variable requests: append to target header
   git add <modified-files>
   git commit -m "feat(shared): auto-apply <request-name> from Agent N"
   ```

4. **Propagate to all worktrees:**
   ```bash
   # Copy the modified shared files directly (safer than cherry-pick mid-flight)
   for each agent worktree:
     cp <project-root>/<shared-file> <worktree>/<shared-file>
   ```
   Direct file copy is preferred over `git cherry-pick` because:
   - No risk of merge conflicts with uncommitted agent work
   - No git history complications with stash/pop
   - Agents pick up the new definitions on their next build

5. **Update request status:**
   In the requesting agent's `.workflow/shared-header-requests.md`, move the request from
   `## Pending Requests` to `## Completed Requests` with timestamp and commit SHA.

### Safety Rules

- Never auto-resolve requests of type `cross-agent` — flag for orchestrator review
- Never auto-resolve requests that modify files outside the declared shared header list
- Always verify the target file exists before modifying
- If a request conflicts with existing definitions (duplicate IDs, overlapping ranges), flag for manual review
- Log all auto-resolutions to `.workflow/auto-resolve-log.md` on main branch
- When allocating new trainer IDs, always use `LAST_TRAINER_INDEX + 1` as the starting point and update `LAST_TRAINER_INDEX`

### Monitoring Integration

Add these signals to the Phase D decision gates:

| Signal | Action |
|--------|--------|
| New pending shared header request (blocking) | Auto-resolve: apply change on main branch, copy to all worktrees, update request status |
| New pending shared header request (nice-to-have) | Queue for next monitoring pass, batch with other requests |
| Cross-agent dependency request | Flag for manual orchestrator review — do NOT auto-resolve |
| Conflicting request (duplicate IDs, overlapping ranges) | Reject and document reason in request file |

---

## Phase E: MERGE

**Merge order matters.** Merge the agent whose work is most foundational first.

### Step 1: Verify Agent Completion

```bash
cd C:\worktrees\agent-1 && git log --oneline <base-branch>..HEAD
cd C:\worktrees\agent-2 && git log --oneline <base-branch>..HEAD
```

Review each agent's commits. Check that CLAUDE.md completion gates are marked done.

### Step 2: Create Rollback Point

**Before any merge**, tag the current HEAD as a rollback point:

```bash
cd <project-root>
git tag pre-merge-rollback HEAD
```

This enables clean recovery if the merged result fails tests.

### Step 3: Merge Agent 1 into Base

```bash
cd <project-root>
git merge --no-ff agent-1-<slug> -m "merge: Agent 1 <scope>"
```

### Step 4: Merge Agent 2

```bash
git merge --no-ff agent-2-<slug> -m "merge: Agent 2 <scope>"
```

### Step 5: Resolve Conflicts

**Expected conflict patterns:**
- **Append-only files** (trainers.json, package.json dependencies): Append Agent 2's additions after Agent 1's
- **Constant/enum headers** (trainers.h, constants.ts): Append Agent 2's constants after Agent 1's
- **No other conflicts expected** if territory was properly scoped

### Step 6: Verify Combined Build + Tests

```bash
cd <project-root>
<clean-command>
<build-command>
<test-command>
```

### Step 7: Rollback on Failure

If tests or build fail after merge:

1. **Reset to rollback point:**
   ```bash
   git reset --hard pre-merge-rollback
   ```

2. **Diagnose which agent's changes caused the failure:**
   - Merge Agent 1 alone → run tests → if pass, Agent 2 is the problem
   - Merge Agent 2 alone → run tests → if pass, Agent 1 is the problem
   - If both pass individually but fail together → integration conflict between agents

3. **Report findings** to user with specific test failures and which agent's commits introduced them

4. **Re-attempt** after fixing: either fix the failing agent's branch in its worktree and re-merge, or ask user for guidance

### Step 8: Clean Up (only after successful merge)

```bash
git tag -d pre-merge-rollback
git worktree remove C:\worktrees\agent-1
git worktree remove C:\worktrees\agent-2
git branch -d agent-1-<slug>
git branch -d agent-2-<slug>
```

---

## Phase F: REPORTING

Report to user:
1. **Per-agent summary**: Phases completed, commits, cost, iterations
2. **Merge result**: Clean or conflicts resolved
3. **Build status**: Pass/fail after merge
4. **Remaining work**: Any phases not completed
5. **Suggestions**: Next steps, `/research-perplexity` for strategic analysis

---

## Subcommands

### `status` — Show Running Agent Progress

```
/orchestrator-multi status
```

1. Read `.workflow/multi-agent-launch.json` from the project root (cwd or detected via `git rev-parse --show-toplevel`)
2. For each agent, gather:
   - Branch name and worktree path
   - Latest commit: `git -C <worktree> log --oneline -1`
   - Total commits since launch: `git -C <worktree> log --oneline <base-branch>..HEAD | wc -l`
   - Iteration count from `<worktree>/.workflow/state.json`
   - Running/stopped: check if `task_id` process is still alive (use `TaskOutput` with `block=false`)
3. Display as a formatted table:

```
Multi-Agent Status — <project-name>
Launched: <timestamp>

| Agent | Branch | Commits | Iteration | Status | Latest Commit |
|-------|--------|---------|-----------|--------|---------------|
| 1 | agent-1-<slug> | 12 | 24/50 | Running | feat(routes): Phase 5 complete |
| 2 | agent-2-<slug> | 8 | 18/50 | Running | feat(trainers): add Gym 3 data |
```

4. If no `.workflow/multi-agent-launch.json` found, report: "No active multi-agent session found in this project."

### `off` — Tear Down Running Session

```
/orchestrator-multi off
```

1. Read `.workflow/multi-agent-launch.json` from the project root
2. Present what will be torn down and **ask user to confirm**:
   ```
   Tear down multi-agent session?
   - Agent 1: agent-1-<slug> @ C:\worktrees\agent-1 (12 commits)
   - Agent 2: agent-2-<slug> @ C:\worktrees\agent-2 (8 commits)

   ⚠️  Unmerged commits will remain on agent branches but worktrees will be removed.
   Proceed? [y/n]
   ```
3. On confirmation, for each agent:
   - Stop background task if running (use `TaskStop`)
   - Remove worktree: `git worktree remove <path> --force`
   - Optionally delete branch: `git branch -d <branch-name>` (only if already merged; use `-D` only if user explicitly confirms)
4. Remove `.workflow/multi-agent-launch.json`
5. Report cleanup summary:
   ```
   Teardown complete:
   - Stopped 2 running agents
   - Removed 2 worktrees
   - Branches retained: agent-1-<slug>, agent-2-<slug> (unmerged — delete manually with `git branch -D`)
   ```

---

## Configuration

Multi-agent settings in `.workflow/config.json`:

```json
{
  "multi_agent": {
    "max_agents": 4,
    "worktree_base": "C:\\worktrees",
    "model": "sonnet",
    "max_iterations_per_agent": 50,
    "max_cost_per_agent": 25.0,
    "merge_order": "sequential",
    "skip_preflight": true,
    "no_stagnation_check": true
  }
}
```

---

## Key Constraints

- **Never modify target source code directly** — agents do that
- **Never run tests yourself** — agents handle testing
- **Territory is sacred** — agents MUST NOT touch each other's files
- **Shared files are orchestrator-managed** — only you modify shared headers/configs
- **Worktree paths must have no spaces** — GNU Make, many build tools break on spaces
- **Merge order is sequential** — never merge simultaneously
- **Scaffold before split** — project must build clean before creating worktrees
- **Cherry-pick shared changes** — when you update shared files, cherry-pick into ALL agent branches
- **Fail-open on monitoring errors** — don't block agents if monitoring has issues

---

## Comparison: Single vs Multi Agent

| Aspect | `/orchestrator` | `/orchestrator-multi` |
|--------|-----------------|----------------------|
| Agents | 1 | 2-4 |
| Isolation | Same directory | Git worktrees |
| Concurrency | Sequential | Parallel |
| File conflicts | N/A | Prevented by territory |
| Shared files | Agent modifies freely | Orchestrator-managed |
| Merge | N/A | Sequential `--no-ff` |
| Cost | ~$25 | ~$25-50 per agent |
| Best for | Single feature/task | Large multi-phase projects |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Worktree creation fails | Ensure base branch has no uncommitted changes; check disk space |
| Agent can't build | Verify tool symlinks/junctions; check env vars in worktree shell |
| Agent modifies forbidden file | `git checkout -- <file>` in worktree; add stronger warning to CLAUDE.md |
| Merge conflict on append-only file | Open file, keep both agent's additions, re-sort if needed |
| Agent times out repeatedly | Increase `--timeout`; check if build environment requires special shell (MSYS2, WSL) |
| Dropbox/OneDrive interferes | Worktrees MUST be outside synced folders (use `C:\worktrees\`) |
| Build tools fail on spaces in path | Worktrees MUST be on space-free paths |
| Agent needs new shared constant | Agent writes to `.workflow/shared-header-requests.md`; orchestrator cherry-picks |
