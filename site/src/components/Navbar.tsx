"use client";

import { useState, useEffect } from "react";
import { motion } from "framer-motion";
import { Menu, X } from "lucide-react";

export function Navbar() {
  const [scrolled, setScrolled] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  useEffect(() => {
    const handleScroll = () => setScrolled(window.scrollY > 20);
    window.addEventListener("scroll", handleScroll);
    return () => window.removeEventListener("scroll", handleScroll);
  }, []);

  return (
    <motion.header
      initial={{ y: -20, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      transition={{ duration: 0.5 }}
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 ${
        scrolled
          ? "bg-[#09090b]/80 backdrop-blur-xl border-b border-white/5"
          : "bg-transparent"
      }`}
    >
      <nav className="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
        <a href="#" className="flex items-center gap-2 group">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-sky-400 to-indigo-500 flex items-center justify-center">
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              xmlns="http://www.w3.org/2000/svg"
            >
              <path
                d="M12 4L4 8L12 12L20 8L12 4Z"
                stroke="white"
                strokeWidth="2"
                strokeLinejoin="round"
              />
              <path
                d="M4 16L12 20L20 16"
                stroke="white"
                strokeWidth="2"
                strokeLinejoin="round"
              />
              <path
                d="M4 12L12 16L20 12"
                stroke="white"
                strokeWidth="2"
                strokeLinejoin="round"
              />
            </svg>
          </div>
          <span className="font-semibold text-lg tracking-tight">Relay</span>
        </a>

        <div className="hidden md:flex items-center gap-8">
          <a
            href="#workflow"
            className="text-sm text-zinc-400 hover:text-white transition-colors"
          >
            How it works
          </a>
          <a
            href="#capabilities"
            className="text-sm text-zinc-400 hover:text-white transition-colors"
          >
            Capabilities
          </a>
          <a
            href="#architecture"
            className="text-sm text-zinc-400 hover:text-white transition-colors"
          >
            Architecture
          </a>
          <a
            href="#waitlist"
            className="px-4 py-2 text-sm font-medium bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg transition-all"
          >
            Join waitlist
          </a>
        </div>

        <button
          className="md:hidden p-2 text-zinc-400 hover:text-white"
          onClick={() => setMobileOpen(!mobileOpen)}
          aria-label="Toggle menu"
        >
          {mobileOpen ? <X size={20} /> : <Menu size={20} />}
        </button>
      </nav>

      {mobileOpen && (
        <motion.div
          initial={{ opacity: 0, y: -10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }}
          className="md:hidden bg-[#09090b] border-b border-white/5 px-6 py-4 space-y-4"
        >
          <a
            href="#workflow"
            className="block text-sm text-zinc-400 hover:text-white"
            onClick={() => setMobileOpen(false)}
          >
            How it works
          </a>
          <a
            href="#capabilities"
            className="block text-sm text-zinc-400 hover:text-white"
            onClick={() => setMobileOpen(false)}
          >
            Capabilities
          </a>
          <a
            href="#architecture"
            className="block text-sm text-zinc-400 hover:text-white"
            onClick={() => setMobileOpen(false)}
          >
            Architecture
          </a>
          <a
            href="#waitlist"
            className="block text-sm font-medium text-white bg-white/5 px-4 py-2 rounded-lg border border-white/10 text-center"
            onClick={() => setMobileOpen(false)}
          >
            Join waitlist
          </a>
        </motion.div>
      )}
    </motion.header>
  );
}
