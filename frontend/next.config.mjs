import { withSentryConfig } from "@sentry/nextjs/config";

/** @type {import("next").NextConfig} */
const localBackendOverride = process.env.LOCAL_BACKEND_OVERRIDE === "1";
const localBackendApiUrl =
  process.env.BACKEND_API_URL ?? "http://127.0.0.1:8010";

if (
  localBackendOverride &&
  !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(localBackendApiUrl)
) {
  throw new Error(
    "LOCAL_BACKEND_OVERRIDE only accepts an http://localhost or http://127.0.0.1 URL",
  );
}

const backendApiUrl = localBackendOverride
  ? localBackendApiUrl
  : process.env.NODE_ENV === "production"
    ? "https://api.blinkai.tw"
    : localBackendApiUrl;

if (
  process.env.NODE_ENV === "production" &&
  process.env.NEXT_PUBLIC_TESTING_UNLOCK_ALL === "1"
) {
  throw new Error(
    "NEXT_PUBLIC_TESTING_UNLOCK_ALL must be disabled in production",
  );
}

if (
  process.env.NODE_ENV === "production" &&
  process.env.NEXT_PUBLIC_USE_MOCK === "true"
) {
  throw new Error("NEXT_PUBLIC_USE_MOCK must be disabled in production");
}


const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Keep development and production build artifacts isolated.
  distDir:
    process.env.NEXT_DIST_DIR ??
    (process.env.NODE_ENV === "development" ? ".next-dev" : ".next"),
  async headers() {
    const immutableAssets = [
      "/face-map-v20261002/:path*",
      "/skin-region-icons-v20261002/:path*",
    ];

    return immutableAssets.map((source) => ({
      source,
      headers: [
        {
          key: "Cache-Control",
          value: "public, max-age=31536000, immutable",
        },
      ],
    }));
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendApiUrl}/api/:path*`,
      },
    ];
  },
};

export default withSentryConfig(nextConfig, {
  silent: true,
  webpack: { treeshake: { removeDebugLogging: true } },
});
