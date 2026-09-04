"use client";

import { Navbar } from "@/components/Navbar";
import { Hero } from "@/components/Hero";
import { OrchestrationViz } from "@/components/OrchestrationViz";
import { Workflow } from "@/components/Workflow";
import { Capabilities } from "@/components/Capabilities";
import { WhyRelay } from "@/components/WhyRelay";
import { DeveloperFirst } from "@/components/DeveloperFirst";
import { Architecture } from "@/components/Architecture";
import { Waitlist } from "@/components/Waitlist";
import { Footer } from "@/components/Footer";
import { SectionDivider } from "@/components/SectionDivider";

export default function Home() {
  return (
    <main className="min-h-screen bg-[#09090b] text-white overflow-x-hidden">
      <Navbar />
      <Hero />
      <SectionDivider />
      <OrchestrationViz />
      <SectionDivider />
      <Workflow />
      <SectionDivider />
      <Capabilities />
      <SectionDivider />
      <WhyRelay />
      <SectionDivider />
      <DeveloperFirst />
      <SectionDivider />
      <Architecture />
      <SectionDivider />
      <Waitlist />
      <Footer />
    </main>
  );
}
