import type { NextConfig } from "next";

// The API URL is set at build time via env. For local dev this proxies to the
// uvicorn backend on 3001. In production (Azure App Service) both processes run
// in the same container so localhost:3001 still works.
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:3001";

const nextConfig: NextConfig = {
  // Rewrite /api/* → FastAPI backend so the browser never makes cross-origin
  // requests. Eliminates CORS issues during local dev and in production.
  async rewrites() {
    return [
      {
        source:      "/api/:path*",
        destination: `${API_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
