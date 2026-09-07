import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Порт backend берётся из BACKEND_PORT (или VITE_BACKEND_PORT), по умолчанию 8001.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const port = env.BACKEND_PORT || env.VITE_BACKEND_PORT || '8001'
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': { target: `http://127.0.0.1:${port}`, changeOrigin: true },
      },
    },
  }
})
