# aakar-web

Aakar v1 frontend — chat → DAG planner, workflow editor, run timeline,
admin, superuser. SPA powered by Vite + React + TypeScript.

## Run

```
npm install
npm run dev          # http://localhost:5173, proxies /api -> :8000
```

In dev the Vite server proxies `/api/*` to the backend at `http://localhost:8000`.
For production builds set `VITE_API_BASE` to the backend's absolute URL.

## Stack

- Vite, React 18, TypeScript 5
- Tailwind CSS for styling, Inter for type
- @xyflow/react (ReactFlow v12) for DAG visualization
- @tanstack/react-query for server state
- React Router 6 for routing
- Zod for runtime validation of API responses
