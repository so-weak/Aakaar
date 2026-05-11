import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const port = Number(env.PORT) || 3000
  // Proxy /api/* to the local FastAPI service so the page can call
  // /api/recon/uploads without hard-coding the backend port.
  const apiTarget = env.ADMIN_API_URL || 'http://127.0.0.1:8001'

  return {
    plugins: [react()],
    server: {
      port,
      open: true,
      proxy: {
        '/api': { target: apiTarget, changeOrigin: true }
      }
    },
    preview: { port }
  }
})
