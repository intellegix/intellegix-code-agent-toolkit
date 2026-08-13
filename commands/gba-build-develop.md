# /gba-build-develop — Self-Driving GBA Game Development Pipeline

Composes `/gba-build develop`, `/gba-ai-full`, `/research-perplexity`, `/creative-research`, and `/orchestrator-multi` into a single autonomous mega-pipeline that researches, plans, parallelizes implementation, AI-playtests, iterates on failures, discovers creative features, and ships production-ready GBA games.

## Usage

```
/gba-build-develop "<game description>"
```

**Arguments**: `$ARGUMENTS` = A natural-language description of the GBA game to build (e.g., "dungeon crawler with 3 floors, enemies, and a boss fight").

---

## 7-Stage Pipeline

```
STAGE 0: INIT ─── Load skill, compile context, detect project state
    │
STAGE 1: RESEARCH ─── /research-perplexity → architecture + module plan
    │
STAGE 2: ORCHESTRATE ─── /orchestrator-multi patterns → parallel agent build
    │
STAGE 3: AI PLAYTEST ─── Generalized /gba-ai-full → save state + assert
    │
STAGE 4: RESEARCH ITERATION ─── Feed test results → fixes/improvements
    │                               ╰─► Loop to STAGE 2 if fixes needed
STAGE 5: CREATIVE DISCOVERY ─── /creative-research adapted for GBA
    │                               ╰─► Quick Wins loop to STAGE 2
STAGE 6: SHIP ─── Full playthrough, profiling, production audit, docs
```

## Prerequisites

- **devkitPro** installed (`pacman -S gba-dev`)
- **Butano** (default engine) or **libtonc** — `BUTANO_PATH` set if using Butano
- **mGBA** running with Lua MCP server on port 61337
- **git** initialized in project directory
- **loop_driver.py** at ``~/.claude/automated-loop/loop_driver.py``
- MCP server registered in `~/.claude/settings.json`

---

## Frame Timing Constants (Research-Derived)

These constants are hardware-derived from GBA specs (280,896 cycles/frame at 16.78 MHz)
and validated against mGBA Lua API behavior. Use these exact values — never guess.

| Constant | Frames | Use Case |
|----------|--------|----------|
| FRAMES_BOOT_BIOS | 270 | After gba_flash_rom() with BIOS intro enabled |
| FRAMES_BOOT_SKIP | 10 | After gba_flash_rom() with BIOS skipped |
| FRAMES_BOOT_BUTANO | 15 | After gba_flash_rom() for Butano engine (heavier C++ init) |
| FRAMES_TITLE_TO_GAME | 60 | After pressing START — covers 32f fade-out + 5f load + 20f fade-in + 3f settle |
| FRAMES_INPUT_SETTLE | 3 | After press_button() before reading memory (key_poll + logic + 1 frame margin) |
| FRAMES_INPUT_VISUAL | 4 | After press_button() before screenshot (input + OAM DMA copy) |
| FRAMES_SCENE_TRANSITION | 75 | After triggering scene change (32f fade + 10f VRAM load + 32f fade + 1f settle) |
| FRAMES_FAST_TRANSITION | 40 | Scene change with same tileset / no fade |
| FRAMES_COMBAT_INIT | 90 | From encounter trigger to stable battle screen (RPG) |
| FRAMES_COMBAT_INIT_ACTION | 45 | From encounter trigger to stable combat (action game, simpler transition) |
| FRAMES_SAVE_COMPLETE | 3 | After triggering SRAM save (1f write + 2f state machine margin) |
| FRAMES_SOFTLOCK_TIMEOUT | 900 | 15 seconds of zero state change = confirmed softlock |
| FRAMES_SCREENSHOT_NORMAL | 60 | Screenshot interval during normal gameplay (1/sec) |
| FRAMES_SCREENSHOT_TRANSITION | 5 | Screenshot interval during transitions (high granularity) |
| FRAMES_SCREENSHOT_GAMEPLAY | 30 | Screenshot interval during active testing (~2/sec) |
| FRAMES_STATE_CHECK_FINE | 5 | Memory read interval for position/HP (catches movement) |
| FRAMES_STATE_CHECK_MODE | 1 | Game mode byte check interval (every frame during transitions) |
| FRAMES_STATE_CHECK_COARSE | 30 | Memory sentinel / corruption check interval |
| FRAMES_CHECKPOINT_INTERVAL | 300 | Save state safety checkpoint (~5 seconds) |

### VBlank Budget Thresholds

| Zone | Cycles | % of Frame | Action |
|------|--------|-----------|--------|
| Green (healthy) | < 196,627 | < 70% | PASS |
| Yellow (warning) | 196,627 – 252,806 | 70-90% | WARN — approaching limit |
| Red (failure) | > 252,806 | > 90% | FAIL — will drop frames |
| Critical | > 280,896 | > 100% | HARD FAIL — frame overrun |

For VBlank-only budget (VRAM/OAM/Palette writes):

