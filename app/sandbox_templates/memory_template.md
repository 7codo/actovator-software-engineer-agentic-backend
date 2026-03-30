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

## Structure

| Key | Path | Role |
|---|---|---|
| `app` | `src/app` | App router pages and layouts |
| `components` | `src/components` | Shared UI components |
| `lib` | `src/lib` | Utilities and helpers |

## Architecture

### Server Components (Rendering Strategy)
- **Chosen:** Use Server Components by default
- **Reason:** Reduce client bundle size
- **Rejected:** All client components

## Conventions

### Naming
- Components (React): `PascalCase`
- Files (filesystem): `kebab-case`
- Custom hook prefix: `use`

---

## Preferences

**Prefer**
- Arrow functions over function declarations

**Avoid**
- Default exports for page components

