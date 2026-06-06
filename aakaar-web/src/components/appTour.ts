import type { TourStep } from "@/components/GuidedTour";

// Short app-overview tour. Steps target stable nav elements by CSS selector
// (data-tour attributes added in Layout). Missing targets — e.g. role-gated
// nav items — are skipped automatically by the tour engine, so the same tour
// works for every role.
export const APP_TOUR: TourStep[] = [
  {
    selector: '[data-tour="nav"]',
    title: "Welcome to Aakaar",
    body: "This sidebar is your console. Each link opens a different workshop — let's walk through the essentials.",
  },
  {
    selector: '[data-tour="nav-dashboard"]',
    title: "Dashboard",
    body: "Start here for a live read on run volume, capability usage, and recent failures across your work.",
  },
  {
    selector: '[data-tour="nav-chat"]',
    title: "Conversation",
    body: "Describe what you want in plain language and the planner shapes it into a runnable workflow.",
  },
  {
    selector: '[data-tour="nav-workflows"]',
    title: "Workflows",
    body: "Saved plans live here. Open one to edit its DAG, schedule it, refine it, or run it on demand.",
  },
  {
    selector: '[data-tour="nav-runs"]',
    title: "Runs",
    body: "Track live and historical executions. Open a run to watch each step stream in real time.",
  },
  {
    selector: '[data-tour="theme"]',
    title: "Make it yours",
    body: "Switch language and theme any time. Ten themes ship in — pick the one that suits your desk.",
  },
];
