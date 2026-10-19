"use client";

import { createPortal } from "react-dom";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

// Must match the tooltip's rendered width below (w-60 = 15rem = 240px at the
// default root font size). Used to clamp the portal's left offset before the
// element has painted, so there's no flash-then-jump on open.
const TOOLTIP_WIDTH = 240;
const VIEWPORT_MARGIN = 12;

/**
 * Focus- and tap-triggerable info tooltip.
 *
 * Deliberately NOT hover-only: on touch there is no hover state, so a
 * hover-only tooltip is simply invisible to phone and tablet users. Click
 * pins it open, hover and keyboard focus open it transiently, and blur
 * releases both -- so it works with a mouse, a finger, and Tab.
 *
 * Two booleans rather than one: on touch, a tap fires a synthetic mouseenter
 * then click, then mouseleave when you tap away. Tracking a single flag makes
 * the tap-open instantly reversible, which reads as a flicker.
 *
 * Rendered via a portal into document.body, positioned with `fixed`
 * coordinates from the trigger's own bounding rect. This is deliberate: the
 * sidebar needs `overflow-y-auto` for its own scroll, and setting overflow-y
 * to anything but `visible` forces overflow-x to `auto` on that same box (a
 * CSS rule, not a Tailwind quirk) -- so an absolutely-positioned tooltip
 * INSIDE that box gets clipped and forces horizontal scrolling to see it. A
 * portal escapes that ancestor entirely; viewport-relative positioning means
 * it can render outside the sidebar's bounds without being clipped by them.
 */
export function InfoTip({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const [pinned, setPinned] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(
    null
  );
  const buttonRef = useRef<HTMLButtonElement>(null);
  const id = useId();
  const open = pinned || hovered;

  // Recomputed every time it opens, from the trigger's CURRENT position --
  // so opening it after scrolling still lands correctly. Clamped to the
  // viewport so it can hang outside the sidebar's box freely, but never off
  // the edge of the screen.
  useLayoutEffect(() => {
    if (!open || !buttonRef.current) {
      setCoords(null);
      return;
    }

    const rect = buttonRef.current.getBoundingClientRect();
    const left = Math.min(
      Math.max(rect.left, VIEWPORT_MARGIN),
      window.innerWidth - TOOLTIP_WIDTH - VIEWPORT_MARGIN
    );
    setCoords({ top: rect.bottom + 6, left });
  }, [open]);

  // A fixed-position tooltip does not move with the page or the sidebar's
  // internal scroll -- it would go stale (detached from its trigger) if left
  // open through a scroll event. Closing on scroll is simpler and more
  // robust than tracking every possible scrolling ancestor (the sidebar
  // scrolls independently of the page); capture:true catches scroll events
  // from any nested scroll container, not just window-level scroll.
  useEffect(() => {
    if (!open) return;

    function handleScroll() {
      setPinned(false);
      setHovered(false);
    }

    window.addEventListener("scroll", handleScroll, true);
    return () => window.removeEventListener("scroll", handleScroll, true);
  }, [open]);

  return (
    <span className="relative inline-block align-middle">
      <button
        ref={buttonRef}
        type="button"
        aria-label={`About ${label}`}
        aria-describedby={open ? id : undefined}
        aria-expanded={open}
        onClick={() => setPinned((v) => !v)}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        onFocus={() => setHovered(true)}
        onBlur={() => {
          setHovered(false);
          setPinned(false);
        }}
        className="relative ml-1.5 inline-flex h-4 w-4 items-center justify-center rounded-full border before:absolute before:-inset-[14px] before:content-[''] border-slate-300 text-[10px] font-bold leading-none text-slate-500 transition hover:border-slate-500 hover:text-slate-700"
      >
        i
      </button>

      {open &&
        coords &&
        createPortal(
          <span
            id={id}
            role="tooltip"
            style={{
              top: coords.top,
              left: coords.left,
              width: TOOLTIP_WIDTH,
            }}
            className="fixed z-50 rounded-xl border border-slate-200 bg-white p-3 text-xs font-normal leading-5 text-slate-600 shadow-xl"
          >
            {children}
          </span>,
          document.body
        )}
    </span>
  );
}