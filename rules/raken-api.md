---
paths:
  - "**/raken*.py"
  - "**/raken*.js"
  - "**/raken*.ts"
  - "**/raken_*.py"
  - "**/raken_*.js"
  - "**/raken_*.ts"
---

# Raken API Documentation Awareness

When working on any Raken API-related file, these rules apply:

1. **Check for loaded context**: If the Raken API reference has not been loaded in this session, prompt: "Run `/raken-api` first to load Raken API documentation context before proceeding."

2. **Check for compiled cache**: The compiled reference lives at:
   ```
   $RAKEN_REFERENCE_DIR/raken-api-reference.md
   ```
   If this file does not exist, prompt the user to run `/raken-api` to generate it.

3. **Never infer endpoint behavior from training data.** Always defer to the compiled reference for endpoint paths, parameters, response schemas, and constraints.

4. **Follow existing code patterns** from the documentation directory (raken_make_calls.py) for headers, base URL, token loading, and error handling.

5. **Security**: Never hardcode or log client_secret or access_token values. Never commit raken_token.json to git.
