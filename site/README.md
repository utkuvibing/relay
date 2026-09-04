# Relay Landing Page

Production-quality landing page for Relay, built with Next.js, TypeScript, Tailwind CSS, and Framer Motion.

## Features

- Dark-first, polished design optimized for developers
- Animated orchestration visualization showing agent workflow
- Interactive workflow demonstration with terminal examples
- Responsive design for all screen sizes
- Smooth scroll-triggered animations
- Waitlist form with validation and error states
- Optimized for performance and accessibility

## Tech Stack

- **Framework**: Next.js 14
- **Language**: TypeScript
- **Styling**: Tailwind CSS
- **Animations**: Framer Motion
- **Icons**: Lucide React

## Getting Started

### Installation

```bash
cd site
npm install
```

### Development

```bash
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) in your browser.

### Build

```bash
npm run build
```

The static export will be in the `out/` directory.

### Production

```bash
npm start
```

## Project Structure

```
site/
├── src/
│   ├── app/
│   │   ├── layout.tsx       # Root layout
│   │   ├── page.tsx         # Main page
│   │   └── globals.css      # Global styles
│   └── components/
│       ├── Navbar.tsx           # Navigation
│       ├── Hero.tsx             # Hero section
│       ├── OrchestrationViz.tsx # Animated visualization
│       ├── Workflow.tsx         # Interactive workflow demo
│       ├── Capabilities.tsx     # Feature cards
│       ├── WhyRelay.tsx         # Problem/solution section
│       ├── DeveloperFirst.tsx   # Terminal examples
│       ├── Architecture.tsx     # Architecture diagram
│       ├── Waitlist.tsx         # Waitlist form
│       ├── Footer.tsx           # Footer
│       └── SectionDivider.tsx   # Visual divider
```

## Design Decisions

- **No Google Fonts dependency**: Uses system font stack for performance and offline compatibility
- **Static export**: Configured for static hosting (Vercel, Netlify, GitHub Pages)
- **Accessibility**: Semantic HTML, keyboard navigation, visible focus states, proper ARIA labels
- **Performance**: Optimized animations, lazy loading, minimal dependencies
- **Responsive**: Mobile-first design with careful attention to smaller screens

## Waitlist Integration

The waitlist form is structured for easy backend integration. Update the `handleSubmit` function in `Waitlist.tsx` to connect to your API endpoint.

## Deployment

### Vercel

```bash
npm install -g vercel
vercel
```

### GitHub Pages

```bash
npm run build
# Deploy the out/ directory
```

### Other Platforms

The static export in `out/` can be deployed to any static hosting service.

## Customization

### Colors

Edit `tailwind.config.ts` to customize the color scheme. The primary gradient uses sky-500 to indigo-500.

### Content

All text content is in the component files. Update headlines, descriptions, and form fields as needed.

### Animations

Animations use Framer Motion. Adjust timing, easing, and triggers in individual components.

## Browser Support

- Chrome/Edge (latest)
- Firefox (latest)
- Safari (latest)
- Mobile browsers (iOS Safari, Chrome for Android)

## License

Part of the Relay project. See the main repository for license details.
