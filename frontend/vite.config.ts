import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: './',
  build: { outDir: '../sleight/deploy/api/static', emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8700', '/viewer': { target: 'http://127.0.0.1:8700', ws: true } } },
})
