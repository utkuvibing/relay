import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Relay — One workflow. Any agent.",
  description:
    "Orchestrate AI coding agents and models without losing context, control, or execution history. Relay is the coordination layer between your agents.",
  keywords: [
    "AI agents",
    "orchestration",
    "developer tools",
    "coding agents",
    "Claude",
    "Codex",
    "Gemini",
  ],
  openGraph: {
    title: "Relay — One workflow. Any agent.",
    description:
      "Orchestrate AI coding agents and models without losing context, control, or execution history.",
    type: "website",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
