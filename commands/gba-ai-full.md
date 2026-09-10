# /gba-ai-full — Full AI Behavior Regression Test

## MANDATORY FIRST STEP
Read `docs/gba-claude-guide.md` from `$GBA_DECOMP_DIR` (set this to your pokefirered decomp checkout) completely before any MCP tool call.

## 5 ABSOLUTE RULES
1. VERIFY state before every action (screenshot + callback2)
2. ONLY named RECIPES — never invent button sequences
3. Frame advances >30 → state check first
4. Screenshot every 5 tool calls
5. Assertion fail → STOP, report, never retry blindly

## Prerequisite
Save state 9 must exist (Route 1 grass, map 3/19). If not, run RECIPE 3 (bedroom → grass) first.

## Test Phases

### Phase 0: Setup Verification
```
RECIPE 2: Load state 9
ASSERT: map 3/19, position near grass
Screenshot
```

### Phase 1: Win Battle — Loyalty & Counter Increments
```
gba_load_state(9)
PRE: pokemon_get_loyalty(0), pokemon_get_ai_party_data(0) → record values
RECIPE 4 → RECIPE 5 → RECIPE 6 → RECIPE 7
ASSERT: battleCount +1, victoryCount +1, loyalty increased (+2 or +3)
```

### Phase 2: Lose Battle — Faint Counter & Loyalty Penalty
```
gba_load_state(9)
PRE: record loyalty, faintCount
RECIPE 8 (force loss: HP=1, speed=0)
POST: pokemon_get_ai_party_data(0)
ASSERT: faintCount +1, loyalty decreased
```

### Phase 3: Trauma Recording
```
RECIPE 9 (full procedure — sets faintCount=2, clears trauma, force loss)
ASSERT: faintCount >= 3, trauma bits non-zero
```

### Phase 4: Revenant Transformation
```
RECIPE 10 (full procedure — enable nightmare, clear reserved, force loss)
ASSERT: reserved byte & 0x02 != 0 (REVENANT_BIT set)
```

### Phase 5: Type KO Counter Increment
```
gba_load_state(9)
Read SB2 pointer, zero all 18 typeKOCounts (SB2 + 0xB20 + 167, 18 bytes)
RECIPE 4 (trigger encounter)
Read enemy types: gba_read_memory(0x02021410 + 88 + 0x21, 1) → type1
                  gba_read_memory(0x02021410 + 88 + 0x22, 1) → type2
RECIPE 5 (win battle — ASSERT outcome=1)
RECIPE 6 (wait end)
Read typeKOCounts via pokemon_get_ai_config
ASSERT: typeKOCounts[enemyType1] >= 1
```

### Phase 6: Revenant Redemption
```
gba_load_state(9)
Read SB1 pointer
Set reserved byte = REVENANT_BIT | (9 << 2) = 0x02 | 0x24 = 0x26
  gba_write_memory(sb1 + 0x348C + 5, 0x26, 1)
RECIPE 4 (encounter) → RECIPE 5 (win, ASSERT outcome=1) → RECIPE 6 (end)
Read reserved byte: gba_read_memory(sb1 + 0x348C + 5, 1)
ASSERT: (reserved & 0x02) == 0 (revenant cleared after 10th win)
```

## Between Each Phase
- `gba_load_state(9)` to reset state
- `gba_screenshot` to verify grass state
- Report phase pass/fail BEFORE proceeding to next

## Report Format
```
| Phase | Test | Expected | Actual | Result |
|-------|------|----------|--------|--------|
| 0 | Map is Route 1 | 3/19 | ... | PASS/FAIL |
| 1 | battleCount delta | +1 | ... | PASS/FAIL |
| 1 | victoryCount delta | +1 | ... | PASS/FAIL |
| 1 | loyalty delta | +2..+3 | ... | PASS/FAIL |
| 2 | faintCount delta | +1 | ... | PASS/FAIL |
| 2 | loyalty delta | negative | ... | PASS/FAIL |
| 3 | trauma bits | non-zero | ... | PASS/FAIL |
| 4 | revenant bit | set (0x02) | ... | PASS/FAIL |
| 5 | typeKO increment | >= 1 | ... | PASS/FAIL |
| 6 | revenant cleared | bit 0x02 = 0 | ... | PASS/FAIL |
```

Stop on first FAIL — report state and suggest debugging steps.
