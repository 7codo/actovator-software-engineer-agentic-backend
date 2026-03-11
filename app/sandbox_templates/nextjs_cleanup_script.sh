#!/usr/bin/env bash
# =============================================================================
# init-next.sh — Strip Next.js default boilerplate
# Assumes: TypeScript · Tailwind CSS · src/app layout
# =============================================================================

set -euo pipefail

PROJECT_DIR="${1:-$(pwd)}"
cd "$PROJECT_DIR"

APP_DIR="src/app"

# ── 1. Reset page.tsx ─────────────────────────────────────────────────────────
cat > "$APP_DIR/page.tsx" <<'EOF'
export default function Home() {
  return (
    <main>
      <h1>Hello, world</h1>
    </main>
  );
}
EOF
echo "✓ Reset $APP_DIR/page.tsx"

# ── 2. Remove public/ assets ──────────────────────────────────────────────────
DEFAULT_PUBLIC_FILES=(
  "public/next.svg"
  "public/vercel.svg"
  "public/file.svg"
  "public/globe.svg"
  "public/window.svg"
  "src/app/favicon.ico"
)

for f in "${DEFAULT_PUBLIC_FILES[@]}"; do
  if [[ -f "$f" ]]; then
    rm "$f"
    echo "✓ Removed $f"
  fi
done

# ── 3. Clean README.md ────────────────────────────────────────────────────────
PROJECT_NAME=$(basename "$(pwd)")
cat > README.md <<EOF
# $PROJECT_NAME

## Getting started

\`\`\`bash
npm install
npm run dev
\`\`\`

Open [http://localhost:3000](http://localhost:3000) in your browser.

## Tech stack

- [Next.js](https://nextjs.org/)
- [Tailwind CSS](https://tailwindcss.com/)
- [TypeScript](https://www.typescriptlang.org/)
EOF
echo "✓ Cleaned README.md"