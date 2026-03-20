---
name: code_editing_tools
description: Precise code editing and navigation skill using symbolic and file-based tools. Use this skill whenever working on a coding project — editing functions, classes, or methods; navigating a codebase; adding new code; refactoring; or understanding relationships between symbols. Trigger this skill for any task involving read_file, find_symbol, replace_symbol_body, replace_content, insert_after_symbol, find_referencing_symbols, or similar code/LSP/file tools.
---

Use symbolic editing tools whenever possible for precise code modifications.

You have two main approaches for editing code: (a) editing at the symbol level and (b) file-based editing.

The symbol-based approach is appropriate if you need to adjust an entire symbol, e.g. a method, a class, a function, etc.

It is not appropriate if you need to adjust just a few lines of code within a larger symbol.

## Two Editing Approaches

### 1. Symbolic Editing — Replace or Insert Whole Symbols

Use when modifying an **entire** symbol (function, method, class). Not for tweaking a few lines inside one.

**Tools:**
| Tool | Use for |
|---|---|
| `find_symbol` | Locate a symbol before editing |
| `replace_symbol_body` | Replace a full symbol definition |
| `insert_after_symbol` | Add code after a symbol (use the last top-level symbol to append to a file) |
| `insert_before_symbol` | Add code before a symbol (use the first to prepend to a file) |
| `find_referencing_symbols` | Find callers/dependents before making breaking changes |

**Backward compatibility:** Either keep changes backward-compatible, or use `find_referencing_symbols` to locate and update all affected call sites. Results include code snippets and symbolic metadata.

> Symbolic tools are reliable — if they return without error, the edit succeeded. No need to verify.

---

### 2. File-Based Editing — Surgical In-Place Changes

Use when changing **a few lines within** a symbol rather than replacing the whole thing.

**Tool:** `replace_content` — supports literal string and **regex** replacements.

**Regex tips:**
- Prefer regex mode to avoid quoting large blocks verbatim
- Use `.*?` (non-greedy) to match variable middle sections
- Anchor patterns with distinctive surrounding text to avoid ambiguous matches
- Avoid leading/trailing `.*` — it's rarely needed and often harmful
- If a pattern matches multiple spots and `allow_multiple_occurrences` is false, an error is returned — refine and retry

---

## Exploration Tools

Orient yourself before editing unfamiliar code:

| Tool | Use for |
|---|---|
| `get_symbols_overview` | High-level map of a file's symbols — start here |
| `read_file` | Read a file or line range for exact content |
| `find_symbol` | Locate a specific symbol by name path |
| `search_for_pattern` | Search the codebase for patterns, usages, or config values |
| `find_file` | Locate files by name or wildcard |
| `list_dir` | Browse directory structure |

Prefer symbolic tools over reading whole files when you know what you're looking for.

---

## Name Path Syntax

Used in `find_symbol`, `replace_symbol_body`, `insert_after_symbol`, etc.:

| Pattern | Meaning |
|---|---|
| `"my_function"` | Any symbol with that name |
| `"MyClass/my_method"` | Any symbol matching that suffix path |
| `"/MyClass/my_method"` | Exact full path match |
| `"MyClass/my_method[1]"` | Second overload (0-based index) |