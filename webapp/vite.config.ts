import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite dev server proxies /api/* to the FastAPI backend so both can be
// bound to different ports without CORS gymnastics.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // listen on all interfaces (Tailscale-friendly)
    port: 5173,
    proxy: {
      // FastAPI backend. Override at launch with:
      //   VITE_API_URL=http://host:port pnpm dev
      "/api": {
        target: process.env.VITE_API_URL || "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
});
