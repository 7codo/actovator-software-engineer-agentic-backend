name: agent-browser
description: "Use this skill for any browser automation, web scraping, UI testing, or web interaction task. Triggers when the user wants to navigate websites, click elements, fill forms, take screenshots, extract page content, test web UIs, automate login flows, scrape data, interact with web apps, or do anything involving controlling a browser. Use it whenever the user mentions browsing, clicking, screenshots, web automation, or testing a webpage — even casually."
---

# agent-browser

Headless browser automation CLI. Already globally installed — skip any installation steps.

## Core Workflow (AI-Optimized)

```bash
# 1. Open a page
agent-browser open <url>

# 2. Get interactive elements with refs
agent-browser snapshot -i

# 3. Interact using refs (@e1, @e2, etc.)
agent-browser click @e2
agent-browser fill @e3 "value"

# 4. Re-snapshot after page changes
agent-browser snapshot -i
```

**Always prefer the snapshot → ref pattern.** It's deterministic and fast. Use CSS/text selectors only as fallback.

---

## Essential Commands

### Navigate & Capture
```bash
agent-browser open <url>             # Navigate (aliases: goto, navigate)
agent-browser snapshot               # Full accessibility tree with refs
agent-browser snapshot -i            # Interactive elements only (best for AI)
agent-browser snapshot -i -c -d 5    # Compact, depth-limited (large pages)
agent-browser snapshot -s "#main"    # Scope to a CSS selector
agent-browser screenshot [path]      # Screenshot (omit path → temp file)
agent-browser screenshot --annotate  # Numbered labels on elements → refs cached
agent-browser get url                # Current URL
agent-browser get title              # Page title
```

### Interact
```bash
agent-browser click <sel>            # Click element
agent-browser fill <sel> <text>      # Clear and fill input
agent-browser type <sel> <text>      # Type (without clearing)
agent-browser press <key>            # Press key: Enter, Tab, Control+a
agent-browser select <sel> <val>     # Select dropdown option
agent-browser check <sel>            # Check checkbox
agent-browser scroll down [px]       # Scroll (up/down/left/right)
agent-browser hover <sel>            # Hover element
agent-browser drag <src> <tgt>       # Drag and drop
```

### Read Data
```bash
agent-browser get text <sel>         # Get text content
agent-browser get html <sel>         # Get innerHTML
agent-browser get value <sel>        # Get input value
agent-browser get attr <sel> <attr>  # Get attribute value
agent-browser get count <sel>        # Count matching elements
```

### Wait (important for dynamic pages)
```bash
agent-browser wait <selector>              # Wait for element visible
agent-browser wait 2000                    # Wait N milliseconds
agent-browser wait --text "Welcome"        # Wait for text to appear
agent-browser wait --url "**/dashboard"    # Wait for URL pattern
agent-browser wait --load networkidle      # Wait for network to settle
agent-browser wait --fn "window.ready"     # Wait for JS condition
```

### Navigation
```bash
agent-browser back
agent-browser forward
agent-browser reload
```

---

## Selectors

### Refs (preferred)
After `snapshot`, use `@e1`, `@e2`, etc. — deterministic, no DOM re-query needed.

### Semantic locators (readable fallback)
```bash
agent-browser find role button click --name "Submit"
agent-browser find text "Sign In" click
agent-browser find label "Email" fill "user@example.com"
agent-browser find placeholder "Search..." fill "query"
```

### CSS / text / XPath
```bash
agent-browser click "#submit-btn"
agent-browser click "text=Sign In"
agent-browser click "xpath=//button[@type='submit']"
```

---

## JSON Output (for parsing results)
```bash
agent-browser snapshot --json
agent-browser get text @e1 --json
agent-browser is visible @e2 --json
```

---

## Sessions (parallel isolation)
```bash
agent-browser --session agent1 open site-a.com
agent-browser --session agent2 open site-b.com
agent-browser session list
```

---

## Tabs
```bash
agent-browser tab new https://example.com
agent-browser tab          # List tabs
agent-browser tab 2        # Switch to tab 2
agent-browser tab close    # Close current tab
```

---

## Auth & State
```bash
# Persist login across runs
agent-browser --profile ~/.myapp-profile open myapp.com

# Headers for API auth (scoped to origin)
agent-browser open api.example.com --headers '{"Authorization": "Bearer <token>"}'

# Save/load state
agent-browser state save ./auth.json
agent-browser state load ./auth.json
```

---

## Network
```bash
agent-browser network route "**/*.png" --abort               # Block images
agent-browser network route "**/api/**" --body '{"ok":true}' # Mock response
agent-browser network requests                               # View tracked requests
```

---

## Cookies & Storage
```bash
agent-browser cookies                    # Get all cookies
agent-browser cookies set name value     # Set cookie
agent-browser storage local              # Get all localStorage
agent-browser storage local set key val  # Set localStorage value
```

---

## Debug
```bash
agent-browser open example.com --headed    # Show browser window
agent-browser console                      # View console messages
agent-browser errors                       # View JS errors
agent-browser highlight <sel>              # Highlight element visually
agent-browser eval "document.title"        # Run JavaScript
```

---

## Command Chaining
```bash
# Use && for efficiency when you don't need intermediate output
agent-browser open example.com && agent-browser wait --load networkidle && agent-browser snapshot -i

agent-browser fill @e1 "user@example.com" && agent-browser fill @e2 "pass" && agent-browser click @e3
```

---

## Annotated Screenshots (multimodal workflow)
```bash
agent-browser screenshot --annotate ./page.png
# → Labels [1], [2], [3] on screenshot correspond to @e1, @e2, @e3
agent-browser click @e2   # Refs are cached after annotate
```
Use for pages with icon-only buttons, canvas elements, or complex visual layouts.

---

## Diff / Visual Testing
```bash
agent-browser diff snapshot                             # vs. last snapshot
agent-browser diff screenshot --baseline before.png    # Pixel diff
agent-browser diff url https://v1.com https://v2.com   # Compare two URLs
```

---

## Key Tips

- **Always `snapshot -i` before interacting** — discover refs first, then act
- **Use `wait --load networkidle`** after navigation on SPAs/dynamic pages
- **Scope snapshots** with `-s "#main"` or `-d 3` on complex pages to reduce noise
- **`--json` flag** makes output machine-parseable for data extraction tasks
- **Default timeout is 25s** — set `AGENT_BROWSER_DEFAULT_TIMEOUT=45000` for slow pages
- The browser **persists as a background daemon** — no need to relaunch between commands
```