---
name: tools_usage
description: Precise code editing and navigation skill using symbolic and file-based tools. Use this skill whenever working on a coding project — editing functions, classes, or methods; navigating a codebase; adding new code; refactoring; or understanding relationships between symbols. Trigger this skill for any task involving read_file, find_symbol, replace_symbol_body, replace_content, insert_after_symbol, find_referencing_symbols, or similar code/LSP/file tools.
---

# Coding Tools Skill

This skill guides precise, reliable code editing using a set of symbolic and file-based tools.

---

## General Principles

Wait for an explicit editing task before making changes. Don't be overly eager — if the user hasn't asked you to edit anything yet, explore and understand the code first.

When writing new code, think carefully about where it belongs. Don't create new files unless you have a clear plan to properly integrate them into the codebase.

---

## Two Editing Approaches

You have two main approaches. Choose based on the scope of the change:

### 1. Symbolic Editing — for replacing or adding whole symbols

Use this when you need to replace an entire symbol (a method, class, function, etc.), or insert new code at a specific location relative to a symbol.

**Key tools:**
- `find_symbol` — locate a symbol by name path before editing it
- `replace_symbol_body` — replace the full definition of a symbol
- `insert_after_symbol` — add new code after the last symbol in a file (or any symbol)
- `insert_before_symbol` — add new code before the first symbol in a file (or any symbol)
- `find_referencing_symbols` — understand what other code depends on a symbol before changing it

**When to use symbolic editing:**
- Replacing an entire method or function
- Adding a new method to a class
- Appending or prepending new top-level code to a file

**When NOT to use it:**
- When you only need to change a few lines *inside* a larger symbol — use file-based editing instead

**Backward compatibility:** Unless the user says otherwise, when you edit a symbol, either keep the change backward-compatible or use `find_referencing_symbols` to find and update all call sites. The tool gives you code snippets around each reference and symbolic metadata to help you do this efficiently.

You can trust the symbolic editing tools — if they return without error, the edit succeeded. No need to verify.

---

### 2. File-Based Editing — for targeted in-place changes

Use this when you need to change just a few lines *within* a symbol, rather than replacing the whole thing. This is your primary tool for small, surgical edits.

**Key tool:** `replace_content`

This tool supports both literal string replacement and **regex-based replacement**. Prefer regex mode — it lets you replace large sections without quoting them verbatim, using wildcards to match the parts that vary.

**Regex tips:**
- Use `.*?` (non-greedy) in the middle of patterns to avoid over-matching
- Anchor with distinctive surrounding text to avoid ambiguous matches
- Never use `.*` at the start or end of a pattern — it's rarely needed and often harmful
- If the regex matches multiple spots and `allow_multiple_occurrences` is false, an error is returned — refine and retry

You're highly skilled at regex. Trust yourself to write effective patterns without needing to verify the result afterward.

---

## Exploration and Navigation

Before editing unfamiliar code, orient yourself:

- `get_symbols_overview` — get a high-level map of a file's symbols; use this first when exploring a new file
- `read_file` — read a file or range of lines; use when you need to see exact content
- `find_symbol` — locate a specific symbol by name path; faster and more precise than reading whole files
- `search_for_pattern` — search across the codebase for arbitrary patterns; useful for finding usages, config values, or non-code content
- `find_file` — locate files by name or wildcard mask
- `list_dir` — browse the directory structure

Prefer symbolic tools over reading whole files when you know what you're looking for.

---

## Name Path Patterns

When using `find_symbol`, `replace_symbol_body`, `insert_after_symbol`, etc., you identify symbols using *name paths*:

- Simple name: `"my_function"` — matches any symbol with that name
- Relative path: `"MyClass/my_method"` — matches any symbol with that name path suffix
- Absolute path: `"/MyClass/my_method"` — requires an exact full match within the file

For overloaded methods (e.g. in Java), append a 0-based index: `"MyClass/my_method[1]"`

---

## Acceptance Criteria

A correct application of this skill satisfies all of the following:

### Tool Selection
- [ ] Uses symbolic editing tools (`replace_symbol_body`, `insert_after_symbol`, etc.) when the target is a whole symbol — never for partial in-symbol changes
- [ ] Uses `replace_content` for surgical edits within a symbol — never replaces an entire symbol just to change two lines
- [ ] Does not reach for `read_file` on a whole file when `find_symbol` or `get_symbols_overview` would suffice

### Exploration Before Editing
- [ ] Calls `get_symbols_overview` or `find_symbol` before editing any unfamiliar file
- [ ] Does not begin making edits until an explicit edit has been requested by the user

### Regex Usage (when using `replace_content` in regex mode)
- [ ] Uses `.*?` (non-greedy) for middle-of-pattern wildcards, not `.*`
- [ ] Does not begin or end a regex pattern with `.*`
- [ ] Anchors patterns with distinctive surrounding text to avoid ambiguous matches
- [ ] If a regex matches multiple locations unexpectedly, refines and retries rather than forcing the replacement

### Backward Compatibility
- [ ] Either keeps changes backward-compatible, OR calls `find_referencing_symbols` and updates all affected call sites before completing the task

### Trust and Verification
- [ ] Does not re-read or re-verify a file after a successful symbolic tool call — trusts the tool's success response
- [ ] Does not create new files unless there is a clear integration plan for them

### Name Path Correctness
- [ ] Uses absolute paths (`/ClassName/method`) when precision is required
- [ ] Appends a 0-based index (e.g. `[1]`) when targeting overloaded methods