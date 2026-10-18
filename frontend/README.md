# NameLeaf frontend

The Next.js client for NameLeaf. See the [root README](../README.md) for what the project is, how it works, and how to run the tests.

```bash
npm ci          # install the pinned dependencies
npm run dev     # http://localhost:3000; proxies /api to the backend on 127.0.0.1:8000
npm run lint
npm run build
```

Searching needs the backend and its database running; without them the pages render but searches fail.

Brand name, tagline, colour and the mark live in `src/lib/brand.ts` (the colour also as `--brand` in `src/app/globals.css`).
