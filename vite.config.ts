import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// En desarrollo, /api se reenvia a Django: el navegador ve un solo origen y no hace falta CORS.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": { target: env.DJANGO_URL || "http://127.0.0.1:8000", changeOrigin: true },
      },
    },
  };
});
