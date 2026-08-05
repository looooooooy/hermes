import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { createHermesCloudProxy } from "./config/cloudProxy";

export default defineConfig(({ command, mode, isPreview }) => {
  const cloudProxy = command === "serve" && mode === "cloud" && isPreview === true
    ? createHermesCloudProxy(loadEnv(mode, ".", "HERMES_WEB_CLOUD_URL"))
    : undefined;
  return {
    build: {
      outDir: "dist/client",
    },
    optimizeDeps: {
      include: ["react", "react-dom/client", "@phosphor-icons/react"],
    },
    server: {
      host: "0.0.0.0",
      allowedHosts: ["terminal.local"],
      warmup: {
        clientFiles: ["./src/main.tsx"],
      },
    },
    preview: {
      host: "localhost",
      ...(cloudProxy === undefined ? {} : { proxy: cloudProxy }),
    },
    plugins: [react()],
  };
});
