## User task
create a middleware


//////////////////////////////////////

## 🔎 Investigation Report

### 1. Change Request Summary
Create a middleware for the Next.js project. Since the project uses a `src` directory, the middleware should be located at `src/middleware.ts`. This will be a new file to handle request-level logic (e.g., authentication, logging, or redirects).

### 2. Codebase Map (Relevant Scope)
```text
.
├── src/
│   ├── app/
│   │   ├── layout.tsx
│   │   └── page.tsx
│   ├── components/
│   │   └── ui/
│   │       └── button.tsx
│   └── lib/
│       └── utils.ts
└── next.config.ts
```

### 3. Files to Modify
No existing files require modification for the basic creation of a middleware.

### 4. Files to Create
| Proposed Path | Reason |
|---|---|
| `src/middleware.ts` | FILE NOT FOUND — new file required for Next.js middleware functionality |

### 5. Files to Delete
No files to delete.

### 6. Symbol Analysis
*No existing symbols are flagged for modification.*

### 7. Dependency & Risk Flags
- **No risks identified.** The middleware is a new standalone file. 
- **Assumption:** The middleware is placed in `src/` because the project uses a `src` directory for its application code.

### 8. Recommended Edit Sequence
1. Create `src/middleware.ts` with standard Next.js middleware boilerplate.


//////////////////////////////////////////


## 📋 Edit Plan

### Meta
- **Source Report:** Create a middleware for the Next.js project. Since the project uses a `src` directory, the middleware should be located at `src/middleware.ts`. This will be a new file to handle request-level logic (e.g., authentication, logging, or redirects).
- **Total Steps:** 1
- **Estimated Risk Level:** LOW
  - LOW: no breaking changes, no deletions, no cross-file dependencies

---

### Edit Instructions

---

**Step 1 — Create middleware file**
- **Operation:** CREATE
- **Target Path:** `src/middleware.ts`
- **Symbol (if symbol-level):** N/A
- **Change Description:**
  Create a new Next.js middleware file. The file must export a `middleware` function that accepts a `NextRequest` parameter and returns `NextResponse.next()`. Additionally, export a `config` object containing a `matcher` property. The `matcher` should be configured to apply to all routes except for internal Next.js paths (e.g., `_next/static`, `_next/image`, `favicon.ico`) and common static asset extensions. Ensure `next/server` is used for the Request and Response types.
- **Depends On:** None
- **Risk Flag:** ✅ None

---

### Post-Execution Checklist
[x] All created files are referenced or imported by at least one existing file (Note: `src/middleware.ts` is automatically detected by Next.js)
[x] All deleted files have had their imports removed in prior steps
[x] All breaking change consumers have a corresponding update step
[x] No step modifies a file before a step that creates it
[x] No circular dependencies introduced by the new structure

//////////////////

next (known from: 14.0.0 → currently installed: 16.1.6)
  middleware: **CRITICAL DEPRECATION:** In v16.0.0, the Middleware API (`middleware.ts`) was officially deprecated in favor of a new `Proxy` API (`proxy.ts`). Next.js now logs a `warnOnce` deprecation message if you use it. A codemod (`middleware-to-proxy`) is available for migration. Additionally, Node.js runtime support for middleware was introduced (v15.2.0) and stabilized (v15.5.0), meaning middleware is no longer strictly bound to the Edge runtime.
  NextRequest: **BREAKING CHANGE:** As of v15.0.0, the `geo` and `ip` properties were completely removed from the `NextRequest` object. Any request-level logic (like authentication, logging, or redirects) cannot rely on these built-in fields.
  matcher: **BREAKING CHANGE:** In v16.0.1, regular expression (RegExp) support was removed from the middleware `config` export. When defining your `matcher` array to exclude internal Next.js paths (`_next/static`, `_next/image`) and static assets, you must use string-based Path-to-RegExp patterns instead of raw regex objects. Also, in v16.0.0, the `MiddlewareMatcher` type was renamed to `ProxyMatcher`.
  config: Starting in v15.0.0, Next.js enforces strict validation on the middleware configuration object. Any invalid `config` export will now trigger a hard error during the build process rather than failing silently or gracefully degrading.