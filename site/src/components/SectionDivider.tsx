"use client";

import { motion } from "framer-motion";

export function SectionDivider() {
  return (
    <div className="relative py-8">
      <div className="max-w-6xl mx-auto px-6">
        <motion.div
          initial={{ opacity: 0, scaleX: 0 }}
          whileInView={{ opacity: 1, scaleX: 1 }}
          viewport={{ once: true }}
          transition={{ duration: 0.8 }}
          className="h-px bg-gradient-to-r from-transparent via-white/10 to-transparent"
        />
      </div>
    </div>
  );
}
