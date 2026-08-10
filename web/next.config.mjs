const apiUrl = (process.env.EVOAGENT_API_URL || "http://127.0.0.1:8080").replace(/\/$/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  async rewrites() {
    return ["api", "v1", "github", "webhooks"].map((prefix) => ({
      source: `/${prefix}/:path*`,
      destination: `${apiUrl}/${prefix}/:path*`,
    })).concat([
      { source: "/health", destination: `${apiUrl}/health` },
      { source: "/metrics", destination: `${apiUrl}/metrics` },
    ]);
  },
};

export default nextConfig;
