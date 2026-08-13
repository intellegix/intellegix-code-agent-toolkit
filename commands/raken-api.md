# /raken-api -- Raken API Context Loader

Load Raken Public API 3.0 documentation into active context. Run this at the start of any Raken API work session to ground all decisions in the actual documentation.

**This is a local context loader -- no Perplexity, no external queries.** For external research, use /raken-perplexity after loading context with this command.

## Usage
- /raken-api -- Load full API context (all endpoints, auth, patterns)
- /raken-api ARGUMENTS -- Load filtered context focused on specific resource

Examples: /raken-api timeCards, /raken-api members, /raken-api dailyReports oauth

## Documentation Directory

```
$RAKEN_REFERENCE_DIR
```

## Execution

### Step 1: Locate and Validate Cache

Check for the compiled reference cache:
```
{docs_dir}/raken-api-reference.md
```

**If the file exists**: Check its modification date. If modified within the last 7 days, proceed to Step 3. If older than 7 days, proceed to Step 2.

**If the file does NOT exist**: Proceed to Step 2.

**If ARGUMENTS contains "rebuild"**: Always proceed to Step 2 regardless of cache age.

### Step 2: Rebuild Cache (only when needed)

Read the following source files from the documentation directory and compile them into raken-api-reference.md. This step runs rarely -- only on first invocation or when cache is stale.

1. Read README_API_TESTING.md -- extract OAuth credentials, endpoint categories, test workflow
2. Read Raken+Public+API+3.csv (all 1807 lines) -- extract and group by resource type: endpoint paths, HTTP methods, query parameters (name, description, optional/required, constraints), response fields (path, type, description)
3. Read Raken_API_SOP.html -- extract OAuth flow steps, token management, troubleshooting, security practices
4. Read raken_make_calls.py -- extract request patterns, base URL, headers, pagination, error handling
5. Read raken_get_token.py and raken_auth_url.py -- extract full OAuth flow code patterns
6. Read Raken Public API 3.0 Developer Guide.pdf (pages 1-20) -- extract any endpoints, data models, or patterns not already captured from the CSV

Compile into structured markdown with sections: Authentication, Base Configuration, Endpoints by Resource (grouped), Existing Code Patterns, Common Troubleshooting. Write to raken-api-reference.md.

### Step 3: Load Context

Read raken-api-reference.md into active context.

**If ARGUMENTS is not empty and does not contain "rebuild"**: After reading the full file, focus your working context on sections matching ARGUMENTS. Use substring matching -- "time" should match "Time Cards" and any time-related content. Still keep the full reference available for cross-referencing.

**If ARGUMENTS is empty**: Load the full reference.

### Step 4: Check Token Status

Read raken_token.json from the documentation directory (if it exists). Check the expires_at field against the current time. Report token status.

### Step 5: Confirm to User

Output a brief confirmation:

```
Raken API context loaded (N endpoints across M resource types)
Auth: OAuth 2.0 -- token at raken_token.json [STATUS: valid/expired/not found]
Base URL: https://developer.rakenapp.com/api/
Rate limit: Per-second + burst + daily quota (plan-dependent)
Cache: raken-api-reference.md [age: N days]

Ready for Raken API work. All decisions will reference local docs.
```

If filtered: add "Focused on: ARGUMENTS"

## Critical Rules -- MANDATORY

1. **NEVER make Raken API decisions based on training data alone.** Always cite which section of the compiled reference justifies an implementation choice.
2. **If an endpoint is NOT in the compiled reference, say so explicitly.** Do not invent endpoints, parameters, or response fields.
3. **When writing code that calls the Raken API**, follow the existing patterns from raken_make_calls.py (headers, base URL, error handling, token loading).
4. **All date parameters** use yyyy-MM-dd format. All datetime parameters use yyyy-MM-ddThh:mm:ssZ format.
5. **Pagination is mandatory** for list endpoints. Always implement offset/limit loops with max 1000 per page.
6. **Time-range constraints**: Most endpoints enforce max 1 month between date filters. Daily reports enforce max 31 days.
7. **Token management**: Always check token expiry before API calls. Implement refresh_token flow for production use.
8. **Never hardcode or log** client_secret or access tokens. Load from environment or token file.
9. **429 responses** require backoff and retry. Distinguish between rate limit (per-second), burst limit (concurrent), and quota (daily).
10. **When the user asks about a Raken endpoint**, always look it up in the loaded reference first. If you need more detail than the reference provides, suggest running /raken-perplexity for external research.

## Integration with Other Commands

- **/raken-perplexity**: Run /raken-api FIRST to establish local context, then /raken-perplexity for external research grounded in the docs.
- **/implement-perplexity**: When implementing Raken features, run /raken-api at session start so all implementation queries have API context.
- **/research-perplexity**: For general Raken research, the loaded context from /raken-api ensures research queries include accurate API details.