| Zone | Cycles | % of VBlank (83,776) | Action |
|------|--------|---------------------|--------|
| Green | < 50,266 | < 60% | PASS |
| Yellow | 50,266 – 71,210 | 60-85% | WARN |
| Red | > 71,210 | > 85% | FAIL |

---

## Instructions

### Stage 0: INIT

**Goal:** Load all context, detect or scaffold the project, establish baseline state.

1. **Load GBA Builder Skill**: Read the `gba-builder` skill (SKILL.md + all 5 reference docs: hardware-ref.md, architecture-patterns.md, asset-pipeline.md, testing-framework.md, docs-generator.md). Hold in context.

2. **Compile session context** (silent, no user interaction):
   - Read project `MEMORY.md` from auto-memory directory
   - `git log --oneline -10` for recent work
   - `git diff --stat` for uncommitted changes
   - `TaskList` for active/pending tasks
   - Synthesize 1-paragraph internal state summary

3. **Detect project state**:
   - If no project directory exists for this game → scaffold with `/gba-build new` patterns (create directory structure, Makefile/CMakeLists.txt, main entry point, .gitignore, CLAUDE.md)
   - If project exists but won't compile → fix build errors first
   - If project exists and compiles → identify which modules already exist and which are needed
   - Verify `git status` is clean — commit any uncommitted work before proceeding

4. **Record game description**: Store `$ARGUMENTS` as the canonical game description for all downstream queries.

Proceed directly to Stage 1. Do NOT ask the user questions.

---

### Stage 1: RESEARCH ARCHITECTURE

**Goal:** Use `/research-perplexity` patterns to determine architecture and module breakdown.

1. **Build research query** using compiled context from Stage 0:

   ```
   You are a GBA game architecture advisor. Design the complete technical architecture
   for this GBA game: "{$ARGUMENTS}"

   Using the project context provided, analyze and recommend:

   1. DISPLAY MODE: Which GBA display mode (0-5)? How many BG layers? Sprite vs bitmap?
   2. VRAM LAYOUT: Charblock/screenblock allocation plan. Sprite VRAM budget.
   3. MODULE BREAKDOWN: Ordered list of independent modules to build. Each module should be:
      - Self-contained (compilable and testable alone)
      - Ordered by dependency (display before sprites, sprites before collision, etc.)
      - Sized for ~1-2 hours of implementation each
   4. MEMORY LAYOUT: EWRAM/IWRAM allocation strategy. Key struct definitions.
   5. ASSET REQUIREMENTS: Sprites, tilesets, audio needed per module.
   6. GBA-SPECIFIC CONSTRAINTS: Gotchas for this genre on GBA hardware.
   7. ESTIMATED BUDGETS: VRAM usage, RAM usage, CPU cycles per frame.
   8. PARALLEL SPLIT: Which modules can be built simultaneously by separate agents?
   ```

3. **Execute query**: Call `mcp__browser-bridge__research_query` with `includeContext: true`.

4. **Parse results into BLUEPRINT.md**: Write `BLUEPRINT.md` to the project root with:
   - Game description and architecture decisions
   - Numbered module list with descriptions, file scopes, and dependencies
   - VRAM/RAM budget allocations
   - Agent territory split (which modules go to which agent)
   - Shared files list (main.cpp entry point, global headers, Makefile)

5. **Save research log**: Write to `~/.claude/council-logs/{YYYY-MM-DD_HHmm}-gba-develop-arch-{projectName}.md`

Proceed directly to Stage 2.

---

### Stage 2: ORCHESTRATED BUILD

**Goal:** Use `/orchestrator-multi` patterns to parallelize module implementation across agents.

#### Step 2.1: Territory Planning

From `BLUEPRINT.md`, create the agent territory map:

- **Agent territories**: Each module's source files are exclusive to one agent
- **Shared files** (orchestrator-managed, FORBIDDEN for agents):
  - `src/main.cpp` (or main entry point)
  - Global headers (`include/game.h`, `include/constants.h`)
  - `Makefile` / `CMakeLists.txt`
  - `BLUEPRINT.md`
- **Split strategy**: Group sequential-dependency modules to the same agent. Independent modules can go to different agents.

#### Step 2.2: Create Worktrees

```bash
mkdir -p C:\worktrees
git worktree add C:\worktrees\gba-agent-1 agent-1-<game-slug>
git worktree add C:\worktrees\gba-agent-2 agent-2-<game-slug>
```

Verify each worktree compiles before proceeding.

#### Step 2.3: Write Per-Agent CLAUDE.md

