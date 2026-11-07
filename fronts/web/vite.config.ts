import react from "@vitejs/plugin-react";
import {defineConfig} from "vite";

// The BFF serves dist/ under one origin (API-01): index.html + hashed assets/.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
    // Source maps are not served (API-01 returns 404 for .map).
    sourcemap: false,
  },
});
