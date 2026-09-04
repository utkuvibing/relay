"use client";

export function Footer() {
  return (
    <footer className="border-t border-white/5 py-8 px-6">
      <div className="max-w-6xl mx-auto flex flex-col md:flex-row items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <div className="w-6 h-6 rounded-md bg-gradient-to-br from-sky-400 to-indigo-500 flex items-center justify-center">
            <svg
              width="12"
              height="12"
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
          <span className="text-sm font-medium text-zinc-400">Relay</span>
        </div>

        <div className="flex items-center gap-6 text-sm text-zinc-500">
          <a
            href="https://github.com/utkuvibing/relay"
            target="_blank"
            rel="noopener noreferrer"
            className="hover:text-zinc-300 transition-colors"
          >
            GitHub
          </a>
          <span className="hover:text-zinc-300 transition-colors cursor-default">
            Documentation
          </span>
        </div>

        <div className="text-xs text-zinc-600">
          © {new Date().getFullYear()} Relay. All rights reserved.
        </div>
      </div>
    </footer>
  );
}
