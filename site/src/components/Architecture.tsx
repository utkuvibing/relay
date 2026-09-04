"use client";

import { motion } from "framer-motion";
import { useState } from "react";

export function Architecture() {
  const [hoveredLayer, setHoveredLayer] = useState<string | null>(null);

  const layers = [
    {
      id: "cli",
      label: "Developer / CLI",
      subtitle: "relay ask, relay build, relay status",
      color: "zinc",
    },
    {
      id: "runtime",
      label: "Relay Runtime",
      subtitle: "Orchestration, context, policies, approvals, artifacts, execution history",
      color: "sky",
      isCore: true,
    },
    {
      id: "adapters",
      label: "Agent Adapters",
      subtitle: "OpenAI · Anthropic · Codex · Claude Code · DeepSeek",
      color: "indigo",
    },
  ];

  return (
    <section id="architecture" className="relative py-24 md:py-32 px-6">
      <div className="max-w-5xl mx-auto">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
          className="text-center mb-16"
        >
          <h2 className="text-3xl md:text-4xl font-bold mb-4">Architecture</h2>
          <p className="text-zinc-400 max-w-xl mx-auto">
            Relay sits between you and your agents, managing context, policies,
            and execution history.
          </p>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          whileInView={{ opacity: 1, scale: 1 }}
          viewport={{ once: true }}
          transition={{ duration: 0.8 }}
          className="relative"
        >
          <div className="bg-[#0a0a0c] rounded-2xl border border-white/10 p-8 md:p-12">
            <div className="space-y-6 md:space-y-8">
              {layers.map((layer, i) => (
                <motion.div
                  key={layer.id}
                  initial={{ opacity: 0, y: 20 }}
                  whileInView={{ opacity: 1, y: 0 }}
                  viewport={{ once: true }}
                  transition={{ duration: 0.5, delay: i * 0.15 }}
                  onMouseEnter={() => setHoveredLayer(layer.id)}
                  onMouseLeave={() => setHoveredLayer(null)}
                >
                  {/* Arrow between layers */}
                  {i > 0 && (
                    <div className="flex items-center justify-center mb-6 md:mb-8">
                      <motion.div
                        initial={{ opacity: 0, scaleY: 0 }}
                        whileInView={{ opacity: 1, scaleY: 1 }}
                        viewport={{ once: true }}
                        transition={{ duration: 0.5, delay: i * 0.15 + 0.1 }}
                        className="flex flex-col items-center"
                      >
                        <div className="w-px h-6 bg-gradient-to-b from-white/10 to-white/20" />
                        <svg width="12" height="8" viewBox="0 0 12 8" fill="none">
                          <path
                            d="M6 8L0 0H12L6 8Z"
                            fill="rgba(56, 189, 248, 0.3)"
                          />
                        </svg>
                      </motion.div>
                    </div>
                  )}

                  {/* Layer card */}
                  <div
                    className={`relative rounded-xl border transition-all duration-300 ${
                      layer.isCore
                        ? "bg-gradient-to-br from-sky-500/10 to-indigo-500/10 border-sky-500/20 p-6 md:p-8"
                        : "bg-white/[0.02] border-white/5 p-4 md:p-6"
                    } ${
                      hoveredLayer === layer.id
                        ? "border-white/20 shadow-lg shadow-sky-500/5"
                        : ""
                    }`}
                  >
                    {/* Core glow effect */}
                    {layer.isCore && (
                      <motion.div
                        animate={{
                          boxShadow: [
                            "0 0 20px rgba(56, 189, 248, 0.1)",
                            "0 0 40px rgba(56, 189, 248, 0.2)",
                            "0 0 20px rgba(56, 189, 248, 0.1)",
                          ],
                        }}
                        transition={{ duration: 4, repeat: Infinity }}
                        className="absolute inset-0 rounded-xl pointer-events-none"
                      />
                    )}

                    <div className="relative">
                      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
                        <div>
                          <div
                            className={`text-lg font-semibold ${
                              layer.isCore ? "text-white" : "text-zinc-300"
                            }`}
                          >
                            {layer.label}
                          </div>
                          <div className="text-sm text-zinc-500 mt-1">
                            {layer.subtitle}
                          </div>
                        </div>
                      </div>

                      {/* Core layer details */}
                      {layer.isCore && (
                        <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mt-6">
                          {["Context", "Policies", "Approvals", "Artifacts", "Execution History", "Run State"].map(
                            (item, j) => (
                              <motion.div
                                key={item}
                                initial={{ opacity: 0, scale: 0.9 }}
                                whileInView={{ opacity: 1, scale: 1 }}
                                viewport={{ once: true }}
                                transition={{ duration: 0.3, delay: j * 0.05 + 0.3 }}
                                className="p-3 rounded-lg bg-white/5 border border-white/5 text-center text-xs text-zinc-300 hover:bg-white/[0.07] hover:border-white/10 transition-all"
                              >
                                {item}
                              </motion.div>
                            )
                          )}
                        </div>
                      )}

                      {/* Adapter layer details */}
                      {layer.id === "adapters" && (
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-6">
                          {[
                            { name: "OpenAI", type: "API" },
                            { name: "Anthropic", type: "API" },
                            { name: "Codex", type: "Harness" },
                            { name: "Claude Code", type: "Harness" },
                          ].map((adapter, j) => (
                            <motion.div
                              key={adapter.name}
                              initial={{ opacity: 0, scale: 0.9 }}
                              whileInView={{ opacity: 1, scale: 1 }}
                              viewport={{ once: true }}
                              transition={{ duration: 0.3, delay: j * 0.05 + 0.3 }}
                              className="p-3 rounded-lg bg-white/5 border border-white/5 text-center hover:bg-white/[0.07] hover:border-white/10 transition-all"
                            >
                              <div className="text-xs font-medium text-white">
                                {adapter.name}
                              </div>
                              <div className="text-[10px] text-zinc-500 mt-0.5">
                                {adapter.type}
                              </div>
                            </motion.div>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </motion.div>
              ))}
            </div>
          </div>
        </motion.div>
      </div>
    </section>
  );
}
