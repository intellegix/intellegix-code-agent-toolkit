# Game Profile: Pokemon EmberGold

## Identification
- **ROM Title Pattern:** `POKEMON EMER` or `POKEMON FIRE`
- **Guide Path:** `$GBA_DECOMP_DIR/docs/gba-claude-guide.md`

## MCP Tools (Game-Specific)
This game provides custom MCP tools beyond the generic `gba_*` set:
- `pokemon_get_party` — Party Pokemon data (count, species, levels)
- `pokemon_get_player_pos` — Player position and facing
- `pokemon_get_map` — Current map group/number
- `pokemon_get_ai_config` — AI system configuration state
- `pokemon_get_loyalty(slot)` — Persona and loyalty for a party slot
- `pokemon_get_ai_party_data(slot)` — AI behavior data for a party slot

## Test Modes

### `smoke` — Quick Health Check (~15 tool calls)
Run 10 assertions:
1. `gba_get_frame` — ASSERT responds with frame number (emulator running)
2. `gba_screenshot` — describe what you see on screen
3. RECIPE 1: `gba_load_state(1)` + `gba_advance_frames(30)` — ASSERT loads without error
4. `pokemon_get_player_pos` — ASSERT coords ~(13, 13) (bedroom)
5. `pokemon_get_map` — ASSERT group=4, num=1 (Player's bedroom)
6. `pokemon_get_party` — ASSERT partyCount >= 1, first mon level ~5
7. `pokemon_get_ai_config` — ASSERT systemEnabled=true
8. `pokemon_get_loyalty(0)` — ASSERT persona 0-7, loyalty in range -128..127
9. `pokemon_get_ai_party_data(0)` — ASSERT readable (no error)
10. `gba_screenshot` — capture final state

### `ai` — Full AI Regression (~100+ tool calls)
All 7 AI phases: win/loss/trauma/revenant/typeKO/redemption.
Delegates to `/gba-ai-full` sub-command.

### `battle` — Single Battle Cycle (~40 tool calls)
RECIPE 2 (load grass) -> RECIPE 4 (encounter) -> RECIPE 5 (spam) -> RECIPE 6 (end) -> RECIPE 7 (verify counters).

### `nav` — Navigation Test (~30 tool calls)
RECIPE 3 (bedroom -> Route 1 grass -> save state 9).

### `custom <description>` — User-Described Test
Map user's description to RECIPES from the guide. If no recipe matches, report and ask for clarification. Never invent sequences.

### `johto-maps` — Johto Map Loading & Warp Tests (~300-400 tool calls)

Tests all 73+ Johto maps for correct loading, tileset rendering, warps, and connections.

**Prerequisite:** At least one Johto save state (slots 2-8) must exist. See guide section 9.

**Phase 1: Map Loading Scan (~292 calls)**
For each of the 73 Johto maps (group=43, num=0..72):
  1. RECIPE 20 (VERIFY_MAP_LOADS) — teleport to map, verify no crash
  2. gba_screenshot — capture viewport
  3. AI-describe: check for rendering artifacts (black tiles, corruption)
  ~4 calls per map × 73 maps

**Phase 2: Warp Verification (~100 calls)**
Test 20 critical warp pairs from KNOWN_WARP_PAIRS (guide section 10):
  - Gym entrance/exit pairs (8 gyms × 2 = 16 warps)
  - Cross-region warps (Ferry, Victory Road — 4 warps)
  For each: RECIPE 21 (VERIFY_WARP) — walk to warp, verify destination
  ~5 calls per warp × 20 warps

**Phase 3: Connection Verification (~60 calls)**
Test 15 outdoor route-to-city connections:
  For each: RECIPE 22 (VERIFY_CONNECTION) — walk to boundary, verify adjacent map
  ~4 calls per connection × 15 connections

**Report Format:**
```
| # | Map | Group/Num | Loads | Tileset | Warps | Result |
|---|-----|-----------|-------|---------|-------|--------|
| 1 | New Bark Town | 43/0 | yes | clean | 2/2 | PASS |
...
```

### `johto-story` — Johto Story Progression Tests (~400-600 tool calls)

Tests the complete 34-step Johto progression via save state checkpoints and memory injection.

**Prerequisite:** Save states at checkpoints per guide section 9. Missing slots are skipped.

**Phase 1: Checkpoint Validation (~80 calls)**
For each available save state (slots 2-8):
  1. RECIPE 12 (LOAD_CHECKPOINT) — load slot, verify map
  2. RECIPE 13 (VERIFY_CHECKPOINT_STATE) — verify badges, flags, vars match expected
  ~10 calls per checkpoint × 8 slots

**Phase 2: Story Gate Testing (~180 calls)**
At each checkpoint, verify gating dependencies:
  1. RECIPE 40 (VERIFY_BADGE) — badge count correct
  2. RECIPE 41 (VERIFY_STORY_FLAGS) — progression flags at correct step
  3. RECIPE 42 (VERIFY_SILVER_ENCOUNTER) — Silver progress correct
  4. RECIPE 43 (VERIFY_ROCKET_PROGRESS) — Rocket events correct
  ~15 calls per gate × 12 key gates

**Phase 3: Intermediate State Injection (~100 calls)**
For 10 story steps between checkpoints:
  1. RECIPE 47 (INJECT_STORY_STATE) — write flags/vars for target step
  2. RECIPE 41 — verify injection matches expected state
  ~10 calls per intermediate × 10 steps

**Phase 4: Screenshot Documentation (~34 calls)**
Screenshot at every checkpoint and major story gate:
  RECIPE 80 (SCREENSHOT_LOCATION) for each key location

**Report Format:**
```
| Step | Location | Checkpoint | Badges | Flags | Gates | Result |
|------|----------|------------|--------|-------|-------|--------|
| 2 | New Bark Town | Slot 2 | 0 | 1/1 | — | PASS |
| 5 | Violet City | Slot 3 | 1 | 3/3 | Falkner | PASS |
...
```

### `johto-trainers` — Johto Trainer Data Verification (~200-350 tool calls)

Verifies trainer data integrity for all Johto trainers (IDs 743-891).

**Phase 1: ROM Trainer Table Scan (~284 calls)**
For each Johto trainer ID (743-891):
  RECIPE 60 (SCAN_TRAINER_STRUCT) — read name, party type, party pointer
  ASSERT: valid struct, proper terminator, ROM-range party pointer
  ~2 calls per trainer × 142 trainers

**Phase 2: Silver Variant Validation (~42 calls)**
RECIPE 61 (VERIFY_SILVER_VARIANTS):
  Verify 21 Silver entries (7 encounters × 3 starter variants)
  ASSERT: consecutive IDs, valid party data, increasing team sizes

**Phase 3: E4 + Champion Verification (~10 calls)**
RECIPE 62 (VERIFY_E4_IDS):
  Verify IDs 880-884 sequenced (Will→Koga→Bruno→Karen→Lance)
  ASSERT: all 5 have valid structs, appropriate party sizes

**Phase 4: Functional Gym Battle Tests (~20 calls per gym)**
For gyms with available save states:
  RECIPE 63 (VERIFY_GYM_BATTLE) — load state, teleport to gym, attempt battle
  Screenshot battle screen if triggered
  ~20 calls per gym × number of available save states

**Report Format:**
```
| Category | IDs | Valid | Invalid | Result |
|----------|-----|-------|---------|--------|
| Route trainers | 743-767 | 25 | 0 | PASS |
| Silver variants | 768-873 | 21 | 0 | PASS |
| Rocket grunts | 841-862 | 22 | 0 | PASS |
| Kimono Girls | 863-867 | 5 | 0 | PASS |
| E4 + Lance | 880-884 | 5 | 0 | PASS |
```

### `johto-visual` — Johto Visual Verification (~200-300 tool calls)

Screenshot-focused aesthetic verification of every major Johto location using AI description.

**Phase 1: City Panoramas (~55 calls)**
For all 10 Johto cities + Indigo Plateau:
  RECIPE 80 (SCREENSHOT_LOCATION) — teleport, screenshot, AI-describe
  Check: tileset rendering, NPC placement, building positions
  ~5 calls per city × 11 locations

**Phase 2: Route Sampling (~60 calls)**
For 15 key routes:
  RECIPE 80 — teleport, screenshot, AI-describe
  Check: grass tiles, tree placement, trainer sprites, water tiles
  ~4 calls per route × 15 routes

**Phase 3: Dungeon/Interior Checks (~60 calls)**
For 10 key interiors (gyms, caves, Rocket Hideout, Lighthouse):
  RECIPE 83 (INTERIOR_CHECK) — exterior + interior screenshots
  Check: floor tiles, furniture, NPCs, no void/corruption
  ~6 calls per interior × 10 interiors

**Phase 4: Tileset Transitions (~48 calls)**
For 8 critical boundary crossings (city-to-route, route-to-cave):
  RECIPE 82 (TILESET_TRANSITION) — screenshot at boundary, cross, screenshot
  Check: both tilesets valid, no corruption on transition
  ~6 calls per transition × 8 crossings

**Phase 5: Anomaly Report**
AI-powered review of all captured screenshots for:
  - Black/missing tiles
  - Sprite misalignment
  - Corrupted metatiles
  - Wrong palette colors

**Report Format:**
```
| Location | Type | Tileset | NPCs | Artifacts | Result |
|----------|------|---------|------|-----------|--------|
| New Bark Town | City | clean | 3 | none | PASS |
| Route 29 | Route | clean | 2 | none | PASS |
| Violet Gym | Interior | clean | 4 | none | PASS |
...
```

### `johto-full` — Complete Johto Test Suite (~1200-2000 tool calls)

Runs all Johto modes in sequence. Stop on first FAIL in any mode.

**Execution order:**
1. `johto-maps` — verify all maps load and connect
2. `johto-story` — verify progression flags and state
3. `johto-trainers` — verify trainer data integrity
4. `johto-visual` — verify visual rendering

**Report:** Combined summary from all 4 modes with total pass/fail counts.

---

## Recipes
All recipes are defined in the game guide file. Read the guide BEFORE executing any recipe. Recipe names: RECIPE 1 through RECIPE 84 as defined there (1-10: core, 11-19: extended, 20-39: maps, 40-59: story, 60-79: trainers, 80-89: visual).

## Smoke Report Format
```
| # | Test | Expected | Actual | Result |
|---|------|----------|--------|--------|
| 1 | Emulator responds | frame > 0 | ... | PASS/FAIL |
| 2 | Screenshot | visible game screen | ... | PASS/FAIL |
| 3 | State 1 loads | no error | ... | PASS/FAIL |
| 4 | Position | ~(13,13) | ... | PASS/FAIL |
| 5 | Map | 4/1 | ... | PASS/FAIL |
| 6 | Party | 1+ mon, Lv5 | ... | PASS/FAIL |
| 7 | AI config | enabled=true | ... | PASS/FAIL |
| 8 | Loyalty | persona 0-7 | ... | PASS/FAIL |
| 9 | AI party data | readable | ... | PASS/FAIL |
| 10 | Final screenshot | captured | ... | PASS/FAIL |
```
