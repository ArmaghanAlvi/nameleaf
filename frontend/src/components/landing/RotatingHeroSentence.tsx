"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

const TRANSITION_MS = 700;

function getRandomDelay() {
  return 4000 + Math.floor(Math.random() * 3000); // 4000–7000ms between switches
}

function getRandomNextIndex(currentIndex: number, length: number): number {
  // Pick any index except the current one
  const next = Math.floor(Math.random() * (length - 1));
  return next >= currentIndex ? next + 1 : next;
}

interface RotatingItem {
  text: string;
  className: string;
}

interface RotatingWordProps {
  items: RotatingItem[];
  initialDelayMs?: number;
}

const actionWords: RotatingItem[] = [
  { text: "find", className: "text-green-600" },
  { text: "craft", className: "text-blue-600" },
  { text: "discover", className: "text-amber-600" },
  { text: "shape", className: "text-violet-600" },
  { text: "imagine", className: "text-pink-600" },
  { text: "choose", className: "text-teal-600" },
  { text: "forge", className: "text-orange-600" },
];

const subjectPhrases: RotatingItem[] = [
  { text: "baby", className: "text-pink-600" },
  { text: "fictional character", className: "text-violet-600" },
  { text: "fantasy character", className: "text-amber-700" },
  { text: "sci-fi character", className: "text-cyan-700" },
  { text: "tabletop character", className: "text-emerald-700" },
  { text: "game character", className: "text-blue-600" },
  { text: "story protagonist", className: "text-rose-600" },
  { text: "original character", className: "text-purple-600" },
];

function RotatingWord({ items, initialDelayMs = 0 }: RotatingWordProps) {
  const [state, setState] = useState<{
    current: { index: number; id: number };
    exiting: { index: number; id: number } | null;
  }>({
    current: { index: 0, id: 0 },
    exiting: null,
  });
  const nextId = useRef(1);
  const sizerRef = useRef<HTMLSpanElement>(null);
  const [containerWidth, setContainerWidth] = useState<number | null>(null);

  // After each word change, read the sizer's rendered width and store it.
  // This gives us a concrete px value to animate `width` between (CSS can't
  // transition `width: auto`). Runs before paint so there's no flicker.
  useLayoutEffect(() => {
    if (sizerRef.current) {
      setContainerWidth(sizerRef.current.offsetWidth);
    }
  }, [state.current.index]);

  useEffect(() => {
    let timerId: ReturnType<typeof setTimeout>;

    const scheduleNext = () => {
      timerId = setTimeout(() => {
        setState((prev) => ({
          current: {
            index: getRandomNextIndex(prev.current.index, items.length),
            id: nextId.current++,
          },
          exiting: prev.current,
        }));
        scheduleNext();
      }, getRandomDelay());
    };

    timerId = setTimeout(scheduleNext, initialDelayMs);
    return () => clearTimeout(timerId);
  }, [items.length, initialDelayMs]);

  const clearExiting = () =>
    setState((prev) => ({ ...prev, exiting: null }));

  return (
    <span
      className="relative inline-flex align-bottom"
      style={{
        clipPath: "inset(0 -9999px -0.25em)",
        width: containerWidth ?? undefined,
        // Only add the width transition after the first measurement so the
        // initial render doesn't animate from undefined → measured.
        transition:
          containerWidth !== null
            ? `width ${TRANSITION_MS}ms ease-in-out`
            : undefined,
      }}
    >
      {/* Invisible sizer: the only in-flow child, so the container's width
          follows it. Showing the incoming word makes the container start
          animating toward the new width at the same moment the words swap. */}
      <span
        ref={sizerRef}
        className="invisible whitespace-nowrap"
        aria-hidden="true"
      >
        {items[state.current.index].text}
      </span>

      {state.exiting && (
        <span
          key={state.exiting.id}
          className={`absolute inset-0 flex items-center justify-center whitespace-nowrap animate-exit-below ${items[state.exiting.index].className}`}
          style={{ animationDuration: `${TRANSITION_MS}ms` }}
          onAnimationEnd={clearExiting}
        >
          {items[state.exiting.index].text}
        </span>
      )}

      <span
        key={state.current.id}
        className={`absolute inset-0 flex items-center justify-center whitespace-nowrap animate-enter-above ${items[state.current.index].className}`}
        style={{ animationDuration: `${TRANSITION_MS}ms` }}
      >
        {items[state.current.index].text}
      </span>
    </span>
  );
}

export function RotatingHeroSentence() {
  return (
    // text-3xl below sm: the rotating phrases don't wrap (whitespace-nowrap),
    // and "fictional character" at text-4xl is wider than a 360 px screen.
    <h1 className="mx-auto max-w-6xl text-center text-3xl font-bold tracking-tight text-slate-950 sm:text-6xl lg:text-7xl">
      <span className="sr-only">
        I want to find or craft a name for my baby, fictional character, or
        creative project.
      </span>

      <span aria-hidden="true" className="flex flex-col items-center gap-y-2">
        <span className="flex flex-wrap items-baseline justify-center gap-x-3">
          <span>I want to</span>
          <RotatingWord items={actionWords} />
          <span>a name</span>
        </span>
        <span className="flex flex-wrap items-baseline justify-center gap-x-3">
          <span>for my</span>
          <RotatingWord items={subjectPhrases} initialDelayMs={3500} />
        </span>
      </span>
    </h1>
  );
}
