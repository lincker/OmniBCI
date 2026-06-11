import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy: the console talks to the backend at /api/* (same-origin in
// production); during development Vite forwards REST and WebSocket traffic
// to the local uvicorn instance. Override with VITE_BACKEND if it runs
// elsewhere, e.g. VITE_BACKEND=http://192.168.1.10:8000 npm run dev
const backend = process.env.VITE_BACKEND ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: backend,
        changeOrigin: true,
        ws: true, // /api/v1/ws/results
      },
      "/healthz": { target: backend, changeOrigin: true },
    },
  },
});
