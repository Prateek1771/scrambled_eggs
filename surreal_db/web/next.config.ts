import type { NextConfig } from "next";

const config: NextConfig = {
  // The browser talks straight to SurrealDB over a WebSocket, so nothing here
  // proxies data. Standalone output keeps the runtime image small.
  output: "standalone",
};

export default config;
