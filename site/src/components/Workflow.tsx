"use client";

import { motion, AnimatePresence } from "framer-motion";
import { useState, useEffect } from "react";
import { Check, Shield, History, GitBranch, FileCode } from "lucide-react";

interface WorkflowStep {
  id: number;
  title: string;
  description: string;
  terminal: string[];
  icon: React.ReactNode;
  color: string;
}

const steps: WorkflowStep[] = [
  {
    id: 1,
    title: "Send a task",
    description: "Define what you need. Relay routes it to the right agent.",
    terminal: [
      "$ relay ask codex",
      "  'Refactor the auth module to use",
      "   JWT tokens with refresh rotation'",
      "",
      "→ Task created: auth-refactor",
      "→ Assigned to: codex (harness)",
    ],
    icon: <FileCode size={18} />,
    color: "sky",
  },
  {
    id: 2,
    title: "Agent produces work",
    description: "The agent executes and Relay captures artifacts and decisions.",
    terminal: [
      "⚡ Running codex on auth-refactor",
      "",
      "  → Plan generated",
      "  → Implementation complete",
      "  → Tests passing (12/12)",
      "  → Verification passed",
      "",
      "✓ Agent complete. 3 artifacts recorded.",
    ],
    icon: <Check size={18} />,
    color: "emerald",
  },
  {
    id: 3,
    title: "Approval boundaries",
    description: "Sensitive actions pause for your review before executing.",
    terminal: [
      "⚠ Approval required",
      "",
      "  Agent: claude",
      "  Action: deploy to staging",
      "  Context: auth-refactor task",
      "",
      "  Allow? [y/n] y",
      "",
      "✓ Approved. Execution recorded.",
    ],
    icon: <Shield size={18} />,
    color: "amber",
  },
  {
    id: 4,
    title: "Context carries forward",
    description: "Hand work between agents without rebuilding context from scratch.",
    terminal: [
      "$ relay ask claude",
      "  'Review the auth-refactor changes",
      "   and suggest improvements'",
      "",
      "→ Context loaded:",
      "  • Original task definition",
      "  • Codex implementation artifacts",
      "  • Test results and verification",
      "",
      "→ Claude has full context.",
    ],
    icon: <GitBranch size={18} />,
    color: "violet",
  },
  {
    id: 5,
    title: "Inspect execution",
    description: "Full history of what happened, when, and why.",
    terminal: [
      "$ relay history auth-refactor",
      "",
      "  Task: auth-refactor",
      "  Status: ✓ Complete",
      "",
      "  Runs:",
      "  1. codex → implementation",
      "  2. claude → review",
      "",
      "  Artifacts: 7",
      "  Approvals: 1",
      "  Duration: 4m 23s",
    ],
    icon: <History size={18} />,
    color: "sky",
  },
];

export function Workflow() {
  const [currentStep, setCurrentStep] = useState(0);
  const [typedLines, setTypedLines] = useState<string[]>([]);

  useEffect(() => {
    const step = steps[currentStep];
    let lineIndex = 0;
    setTypedLines([]);

    const interval = setInterval(() => {
      if (lineIndex < step.terminal.length) {
        setTypedLines((prev) => [...prev, step.terminal[lineIndex]]);
        lineIndex++;
      } else {
        clearInterval(interval);
      }
    }, 100);

    return () => clearInterval(interval);
  }, [currentStep]);

  useEffect(() => {
    const interval = setInterval(() => {
      setCurrentStep((prev) => (prev + 1) % steps.length);
    }, 8000);

    return () => clearInterval(interval);
  }, []);

  const step = steps[currentStep];

  return (
    <section id="workflow" className="relative py-24 md:py-32 px-6">
      <div className="max-w-6xl mx-auto">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
          className="text-center mb-16"
        >
          <h2 className="text-3xl md:text-4xl font-bold mb-4">
            How Relay works
          </h2>
          <p className="text-zinc-400 max-w-xl mx-auto">
            From task to execution, Relay keeps everything connected and
            inspectable.
          </p>
        </motion.div>

        <div className="grid lg:grid-cols-[1fr,1.5fr] gap-8 items-start">
          {/* Steps list */}
          <div className="space-y-3">
            {steps.map((s, i) => (
              <motion.button
                key={s.id}
                onClick={() => setCurrentStep(i)}
                className={`w-full text-left p-4 rounded-xl border transition-all ${
                  i === currentStep
                    ? "bg-white/5 border-white/10"
                    : "bg-transparent border-transparent hover:bg-white/[0.02] hover:border-white/5"
                }`}
                whileHover={{ scale: 1.01 }}
                whileTap={{ scale: 0.99 }}
              >
                <div className="flex items-start gap-3">
                  <div
                    className={`mt-0.5 p-2 rounded-lg ${
                      i === currentStep
                        ? `bg-${s.color}-500/10 text-${s.color}-400`
                        : "bg-white/5 text-zinc-500"
                    }`}
                  >
                    {s.icon}
                  </div>
                  <div className="flex-1">
                    <h3
                      className={`font-medium mb-1 ${
                        i === currentStep ? "text-white" : "text-zinc-400"
                      }`}
                    >
                      {s.title}
                    </h3>
                    <p
                      className={`text-sm ${
                        i === currentStep ? "text-zinc-400" : "text-zinc-600"
                      }`}
                    >
                      {s.description}
                    </p>
                  </div>
                </div>
              </motion.button>
            ))}
          </div>

          {/* Terminal */}
          <motion.div
            key={currentStep}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4 }}
            className="relative bg-[#0a0a0c] rounded-xl border border-white/10 overflow-hidden"
          >
            <div className="flex items-center gap-2 px-4 py-3 border-b border-white/5 bg-white/[0.02]">
              <div className="w-3 h-3 rounded-full bg-red-500/20" />
              <div className="w-3 h-3 rounded-full bg-yellow-500/20" />
              <div className="w-3 h-3 rounded-full bg-green-500/20" />
              <span className="ml-2 text-xs text-zinc-500">terminal</span>
            </div>
            <div className="p-6 font-mono text-sm min-h-[320px]">
              <AnimatePresence mode="wait">
                {typedLines.map((line, i) => (
                  <motion.div
                    key={i}
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ duration: 0.2 }}
                    className={`${
                      line.startsWith("$")
                        ? "text-sky-400"
                        : line.startsWith("✓") || line.startsWith("→")
                        ? "text-emerald-400"
                        : line.startsWith("⚠") || line.startsWith("⚡")
                        ? "text-amber-400"
                        : line.startsWith("  →") || line.startsWith("  •")
                        ? "text-zinc-400"
                        : "text-zinc-300"
                    }`}
                  >
                    {line || "\u00A0"}
                  </motion.div>
                ))}
              </AnimatePresence>
              {typedLines.length < step.terminal.length && (
                <motion.span
                  animate={{ opacity: [1, 0] }}
                  transition={{ duration: 0.5, repeat: Infinity }}
                  className="inline-block w-2 h-4 bg-white"
                />
              )}
            </div>
          </motion.div>
        </div>
      </div>
    </section>
  );
}
