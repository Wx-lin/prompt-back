import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  base: process.env.ADMIN_BASE_PATH || '/',
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/local-media': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/mediacrawler-media': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
