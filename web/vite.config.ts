import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 開発時は Vite の dev サーバーから FastAPI (8000) にプロキシする
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
