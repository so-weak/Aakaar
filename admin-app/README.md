# BOSS — Bank's Operational Support System (Admin)

React + Vite mock of the HDFC BOSS admin portal. Three pages:

1. **/login** — Welcome screen with HDFC logo, hero image, hardcoded auth.
2. **/dashboard** — KPI cards (Pending / Pre-Approved / Approved / Rejected).
3. **/recon/upload** — Recon Upload Files form (View / Upload tabs).

## Hardcoded credentials

| Field    | Value      |
| -------- | ---------- |
| User ID  | `K22408m`  |
| Password | `hdfc@123` |

## Run

```bash
cd admin-app
npm install
npm run dev
```

Then open http://localhost:3000.

## Configuration

Port is read from `.env`:

```
PORT=3000
```

Override per-machine without committing by creating `.env.local` (gitignored), or
inline: `PORT=4000 npm run dev`.
