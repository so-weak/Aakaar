import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const port = Number(env.VITE_PORT) || 3001

  return {
    plugins: [react()],
    server: {
      port,
      open: true
    },
    preview: {
      port
    }
  }
})
