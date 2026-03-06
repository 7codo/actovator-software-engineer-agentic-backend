## Inputs
- The coding task description

## Completion Checklist

### 1. Task Verification
- [ ] Confirm the task requirements are fully implemented

### 2. Code Integrity
- [ ] All files, functions, and imports are used — no dead code or orphaned references
- [ ] All links in the dependency chain are connected

### 3. Lint Check
````bash
npm run lint
````

### 4. User Flow Test
Simulate key user interactions end-to-end using `agent-browser`:
````bash
agent-browser open http://localhost:3000
agent-browser snapshot -i --json
````

For each primary user flow relevant to the task (e.g. sign-up, form submit, navigation):
````bash
# Example: test a login flow
agent-browser fill @e1 "user@example.com"
agent-browser fill @e2 "password"
agent-browser click @e3
agent-browser wait --load networkidle
agent-browser snapshot -i --json   # Confirm expected state
````

- Re-snapshot after page changes to discover new refs
- Verify the UI reaches the expected state after each action
- Check for any visual regressions or broken interactions
- Take a screenshot on failure for the report: `agent-browser screenshot flow-failure.png`
````bash
agent-browser close
````

### 5. Browser Console Check
````bash
agent-browser open http://localhost:3000
agent-browser console
````

* Review all console output for client-side errors or warnings
* Close the browser when done

### 6. Dev Server Logs
````bash
get_server_logs
````

* Review for any server-side errors or warnings

Create full report