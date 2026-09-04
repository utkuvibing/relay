"use client";

import { motion } from "framer-motion";
import { useState } from "react";

interface TerminalExample {
  id: string;
  title: string;
  description: string;
  commands: string[];
}

const examples: TerminalExample[] = [
  {
    id: "init",
    title: "Initialize",
    description: "Set up Relay in your project.",
    commands: [
      "$ relay init",
      "",
      "Relay initialized in .relay/",
      "  Configuration: relay.yaml",
      "  Database: .relay/relay.sqlite3",
      "",
      "Ready. Run 'relay status' to verify.",
    ],
  },
  {
    id: "ask",
    title: "Ask an agent",
    description: "Send a task to any configured agent.",
    commands: [
      "$ relay ask gpt 'Review this implementation'",
      "",
      "→ Agent: gpt (openai)",
      "→ Model: gpt-4-turbo",
      "→ Context: 3 files, 847 lines",
      "",
      "Review complete.",
      "  2 suggestions recorded",
      "  Artifacts saved to .relay/runs/run_20240115_143022/",
    ],
  },
  {
    id: "status",
    title: "Check status",
    description: "See what's running and what's happened.",
    commands: [
      "$ relay status",
      "",
      "Relay runtime: active",
      "",
      "Agents configured: 4",
      "  codex (harness) · claude (harness)",
      "  gpt (api) · deepseek (api)",
      "",
      "Recent runs:",
      "  ✓ auth-refactor     3m ago   codex → claude",
      "  ✓ api-tests         12m ago  gpt",
      "  ⚡ deploy-review     running  claude",
    ],
  },
  {
    id: "history",
    title: "Inspect history",
    description: "Full audit trail of every execution.",
    commands: [
      "$ relay history auth-refactor",
      "",
      "Task: auth-refactor",
      "Status: ✓ Complete",
      "Duration: 4m 23s",
      "",
      "Execution:",
      "  1. codex → implementation",
      "     Artifacts: 4  Approvals: 0",
      "  2. claude → review",
      "     Artifacts: 3  Approvals: 1",
      "",
      "Run 'relay inspect run_<id>' for details.",
    ],
  },
];

export function DeveloperFirst() {
  const [activeExample, setActiveExample] = useState(examples[0]);

  return (
    <section className="relative py-24 md:py-32 px-6">
      <div className="max-w-6xl mx-auto">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
          className="text-center mb-16"
        >
          <h2 className="text-3xl md:text-4xl font-bold mb-4">
            Built for your terminal
          </h2>
          <p className="text-zinc-400 max-w-xl mx-auto">
            Relay fits into your existing workflow. No new dashboards to learn.
          </p>
        </motion.div>

        <div className="grid lg:grid-cols-[280px,1fr] gap-8">
          {/* Example selector */}
          <div className="space-y-2">
            {examples.map((example) => (
              <button
                key={example.id}
                onClick={() => setActiveExample(example)}
                className={`w-full text-left p-4 rounded-lg transition-all ${
                  activeExample.id === example.id
                    ? "bg-white/5 border border-white/10"
                    : "hover:bg-white/[0.02] border border-transparent"
                }`}
              >
                <div
                  className={`font-medium mb-1 ${
                    activeExample.id === example.id
                      ? "text-white"
                      : "text-zinc-400"
                  }`}
                >
                  {example.title}
                </div>
                <div
                  className={`text-sm ${
                    activeExample.id === example.id
                      ? "text-zinc-400"
                      : "text-zinc-600"
                  }`}
                >
                  {example.description}
                </div>
              </button>
            ))}
          </div>

          {/* Terminal */}
          <motion.div
            key={activeExample.id}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.3 }}
            className="relative bg-[#0a0a0c] rounded-xl border border-white/10 overflow-hidden"
          >
            <div className="flex items-center gap-2 px-4 py-3 border-b border-white/5 bg-white/[0.02]">
              <div className="w-3 h-3 rounded-full bg-red-500/20" />
              <div className="w-3 h-3 rounded-full bg-yellow-500/20" />
              <div className="w-3 h-3 rounded-full bg-green-500/20" />
              <span className="ml-2 text-xs text-zinc-500">terminal</span>
            </div>
            <div className="p-6 font-mono text-sm overflow-x-auto">
              {activeExample.commands.map((line, i) => (
                <div
                  key={i}
                  className={`${
                    line.startsWith("$")
                      ? "text-sky-400"
                      : line.startsWith("✓") || line.startsWith("→")
                      ? "text-emerald-400"
                      : line.startsWith("⚡")
                      ? "text-amber-400"
                      : line.startsWith("  ")
                      ? "text-zinc-400"
                      : "text-zinc-300"
                  }`}
                >
                  {line || "\u00A0"}
                </div>
              ))}
            </div>
          </motion.div>
        </div>
      </div>
    </section>
  );
}
