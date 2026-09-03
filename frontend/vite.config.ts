import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// PORT lets a launcher assign the port when 5173 is already taken.
const port = Number(process.env.PORT) || 5173;

export default defineConfig({
  plugins: [react()],
  server: {
    port,
    // The browser never talks to Gate directly. Requests to /api are proxied to
    // the local FastAPI process, which is the only thing holding the API secret.
    // Because the page and the API share the Vite origin in dev, CORS never
    // comes into play.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
