import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // make e2e는 켜 둔 make dev와 따로 빌드 폴더를 쓴다 — Next는 빌드 폴더마다 개발 서버 하나만 띄운다(<distDir>/dev/lock)
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  // API는 같은 출처로 프록시한다 — CORS를 쓰지 않는다(앱 뼈대 설계안 'API 연결')
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        // AX_API_URL — Playwright가 복사본 데이터로 띄운 API(make e2e)
        destination: `${process.env.AX_API_URL ?? "http://localhost:8000"}/:path*`,
      },
    ];
  },
  experimental: {
    // 기본 30초. 임베딩 1번 + 버킷별 리랭크 2번이 각각 30초씩 3번까지 시도한다
    // (backend/embeddings.py API_TIMEOUT·API_RETRIES) — 4.5분을 기다려야 백엔드의 503 문구가 화면에 온다
    proxyTimeout: 300_000,
  },
};

export default nextConfig;
