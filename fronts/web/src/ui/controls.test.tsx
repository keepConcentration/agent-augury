import assert from "node:assert/strict";
import {after, before, test} from "node:test";
import {GlobalRegistrator} from "@happy-dom/global-registrator";
import {renderToStaticMarkup} from "react-dom/server";
import {ApprovalCard, GateNotice, QuestionPanel, type Approval, type Can} from "./controls.js";

const yes: Can = {act: true, reason: null};
const readOnly: Can = {act: false, reason: "👁 읽기 전용 탭입니다"};
const card = (extra: Partial<Approval> = {}): Approval => ({
  approval_id: "a1",
  agent_id: "bob",
  tool: "run_command",
  args_preview: {command: "ls"},
  revision: 1,
  ttl_seconds: null,
  received_at: Date.now(),
  resolving: false,
  ...extra,
});
const noop = () => {};
const html = (a: Approval, can = yes, extra: {unknown?: boolean; error?: string | null} = {}) =>
  renderToStaticMarkup(
    <ApprovalCard a={a} index={1} total={1} questions={0} can={can} unknown={extra.unknown ?? false} error={extra.error ?? null} onDecide={noop} onClaim={noop} />,
  );

test("approval card: every field visible, key/value cells — a newline in a key cannot forge a row (10 M2)", () => {
  const out = html(
    card({
      agent_id: "<img src=x onerror=alert(1)>",
      tool: "run‮command",
      args_preview: {"cwd\ncommand": "/tmp", command: "echo ok\u001b[2J", n: 3},
    }),
  );
  assert.doesNotMatch(out, /<img/);
  assert.match(out, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(out, /run⟨U\+202E⟩command/);
  assert.equal((out.match(/<tr>/g) ?? []).length, 3, "one row per real key");
  assert.match(out, /<th>cwd·command<\/th>/);
  assert.match(out, /echo ok·\[2J/);
  assert.match(out, /<td>3<\/td>/);
});

test("approval card: buttons start locked; read-only has no Approve; folded long values; changed content", () => {
  const fresh = html(card());
  assert.match(fresh, /<button disabled="">✕ Deny \(d\)<\/button>/);
  assert.match(fresh, /<button disabled="">✓ Approve… \(a\)<\/button>/);
  const ro = html(card(), readOnly);
  assert.doesNotMatch(ro, /Approve/);
  assert.match(ro, /조작 탭에서만 승인할 수 있습니다/);
  const long = html(card({args_preview: {content: "x".repeat(500)}}));
  assert.match(long, /… \(\+200 chars\)/);
  assert.match(long, />전체 보기</);
  assert.match(html(card({revision: 3})), /내용이 변경됨 \(rev 3\)/);
  assert.match(html(card({resolving: true}), yes, {unknown: true}), /결과 확인 중/);
  assert.match(html(card(), yes, {error: "decision must be granted|denied"}), /decision must be granted\|denied/);
});

test("question panel: safe Markdown question, numbered options, read-only disables", () => {
  const q = {question_id: "q1", agent_id: "bob", thread_id: "t1", question: "**pick** one <script>x</script>", options: ["a", "b"], resolving: false};
  const out = renderToStaticMarkup(<QuestionPanel q={q} index={1} total={2} can={yes} onAnswer={noop} onSkip={noop} onClaim={noop} />);
  assert.doesNotMatch(out, /<script/);
  assert.match(out, /<strong>pick<\/strong>/);
  assert.match(out, /\[1\] a/);
  assert.match(out, /1\/2/);
  const ro = renderToStaticMarkup(<QuestionPanel q={q} index={1} total={1} can={readOnly} onAnswer={noop} onSkip={noop} onClaim={noop} />);
  assert.equal((ro.match(/disabled=""/g) ?? []).length, 2, "both options disabled");
});

test("gate notice: escaped text, idle warning", () => {
  const out = renderToStaticMarkup(
    <GateNotice pending={{phase: "P2", thread_id: "t", text: "<b>approve</b>"}} summary="P2 · awaiting human APPROVE:" idle can={yes} onPrefix={noop} />,
  );
  assert.match(out, /&lt;b&gt;approve&lt;\/b&gt;/);
  assert.match(out, /idle 상태라 스레드 지정이 전달되지 않습니다/);
});

// ---- Clicks in a DOM: the locks and the two-step confirm (10 M1) --------------------------

let ui: {render: (el: React.ReactElement) => void; root: HTMLElement; unmount: () => void};

before(async () => {
  GlobalRegistrator.register();
  const {createRoot} = await import("react-dom/client");
  const {act} = await import("react");
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
  const root = document.createElement("div");
  document.body.appendChild(root);
  const r = createRoot(root);
  ui = {
    render: (el) => act(() => r.render(el)),
    root,
    unmount: () => act(() => r.unmount()),
  };
});

after(async () => {
  ui.unmount();
  await GlobalRegistrator.unregister();
});

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function click(label: RegExp) {
  const {act} = await import("react");
  const button = [...ui.root.querySelectorAll("button")].find((b) => label.test(b.textContent ?? ""));
  assert.ok(button, `no button ${label}`);
  await act(async () => button!.click());
}
async function tick(ms: number) {
  const {act} = await import("react");
  await act(async () => wait(ms));
}

test("clicks: locked right after appearing; Approve needs a second, separate click after its own lock", async () => {
  const decisions: string[] = [];
  const el = (a: Approval) => (
    <ApprovalCard a={a} index={1} total={1} questions={0} can={yes} unknown={false} error={null} onDecide={(d) => decisions.push(d)} onClaim={noop} />
  );
  ui.render(el(card()));
  await click(/Approve…/); // within 600 ms: ignored
  assert.equal(ui.root.querySelector(".confirm-row"), null);
  await tick(700);
  await click(/Approve…/);
  assert.ok(ui.root.querySelector(".confirm-row"), "now asking to confirm");
  assert.deepEqual(decisions, [], "the first click never approves");
  await click(/확정/); // within 300 ms of the confirm step: a double click cannot pass
  assert.deepEqual(decisions, []);
  await tick(500);
  await click(/확정/);
  assert.deepEqual(decisions, ["granted"]);
});

test("clicks: content changing mid-confirm resets the step and locks again (10 M3)", async () => {
  const decisions: string[] = [];
  const el = (a: Approval) => (
    <ApprovalCard a={a} index={1} total={1} questions={0} can={yes} unknown={false} error={null} onDecide={(d) => decisions.push(d)} onClaim={noop} />
  );
  ui.render(el(card({approval_id: "a9", revision: 1})));
  await tick(700);
  await click(/Approve…/);
  assert.ok(ui.root.querySelector(".confirm-row"));
  ui.render(el(card({approval_id: "a9", revision: 2, args_preview: {command: "rm -rf /"}})));
  assert.equal(ui.root.querySelector(".confirm-row"), null, "confirmation reset");
  await click(/Deny/); // locked again
  assert.deepEqual(decisions, []);
  await tick(700);
  await click(/Deny/);
  assert.deepEqual(decisions, ["denied"], "Deny needs no confirmation");
});

async function key(target: Element, k: string) {
  const {act} = await import("react");
  await act(async () => {
    target.dispatchEvent(new KeyboardEvent("keydown", {key: k, bubbles: true}));
  });
}

test("keys: Enter alone never approves; Enter on the cancel button cancels; a → Enter on the card approves (N-012)", async () => {
  const decisions: string[] = [];
  const el = (a: Approval) => (
    <ApprovalCard a={a} index={1} total={1} questions={0} can={yes} unknown={false} error={null} onDecide={(d) => decisions.push(d)} onClaim={noop} />
  );
  ui.render(el(card({approval_id: "k1"})));
  await tick(700);
  const section = () => ui.root.querySelector("section.card.approval")!;
  await key(section(), "Enter");
  assert.deepEqual(decisions, [], "Enter without the 'a' step");
  await key(section(), "a");
  assert.ok(ui.root.querySelector(".confirm-row"));
  assert.equal(document.activeElement, section(), "confirm step takes focus (Esc works after a mouse click)");
  await tick(400);
  // Tab to "취소" and press Enter there: must not approve.
  const cancel = [...ui.root.querySelectorAll("button")].find((b) => /취소/.test(b.textContent ?? ""))!;
  await key(cancel, "Enter");
  assert.deepEqual(decisions, [], "Enter on the cancel button is not an approval");
  await key(section(), "Enter");
  assert.deepEqual(decisions, ["granted"]);
});

test("keys: Esc cancels the confirm step; a/d do nothing on a read-only tab", async () => {
  const decisions: string[] = [];
  const el = (a: Approval, can: Can) => (
    <ApprovalCard a={a} index={1} total={1} questions={0} can={can} unknown={false} error={null} onDecide={(d) => decisions.push(d)} onClaim={noop} />
  );
  ui.render(el(card({approval_id: "k2"}), yes));
  await tick(700);
  const section = () => ui.root.querySelector("section.card.approval")!;
  await key(section(), "a");
  await key(section(), "Escape");
  assert.equal(ui.root.querySelector(".confirm-row"), null);
  ui.render(el(card({approval_id: "k3"}), readOnly));
  await tick(700);
  await key(section(), "a");
  await key(section(), "d");
  assert.deepEqual(decisions, []);
});

test("gaining control locks the card again: the claiming click cannot land on Deny", async () => {
  const decisions: string[] = [];
  const el = (can: Can) => (
    <ApprovalCard a={card({approval_id: "k4"})} index={1} total={1} questions={0} can={can} unknown={false} error={null} onDecide={(d) => decisions.push(d)} onClaim={noop} />
  );
  ui.render(el(readOnly));
  await tick(700);
  ui.render(el(yes)); // just became the controller
  await click(/Deny/);
  assert.deepEqual(decisions, []);
  await tick(700);
  await click(/Deny/);
  assert.deepEqual(decisions, ["denied"]);
});

test("changed arguments are highlighted after a re-issue with new content (10 M3)", async () => {
  const el = (a: Approval) => (
    <ApprovalCard a={a} index={1} total={1} questions={0} can={yes} unknown={false} error={null} onDecide={noop} onClaim={noop} />
  );
  ui.render(el(card({approval_id: "k5", revision: 1, args_preview: {command: "ls", cwd: "/w"}})));
  ui.render(el(card({approval_id: "k5", revision: 2, args_preview: {command: "rm -rf /", cwd: "/w"}})));
  const rows = [...ui.root.querySelectorAll("table.args tr")].map((tr) => [tr.querySelector("th")?.textContent, tr.className]);
  assert.deepEqual(rows, [["command", "changed-cell"], ["cwd", ""]]);
});

// ---- Review fixes: revision, folded/clipped values, external reset, question keys ------------

test("decisions carry the revision the card showed (stale-approval guard)", async () => {
  const got: [string, number][] = [];
  ui.render(
    <ApprovalCard a={card({approval_id: "r1", revision: 4})} index={1} total={1} questions={0} can={yes} unknown={false} error={null} onDecide={(d, rev) => got.push([d, rev])} onClaim={noop} />,
  );
  await tick(700);
  await click(/Deny/);
  assert.deepEqual(got, [["denied", 4]]);
});

test("folded values: Approve stays locked until every one is opened; Deny never waits", async () => {
  const decisions: string[] = [];
  const el = (a: Approval) => (
    <ApprovalCard a={a} index={1} total={1} questions={0} can={yes} unknown={false} error={null} onDecide={(d) => decisions.push(d)} onClaim={noop} />
  );
  ui.render(el(card({approval_id: "f1", args_preview: {content: "x".repeat(500), other: "y".repeat(400)}})));
  await tick(700);
  const approveBtn = () => [...ui.root.querySelectorAll("button")].find((b) => /Approve…/.test(b.textContent ?? ""))!;
  assert.equal(approveBtn().disabled, true);
  assert.match(ui.root.textContent ?? "", /접힌 값 2개/);
  await key(ui.root.querySelector("section.card.approval")!, "a");
  assert.equal(ui.root.querySelector(".confirm-row"), null, "the 'a' key is locked too");
  const opens = () => [...ui.root.querySelectorAll("button")].filter((b) => b.textContent === "전체 보기");
  const {act} = await import("react");
  await act(async () => opens()[0].click());
  assert.equal(approveBtn().disabled, true, "one still folded");
  await act(async () => opens()[0].click());
  assert.equal(approveBtn().disabled, false);
  await click(/Approve…/);
  await tick(400);
  // Folding one again during the confirm step locks 확정.
  await act(async () => [...ui.root.querySelectorAll("button")].find((b) => b.textContent === "접기")!.click());
  await click(/확정/);
  assert.deepEqual(decisions, []);
  ui.render(el(card({approval_id: "f2", args_preview: {content: "x".repeat(500)}})));
  await tick(700);
  await click(/Deny/);
  assert.deepEqual(decisions, ["denied"], "Deny with values still folded");
});

test("clipped values: header warns and 확정 needs an extra tick", async () => {
  const decisions: string[] = [];
  const a = card({approval_id: "t1", args_preview: {content: `${"y".repeat(20)}… (+5000 chars)`}});
  ui.render(<ApprovalCard a={a} index={1} total={1} questions={0} can={yes} unknown={false} error={null} onDecide={(d) => decisions.push(d)} onClaim={noop} />);
  assert.match(ui.root.querySelector("header")!.textContent ?? "", /일부만 표시됨 — 전체 내용을 볼 수 없음/);
  await tick(700);
  await click(/Approve…/);
  await tick(400);
  await click(/확정/);
  assert.deepEqual(decisions, [], "no approval without the tick");
  const box = ui.root.querySelector<HTMLInputElement>('.confirm-row input[type="checkbox"]')!;
  const {act} = await import("react");
  await act(async () => box.click());
  await click(/확정/);
  assert.deepEqual(decisions, ["granted"]);
  assert.doesNotMatch(html(card()), /일부만 표시됨/);
});

test("reset from the app drops the confirm step (Esc outside the card, stale_revision)", async () => {
  const el = (reset: number) => (
    <ApprovalCard a={card({approval_id: "e1"})} index={1} total={1} questions={0} can={yes} unknown={false} error={null} reset={reset} onDecide={noop} onClaim={noop} />
  );
  ui.render(el(0));
  await tick(700);
  await click(/Approve…/);
  assert.ok(ui.root.querySelector(".confirm-row"));
  ui.render(el(1));
  assert.equal(ui.root.querySelector(".confirm-row"), null);
});

test("question panel: Cmd/Ctrl/Alt combos and held keys do nothing; a new question locks briefly", async () => {
  const acts: string[] = [];
  const q = (id: string) => ({question_id: id, agent_id: "bob", thread_id: null, question: "pick", options: ["a", "b"], resolving: false});
  const el = (id: string) => (
    <QuestionPanel q={q(id)} index={1} total={1} can={yes} onAnswer={(c) => acts.push(`answer ${c}`)} onSkip={() => acts.push("skip")} onClaim={noop} />
  );
  ui.render(el("q1"));
  const panel = () => ui.root.querySelector("section.card.question")!;
  const {act} = await import("react");
  const press = (init: KeyboardEventInit) => act(async () => void panel().dispatchEvent(new KeyboardEvent("keydown", {bubbles: true, ...init})));
  await press({key: "s"});
  assert.deepEqual(acts, [], "locked right after appearing");
  await tick(700);
  await press({key: "s", metaKey: true});
  await press({key: "s", ctrlKey: true});
  await press({key: "1", altKey: true});
  await press({key: "1", repeat: true});
  assert.deepEqual(acts, []);
  await press({key: "1"});
  assert.deepEqual(acts, ["answer 1"]);
  ui.render(el("q2"));
  await press({key: "2"});
  assert.deepEqual(acts, ["answer 1"], "next question: the same key cannot answer it at once");
  await tick(700);
  await press({key: "2"});
  assert.deepEqual(acts, ["answer 1", "answer 2"]);
});

test("gate notice text is clipped and made visible like every other field", () => {
  const out = renderToStaticMarkup(
    <GateNotice pending={{phase: "P2", thread_id: "t", text: `go‮${"z".repeat(2100)}`}} summary={null} idle={false} can={yes} onPrefix={noop} />,
  );
  assert.match(out, /go⟨U\+202E⟩/);
  assert.match(out, /… \(\+\d+ chars\)/);
});
