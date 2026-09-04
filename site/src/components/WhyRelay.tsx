"use client";

import { motion } from "framer-motion";
import { AlertTriangle, Zap } from "lucide-react";

interface AgentNode {
  id: string;
  label: string;
  x: number;
  y: number;
}

const withoutRelay: AgentNode[] = [
  { id: "codex1", label: "Codex", x: 15, y: 20 },
  { id: "claude1", label: "Claude", x: 50, y: 20 },
  { id: "gemini1", label: "Gemini", x: 85, y: 20 },
  { id: "local1", label: "Local", x: 25, y: 60 },
  { id: "cli1", label: "CLI", x: 50, y: 80 },
  { id: "ide1", label: "IDE", x: 75, y: 60 },
];

const withRelay: AgentNode[] = [
  { id: "codex2", label: "Codex", x: 15, y: 25 },
  { id: "relay2", label: "Relay", x: 50, y: 50 },
  { id: "claude2", label: "Claude", x: 85, y: 25 },
  { id: "gemini2", label: "Gemini", x: 15, y: 75 },
  { id: "local2", label: "Local", x: 85, y: 75 },
];

function AgentGraph({
  nodes,
  showConnections,
  title,
  subtitle,
}: {
  nodes: AgentNode[];
  showConnections: boolean;
  title: string;
  subtitle: string;
}) {
  const relayNode = nodes.find((n) => n.id.includes("relay"));

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true }}
      transition={{ duration: 0.6 }}
      className="relative"
    >
      <div className="bg-[#0a0a0c] rounded-xl border border-white/5 p-6">
        <div className="flex items-center gap-2 mb-4">
          {showConnections ? (
            <div className="p-1.5 rounded-md bg-sky-500/10">
              <Zap size={14} className="text-sky-400" />
            </div>
          ) : (
            <div className="p-1.5 rounded-md bg-amber-500/10">
              <AlertTriangle size={14} className="text-amber-400" />
            </div>
          )}
          <h4 className="text-sm font-semibold text-white">{title}</h4>
        </div>
        <p className="text-xs text-zinc-500 mb-6">{subtitle}</p>

        <div className="relative aspect-square max-w-[280px] mx-auto">
          <svg viewBox="0 0 100 100" className="w-full h-full">
            <defs>
              <linearGradient id="relayGradientViz" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stopColor="#0ea5e9" />
                <stop offset="100%" stopColor="#6366f1" />
              </linearGradient>
              <filter id="nodeGlowViz">
                <feGaussianBlur stdDeviation="1.5" result="coloredBlur" />
                <feMerge>
                  <feMergeNode in="coloredBlur" />
                  <feMergeNode in="SourceGraphic" />
                </feMerge>
              </filter>
            </defs>

            {/* Connection lines */}
            {showConnections &&
              relayNode &&
              nodes
                .filter((n) => !n.id.includes("relay"))
                .map((node, i) => (
                  <motion.line
                    key={i}
                    x1={relayNode.x}
                    y1={relayNode.y}
                    x2={node.x}
                    y2={node.y}
                    stroke="rgba(56, 189, 248, 0.15)"
                    strokeWidth="0.4"
                    strokeDasharray="2 1"
                    initial={{ opacity: 0, pathLength: 0 }}
                    whileInView={{ opacity: 1, pathLength: 1 }}
                    viewport={{ once: true }}
                    transition={{ duration: 0.8, delay: i * 0.1 }}
                  />
                ))}

            {/* Nodes */}
            {nodes.map((node) => {
              const isRelay = node.id.includes("relay");
              const size = isRelay ? 5.5 : 4;

              return (
                <motion.g
                  key={node.id}
                  initial={{ opacity: 0, scale: 0 }}
                  whileInView={{ opacity: 1, scale: 1 }}
                  viewport={{ once: true }}
                  transition={{ duration: 0.4, delay: isRelay ? 0.3 : 0.1 }}
                >
                  {isRelay && (
                    <circle
                      cx={node.x}
                      cy={node.y}
                      r={size + 3}
                      fill="none"
                      stroke="rgba(56, 189, 248, 0.15)"
                      strokeWidth="0.2"
                    >
                      <animate
                        attributeName="r"
                        values={`${size + 2};${size + 4};${size + 2}`}
                        dur="3s"
                        repeatCount="indefinite"
                      />
                      <animate
                        attributeName="opacity"
                        values="0.3;0.1;0.3"
                        dur="3s"
                        repeatCount="indefinite"
                      />
                    </circle>
                  )}

                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={size}
                    fill={isRelay ? "url(#relayGradientViz)" : "#18181b"}
                    stroke={
                      isRelay ? "#38bdf8" : "rgba(255, 255, 255, 0.08)"
                    }
                    strokeWidth="0.4"
                    filter={isRelay ? "url(#nodeGlowViz)" : undefined}
                  />

                  <text
                    x={node.x}
                    y={node.y + size + 4.5}
                    textAnchor="middle"
                    fill={isRelay ? "#38bdf8" : "#a1a1aa"}
                    fontSize="2.8"
                    fontWeight={isRelay ? "600" : "400"}
                  >
                    {node.label}
                  </text>
                </motion.g>
              );
            })}
          </svg>
        </div>
      </div>
    </motion.div>
  );
}

export function WhyRelay() {
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
            The problem with isolated agents
          </h2>
          <p className="text-zinc-400 max-w-2xl mx-auto leading-relaxed">
            Developers use multiple AI tools — coding agents, terminal agents,
            IDE agents, frontier models, local models. But every tool behaves
            like an isolated island.
          </p>
        </motion.div>

        <div className="grid md:grid-cols-2 gap-6 md:gap-8">
          <AgentGraph
            nodes={withoutRelay}
            showConnections={false}
            title="Without Relay"
            subtitle="Disconnected agents, duplicated context, fragmented history"
          />
          <AgentGraph
            nodes={withRelay}
            showConnections={true}
            title="With Relay"
            subtitle="One orchestration layer connecting your workflow"
          />
        </div>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6, delay: 0.3 }}
          className="mt-12 text-center"
        >
          <div className="inline-flex items-center gap-3 px-5 py-3 rounded-lg bg-white/[0.02] border border-white/5">
            <div className="flex -space-x-1">
              <div className="w-2 h-2 rounded-full bg-sky-400" />
              <div className="w-2 h-2 rounded-full bg-indigo-400" />
              <div className="w-2 h-2 rounded-full bg-emerald-400" />
            </div>
            <p className="text-sm text-zinc-400">
              Relay connects the agents you already use, carries context between
              them, and preserves the full execution history.
            </p>
          </div>
        </motion.div>
      </div>
    </section>
  );
}
