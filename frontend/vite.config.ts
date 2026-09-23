import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * O proxy é o detalhe que mais economiza dor de cabeça aqui.
 *
 * Sem ele, o browser em :5173 falando com a API em :8000 são origens
 * diferentes — cai em CORS, preflight, e a primeira coisa que quebra é o
 * streaming. Com o proxy, tudo que o browser vê é `/api/...` na PRÓPRIA
 * origem; o Vite é quem repassa para o uvicorn.
 *
 * É também o formato que se usa em produção, onde um nginx serve os estáticos
 * e repassa `/api` para o backend. O `CORS_ORIGINS` da API continua lá como
 * rede de segurança (e para quem preferir apontar o VITE_API_URL direto).
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
        // SSE não pode ser bufferizado: sem isto o proxy poderia segurar os
        // frames e entregar tudo junto no fim — o streaming "sumiria".
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            if (proxyRes.headers["content-type"]?.includes("text/event-stream")) {
              proxyRes.headers["cache-control"] = "no-cache, no-transform";
            }
          });
        },
      },
    },
  },
});
