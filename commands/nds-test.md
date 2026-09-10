# /nds-test -- NDS Emulator Test Runner for HGSS KantoReborn

## MANDATORY FIRST STEP

Read the knowledge base BEFORE any MCP tool call:
```
Read file: C:\dev\desmume-mcp-server\docs\HGSS_KNOWLEDGE.md
```
This contains verified memory addresses, frame budgets, touch blacklist, and known limitations. Every claim in that file is verified against ROM source and E2E tests.

## 6 ANTI-HALLUCINATION RULES

1. **ALWAYS call `nds_detect_state` before any action** -- never assume the game state
2. **NEVER guess touch coordinates** -- only use coordinates from HGSS_KNOWLEDGE.md
3. **NEVER navigate the intro manually** -- always use `hgss_new_game_to_overworld`
4. **PREFER memory reads over screenshots** -- `hgss_verify_flags`, `nds_read_memory` are faster and more reliable
5. **USE structured test plans** for repeatable scenarios -- `nds_run_builtin_test` or `nds_run_test`
6. **SAVE STATE before risky actions** -- `nds_save_state` before battles or transitions

## TOOL HIERARCHY (prefer higher-level tools)

| Priority | Tool | When to Use |
|----------|------|-------------|
| 1 (best) | `nds_run_builtin_test` | Run a named test suite: boot, new_game_intro, flag_readwrite, full_kanto, full_kanto_real |
| 2 | `nds_run_test` | Run a custom JSON test plan |
| 3 | `hgss_run_recipe` | Run a named battle recipe (BROCK, MISTY, etc.) -- handles full lifecycle |
| 3 | `hgss_real_battle` | State-aware battle with BATTLE detection + win_flag reading |
| 3 | `hgss_new_game_to_overworld` | Start fresh game and reach overworld (story_state >= 3) |
| 3 | `hgss_advance_dialogue` | Press A through dialogue until script ends |
| 3 | `hgss_walk` | Walk N steps in a direction (requires OVERWORLD state) |
| 3 | `hgss_interact` | Press A to interact with NPC/object (requires OVERWORLD) |
| 3 | `hgss_battle_spam` | A-spam through a battle (any state, frame-count timeout, legacy) |
| 3 | `hgss_battle_with_verify` | Composite: buff -> walk -> battle -> verify (supports real_battle mode) |
| 3 | `hgss_buff_party` | Discover party, inject 999 stats for automated battle testing |
| 3 | `hgss_verify_flags` | Batch flag verification (returns PASS/FAIL per flag) |
| 3 | `hgss_read_map_id` | Read current map ID via FieldSystem discovery |
| 3 | `hgss_read_location` | Read full Location (mapId, x, y, direction) |
| 3 | `hgss_save_checkpoint` / `hgss_load_checkpoint` | Named save states for test checkpoints |
| 4 | `nds_detect_state` | Check current game state (OVERWORLD, DIALOGUE, BATTLE, PRE_INIT) |
| 5 (last) | `nds_press_button`, `nds_advance_frames`, `nds_touch_screen` | Raw inputs -- LAST RESORT only |

## PROCESS

1. **Parse $ARGUMENTS** to determine test strategy
2. **Choose strategy:**
   - If argument matches a builtin name (boot, new_game_intro, flag_readwrite, full_kanto, full_kanto_real) -> run `nds_run_builtin_test`
   - If argument is a JSON plan -> run `nds_run_test`
   - If argument describes an exploratory test -> build steps using high-level tools
   - If no argument -> run `nds_run_builtin_test("boot")` as smoke test
3. **Execute** the test plan
4. **Report** results in the table format below

## AVAILABLE BUILTIN TESTS

| Name | Description | Duration |
|------|-------------|----------|
| `boot` | Load ROM, advance 720f, verify game booted | ~2s |
| `new_game_intro` | Full intro to overworld, verify story_state >= 3 | ~15s |
| `flag_readwrite` | Write/read FR flags, verify memory integrity | ~20s |
| `full_kanto` | Tier 1 intro + Tier 2 flag sim: boot through Champion Blue | ~20s |
| `full_kanto_real` | v2: Recipe system with real battles. Tier 1-2 gyms + E4 + Champion use real A-spam battles with BATTLE state detection + win_flag reading. Tier 3 gyms and rivals remain flag-simulated. | ~60-300s |

## CUSTOM JSON TEST PLAN SCHEMA

```json
{
  "name": "my_test",
  "description": "What this test verifies",
  "setup": "none | new_game | load_state:N",
  "steps": [
    {
      "action": "advance_frames | press_button | walk | interact | advance_dialogue | battle_spam | real_battle | new_game_to_overworld | verify_flags | detect_state | write_flag | write_var | read_var | save_state | load_state | save_checkpoint | load_checkpoint | buff_party | battle_with_verify | walk_recipe | run_recipe | read_map_id | read_location",
      "args": {},
      "expected_state": "OVERWORLD | DIALOGUE | PRE_INIT | null",
      "assertions": {
        "story_state_gte": 3,
        "flag_set": ["0x0B60"],
        "flag_clear": ["0x0B67"],
        "var_equals": {"0x4180": 3}
      },
      "description": "Step description"
    }
  ]
}
```

## REPORT FORMAT

After every test run, present results as:

```
| Step | Action | Expected | Actual | Result |
|------|--------|----------|--------|--------|
| 1 | advance_frames(720) | -- | -- | PASS |
| 2 | detect_state | OVERWORLD | OVERWORLD | PASS |
```

**Summary:** N/N steps passed | Duration: Xs | Final state: STATE

## KEY MEMORY REFERENCES

- `sSaveDataPtr`: 0x021D22A8 (becomes valid ~frame 60)
- `SaveVarsFlags`: sSaveDataPtr value + 0x0DF4
- `FR_VAR_STORY_STATE`: var 0x4180 (0=default, 1=Kanto chosen, 2=warped, 3=intro done)
- `ScriptEnvironment magic`: 222271 (0x000363FF) -- heap-scanned, cached
- `activeScriptContextCount`: ScriptEnv + 0x09 (>0 = DIALOGUE)
- `battleWinFlag`: ScriptEnv + 0x0C (1=won, 0=lost, read after battle)
- `FieldSystem`: heap-scanned via SaveData anchor at +0x0C
- `Location`: FieldSystem + 0x20 -> mapId, x, y, direction

## COMMON SCENARIOS

### Verify a ROM change didn't break the intro
```
/nds-test full_kanto
```

### Full Kanto with stat injection and checkpoints (real gameplay pipeline)
```
/nds-test full_kanto_real
```

### Quick smoke test
```
/nds-test boot
```

### Test specific flags after a change
```
/nds-test {"name":"badge_check","description":"Verify Brock badge","setup":"new_game","steps":[{"action":"write_flag","args":{"flag_id":"0x0B60","value":true},"description":"Set Brock badge"},{"action":"verify_flags","args":{"expected":{"0x0B60":true}},"description":"Verify badge set"}]}
```

## ERROR HANDLING

- If `nds_detect_state` returns UNKNOWN -> report it, do not guess
- If a tool returns `success: false` -> report the error, do not retry blindly
- If the intro fails (story_state < 3) -> report the story_state value and which phase stalled
- If ROM is not loaded -> call `nds_load_rom` with the ROM path first
- Default ROM path: `$NDS_ROM_DIR/pokeheartgold.us.nds`
