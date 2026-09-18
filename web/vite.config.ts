import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: {
    // NOT web/dist: .gitignore has an unanchored `dist/`, so a conventional
    // output directory would be silently uncommitted and every fresh clone
    // would get an assetless dashboard. This path is committed on purpose.
    outDir: path.resolve(__dirname, "../runner/web/static"),
    emptyOutDir: true,
  },
  server: { proxy: { "/api": "http://127.0.0.1:8787" } },
});
