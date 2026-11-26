import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwind from '@tailwindcss/vite'
import { copyFile, mkdir } from 'node:fs/promises'

const backend = `127.0.0.1:${process.env.VISIONATTEND_BACKEND_PORT ?? '8000'}`

export default defineConfig({
  plugins: [react(), tailwind(), {
    name: 'local-api-docs',
    apply: 'build',
    async closeBundle() {
      await mkdir('dist/docs-assets', { recursive: true })
      await Promise.all(['swagger-ui.css', 'swagger-ui-bundle.js', 'favicon-32x32.png'].map(name =>
        copyFile('node_modules/swagger-ui-dist/' + name, 'dist/docs-assets/' + name)))
    },
  }],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': 'http://' + backend,
      '/docs': 'http://' + backend,
      '/openapi.json': 'http://' + backend,
      '/ws': { target: 'ws://' + backend, ws: true },
    },
  },
})
