# Project

## Ecosystem

| Concern | Choice |
|---|---|
| UI Framework / Runtime | Next.js |
| Language / Type System | TypeScript |
| Router / Navigation | App Directory |
| Source Entry Point | `src/` |
| Styling | Tailwind CSS |
| Component Primitives | shadcn/ui |
| Icons | lucide-react |

## Structure

| Key | Path | Role |
|---|---|---|
| `app` | `src/app` | App router pages and layouts |
| `components` | `src/components` | Shared UI components |
| `lib` | `src/lib` | Utilities and helpers |

## Architecture
Architectural decisions made for the project, including chosen patterns and rational.

### Server Components (Rendering Strategy)
- **Chosen:** Use Server Components by default
- **Reason:** Reduce client bundle size

## Conventions
Coding standards and naming rules followed across the codebase.

### Naming
- Components (React): `PascalCase`
- Files (filesystem): `kebab-case`
- Custom hook prefix: `use`

---

# Development
Running log of development activity, preferences, and issues encountered.

## User Requests
Tasks requested by the user, one per item.

## Preferences

**Prefer**
- Arrow functions over function declarations

**Avoid**
- Default exports for page components

## Errors
Known errors and fixes for handling them.

---

# Knowledge
Accumulated knowledge relevant to the project.

## Domain
Domain-specific knowledge and concepts.