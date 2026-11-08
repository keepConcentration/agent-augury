import assert from "node:assert/strict";
import {after, before, test} from "node:test";
import {GlobalRegistrator} from "@happy-dom/global-registrator";
import {BOTTOM_SLOP_PX, ESTIMATE_PX, VirtualList, followAfterScroll, layout, rowAt, shiftFor, windowOf} from "./virtual.js";

test("layout: measured heights where known, the estimate elsewhere", () => {
  const tops = layout([5, 6, 7], new Map([[6, 100]]));
  assert.deepEqual(tops, [0, ESTIMATE_PX, ESTIMATE_PX + 100, 2 * ESTIMATE_PX + 100]);
  assert.deepEqual(layout([], new Map()), [0]);
});

test("rowAt: the row containing a pixel, clamped at both ends", () => {
  const tops = [0, 10, 30, 60];
  assert.equal(rowAt(tops, -5), 0);
  assert.equal(rowAt(tops, 0), 0);
  assert.equal(rowAt(tops, 9.9), 0);
  assert.equal(rowAt(tops, 10), 1);
  assert.equal(rowAt(tops, 59), 2);
  assert.equal(rowAt(tops, 1000), 2);
  assert.equal(rowAt([0], 5), 0);
});

test("windowOf: viewport plus overscan on both sides, never past the list", () => {
  const tops = layout(
    Array.from({length: 1000}, (_, i) => i),
    new Map(),
    10,
  );
  assert.deepEqual(windowOf(tops, 5000, 100, 50), [495, 516]);
  assert.deepEqual(windowOf(tops, 0, 100, 50), [0, 16]);
  assert.deepEqual(windowOf(tops, 9_960, 100, 50), [991, 1000]);
  assert.deepEqual(windowOf([0], 0, 100), [0, 0]);
});

test("shiftFor: only a row wholly above the viewport moves scrollTop, and not while following", () => {
  assert.equal(shiftFor(100, 24, 200, 500, false), 176, "above: grow");
  assert.equal(shiftFor(100, 200, 50, 500, false), -150, "above: shrink");
  assert.equal(shiftFor(476, 24, 200, 500, false), 176, "bottom edge exactly at the view top counts as above");
  assert.equal(shiftFor(490, 24, 200, 500, false), 0, "straddling the top: the user sees it grow");
  assert.equal(shiftFor(800, 24, 200, 500, false), 0, "below the top");
  assert.equal(shiftFor(100, 24, 200, 500, true), 0, "following: the pin handles it");
});

test("followAfterScroll: end follows; only a move up leaves; growth under a pin does not", () => {
  assert.equal(followAfterScroll(0, 1000, 1000, false), true, "at the end");
  assert.equal(followAfterScroll(BOTTOM_SLOP_PX - 1, 990, 1000, true), true, "small move up inside the slop keeps following");
  assert.equal(followAfterScroll(300, 700, 1000, true), false, "user scrolled up");
  assert.equal(followAfterScroll(300, 1000, 1000, true), true, "content grew below a pin: still following");
  assert.equal(followAfterScroll(300, 1200, 1000, false), false, "scrolling down short of the end: still not following");
});

type Item = {key: number; text: string};
type Mounted = {render: (el: React.ReactElement) => void; root: HTMLElement; unmount: () => void};
let createRoot: typeof import("react-dom/client").createRoot;
let act: typeof import("react").act;

/** A fresh host per test, removed again by ``unmount``. */
function mount(): Mounted {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const r = createRoot(root);
  return {
    render: (el) => act(() => r.render(el)),
    root,
    unmount: () => {
      act(() => r.unmount());
      root.remove();
    },
  };
}

/** Records observers so a test can drive measuring by hand. */
class FakeRO {
  static made: FakeRO[] = [];
  observed = new Set<Element>();
  live = true;
  constructor(readonly cb: ResizeObserverCallback) {
    FakeRO.made.push(this);
  }
  observe(el: Element) {
    if (this.live) this.observed.add(el);
  }
  unobserve(el: Element) {
    this.observed.delete(el);
  }
  disconnect() {
    this.live = false;
    this.observed.clear();
  }
}

