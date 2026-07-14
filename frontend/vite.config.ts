import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import path from "path"

// dev: Vite 5173 代理 /api /ws 到 FastAPI 8000;prod: 构建到 dist/,由 FastAPI 托管
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
})
