"use client";

import { motion, AnimatePresence } from "framer-motion";
import { useState } from "react";
import { ArrowRight, Loader2, Check, AlertCircle } from "lucide-react";

type WaitlistState = "idle" | "loading" | "success" | "error";

export function Waitlist() {
  const [state, setState] = useState<WaitlistState>("idle");
  const [email, setEmail] = useState("");
  const [useCase, setUseCase] = useState("");
  const [errorMessage, setErrorMessage] = useState("");

  const validateEmail = (email: string): boolean => {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!validateEmail(email)) {
      setState("error");
      setErrorMessage("Please enter a valid email address.");
      return;
    }

    setState("loading");

    // Simulate API call
    // In production, replace with actual API endpoint
    try {
      await new Promise((resolve) => setTimeout(resolve, 1500));

      // Simulate success
      setState("success");

      // Reset form after showing success
      setTimeout(() => {
        setEmail("");
        setUseCase("");
        setState("idle");
      }, 3000);
    } catch (err) {
      setState("error");
      setErrorMessage("Something went wrong. Please try again.");
      setTimeout(() => setState("idle"), 3000);
    }
  };

  return (
    <section id="waitlist" className="relative py-24 md:py-32 px-6">
      {/* Background effects */}
      <div className="absolute inset-0 bg-gradient-to-b from-transparent via-sky-500/[0.02] to-transparent pointer-events-none" />

      <div className="relative max-w-2xl mx-auto">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
          className="text-center mb-10"
        >
          <h2 className="text-3xl md:text-4xl font-bold mb-4">
            Get early access
          </h2>
          <p className="text-zinc-400 leading-relaxed">
            Relay is in early access. Join the waitlist to be among the first to
            try it.
          </p>
        </motion.div>

        <motion.form
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6, delay: 0.1 }}
          onSubmit={handleSubmit}
          className="relative bg-[#0a0a0c] rounded-2xl border border-white/10 p-8"
        >
          <AnimatePresence mode="wait">
            {state === "success" ? (
              <motion.div
                key="success"
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.95 }}
                className="text-center py-8"
              >
                <motion.div
                  initial={{ scale: 0 }}
                  animate={{ scale: 1 }}
                  transition={{ type: "spring", stiffness: 200, damping: 15 }}
                  className="inline-flex p-4 rounded-full bg-emerald-500/10 text-emerald-400 mb-4"
                >
                  <Check size={32} />
                </motion.div>
                <h3 className="text-xl font-semibold text-white mb-2">
                  You're on the list!
                </h3>
                <p className="text-zinc-400">
                  We'll be in touch when early access opens.
                </p>
              </motion.div>
            ) : (
              <motion.div
                key="form"
                initial={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="space-y-5"
              >
                <div>
                  <label
                    htmlFor="email"
                    className="block text-sm font-medium text-zinc-300 mb-2"
                  >
                    Email address
                  </label>
                  <input
                    id="email"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="you@example.com"
                    disabled={state === "loading"}
                    className="w-full px-4 py-3 rounded-lg bg-white/5 border border-white/10 text-white placeholder-zinc-500 focus:outline-none focus:border-sky-500/50 focus:bg-white/[0.07] transition-all disabled:opacity-50"
                    required
                  />
                </div>

                <div>
                  <label
                    htmlFor="useCase"
                    className="block text-sm font-medium text-zinc-300 mb-2"
                  >
                    What do you currently use AI agents for?{" "}
                    <span className="text-zinc-500">(optional)</span>
                  </label>
                  <textarea
                    id="useCase"
                    value={useCase}
                    onChange={(e) => setUseCase(e.target.value)}
                    placeholder="e.g., Code review, debugging, writing tests..."
                    disabled={state === "loading"}
                    rows={3}
                    className="w-full px-4 py-3 rounded-lg bg-white/5 border border-white/10 text-white placeholder-zinc-500 focus:outline-none focus:border-sky-500/50 focus:bg-white/[0.07] transition-all disabled:opacity-50 resize-none"
                  />
                </div>

                {state === "error" && (
                  <motion.div
                    initial={{ opacity: 0, y: -10 }}
                    animate={{ opacity: 1, y: 0 }}
                    className="flex items-center gap-2 text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-4 py-3"
                  >
                    <AlertCircle size={16} />
                    {errorMessage}
                  </motion.div>
                )}

                <button
                  type="submit"
                  disabled={state === "loading"}
                  className="group w-full relative px-6 py-3 text-sm font-medium text-white bg-gradient-to-r from-sky-500 to-indigo-500 rounded-lg overflow-hidden transition-all hover:shadow-lg hover:shadow-sky-500/25 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <span className="relative z-10 flex items-center justify-center gap-2">
                    {state === "loading" ? (
                      <>
                        <Loader2 size={16} className="animate-spin" />
                        Joining waitlist...
                      </>
                    ) : (
                      <>
                        Join the waitlist
                        <ArrowRight
                          size={16}
                          className="transition-transform group-hover:translate-x-1"
                        />
                      </>
                    )}
                  </span>
                  <div className="absolute inset-0 bg-gradient-to-r from-sky-600 to-indigo-600 opacity-0 group-hover:opacity-100 transition-opacity" />
                </button>

                <p className="text-xs text-zinc-500 text-center">
                  We'll never share your email. Unsubscribe anytime.
                </p>
              </motion.div>
            )}
          </AnimatePresence>
        </motion.form>
      </div>
    </section>
  );
}