async function withFakeRO(fn: () => void | Promise<void>) {
  const real = globalThis.ResizeObserver;
  FakeRO.made = [];
  globalThis.ResizeObserver = FakeRO as unknown as typeof ResizeObserver;
  try {
    await fn();
  } finally {
    globalThis.ResizeObserver = real;
  }
}

before(async () => {
  GlobalRegistrator.register();
  ({createRoot} = await import("react-dom/client"));
  ({act} = await import("react"));
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
});

after(async () => {
  await GlobalRegistrator.unregister();
});

test("VirtualList: 5000 rows put a bounded window in the DOM, starting at the end; rows are not re-rendered", () => {
  const ui = mount();
  let renders = 0;
  const render = (it: Item) => {
    renders++;
    return <div className="row">{it.text}</div>;
  };
  const items = Array.from({length: 5000}, (_, i) => ({key: i + 1, text: `row ${i + 1}`}));
  ui.render(<VirtualList items={items} render={render} boxProps={{className: "timeline"}} />);
  const keys = () => Array.from(ui.root.querySelectorAll<HTMLElement>("[data-key]"), (n) => Number(n.dataset.key));
  const first = keys();
  // No layout in happy-dom: viewport falls back to 1000px, rows to the estimate.
  assert.ok(first.length > 0 && first.length < 200, `window size ${first.length}`);
  assert.equal(first.at(-1), 5000, "following: the window is the end");
  assert.equal(ui.root.querySelector<HTMLElement>(".vlist")!.style.height, `${5000 * ESTIMATE_PX}px`);

  // One more row: it joins the window and only it renders (memoized rows).
  renders = 0;
  ui.render(<VirtualList items={[...items, {key: 5001, text: "row 5001"}]} render={render} boxProps={{className: "timeline"}} />);
  assert.equal(keys().at(-1), 5001);
  assert.equal(renders, 1, "existing rows kept");
  assert.equal(keys().includes(1), false, "the start is not in the DOM");
  ui.unmount();
});

test("VirtualList under StrictMode: every rendered row stays observed after the effect re-runs (review M1)", async () => {
  const {StrictMode} = await import("react");
  await withFakeRO(() => {
    const ui = mount();
    const items = Array.from({length: 10}, (_, i) => ({key: i + 1, text: `row ${i + 1}`}));
    ui.render(
      <StrictMode>
        <VirtualList items={items} render={(it: Item) => <div>{it.text}</div>} />
      </StrictMode>,
    );
    const rows = Array.from(ui.root.querySelectorAll("[data-key]"));
    assert.equal(rows.length, 10);
    const watching = (el: Element) => FakeRO.made.some((ro) => ro.live && ro.observed.has(el));
    assert.ok(rows.every(watching), "a live ResizeObserver watches every row");
    ui.unmount();
    assert.ok(FakeRO.made.every((ro) => !ro.live), "unmount disconnects");
  });
});

test("VirtualList: a new list reusing old keys does not inherit their measured heights (BFF restart)", async () => {
  await withFakeRO(() => {
    const ui = mount();
    const render = (it: Item) => <div>{it.text}</div>;
    const list = (n: number, tag: string) => Array.from({length: n}, (_, i) => ({key: i + 1, text: `${tag} ${i + 1}`}));
    ui.render(<VirtualList items={list(10, "old")} render={render} />);
    // Measure every row at 100px through the row observer (the one watching rows).
    const rowRO = FakeRO.made.find((ro) => ro.live && ro.observed.size > 1)!;
    const nodes = [...rowRO.observed] as HTMLElement[];
    for (const n of nodes) Object.defineProperty(n, "offsetHeight", {configurable: true, value: 100});
    act(() => rowRO.cb(nodes.map((target) => ({target}) as unknown as ResizeObserverEntry), rowRO as unknown as ResizeObserver));
    const height = () => ui.root.querySelector<HTMLElement>(".vlist")!.style.height;
    assert.equal(height(), "1000px", "measured");

    ui.render(<VirtualList items={list(3, "new")} render={render} />);
    assert.equal(height(), `${3 * ESTIMATE_PX}px`, "the new rows start from the estimate");
    ui.unmount();
  });
});
