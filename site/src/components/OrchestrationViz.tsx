"use client";

import { motion } from "framer-motion";
import { useEffect, useState, useCallback } from "react";

interface Packet {
  id: number;
  fromX: number;
  fromY: number;
  toX: number;
  toY: number;
  progress: number;
  color: string;
}

export function OrchestrationViz() {
  const [packets, setPackets] = useState<Packet[]>([]);
  const [activeNodes, setActiveNodes] = useState<Set<string>>(new Set());

  const connections = [
    { from: "dev", to: "relay", fromX: 50, fromY: 15, toX: 50, toY: 45 },
    { from: "relay", to: "codex", fromX: 50, fromY: 45, toX: 20, toY: 75 },
    { from: "relay", to: "claude", fromX: 50, fromY: 45, toX: 50, toY: 80 },
    { from: "relay", to: "gemini", fromX: 50, fromY: 45, toX: 80, toY: 75 },
    { from: "codex", to: "relay", fromX: 20, fromY: 75, toX: 50, toY: 45 },
    { from: "claude", to: "relay", fromX: 50, fromY: 80, toX: 50, toY: 45 },
    { from: "gemini", to: "relay", fromX: 80, fromY: 75, toX: 50, toY: 45 },
  ];

  const colors = ["#38bdf8", "#818cf8", "#34d399", "#fbbf24"];

  const createPacket = useCallback(() => {
    const conn = connections[Math.floor(Math.random() * connections.length)];
    const color = colors[Math.floor(Math.random() * colors.length)];
    const packet: Packet = {
      id: Date.now() + Math.random(),
      fromX: conn.fromX,
      fromY: conn.fromY,
      toX: conn.toX,
      toY: conn.toY,
      progress: 0,
      color,
    };

    setPackets((prev) => [...prev.slice(-8), packet]);
    setActiveNodes((prev) => new Set([...prev, conn.from, conn.to]));

    setTimeout(() => {
      setActiveNodes((prev) => {
        const next = new Set(prev);
        next.delete(conn.from);
        next.delete(conn.to);
        return next;
      });
    }, 1500);
  }, []);

  useEffect(() => {
    const interval = setInterval(createPacket, 1200);
    return () => clearInterval(interval);
  }, [createPacket]);

  // Animate packets
  useEffect(() => {
    const interval = setInterval(() => {
      setPackets((prev) =>
        prev
          .map((p) => ({ ...p, progress: p.progress + 0.03 }))
          .filter((p) => p.progress <= 1)
      );
    }, 30);
    return () => clearInterval(interval);
  }, []);

  const nodes = [
    {
      id: "dev",
      label: "Developer",
      x: 50,
      y: 15,
      type: "developer",
      icon: (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
          <circle cx="12" cy="7" r="4" />
        </svg>
      ),
    },
    {
      id: "relay",
      label: "Relay",
      x: 50,
      y: 45,
      type: "relay",
      icon: (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M12 4L4 8L12 12L20 8L12 4Z" />
          <path d="M4 16L12 20L20 16" />
          <path d="M4 12L12 16L20 12" />
        </svg>
      ),
    },
    {
      id: "codex",
      label: "Codex",
      x: 20,
      y: 75,
      type: "agent",
      icon: (
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <polyline points="16 18 22 12 16 6" />
          <polyline points="8 6 2 12 8 18" />
        </svg>
      ),
    },
    {
      id: "claude",
      label: "Claude",
      x: 50,
      y: 80,
      type: "agent",
      icon: (
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      ),
    },
    {
      id: "gemini",
      label: "Gemini",
      x: 80,
      y: 75,
      type: "agent",
      icon: (
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="12" cy="12" r="10" />
          <circle cx="12" cy="12" r="4" />
        </svg>
      ),
    },
  ];

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
            Agents work through Relay
          </h2>
          <p className="text-zinc-400 max-w-xl mx-auto">
            Tasks flow from you through Relay to the right agents, with shared
            context flowing back.
          </p>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          whileInView={{ opacity: 1, scale: 1 }}
          viewport={{ once: true }}
          transition={{ duration: 0.8 }}
          className="relative"
        >
          {/* Container with glow */}
          <div className="absolute inset-0 bg-gradient-to-b from-sky-500/5 to-transparent rounded-3xl blur-3xl pointer-events-none" />
          
          <div className="relative bg-[#0a0a0c]/50 backdrop-blur-sm border border-white/5 rounded-2xl p-6 md:p-12">
            <div className="relative aspect-[4/3] md:aspect-[16/9] max-w-4xl mx-auto">
              <svg viewBox="0 0 100 95" className="w-full h-full">
                <defs>
                  <filter id="glow">
                    <feGaussianBlur stdDeviation="2" result="coloredBlur" />
                    <feMerge>
                      <feMergeNode in="coloredBlur" />
                      <feMergeNode in="SourceGraphic" />
                    </feMerge>
                  </filter>
                  <linearGradient id="relayGradient" x1="0%" y1="0%" x2="100%" y2="100%">
                    <stop offset="0%" stopColor="#0ea5e9" />
                    <stop offset="100%" stopColor="#6366f1" />
                  </linearGradient>
                  <radialGradient id="nodeGlow">
                    <stop offset="0%" stopColor="rgba(56, 189, 248, 0.3)" />
                    <stop offset="100%" stopColor="rgba(56, 189, 248, 0)" />
                  </radialGradient>
                </defs>

                {/* Connection lines */}
                {connections.map((conn, i) => (
                  <motion.line
                    key={i}
                    x1={conn.fromX}
                    y1={conn.fromY}
                    x2={conn.toX}
                    y2={conn.toY}
                    stroke="rgba(56, 189, 248, 0.08)"
                    strokeWidth="0.3"
                    initial={{ opacity: 0 }}
                    whileInView={{ opacity: 1 }}
                    viewport={{ once: true }}
                    transition={{ duration: 1, delay: i * 0.1 }}
                  />
                ))}

                {/* Animated data packets */}
                {packets.map((packet) => {
                  const x = packet.fromX + (packet.toX - packet.fromX) * packet.progress;
                  const y = packet.fromY + (packet.toY - packet.fromY) * packet.progress;
                  const opacity = packet.progress < 0.1
                    ? packet.progress * 10
                    : packet.progress > 0.9
                    ? (1 - packet.progress) * 10
                    : 1;

                  return (
                    <g key={packet.id}>
                      {/* Trail */}
                      <circle
                        cx={x}
                        cy={y}
                        r="1.5"
                        fill={packet.color}
                        opacity={opacity * 0.3}
                        filter="url(#glow)"
                      />
                      {/* Main packet */}
                      <circle
                        cx={x}
                        cy={y}
                        r="0.8"
                        fill={packet.color}
                        opacity={opacity}
                      />
                    </g>
                  );
                })}

                {/* Nodes */}
                {nodes.map((node) => {
                  const isActive = activeNodes.has(node.id);
                  const isRelay = node.type === "relay";
                  const size = isRelay ? 7 : node.type === "developer" ? 5.5 : 4.5;

                  return (
                    <g key={node.id}>
                      {/* Active glow */}
                      {isActive && (
                        <>
                          <circle
                            cx={node.x}
                            cy={node.y}
                            r={size + 4}
                            fill="url(#nodeGlow)"
                          />
                          <motion.circle
                            cx={node.x}
                            cy={node.y}
                            r={size + 3}
                            fill="none"
                            stroke="rgba(56, 189, 248, 0.4)"
                            strokeWidth="0.2"
                            initial={{ scale: 0.8, opacity: 0.8 }}
                            animate={{ scale: 1.5, opacity: 0 }}
                            transition={{ duration: 1 }}
                          />
                        </>
                      )}

                      {/* Node background */}
                      <circle
                        cx={node.x}
                        cy={node.y}
                        r={size}
                        fill={
                          isRelay
                            ? "url(#relayGradient)"
                            : node.type === "developer"
                            ? "#18181b"
                            : "#0f172a"
                        }
                        stroke={
                          isActive
                            ? "rgba(56, 189, 248, 0.6)"
                            : isRelay
                            ? "#38bdf8"
                            : "rgba(255, 255, 255, 0.1)"
                        }
                        strokeWidth="0.3"
                        filter={isRelay ? "url(#glow)" : undefined}
                      />

                      {/* Icon */}
                      <foreignObject
                        x={node.x - (isRelay ? 4 : 3.5)}
                        y={node.y - (isRelay ? 4 : 3.5)}
                        width={isRelay ? 8 : 7}
                        height={isRelay ? 8 : 7}
                      >
                        <div
                          className={`flex items-center justify-center w-full h-full ${
                            isRelay
                              ? "text-white"
                              : node.type === "developer"
                              ? "text-zinc-300"
                              : "text-zinc-400"
                          }`}
                          style={{ transform: "scale(0.45)" }}
                        >
                          {node.icon}
                        </div>
                      </foreignObject>

                      {/* Label */}
                      <text
                        x={node.x}
                        y={node.y + size + 4}
                        textAnchor="middle"
                        fill={isActive ? "white" : isRelay ? "#38bdf8" : "#a1a1aa"}
                        fontSize="2.5"
                        fontWeight={isRelay ? "600" : "400"}
                      >
                        {node.label}
                      </text>
                    </g>
                  );
                })}
              </svg>
            </div>

            {/* Legend */}
            <div className="flex flex-wrap items-center justify-center gap-4 md:gap-6 mt-6 text-xs text-zinc-500">
              <div className="flex items-center gap-2">
                <div className="w-2 h-2 rounded-full bg-sky-400" />
                <span>Task routing</span>
              </div>
              <div className="flex items-center gap-2">
                <div className="w-2 h-2 rounded-full bg-indigo-400" />
                <span>Context sharing</span>
              </div>
              <div className="flex items-center gap-2">
                <div className="w-2 h-2 rounded-full bg-emerald-400" />
                <span>Results</span>
              </div>
            </div>
          </div>
        </motion.div>
      </div>
    </section>
  );
}
