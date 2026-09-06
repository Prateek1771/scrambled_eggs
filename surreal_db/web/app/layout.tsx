import type { Metadata } from "next";
import { DotGothic16, JetBrains_Mono } from "next/font/google";

import "./globals.css";

// Display and body faces from docs/design.md. Loaded through next/font so they
// are self-hosted and do not block first paint on a third-party request.
const display = DotGothic16({
  weight: "400",
  subsets: ["latin"],
  variable: "--font-display-loaded",
  display: "swap",
});

const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono-loaded",
  display: "swap",
});

export const metadata: Metadata = {
  title: "CORTEX",
  description: "An AI agent whose memory is a database. Watch it form.",
};

/** Root layout. Dark by default -- a memory graph needs somewhere dark to glow. */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${mono.variable}`}>
      <body className="h-dvh overflow-hidden">{children}</body>
    </html>
  );
}
