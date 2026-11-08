/** Variable-height virtual list for the timeline (WBS 3.3, N-008).
 *
 * Only the rows near the viewport are in the DOM. Heights start as an estimate
 * and are replaced by measured ones (ResizeObserver). When a row above the
 * viewport changes height, scrollTop moves by the same amount so what the user
 * reads stays put (native scroll anchoring is off: Safari lacks it and the two
 * would correct twice). Following the bottom lives here too: measuring makes
 * the content grow after a pin, and only this component knows when.
 */

import {
  memo,
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
  type HTMLAttributes,
  type MutableRefObject,
  type ReactNode,
  type UIEvent,
} from "react";
import {flushSync} from "react-dom";

/** Assumed row height before measuring (one text line + padding). */
export const ESTIMATE_PX = 24;
/** Pixels rendered above and below the viewport. */
export const OVERSCAN_PX = 800;
/** Within this many pixels of the end counts as "at the bottom". */
export const BOTTOM_SLOP_PX = 24;

/** Prefix sums: ``tops[i]`` is row i's top, ``tops[n]`` the total height. */
export function layout(keys: readonly number[], heights: ReadonlyMap<number, number>, estimate = ESTIMATE_PX): number[] {
  const tops = new Array<number>(keys.length + 1);
  tops[0] = 0;
  for (let i = 0; i < keys.length; i++) {
    tops[i + 1] = tops[i] + (heights.get(keys[i]) ?? estimate);
  }
  return tops;
}

