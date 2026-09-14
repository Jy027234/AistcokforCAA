import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发与预览都把 /api 代理到本地 API，避免前端硬编码地址与 CORS 配置。
const API_TARGET = process.env.AQUANT_API_TARGET ?? "http://127.0.0.1:8000";
const proxy = {
  "/api": { target: API_TARGET, changeOrigin: true },
};

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
});
