"use client";

import { motion } from "framer-motion";
import {
  GitBranch,
  Globe,
  Shield,
  History,
  RefreshCw,
} from "lucide-react";

const capabilities = [
  {
    icon: <GitBranch size={20} />,
    title: "Cross-agent continuity",
    description:
      "Move work between models and coding agents without rebuilding context from scratch. Context flows with the work.",
    gradient: "from-sky-500/20 to-sky-500/0",
  },
  {
    icon: <Globe size={20} />,
    title: "Provider independent",
    description:
      "Work across different AI providers instead of locking your workflow to one ecosystem. Use the right model for each task.",
    gradient: "from-indigo-500/20 to-indigo-500/0",
  },
  {
    icon: <Shield size={20} />,
    title: "Approval boundaries",
    description:
      "Let agents move quickly while keeping sensitive actions behind explicit permission gates. You decide what needs review.",
    gradient: "from-amber-500/20 to-amber-500/0",
  },
  {
    icon: <History size={20} />,
    title: "Execution evidence",
    description:
      "Preserve artifacts, outputs, decisions, and execution history so you can understand what actually happened.",
    gradient: "from-emerald-500/20 to-emerald-500/0",
  },
  {
    icon: <RefreshCw size={20} />,
    title: "Recoverable workflows",
    description:
      "Design workflows where pending and committed actions can be distinguished instead of blindly retrying side effects.",
    gradient: "from-violet-500/20 to-violet-500/0",
  },
];

export function Capabilities() {
  return (
    <section id="capabilities" className="relative py-24 md:py-32 px-6">
      {/* Background accent */}
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top,rgba(56,189,248,0.03),transparent_50%)] pointer-events-none" />

      <div className="relative max-w-6xl mx-auto">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
          className="text-center mb-16"
        >
          <h2 className="text-3xl md:text-4xl font-bold mb-4">
            Built for real workflows
          </h2>
          <p className="text-zinc-400 max-w-xl mx-auto">
            Relay gives you the infrastructure to coordinate agents without
            losing control or visibility.
          </p>
        </motion.div>

        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-5">
          {capabilities.map((cap, i) => (
            <motion.div
              key={i}
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.5, delay: i * 0.08 }}
              className="group relative"
            >
              <div className="absolute inset-0 bg-gradient-to-br opacity-0 group-hover:opacity-100 transition-opacity duration-500 rounded-xl pointer-events-none"
                   style={{ backgroundImage: `linear-gradient(to bottom right, var(--tw-gradient-stops))` }}
              />
              <div className={`relative h-full p-6 rounded-xl bg-[#0a0a0c] border border-white/5 card-hover overflow-hidden`}>
                {/* Subtle gradient overlay */}
                <div className={`absolute inset-0 bg-gradient-to-br ${cap.gradient} opacity-0 group-hover:opacity-100 transition-opacity duration-500`} />
                
                <div className="relative">
                  <div className="inline-flex p-3 rounded-lg bg-white/5 text-sky-400 mb-4 group-hover:bg-white/10 transition-colors">
                    {cap.icon}
                  </div>
                  <h3 className="text-lg font-semibold mb-2 text-white">
                    {cap.title}
                  </h3>
                  <p className="text-sm text-zinc-400 leading-relaxed">
                    {cap.description}
                  </p>
                </div>
              </div>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
}