/** Index of the row containing pixel *y* (clamped to the list). */
export function rowAt(tops: readonly number[], y: number): number {
  const n = tops.length - 1;
  if (n <= 0) return 0;
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (tops[mid] <= y) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

/** Rows to render for a viewport: [start, end). */
export function windowOf(tops: readonly number[], scrollTop: number, viewport: number, overscan = OVERSCAN_PX): [number, number] {
  const n = tops.length - 1;
  if (n <= 0) return [0, 0];
  const start = rowAt(tops, Math.max(0, scrollTop - overscan));
  const end = rowAt(tops, scrollTop + viewport + overscan) + 1;
  return [start, Math.min(n, end)];
}

/** Scroll correction when a row changes height: only rows wholly above the
 * viewport push what the user reads; while following, the pin handles it. */
export function shiftFor(rowTop: number, oldHeight: number, newHeight: number, scrollTop: number, following: boolean): number {
  return !following && rowTop + oldHeight <= scrollTop ? newHeight - oldHeight : 0;
}

/** Follow state after a scroll event. Reaching the end (within the slop)
 * follows; only a move up from where this list last put scrollTop leaves —
 * content growing under a pin does not. A move up that stays inside the slop
 * keeps following (and is re-pinned), as in 3.2: the slop is small. */
export function followAfterScroll(gap: number, scrollTop: number, pinned: number, following: boolean): boolean {
  if (gap < BOTTOM_SLOP_PX) return true;
  if (scrollTop < pinned - 1) return false;
  return following;
}

export type ListHandle = {
  /** Jump to the end and follow new rows again. */
  toBottom(): void;
};

type Props<T extends {key: number}> = {
  items: readonly T[];
  /** Must be stable (module-level or useCallback): rows are memoized on it. */
  render: (item: T) => ReactNode;
  /** Props for the scrolling box this list renders (class, tabIndex, aria-*, onKeyDown). */
  boxProps?: HTMLAttributes<HTMLDivElement>;
  /** Filled with the list's controls. */
  handle?: MutableRefObject<ListHandle | null>;
  /** The user left or reached the bottom. */
  onFollowChange?: (following: boolean) => void;
};

type MeasuredProps = {
  item: {key: number};
  render: (item: never) => ReactNode;
  observe: (id: number, el: HTMLDivElement | null) => void;
};

/** One row in a measured box. Memoized: a new row elsewhere does not re-render it. */
const Measured = memo(function Measured({item, render, observe}: MeasuredProps) {
  const id = item.key;
  const ref = useCallback((el: HTMLDivElement | null) => observe(id, el), [id, observe]);
  return (
    <div ref={ref} data-key={id}>
      {render(item as never)}
    </div>
  );
});

/** The box is rendered here, not by the caller: a parent's ref is attached only
 * after this component's layout effects run, so they would see null. */
export function VirtualList<T extends {key: number}>({items, render, boxProps, handle, onFollowChange}: Props<T>) {
  const box = useRef<HTMLDivElement | null>(null);
  const heights = useRef(new Map<number, number>());
  const [, setVersion] = useState(0);
  const [view, setView] = useState({top: 0, height: 0});
  const following = useRef(true);
  /** scrollTop this component last set: a smaller one means the user scrolled up. */
  const pinned = useRef(0);
  const onFollowRef = useRef(onFollowChange);
  onFollowRef.current = onFollowChange;

  // A new first row means a new list (the BFF restarted and the model began
  // again): its keys restart too, so heights measured for the old rows no
  // longer belong to them. Rows are otherwise only appended.
  const first = useRef<T | undefined>(undefined);
  if (items[0] !== first.current) {
    first.current = items[0];
    heights.current.clear();
  }

  const tops = layout(
    items.map((it) => it.key),
    heights.current,
  );
  const topsRef = useRef(tops);
  topsRef.current = tops;
  const index = useRef(new Map<number, number>());
  index.current = new Map(items.map((it, i) => [it.key, i]));

  const setFollowing = useCallback((f: boolean) => {
    if (following.current !== f) {
      following.current = f;
      onFollowRef.current?.(f);
    }
  }, []);

  const observer = useRef<ResizeObserver | null>(null);
  const nodes = useRef(new Map<number, HTMLDivElement>());
  /** Scroll correction measured but not yet applied (applied after the re-render). */
  const pendingShift = useRef(0);

  const observe = useCallback((id: number, el: HTMLDivElement | null) => {
    const ro = observer.current;
    const prev = nodes.current.get(id);
    if (prev && prev !== el) {
      ro?.unobserve(prev);
      nodes.current.delete(id);
    }
    if (el) {
      nodes.current.set(id, el);
      // Before the effect below has run there is no observer: it observes these then.
      ro?.observe(el);
    }
  }, []);

  // Viewport tracking (scroll + box resize, one read per frame) and row measuring.
  // Everything is made here and torn down in the cleanup, so StrictMode's
  // effect re-run builds a fresh observer and re-observes the mounted rows
  // (React 18 does not call ref callbacks again).
  useLayoutEffect(() => {
    const el = box.current!;
    let frame = 0;
    const read = () => {
      frame = 0;
      setView((v) => (v.top === el.scrollTop && v.height === el.clientHeight ? v : {top: el.scrollTop, height: el.clientHeight}));
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(read);
    };
    read();
    el.addEventListener("scroll", schedule, {passive: true});
    const hasRO = typeof ResizeObserver !== "undefined";
    const boxRO = hasRO ? new ResizeObserver(schedule) : null;
    boxRO?.observe(el);
    const rowRO = hasRO
      ? new ResizeObserver((entries) => {
          let changed = false;
          for (const entry of entries) {
            const node = entry.target as HTMLDivElement;
            const id = Number(node.dataset.key);
            const h = node.offsetHeight;
            const known = heights.current.get(id);
            if (known === h) continue;
            const old = known ?? ESTIMATE_PX;
            heights.current.set(id, h);
            changed = true;
            const i = index.current.get(id);
            if (i !== undefined) {
              pendingShift.current += shiftFor(topsRef.current[i], old, h, el.scrollTop, following.current);
            }
          }
          // Render now, before paint: the new total height must exist before
          // scrollTop moves (else it is clamped), and the window must not be a
          // frame stale. The layout effect below applies pendingShift.
          if (changed) flushSync(() => setVersion((v) => v + 1));
        })
      : null;
    observer.current = rowRO;
    for (const node of nodes.current.values()) rowRO?.observe(node);
    return () => {
      el.removeEventListener("scroll", schedule);
      boxRO?.disconnect();
      rowRO?.disconnect();
      observer.current = null;
      if (frame) cancelAnimationFrame(frame);
    };
  }, []);

  // After every render: stay pinned while following (new rows, measured
  // growth); otherwise apply the correction for rows that grew above the view.
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    if (following.current) {
      el.scrollTop = el.scrollHeight;
      pinned.current = el.scrollTop;
    } else if (pendingShift.current !== 0) {
      el.scrollTop += pendingShift.current;
      pinned.current = el.scrollTop;
    }
    pendingShift.current = 0;
  });

  // Render the end window first; the layout effect then pins. Moving scrollTop
  // before the end rows exist would show blank space until the next frame.
  const toBottom = useCallback(() => {
    setFollowing(true);
    flushSync(() => setVersion((v) => v + 1));
  }, [setFollowing]);
  if (handle) handle.current = {toBottom};

  const onScroll = (ev: UIEvent<HTMLDivElement>) => {
    const el = ev.currentTarget;
    setFollowing(followAfterScroll(el.scrollHeight - el.scrollTop - el.clientHeight, el.scrollTop, pinned.current, following.current));
    pinned.current = el.scrollTop;
    boxProps?.onScroll?.(ev);
  };

  // Forget heights of rows that left the list.
  if (heights.current.size > items.length * 2) {
    for (const k of heights.current.keys()) {
      if (!index.current.has(k)) heights.current.delete(k);
    }
  }

  // While following, the window is the end even before the scroll event lands.
  const viewport = view.height || 1000;
  const total = tops[tops.length - 1];
  const [start, end] = windowOf(tops, following.current ? Math.max(0, total - viewport) : view.top, viewport);
  return (
    <div {...boxProps} ref={box} onScroll={onScroll}>
      <div className="vlist" style={{height: total}}>
        <div style={{transform: `translateY(${tops[start]}px)`}}>
          {items.slice(start, end).map((it) => (
            <Measured key={it.key} item={it} render={render as (item: never) => ReactNode} observe={observe} />
          ))}
        </div>
      </div>
    </div>
  );
}