Each agent CLAUDE.md includes:
- Assigned modules with phase descriptions and acceptance criteria
- FORBIDDEN files list (shared files + other agent's territory)
- ALLOWED files list (only this agent's modules)
- Build commands (engine-specific: cmake or make)
- **Production Gate** (see Production Gate section below) — agent MUST pass this after every module commit
- Shared header request protocol (write to `.workflow/shared-header-requests.md`)
- Phase completion checklist

#### Step 2.4: Launch Agents

For each agent, launch via `loop_driver.py` as a background process:

```bash
python "<path-to>/loop_driver.py" \
  --project "C:\worktrees\gba-agent-N" \
  --initial-prompt "Implement <assigned modules> of <game description>. Read CLAUDE.md for scope, build instructions, and phase details. Start with <first module>. After each module: build, test via MCP tools, pass Production Gate, commit." \
  --model sonnet \
  --max-iterations 50 \
  --verbose \
  --skip-preflight \
  --no-stagnation-check
```

Stagger launches by 8 seconds. Record task IDs. Write `.workflow/multi-agent-launch.json`.

#### Step 2.5: Monitor Agents

Follow `/orchestrator-multi` Phase D monitoring:
- Check `git log` and `state.json` per agent periodically
- Handle shared header requests (auto-resolve blocking requests, propagate to all worktrees)
- Apply agent liveness decision tree (Healthy / Stuck / Crashed / At Risk)
- Track cost projections

#### Step 2.6: Merge Results

Follow `/orchestrator-multi` Phase E merge protocol:
1. Tag rollback point: `git tag pre-merge-rollback HEAD`
2. Merge agents sequentially (most foundational first)
3. Resolve conflicts (append-only files, constant headers)
4. Verify combined build compiles and runs
5. If build fails → rollback, diagnose, fix, re-merge
6. Clean up worktrees and branches on success

Proceed to Stage 3 after successful merge.

---

### Stage 3: AI PLAYTEST

**Goal:** Generalized `/gba-ai-full` patterns — save state, execute gameplay, screenshot, memory read, assert per module.

#### 5 Absolute Rules

1. **VERIFY state before every action** — screenshot + memory read to confirm expected state
2. **ONLY named test sequences** — never invent button sequences ad-hoc
3. **Frame advances >30 → state check first** — screenshot before long waits
4. **Screenshot every 5 tool calls** — visual audit trail
5. **Assertion fail → STOP, report, never retry blindly** — log failure details and halt

#### Playtest Protocol

For each module in BLUEPRINT.md, execute the matching test sequence from the AI Playtest Sequences section below.

```
FOR each module:
  1. gba_save_state(0) — baseline for this module's tests
  2. gba_screenshot() + gba_read_memory({game_state_addr}) — verify expected state before testing
  3. Execute module-specific test sequence (see templates)
     - Between tool calls: gba_advance_frames(FRAMES_INPUT_SETTLE) (3) before state reads
     - Screenshot cadence: every FRAMES_SCREENSHOT_GAMEPLAY (30) frames during gameplay,
       every FRAMES_SCREENSHOT_TRANSITION (5) frames during transitions
     - State check frequency: game_mode byte every FRAMES_STATE_CHECK_MODE (1) frame during
       transitions, position/HP every FRAMES_STATE_CHECK_FINE (5) frames during gameplay
     - Softlock monitor: if sequence exceeds FRAMES_SOFTLOCK_TIMEOUT (900) without expected
       state change → abort with SOFTLOCK failure
  4. Record results in test report table
  5. gba_load_state(0) — reset to baseline for next module
  6. If ANY assertion fails → STOP, record failure details, proceed to Stage 4
```

#### Test Report Format

```
| Module | Test | Expected | Actual | Result |
|--------|------|----------|--------|--------|
| Display Setup | Display mode | Mode 0 | ... | PASS/FAIL |
| Display Setup | BG0 enabled | bit set | ... | PASS/FAIL |
| Player Sprite | OAM slot 0 visible | true | ... | PASS/FAIL |
| Player Sprite | Movement right | x increased | ... | PASS/FAIL |
| Input Handling | D-pad response | position changed | ... | PASS/FAIL |
| Collision | Wall stops player | x < wall_x | ... | PASS/FAIL |
| ... | ... | ... | ... | ... |
```

Also record:
- Total MCP tool calls
- Screenshots taken (count)
- Performance: `gba_profile_frame()` → cycles, % of VBlank budget

Proceed to Stage 4.

---

### Stage 4: RESEARCH ITERATION

**Goal:** Feed AI playtest results into `/research-perplexity` for analysis and next-step recommendations.

1. **Build iteration query** with test results:

   ```
   Given this GBA game build progress and test results:

   GAME: "{$ARGUMENTS}"
   MODULES COMPLETED: [list each module with PASS/FAIL status]
   TEST FAILURES: [for each failure: module, test name, expected vs actual, memory state]
   PERFORMANCE: [cycle count per frame, VBlank budget % used]
   CURRENT ARCHITECTURE: [summary from BLUEPRINT.md]
   SCREENSHOTS: [describe visual state observed in latest screenshots]

   Analyze and recommend:
   1. ROOT CAUSE of any test failures — what specific code changes fix them?
   2. MISSING FEATURES — what gameplay gaps exist for this genre?
   3. NEXT PRIORITY — which module or fix should be implemented next?
   4. PERFORMANCE — any optimization opportunities? Are we within VBlank budget?
   5. POLISH — what would make the game feel more complete?
   ```

3. **Execute query**: Call `mcp__browser-bridge__research_query` with `includeContext: true`.

4. **Decision gate**:
   - **If fixes needed** (test failures or critical gaps) → create fix tasks, update BLUEPRINT.md, **RETURN TO STAGE 2** (agents fix specific modules)
   - **If all tests passing and no critical gaps** → proceed to Stage 5
   - **Max iteration loops**: 3 (prevent infinite fix cycles — after 3 rounds, report status and ask user)

5. **Save research log**: Write to `~/.claude/council-logs/{YYYY-MM-DD_HHmm}-gba-develop-iter-{projectName}.md`

---

### Stage 5: CREATIVE DISCOVERY

**Goal:** Use `/creative-research` 3-stage pipeline adapted for GBA to discover novel features.

Only enter this stage when all core modules pass AI playtest.

#### Stage 5.1: GBA Creative Ideation (Query 1/3)

Query:

```
You are an innovation consultant specializing in GBA game development. The game is:
"{$ARGUMENTS}"

Brainstorm 10-15 novel features for this GBA game. Consider:
- Hidden mechanics, easter eggs, secrets
- Visual polish: screen transitions, palette cycling, mosaic effects, window masking
- Audio tricks: pitch bending, dynamic BGM, channel-based sound layering
- GBA-specific capabilities: Mode 7 rotation/scaling, DMA tricks, HDMA scanline effects
- Speedrun features: timers, route optimization hooks
- Accessibility within GBA constraints: button remapping via save data, difficulty options
- Emergent gameplay: procedural generation within RAM limits, replayability hooks

Push boundaries. Think about what would surprise a player on original GBA hardware.
```

Call `mcp__browser-bridge__research_query` with `includeContext: true`.

#### Stage 5.2: GBA Viability Analysis (Query 2/3)

Score each idea with GBA-specific feasibility:

```
Score each feature idea on these dimensions (1-10):
- Feasibility: Account for VRAM budget remaining ({X}KB free), RAM budget ({Y}KB free),
  CPU budget ({Z}% of VBlank used). What's possible in the current display mode?
- Effort: Implementation time given the existing module structure
- Impact: How much this improves the player experience
- Uniqueness: How differentiated from typical GBA games of this genre

Composite Score = (Impact x Uniqueness) / Effort
Classify: HIGHLY_VIABLE | VIABLE | CHALLENGING | NOT_VIABLE

Identify:
- Quick Wins: HIGHLY_VIABLE with Effort >= 7 (easy to add)
- Moonshots: CHALLENGING but Impact >= 8 AND Uniqueness >= 8

{FULL STAGE 5.1 RESPONSE}
```

Call `mcp__browser-bridge__research_query`.

#### Stage 5.3: GBA Blueprint Generation (Query 3/3)

For TOP 5 features, generate implementation blueprints with GBA specifics:

```
For each TOP 5 feature, build a GBA-specific implementation blueprint:
- Which registers/memory regions are affected
- VRAM budget impact (charblocks, screenblocks, OAM slots consumed)
- Cycle cost estimate per frame
- Implementation phases (2-4 steps)
- Files to create/modify in the existing project structure
- Which existing modules this connects to

{TOP 5 FROM STAGE 5.2}
```

Call `mcp__browser-bridge__research_query`.

#### Stage 5.4: Auto-Add Quick Wins

- **Quick Wins** (HIGHLY_VIABLE, low effort): Automatically add as new modules in BLUEPRINT.md → **RETURN TO STAGE 2** for implementation
- **Moonshots** (high impact, high effort): Present to user for approval. If approved, add to BLUEPRINT.md → RETURN TO STAGE 2. If declined, skip.

Save all creative logs to `~/.claude/council-logs/{YYYY-MM-DD_HHmm}-gba-develop-creative-{projectName}.md`.

---

### Stage 6: SHIP

**Goal:** Final integration, production audit, documentation, and session report.

#### Step 6.1: Full Playthrough

Run an AI-driven end-to-end playthrough of the complete game:

```
1. gba_flash_rom → load final ROM
2. gba_advance_frames(FRAMES_BOOT_SKIP) → 10 frames (or FRAMES_BOOT_BUTANO=15 for Butano)
3. gba_screenshot → verify title screen
4. gba_press_button("START", FRAMES_INPUT_SETTLE) → 3-frame hold
5. gba_advance_frames(FRAMES_TITLE_TO_GAME) → 60 frames to gameplay
6. For each level/area in the game:
   a. Navigate through gameplay (using known button sequences)
   b. gba_screenshot every FRAMES_SCREENSHOT_GAMEPLAY (30) frames (~2/sec)
   c. gba_read_memory for key state (HP, score, progress) every FRAMES_STATE_CHECK_FINE (5) frames
   d. Trigger key mechanics (combat, items, puzzles)
   e. Between levels: gba_advance_frames(FRAMES_SCENE_TRANSITION) → 75 frames
   f. Softlock monitor: if any area exceeds FRAMES_SOFTLOCK_TIMEOUT (900) → abort
7. Reach game over / completion state
8. Verify save/load cycle works (FRAMES_SAVE_COMPLETE=3 after save trigger)
9. gba_screenshot → final state
```

#### Step 6.2: Performance Profiling

```
1. gba_profile_frame() → record cycles
2. Apply 4-tier threshold (Ship gate — YELLOW is also FAIL for headroom):
   - GREEN:    cycles < 196,627 (< 70% of frame) → PASS
   - YELLOW:   cycles 196,627-252,806 (70-90%) → FAIL (Ship gate requires headroom)
   - RED:      cycles > 252,806 (> 90%) → FAIL — "Near frame overrun: {cycles} ({percent}%)"
   - CRITICAL: cycles > 280,896 (> 100%) → HARD FAIL — "Frame overrun: {cycles} ({percent}%)"
3. gba_check_vblank_budget() → confirm VBlank-specific timing
   - VBlank GREEN: < 50,266 cycles (< 60% of VBlank) → PASS
   - VBlank YELLOW: 50,266-71,210 (60-85%) → WARN
   - VBlank RED: > 71,210 (> 85%) → FAIL
4. Document any YELLOW or RED hotspots for optimization
```

#### Step 6.3: Production Audit

Run the Production Gate (see section below) on the ENTIRE codebase, not just individual modules.

#### Step 6.4: Documentation

Run `/gba-build docs` patterns:
- Generate `docs/GAMEPLAY.md` — game mechanics and controls
- Generate `docs/TECHNICAL.md` — memory layout and performance
- Generate `docs/MCP-TOOLS.md` — memory addresses for testing

#### Step 6.5: Session Report

```
══════════════════════════════════════════════════════
  GBA Build-Develop Report: {game name}
══════════════════════════════════════════════════════

Game: {$ARGUMENTS}
Engine: {Butano/libtonc}
ROM Size: {X}KB
Modules: {N} implemented, {M} from creative discovery

Architecture:
  Display Mode: {mode}
  BG Layers: {count}
  Sprite Slots Used: {N}/128
  VRAM Usage: {X}KB / 96KB
  RAM Usage: {X}KB / 256KB EWRAM + {Y}KB / 32KB IWRAM

Module Results:
  {module 1}: PASS — {description}
  {module 2}: PASS — {description}
  ...

Performance:
  Cycles/Frame: {X} ({Y}% of VBlank budget)
  Headroom: {Z}% remaining

Creative Features Added:
  {feature 1} — Quick Win, {impact} impact
  {feature 2} — Quick Win, {impact} impact

Production Audit: {CLEAN / N issues found}
Documentation: Generated (GAMEPLAY.md, TECHNICAL.md, MCP-TOOLS.md)

Pipeline Stats:
  Research Queries: {N}
  Agents Used: {N}
  Build-Test-Fix Iterations: {N}
  Total MCP Tool Calls: {N}
  Creative Ideas Generated: {N}
──────────────────────────────────────────────────────
```

End with: "Build complete. Suggest running `/research-perplexity` for strategic next steps — want me to proceed?"

---

## Production Gate (Per-Module)

This replaces `/stub-check`. Runs after EVERY module commit and during Stage 6 final audit.

```
PRODUCTION GATE:

1. GREP for incomplete markers in source files:
   Pattern: TODO|FIXME|HACK|STUB|PLACEHOLDER|not implemented|WIP
   Scope: src/**/*.{cpp,c,h,hpp}
   → If found: FAIL — "Incomplete marker found: {file}:{line}: {match}"

2. GREP for empty function/method bodies:
   Pattern: function signature followed by { } with only whitespace
   Scope: src/**/*.{cpp,c,h,hpp}
   → If found: FAIL — "Empty function body: {file}:{line}"

3. GREP for hardcoded magic numbers in game logic:
   Pattern: numeric literals in gameplay code (exclude register constants, array sizes)
   Scope: src/**/*.{cpp,c} (excluding hardware register definitions)
   → If found: WARN — "Consider using named constant: {file}:{line}: {match}"

4. GREP for debug-only code with TODOs:
   Pattern: #if 0|#ifdef DEBUG containing TODO/FIXME
   Scope: src/**/*.{cpp,c,h,hpp}
   → If found: FAIL — "Debug block with TODO: {file}:{line}"

5. COMPILE with warnings as errors:
   Command: make CFLAGS+="-Wall -Werror" (or cmake equivalent)
   → If warnings: FAIL — "Compilation warning treated as error"

6. PROFILE frame timing (4-tier):
   gba_profile_frame() → cycles
   - GREEN:    cycles < 196,627 (< 70% of frame) → PASS
   - YELLOW:   cycles 196,627-252,806 (70-90%) → WARN — "Approaching budget: {cycles} ({percent}%)"
   - RED:      cycles > 252,806 (> 90%) → FAIL — "Near frame overrun: {cycles} ({percent}%)"
   - CRITICAL: cycles > 280,896 (> 100%) → HARD FAIL — "Frame overrun: {cycles} ({percent}%)"
   Per-module gate: RED and CRITICAL are FAIL. YELLOW is WARN (acceptable but flagged).
   Ship gate (Stage 6): YELLOW is also FAIL (must have headroom for future features).

7. ROM size check:
   Check .gba file size < 33,554,432 bytes (32MB GBA cart limit)
   → If over: FAIL — "ROM exceeds GBA cart limit: {size}MB"
```

A module CANNOT proceed to the next stage until ALL Production Gate checks pass (WARNs are acceptable, FAILs are not).

---

## Convergence Criteria

The mega-loop has CONVERGED when ALL of these are true:

1. **All planned modules** from BLUEPRINT.md are implemented and passing AI playtest
2. **Zero TODO/FIXME/STUB markers** in any source file
3. **Full E2E playthrough passes** (title screen → gameplay → game over/completion)
4. **VBlank budget GREEN (< 70%)** — all frames pass 4-tier profiling at Ship gate level
5. **At least one creative discovery round** completed (Stage 5 ran at least once)
6. **Documentation generated** and up to date (GAMEPLAY.md, TECHNICAL.md, MCP-TOOLS.md)

If convergence is not reached after **3 full pipeline iterations** (Stage 2→3→4→2 loops), report current status to user and ask for guidance rather than looping indefinitely.

---

## Softlock Detection

During AI Playtest (Stage 3) and Full Playthrough (Stage 6), monitor for softlocks:

```
ALGORITHM:
1. Every FRAMES_CHECKPOINT_INTERVAL (300) frames, record:
   - game_state byte value
   - player position (x, y)
   - frame counter
2. If ALL of these are unchanged for FRAMES_SOFTLOCK_TIMEOUT (900) consecutive frames:
   - game_state_byte identical
   - player_position (x, y) identical
   - No animation counter incrementing
   → Flag as SUSPECTED SOFTLOCK
3. Advance 300 more frames. If still unchanged → CONFIRMED SOFTLOCK
4. On confirmed softlock:
   - gba_screenshot() — capture frozen state
   - gba_read_memory for full state dump
   - Report: "SOFTLOCK DETECTED at frame {N}, state={state}, pos=({x},{y})"
   - STOP playtest, proceed to Stage 4 with softlock as test failure
```

EXCLUSIONS: Do not flag as softlock during:
- Title screen (game_state == TITLE)
- Pause menu (game_state == PAUSED)
- Cutscenes (if game has a cutscene flag, check it)

---

## Smart Wait Pattern

For transitions where exact frame count may vary (e.g., procedural content loading), use state-polling with a hard timeout:

```
PATTERN: wait_for_state(expected_state, max_frames)
  1. FOR i = 1 to max_frames:
     a. gba_advance_frames(1)
     b. gba_read_memory({game_state_addr}, 1) → current_state
     c. IF current_state == expected_state → RETURN i (frames waited)
  2. IF timeout reached → FAIL "Timeout waiting for state {expected} after {max_frames} frames"
```

USAGE:
- Boot to title: `wait_for_state(STATE_TITLE, FRAMES_BOOT_BIOS)` — max 270 frames
- Title to gameplay: `wait_for_state(STATE_PLAYING, FRAMES_TITLE_TO_GAME)` — max 60 frames
- Scene transition: `wait_for_state(expected_scene, FRAMES_SCENE_TRANSITION)` — max 75 frames
- Combat start: `wait_for_state(STATE_BATTLE, FRAMES_COMBAT_INIT)` — max 90 frames

RULE: Use fixed frame counts as PRIMARY mechanism (deterministic, catches regressions). Use smart wait as FALLBACK when fixed count times out (adaptive, handles variation).

---

## RNG Seed Control (Deterministic Testing)

For games with random/procedural elements, control the RNG for reproducible tests:

**METHOD 1 — Force seed via memory write (recommended):**
After boot, locate RNG state address from .map/.elf file:
```
arm-none-eabi-nm build/game.elf | grep -i rng
```
Write known seed: `gba_write_memory({rng_addr}, {known_seed}, 4)`
All subsequent random events are now deterministic.

**METHOD 2 — Fixed-frame input timing:**
If RNG seeds from frame count at first input, ensure `press_button()` calls happen at identical frame numbers across test runs. Use save states to create deterministic entry points.

**METHOD 3 — Compile-time test seed:**
Add a `#define TEST_SEED` in a header, used when `TEST_MODE` is defined. Agents compile with `-DTEST_MODE` for testing, without for production.

RULE: Always use save states as the primary isolation mechanism. RNG control is a secondary defense for procedural content testing.

---

## AI Playtest Sequences (Per Module Type)

These are the named test sequences referenced in Stage 3. Agents and the orchestrator use these — never invent sequences ad-hoc.

### SEQUENCE: DISPLAY_SETUP
```
1. gba_flash_rom("{rom_path}")
2. gba_advance_frames(FRAMES_BOOT_SKIP) → 10 frames (or FRAMES_BOOT_BUTANO=15 for Butano engine)
3. gba_screenshot() → verify visual output (not blank/garbled)
4. gba_read_io("REG_DISPCNT") → assert correct display mode bits
5. gba_read_io("REG_DISPCNT") → assert expected BG layers enabled
6. gba_read_io("REG_DISPCNT") → assert OBJ layer enabled (if sprites used)
NOTE: Detect engine type from build system. Use FRAMES_BOOT_BUTANO (15) for Butano, FRAMES_BOOT_SKIP (10) for libtonc/bare.
      Use FRAMES_BOOT_BIOS (270) only if BIOS intro is enabled.
```

### SEQUENCE: PLAYER_SPRITE
```
1. gba_save_state(0) → baseline
2. gba_screenshot() → verify player visible on screen
3. gba_read_oam(0) → assert player sprite visible, position reasonable
4. gba_press_button("RIGHT", 10)
5. gba_advance_frames(FRAMES_INPUT_VISUAL) → 4 frames for OAM DMA copy
6. gba_screenshot() → verify player moved
7. gba_read_oam(0) → assert x position increased
8. gba_load_state(0) → restore
```

### SEQUENCE: INPUT_HANDLING
```
1. gba_save_state(0) → baseline
2. For each button in [UP, DOWN, LEFT, RIGHT, A, B]:
   a. gba_read_memory({player_pos_addr}, 4) → record start position
   b. gba_press_button({button}, 10)
   c. gba_advance_frames(FRAMES_INPUT_SETTLE) → 3 frames for key_poll + logic
   d. gba_read_memory({player_pos_addr}, 4) → record end position
   e. gba_advance_frames(1) → +1 frame for OAM DMA (total FRAMES_INPUT_VISUAL=4)
   f. gba_screenshot()
   g. Assert position changed appropriately for this button
   h. gba_load_state(0) → reset for next button
```

### SEQUENCE: TILEMAP
```
1. gba_save_state(0) → baseline
2. gba_read_io("REG_BG0HOFS") → record initial scroll
3. gba_press_button("RIGHT", 30) → move camera
4. gba_advance_frames(FRAMES_INPUT_SETTLE) → 3 frames for scroll register update
5. gba_read_io("REG_BG0HOFS") → assert scroll changed
6. gba_advance_frames(1) → +1 frame for visual (total FRAMES_INPUT_VISUAL=4)
7. gba_screenshot() → verify new tiles visible
8. gba_read_tilemap(0, {x}, {y}) → verify tile data at expected position
9. gba_load_state(0) → restore
```

### SEQUENCE: COLLISION
```
1. gba_save_state(0) → baseline
2. gba_read_memory({player_x_addr}, 2) → record start_x
3. gba_press_button("RIGHT", 120) → walk into known wall (intentionally long to ensure contact)
4. gba_advance_frames(FRAMES_INPUT_SETTLE) → 3 frames for collision logic to resolve
5. gba_read_memory({player_x_addr}, 2) → record end_x
6. Assert end_x < {wall_x} — player stopped at wall boundary
7. gba_advance_frames(1) → +1 frame for visual (total FRAMES_INPUT_VISUAL=4)
8. gba_screenshot() → visual verification
9. gba_load_state(0) → restore
```

### SEQUENCE: COMBAT
```
1. gba_save_state(0) → baseline (near enemy or trigger)
2. gba_read_memory({player_hp_addr}, 2) → record player HP
3. <trigger encounter — game-specific button sequence>
4. gba_advance_frames(FRAMES_COMBAT_INIT_ACTION) → 45 frames for action game (or FRAMES_COMBAT_INIT=90 for RPG)
5. gba_screenshot() → verify combat screen
6. gba_press_button("A", 30) → execute attack
7. gba_advance_frames(FRAMES_INPUT_SETTLE) → 3 frames for damage calculation
8. gba_read_memory({enemy_hp_addr}, 2) → assert damage dealt (HP decreased)
9. gba_read_memory({player_hp_addr}, 2) → record post-combat HP
10. gba_screenshot() → verify combat resolution
11. gba_load_state(0) → restore
NOTE: Use FRAMES_COMBAT_INIT (90) for RPG-style turn-based combat with heavy transitions.
      Use FRAMES_COMBAT_INIT_ACTION (45) for action games with simpler combat entry.
```

### SEQUENCE: UI_HUD
```
1. gba_screenshot() → verify HUD elements visible
2. gba_read_oam({hud_sprite_slot}) → verify HUD sprite positions
3. gba_read_memory({score_addr}, 4) → verify score displays correctly
4. gba_read_memory({hp_addr}, 2) → verify HP bar reflects actual value
5. <trigger score change> → gba_screenshot() → verify HUD updated
```

### SEQUENCE: AUDIO
```
1. gba_read_io("REG_SOUNDCNT_H") → verify sound channels enabled
2. gba_read_io("REG_SOUNDCNT_X") → verify master sound enabled
3. <trigger sound event> → gba_advance_frames(FRAMES_BOOT_SKIP) → 10 frames for sound DMA to start
4. gba_read_io("REG_SOUNDCNT_H") → verify channel activity changed
```

### SEQUENCE: SAVE_SYSTEM
```
1. gba_read_memory({game_state_addr}, 4) → record current state
2. <trigger save mechanic>
3. gba_advance_frames(FRAMES_SAVE_COMPLETE) → 3 frames for SRAM write + state machine
4. gba_read_memory(0x0E000000, 32) → verify SRAM has data (not all 0xFF)
5. gba_reset()
6. gba_advance_frames(FRAMES_BOOT_SKIP) → 10 frames for boot (no BIOS)
7. <trigger load mechanic>
8. gba_advance_frames(FRAMES_INPUT_SETTLE) → 3 frames for load logic
9. gba_read_memory({game_state_addr}, 4) → assert matches saved state
```

### SEQUENCE: GAME_FLOW
```
1. gba_flash_rom("{rom_path}") → fresh boot
2. gba_advance_frames(FRAMES_BOOT_SKIP) → 10 frames (or FRAMES_BOOT_BIOS=270 if BIOS enabled)
3. gba_screenshot() → verify title screen
4. gba_press_button("START", FRAMES_INPUT_SETTLE) → 3-frame hold for menu input
5. gba_advance_frames(FRAMES_TITLE_TO_GAME) → 60 frames (32f fade + 5f load + 20f fade + 3f settle)
6. gba_screenshot() → verify gameplay state
7. gba_read_memory({game_state_addr}, 1) → assert PLAYING state
8. <trigger game over condition>
9. gba_advance_frames(FRAMES_SCENE_TRANSITION) → 75 frames for game over transition
10. gba_screenshot() → verify game over screen
11. gba_read_memory({game_state_addr}, 1) → assert GAME_OVER state
```

### SEQUENCE: FULL_PLAYTHROUGH (Stage 6 only)
```
1. gba_flash_rom("{rom_path}") → fresh boot
2. gba_advance_frames(FRAMES_BOOT_SKIP) → 10 frames (or FRAMES_BOOT_BUTANO=15 for Butano)
3. gba_screenshot()
4. gba_press_button("START", FRAMES_INPUT_SETTLE) → 3-frame hold
5. gba_advance_frames(FRAMES_TITLE_TO_GAME) → 60 frames to gameplay
6. FOR each level/area defined in BLUEPRINT.md:
   a. Navigate through area using defined button sequences
   b. gba_screenshot() every FRAMES_SCREENSHOT_GAMEPLAY (30) frames (~2/sec visual audit)
   c. gba_read_memory for key state variables every FRAMES_STATE_CHECK_FINE (5) frames
   d. Trigger key mechanics (combat, items, transitions)
   e. Between levels: gba_advance_frames(FRAMES_SCENE_TRANSITION) → 75 frames
   f. Assert level completion state
   g. If any transition exceeds FRAMES_SOFTLOCK_TIMEOUT (900) → abort with SOFTLOCK
7. gba_screenshot() → final state
8. gba_profile_frame() → performance check (4-tier threshold — see VBlank Budget Thresholds)
```

---

## Research Query Templates

### Architecture Query (Stage 1)
Focus: display mode, VRAM layout, module breakdown, memory layout, asset requirements, GBA constraints, budget estimates, parallel split.

### Iteration Query (Stage 4)
Focus: root cause of test failures, missing features, next priority, performance optimizations, polish opportunities. Includes test report table and memory state.

### Creative Query (Stage 5)
Focus: novel GBA-specific features, visual polish, audio tricks, hardware exploitation, speedrun features, accessibility. Scored with hardware-aware feasibility.

---

## Error Handling

| Stage | Error | Recovery |
|-------|-------|----------|
| 1 | Research query fails | Retry once after closing browser-bridge. If still fails, fall back to manual architecture planning from `architecture-patterns.md` |
| 2 | Agent crashes mid-build | Release locks, report partial progress, ask user: relaunch or merge partial? |
| 2 | Agent stuck (3+ checks, no commits) | Read `state.json`, check for build errors. Kill and relaunch with targeted prompt. |
| 2 | Merge conflicts | Follow orchestrator-multi conflict resolution. If unresolvable, rollback and re-split. |
| 3 | MCP connection lost | Verify mGBA running, MCP server on port 61337. Reconnect and retry. |
| 3 | Test assertion fails | STOP testing. Record failure. Proceed to Stage 4 for research-driven fix. |
| 4 | Research query fails | Retry once. If fails, use test failure details to manually diagnose. |
| 4 | Fix loop exceeds 3 iterations | Report status to user, ask for guidance. |
| 5 | Creative query fails | Skip creative discovery, proceed to Stage 6 with core features. |
| 5 | All creative ideas NOT_VIABLE | Log results, skip feature additions, proceed to Stage 6. |
| 6 | Full playthrough fails | Report where it failed, suggest manual debugging. |
| 6 | Production audit finds issues | List all issues, fix them, re-run audit. |
| ANY | Empty research results | Retry once. If still empty, suggest `/cache-perplexity-session`. |

---

## Loop Control Limits

To prevent infinite loops:

| Loop | Max Iterations | On Exceed |
|------|---------------|-----------|
| Stage 2↔4 (build-test-fix) | 3 | Report status, ask user |
| Stage 2↔5 (creative additions) | 2 | Stop adding features, proceed to Stage 6 |
| Production Gate retries per module | 3 | Report persistent failures, ask user |
| Total pipeline runtime | No hard limit | Report progress every 30 minutes |
