// Builds SwiftServe_Implementation_and_Policies.pptx: the policy explanations and the implementation walkthrough.
// Same theme and helpers as build_deck.js.
// Builds SwiftServe_Presentation.pptx.
//
//   NODE_PATH=<dir containing node_modules with pptxgenjs, react-icons, react, react-dom, sharp> \
//     node docs/presentation/build_deck.js
//
// Numbers on the results slides come from measurements recorded in
// docs/COMPLETE_PROJECT_WALKTHROUGH.md. The policy-comparison slide is built only
// when results.json (written by scripts/demo.py --json-out) holds real measurements for at least two policies.

const fs = require("fs");
const path = require("path");
const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const fa = require("react-icons/fa");

const SKILL = process.env.PPTX_SKILL_DIR || "/root/.claude/skills/synced/d42a66c5-f281-456b-90d7-b4667f05a877_9e64ec46-1921-4a4d-85b5-eb3a05789030/pptx";
const { applyTheme } = require(path.join(SKILL, "scripts/apply_theme.js"));

const HERE = __dirname;
const OUT = process.env.DECK_OUT || path.join(HERE, "SwiftServe_Implementation_and_Policies.pptx");

const THEME = {
  name: "Graphite Teal",
  headFontFace: "Cambria",
  bodyFontFace: "Calibri",
  colors: {
    dk1: "1B1F2A", lt1: "FFFFFF", dk2: "3A4457", lt2: "F1F3F6",
    accent1: "0E8F7E", accent2: "E8A23B", accent3: "5878A8", accent4: "8B95A7", accent5: "C8445A", accent6: "3E9E5F",
    hlink: "0E8F7E", folHlink: "8B95A7",
  },
};
const HEX = THEME.colors;

const pres = new pptxgen();
pres.layout = "LAYOUT_16x9"; // 10 x 5.625 in
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.author = " ";
pres.title = "Implementation and Routing Policies";
pres.subject = "How the router chooses a replica and how it is built";
const C = pres.SchemeColor;

// ---- layouts -------------------------------------------------------------------------------
pres.defineSlideMaster({
  title: "DARK",
  background: { color: HEX.dk1 },
  objects: [],
});
pres.defineSlideMaster({
  title: "CONTENT",
  background: { color: HEX.lt1 },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.3, w: 9, h: 0.8, fontFace: THEME.headFontFace, fontSize: 32, bold: true, color: C.text1, margin: 0, valign: "middle", align: "left" }, text: "" } },
  ],
  slideNumber: { x: 9.1, y: 5.2, w: 0.5, h: 0.3, fontFace: THEME.bodyFontFace, fontSize: 10, color: C.accent4 },
});

// ---- helpers ---------------------------------------------------------------------------------
async function icon(Comp, color, size = 256) {
  const svg = ReactDOMServer.renderToStaticMarkup(React.createElement(Comp, { color: "#" + color, size: String(size) }));
  const buf = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + buf.toString("base64");
}
const shadow = () => ({ type: "outer", color: "000000", opacity: 0.12, blur: 8, offset: 2, angle: 90 });

function text(slide, str, o) {
  slide.addText(str, { isTextBox: true, margin: 0, fontFace: THEME.bodyFontFace, color: C.text1, valign: "top", ...o });
}
function card(slide, x, y, w, h, name) {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.08, fill: { color: C.background2 }, line: { color: C.background2, width: 0 }, shadow: shadow(), objectName: name });
}
async function iconCircle(slide, Comp, x, y, d, fillColor, name) {
  slide.addShape(pres.shapes.OVAL, { x, y, w: d, h: d, fill: { color: fillColor }, line: { color: fillColor, width: 0 }, objectName: name + " badge" });
  const p = d * 0.26;
  slide.addImage({ data: await icon(Comp, "FFFFFF"), x: x + p, y: y + p, w: d - 2 * p, h: d - 2 * p, objectName: name + " icon" });
}
function arrow(slide, x1, y1, x2, y2, color = C.accent4, name = "arrow") {
  slide.addShape(pres.shapes.LINE, {
    x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1), h: Math.abs(y2 - y1),
    flipV: y2 < y1, line: { color, width: 1.75, endArrowType: "triangle" }, objectName: name,
  });
}
function imageFit(slide, file, x, y, maxW, maxH, name) {
  const { width, height } = require("child_process").execFileSync("python3", ["-c", `from PIL import Image;import sys;im=Image.open(sys.argv[1]);print(im.size[0],im.size[1])`, file], { encoding: "utf8" })
    .trim().split(" ").reduce((a, v, i) => ({ ...a, [i ? "height" : "width"]: +v }), {});
  const s = Math.min(maxW / width, maxH / height);
  const w = width * s, h = height * s;
  slide.addImage({ path: file, x: x + (maxW - w) / 2, y: y + (maxH - h) / 2, w, h, objectName: name, altText: name, shadow: shadow() });
}
function addSlide(section, layout = "CONTENT") {
  return pres.addSlide({ masterName: layout, sectionTitle: section });
}
function title(slide, str) { slide.addText(str, { placeholder: "title" }); }

// Measurements for the evidence slide: every *.json in docs/presentation/runs/ is one run.
const RUNS_DIR = path.join(HERE, "runs");
const RUNS = fs.existsSync(RUNS_DIR) ? fs.readdirSync(RUNS_DIR).filter((f) => f.endsWith(".json")).sort().map((f) => JSON.parse(fs.readFileSync(path.join(RUNS_DIR, f), "utf8"))) : [];

(async () => {
  for (const s of ["Policies", "Implementation"]) pres.addSection({ title: s });

  const POLS = ["round_robin", "least_connections", "swiftserve", "prefix_aware"];
  const head = (t, size = 13) => ({ text: t, options: { bold: true, color: C.background1, fill: { color: C.text1 }, fontSize: size, fontFace: THEME.bodyFontFace, valign: "middle" } });
  const cell = (t, i, o = {}) => ({ text: String(t), options: { fontSize: 12, fontFace: THEME.bodyFontFace, color: C.text1, valign: "middle", fill: { color: i % 2 ? C.background1 : C.background2 }, ...o } });
  const numberedRow = (s, i, y, d = 0.46) => {
    s.addShape(pres.shapes.OVAL, { x: 0.5, y: y + 0.08, w: d, h: d, fill: { color: C.accent1 }, line: { color: C.accent1, width: 0 }, objectName: `Number ${i + 1}` });
    text(s, String(i + 1), { x: 0.5, y: y + 0.08, w: d, h: d, fontSize: 15, bold: true, color: C.background1, align: "center", valign: "middle" });
  };
  const bullets = (s, items, o) => text(s, items.map((t, i) => (typeof t === "string" ? { text: t, options: { bullet: true, breakLine: i < items.length - 1 } } : { text: t.text, options: { bullet: true, bold: !!t.bold, breakLine: i < items.length - 1 } })), { fontSize: 14, paraSpaceAfter: 8, ...o });
  const leadBullets = (s, items, o) => text(s, items.flatMap(([lead, body], i) => [{ text: lead + " ", options: { bullet: true, bold: true } }, { text: body, options: { breakLine: i < items.length - 1 } }]), { fontSize: 14, paraSpaceAfter: 9, ...o });
  const codeTag = (s, label, x, y, w) => {
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h: 0.34, rectRadius: 0.05, fill: { color: C.text1 }, line: { color: C.text1, width: 0 }, objectName: "Code reference" });
    text(s, label, { x, y, w, h: 0.34, fontFace: "Courier New", fontSize: 11, color: C.background1, align: "center", valign: "middle" });
  };

  // ============ 1. Section title ============
  {
    const s = addSlide("Policies", "DARK");
    text(s, "Implementation and Routing Policies", { x: 0.7, y: 1.4, w: 6.2, h: 1.6, fontFace: THEME.headFontFace, fontSize: 38, bold: true, color: C.background1 });
    text(s, "How each policy chooses a replica, and how the router is built around them", { x: 0.7, y: 3.3, w: 5.7, h: 0.9, fontSize: 18, color: C.accent4 });
    [[7.35, 1.55], [8.55, 1.55], [7.35, 2.75], [8.55, 2.75]].forEach(([x, y], i) => {
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: 1.0, h: 1.0, rectRadius: 0.1, fill: { color: i === 3 ? C.accent1 : C.text2 }, line: { color: i === 3 ? C.accent1 : C.text2, width: 0 }, objectName: `Policy tile ${i + 1}` });
      text(s, String(i + 1), { x, y: y + 0.3, w: 1.0, h: 0.4, fontSize: 20, bold: true, color: C.background1, align: "center" });
    });
    s.addNotes("This section covers two things: the four routing policies one by one, and how the router around them is implemented. The four tiles stand for the four policies, ordered from the simplest to the one that matches token blocks.");
  }

  // ============ 2. Ladder ============
  {
    const s = addSlide("Policies");
    title(s, "Four policies, each adding one idea");
    const steps = [["round_robin", "Takes replicas in turn", "Baseline: knows nothing"], ["least_connections", "Picks the least busy replica", "Adds load awareness"], ["swiftserve", "Remembers sessions and message prefixes", "Adds conversation memory"], ["prefix_aware", "Matches 16-token blocks of the prompt", "Adds the unit the engine caches"]];
    steps.forEach(([n, d, a], i) => {
      const x = 0.5 + i * 2.28, y = 2.7 - i * 0.35;
      card(s, x, y, 2.1, 2.35 + i * 0.35 - 0.0, `Policy card ${i + 1}`);
      text(s, n, { x: x + 0.12, y: y + 0.15, w: 1.9, h: 0.4, fontSize: 15, bold: true, color: i === 3 ? C.accent1 : C.text1 });
      text(s, d, { x: x + 0.12, y: y + 0.65, w: 1.9, h: 0.95, fontSize: 14, color: C.text2 });
      text(s, a, { x: x + 0.12, y: y + 1.65, w: 1.9, h: 0.6, fontSize: 13, bold: true, color: C.accent1 });
    });
    text(s, "Each policy is a class in swiftserve/policy.py with one method, select(). The router calls whichever policy is active.", { x: 0.5, y: 1.2, w: 9, h: 0.6, fontSize: 14, color: C.text2 });
    s.addNotes("The four policies form a ladder. Round robin knows nothing. Least connections adds load awareness. Swiftserve adds memory of sessions and of message-level prefixes. Prefix-aware moves that memory to the unit vLLM actually caches, which is blocks of sixteen tokens. Each is a class in policy.py with a select method, so they are interchangeable and can be compared on the same traffic.");
  }

  // ============ 3. round_robin ============
  {
    const s = addSlide("Policies");
    title(s, "round_robin");
    codeTag(s, "RoundRobinPolicy.select", 0.5, 1.2, 2.6);
    bullets(s, [
      "Keeps one counter. Request n goes to replica n mod 4.",
      "Uses nothing else: not load, not the prompt, not the session.",
      "Strength: a perfectly even spread. In every run its busiest-to-mean load was 1.00.",
      "Weakness: a request with a cached prefix lands on the replica that holds it only by chance, and over time every replica ends up caching every tenant.",
    ], { x: 0.5, y: 1.75, w: 4.8, h: 3.4 });
    card(s, 5.6, 1.2, 3.9, 3.9, "Rotation panel");
    text(s, "Requests in arrival order", { x: 5.8, y: 1.32, w: 3.5, h: 0.3, fontSize: 13, bold: true, color: C.text2 });
    for (let i = 0; i < 6; i++) {
      const y = 1.75 + i * 0.55;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 5.8, y, w: 1.3, h: 0.42, rectRadius: 0.05, fill: { color: C.text1 }, line: { color: C.text1, width: 0 }, objectName: `Request ${i + 1}` });
      text(s, `Request ${i + 1}`, { x: 5.8, y, w: 1.3, h: 0.42, fontSize: 13, bold: true, color: C.background1, align: "center", valign: "middle" });
      arrow(s, 7.15, y + 0.21, 7.95, y + 0.21, C.accent4, `Arrow ${i + 1}`);
      const rep = i % 4;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 8.0, y, w: 1.3, h: 0.42, rectRadius: 0.05, fill: { color: rep === 0 ? C.accent1 : C.background1 }, line: { color: C.accent1, width: 1 }, objectName: `Replica for ${i + 1}` });
      text(s, `Replica ${"ABCD"[rep]}`, { x: 8.0, y, w: 1.3, h: 0.42, fontSize: 13, bold: true, color: rep === 0 ? C.background1 : C.text1, align: "center", valign: "middle" });
    }
    s.addNotes("Round robin is the simplest baseline. A single counter decides the replica, so requests one to four go to A, B, C and D, and request five goes back to A. It spreads load perfectly evenly, which is why its busiest-to-mean load was exactly one in all three measured runs. But it never looks at the prompt, so whether a request meets a warm cache is luck, and with several tenants every replica ends up holding a bit of every tenant.");
  }

  // ============ 4. least_connections ============
  {
    const s = addSlide("Policies");
    title(s, "least_connections");
    codeTag(s, "LeastConnectionsPolicy.select", 0.5, 1.2, 3.2);
    bullets(s, [
      "Chooses the replica with the smallest queue depth. Ties go to the lowest replica id.",
      "Queue depth is the larger of the replica's running plus waiting requests (scraped from vLLM) and the requests the router has in flight there. If the scrape is older than 5 seconds, the router's own count is used.",
      "Strength: adapts as load changes.",
      "Weakness: it never asks which replica holds the cache.",
    ], { x: 0.5, y: 1.75, w: 4.9, h: 3.4, fontSize: 14 });
    card(s, 5.6, 1.2, 3.9, 3.9, "Queue panel");
    text(s, "Queue depth now (illustrative values)", { x: 5.8, y: 1.32, w: 3.5, h: 0.3, fontSize: 13, bold: true, color: C.text2 });
    const q = [3, 1, 2, 4];
    q.forEach((v, i) => {
      const y = 1.85 + i * 0.75;
      text(s, "Replica " + "ABCD"[i], { x: 5.8, y, w: 1.1, h: 0.45, fontSize: 13, valign: "middle" });
      s.addShape(pres.shapes.RECTANGLE, { x: 6.95, y: y + 0.05, w: v * 0.55, h: 0.35, fill: { color: v === 1 ? C.accent1 : C.accent4 }, line: { color: v === 1 ? C.accent1 : C.accent4, width: 0 }, objectName: `Queue ${"ABCD"[i]}` });
      text(s, String(v), { x: 6.95 + v * 0.55 + 0.1, y, w: 0.4, h: 0.45, fontSize: 13, bold: true, valign: "middle" });
    });
    text(s, "The next request goes to replica B, the shortest queue.", { x: 5.8, y: 4.65, w: 3.5, h: 0.4, fontSize: 13, color: C.text2 });
    s.addNotes("Least connections looks at how busy each replica is and picks the shortest queue, using the lowest replica number to break ties. The queue depth blends two sources: what the replica itself reports about running and waiting requests, and how many requests the router has in flight to it. This adapts to uneven load, which is why its busiest-to-mean ratio was above one in the runs, but it still has no idea which replica holds the cache for the request in hand. The bars here are illustrative numbers.");
  }

  // ============ 5. swiftserve ============
  {
    const s = addSlide("Policies");
    title(s, "swiftserve");
    codeTag(s, "SwiftServePolicy.select", 0.5, 1.2, 2.6);
    const rows = [
      ["Session owner", "Does a replica hold this session's warm cache, and is its estimated latency within the response-time target?", "Yes: route to the owner (session_affinity)"],
      ["Message prefix", "Does the trie show an earlier request on some replica that shares at least 2 whole messages, within the target?", "Yes: route there (prefix_trie)"],
      ["Fallback", "Otherwise: shortest queue, then lowest estimated latency plus a cold-start penalty.", "Route to the least-loaded replica (least_loaded)"],
    ];
    rows.forEach(([h, d, r], i) => {
      const y = 1.7 + i * 1.2;
      numberedRow(s, i, y + 0.1);
      text(s, h, { x: 1.2, y: y + 0.05, w: 1.9, h: 0.9, fontSize: 15, bold: true, valign: "middle" });
      text(s, d, { x: 3.2, y: y + 0.05, w: 3.7, h: 0.95, fontSize: 13, color: C.text2, valign: "middle" });
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 7.1, y: y + 0.1, w: 2.4, h: 0.85, rectRadius: 0.06, fill: { color: i === 2 ? C.text2 : C.accent1 }, line: { color: i === 2 ? C.text2 : C.accent1, width: 0 }, objectName: `Result ${i + 1}` });
      text(s, r, { x: 7.15, y: y + 0.1, w: 2.3, h: 0.85, fontSize: 12, bold: true, color: C.background1, align: "center", valign: "middle" });
    });
    s.addNotes("Swiftserve is the earlier policy. It tries three things in order. First, if some replica served this session recently and is still within the response-time target, send the request back to it. Second, consult a prefix trie over whole messages: if an earlier request on some replica shared at least two messages with this one, and that replica is within target, route there. Third, fall back to the least-loaded replica. Every call records the request's messages into the trie, so it keeps learning from traffic.");
  }

  // ============ 6. swiftserve limitation ============
  {
    const s = addSlide("Policies");
    title(s, "swiftserve: why messages are the wrong unit");
    card(s, 0.5, 1.25, 4.4, 1.7, "Case A");
    text(s, "Case A: shared system prompt only", { x: 0.7, y: 1.35, w: 4.0, h: 0.35, fontSize: 15, bold: true });
    text(s, "Two requests share one 2,000-token system prompt and differ in the user message. Match depth is 1 message, below the minimum of 2, so the match is rejected.", { x: 0.7, y: 1.75, w: 4.0, h: 1.1, fontSize: 13, color: C.text2 });
    card(s, 5.1, 1.25, 4.4, 1.7, "Case B");
    text(s, "Case B: two short shared messages", { x: 5.3, y: 1.35, w: 4.0, h: 0.35, fontSize: 15, bold: true });
    text(s, "Two requests share a short system line and a short greeting. Match depth is 2 messages, so the match is accepted, although almost no tokens can be reused.", { x: 5.3, y: 1.75, w: 4.0, h: 1.1, fontSize: 13, color: C.text2 });
    bullets(s, [
      "Message count has no relation to what the engine can reuse, which is token blocks. This is the finding that motivates prefix_aware.",
      "In our workload (6 tenants, 8 questions) the same tenant and question pair repeats, so a depth-2 match does happen. That is a likely reason swiftserve reused more cache than the baselines (66% against 52 to 53%). This follows from the code; it was not checked request by request.",
    ], { x: 0.5, y: 3.2, w: 9, h: 2.0, fontSize: 14 });
    s.addNotes("The weakness of swiftserve is the unit it matches on. It counts messages, but the engine reuses blocks of tokens. A 2,000-token system prompt shared by two sessions is a depth of one and is rejected, while two trivial shared messages are accepted. In our experiment the same tenant and question pair does repeat, so depth-two matches happen, which probably explains why swiftserve reused more cache than the baselines. That explanation follows from reading the code and has not been verified per request.");
  }

  // ============ 7. prefix_aware steps ============
  {
    const s = addSlide("Policies");
    title(s, "prefix_aware: the decision");
    codeTag(s, "PrefixAwarePolicy.select_with_prediction", 0.5, 1.15, 4.0);
    const steps = [
      ["Tokenize and hash", "encode_chat applies the model's chat template; block_hashes cuts the tokens into 16-token blocks and chains their hashes."],
      ["Look up holders", "index.match returns, for each replica, how many leading blocks it holds."],
      ["Check eligibility", "Eligible if the match covers at least 50% of the prompt blocks, or at least 256 tokens."],
      ["Take the best", "Longest match wins. Ties go to the shorter queue, then the lower replica id."],
      ["Check the target", "If the winner's latency estimate is within the SLA, route there. Otherwise fall back to the least-loaded replica."],
      ["Record the belief", "The prompt's hashes are inserted for the chosen replica at once, before the response."],
    ];
    steps.forEach(([h, d], i) => {
      const y = 1.58 + i * 0.6;
      numberedRow(s, i, y - 0.06, 0.4);
      text(s, h, { x: 1.1, y, w: 2.2, h: 0.5, fontSize: 14, bold: true, valign: "middle" });
      text(s, d, { x: 3.3, y, w: 6.2, h: 0.55, fontSize: 12, color: C.text2, valign: "middle" });
    });
    s.addNotes("This is the prefix-aware decision, step by step, with the function that does each. The prompt is tokenized with the model's chat template and cut into blocks of sixteen tokens whose hashes are chained. The index returns how many leading blocks each replica holds. A replica is eligible only if the match is big enough to matter: half the prompt, or at least 256 tokens. The longest match wins, with ties going to the shorter queue. If the winner's latency estimate is within the response-time target it is chosen; otherwise the least-loaded replica is used. Finally the prompt's hashes are recorded against the chosen replica immediately.");
  }

  // ============ 8. chained hashing ============
  {
    const s = addSlide("Policies");
    title(s, "prefix_aware: chained block hashes");
    codeTag(s, "swiftserve/prefix_index.py", 0.5, 1.15, 2.7);
    ["Block 0", "Block 1", "Block 2", "Block 3"].forEach((b, i) => {
      const x = 0.5 + i * 2.28;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 1.6, w: 2.0, h: 0.55, rectRadius: 0.06, fill: { color: C.background2 }, line: { color: C.accent4, width: 1 }, objectName: b });
      text(s, b + ": 16 tokens", { x, y: 1.6, w: 2.0, h: 0.55, fontSize: 13, bold: true, align: "center", valign: "middle" });
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 2.4, w: 2.0, h: 0.55, rectRadius: 0.06, fill: { color: i === 0 ? C.accent1 : C.text1 }, line: { color: i === 0 ? C.accent1 : C.text1, width: 0 }, objectName: `Hash ${i}` });
      text(s, i === 0 ? "h0 = H(namespace, b0)" : `h${i} = H(h${i - 1}, b${i})`, { x, y: 2.4, w: 2.0, h: 0.55, fontFace: "Courier New", fontSize: 11, color: C.background1, align: "center", valign: "middle" });
      arrow(s, x + 1.0, 2.15, x + 1.0, 2.4, C.accent4, `Hash arrow ${i}`);
    });
    bullets(s, [
      "Each hash includes its parent's, so one hash identifies the whole prefix up to that block. Change anything early and every later hash changes.",
      "Only full blocks count, as in vLLM: a 100-token prompt gives 6 blocks, not 7.",
      "The namespace starts the chain with the model name, so text cached under another model is never credited. blake2b is used instead of Python's hash(), so a restart changes no hash.",
      "PrefixIndex maps each hash to the set of replicas believed to hold it, with a per-replica LRU cap (20,000 blocks by default).",
    ], { x: 0.5, y: 3.2, w: 9, h: 1.9, fontSize: 13, paraSpaceAfter: 6 });
    s.addNotes("This is the data structure behind prefix-aware routing. Each block's hash is computed from the previous hash and the block's own tokens, so a single hash stands for the entire prefix before it. That gives prefix-only matching for free: change the first block and every hash after it changes. Only full blocks of sixteen tokens count, matching what vLLM caches. The model name seeds the chain so two models never share credit. A stable hash function is used so a router restart keeps the same hashes. The index stores, for each hash, the set of replicas believed to hold it, and caps how many blocks it tracks per replica to imitate the engine's own eviction.");
  }

  // ============ 9. SLA veto / latency estimate ============
  {
    const s = addSlide("Policies");
    title(s, "prefix_aware: the response-time check");
    codeTag(s, "ReplicaState.estimate_latency_ms", 0.5, 1.15, 3.2);
    bullets(s, [
      "Below capacity, a new request runs alongside the others, so the estimate is the replica's recent latency at the current occupancy (three occupancy buckets).",
      "At or above capacity it queues: service time × (queue depth + 1) ÷ capacity. This is an M/M/c queue estimate.",
      "Latencies are learned from completed requests with an exponential average (weight 0.2). Capacity starts at the configured batch size and rises to the highest concurrency scraped.",
    ], { x: 0.5, y: 1.65, w: 5.0, h: 3.5, fontSize: 13, paraSpaceAfter: 6 });
    const rows = [["prefix_match", "Eligible winner is within the target: routed there"], ["sla_veto", "Winner would miss the target: least-loaded replica instead"], ["no_eligible_match", "No replica holds enough: least-loaded replica"]];
    s.addTable([[head("Decision path"), head("Meaning")], ...rows.map((r, i) => r.map((t, j) => cell(t, i, { bold: j === 0 })))],
      { x: 5.7, y: 1.3, w: 3.8, colW: [1.5, 2.3], border: { type: "solid", pt: 0.5, color: C.accent4 }, rowH: [0.4, 0.8, 0.8, 0.8], margin: [0.05, 0.08, 0.05, 0.08], objectName: "Decision paths table" });
    text(s, "The console shows which path each request took.", { x: 5.7, y: 4.5, w: 3.8, h: 0.5, fontSize: 13, color: C.text2 });
    s.addNotes("Prefix-aware does not blindly follow the cache. Before routing to the best match it asks the replica's latency estimate whether it would stay within the response-time target. The estimate treats the replica as a queue with as many servers as it can batch. Below that capacity a request runs alongside others and the estimate is the recent latency at that occupancy; above it, requests queue and the estimate grows with queue depth. Latencies are learned from completed requests. If the check fails, the decision path is recorded as an SLA veto and the least-loaded replica is used. The console shows which of the three paths each request took.");
  }

  // ============ 10. What each policy knows ============
  {
    const s = addSlide("Policies");
    title(s, "What each policy knows");
    const yes = "Yes", no = "No";
    const rows = [
      ["Current load", no, yes, yes + " (fallback)", yes + " (fallback)"],
      ["Which replica served this session", no, no, yes, no],
      ["Shared whole messages", no, no, yes, no],
      ["Shared 16-token blocks", no, no, no, yes],
      ["Checks the response-time target", no, no, yes, yes],
      ["Needs the model's tokenizer", no, no, no, yes],
    ];
    s.addTable([[head("Signal"), ...POLS.map((p) => head(p, 12))], ...rows.map((r, i) => r.map((t, j) => cell(t, i, { bold: j === 0, align: j === 0 ? "left" : "center", color: j > 0 && t.startsWith("Yes") ? C.accent1 : C.text1 })))],
      { x: 0.5, y: 1.3, w: 9, colW: [2.9, 1.5, 1.5, 1.55, 1.55], border: { type: "solid", pt: 0.5, color: C.accent4 }, rowH: [0.42, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5], margin: [0.05, 0.08, 0.05, 0.08], objectName: "Policy knowledge table" });
    text(s, "prefix_aware records the session but does not route on it. The tokenizer must match the model, or block boundaries differ from the engine's and predictions fail.", { x: 0.5, y: 4.85, w: 9, h: 0.5, fontSize: 12, color: C.text2 });
    s.addNotes("This table summarizes the four policies by what each can see. Only prefix-aware looks at token blocks, and it is also the only one that needs the model's tokenizer, which has to match the model or its block boundaries will not line up with the engine's. Swiftserve and prefix-aware both check the response-time target. Round robin and least connections are the cache-blind baselines.");
  }

  // ============ 11. Policies in the measurements ============
  if (RUNS.length) {
    const avg = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;
    const get = (r, p, k) => { const v = (r.results || r.policies)[p]; return k === "cache" ? (v.cache_reuse ?? v.true_cache_ratio) : v[k]; };
    const cache = POLS.map((p) => Math.round(avg(RUNS.map((r) => get(r, p, "cache"))) * 100));
    const p50 = POLS.map((p) => Math.round(avg(RUNS.map((r) => get(r, p, "ttft_p50")))));
    const s = addSlide("Policies");
    title(s, `What the measurements show (${RUNS.length} runs)`);
    s.addChart(pres.charts.BAR, [{ name: "Cache reuse (%)", labels: POLS, values: cache }], {
      x: 0.5, y: 1.2, w: 4.6, h: 3.2, barDir: "col", chartColors: [HEX.accent1], showTitle: true, title: "Prompt reused from cache (%)", titleFontSize: 14, titleColor: HEX.dk1, titleFontFace: "+mn-lt",
      showValue: true, dataLabelPosition: "outEnd", dataLabelFontSize: 12, dataLabelColor: HEX.dk1, dataLabelFontFace: "+mn-lt", catAxisLabelColor: HEX.dk2, valAxisLabelColor: HEX.dk2, catAxisLabelFontSize: 10, valAxisLabelFontSize: 11,
      catAxisLabelFontFace: "+mn-lt", valAxisLabelFontFace: "+mn-lt", valGridLine: { color: "E1E4EA", size: 0.5 }, catGridLine: { style: "none" }, showLegend: false, valAxisMinVal: 0, valAxisMaxVal: 100,
    });
    s.addTable([[head("Policy"), head("Median TTFT, mean (ms)")], ...POLS.map((p, i) => [cell(p, i, { bold: true }), cell(p50[i], i)])],
      { x: 5.4, y: 1.3, w: 4.1, colW: [2.2, 1.9], border: { type: "solid", pt: 0.5, color: C.accent4 }, margin: [0.05, 0.08, 0.05, 0.08], objectName: "TTFT table" });
    text(s, "Cache reuse rises with how much each policy knows about the cache, and the policy with the most knowledge had the lowest median in every run. The tail was not consistently better. Details are in the results section.", { x: 5.4, y: 3.2, w: 4.1, h: 1.9, fontSize: 13, color: C.text2 });
    s.addNotes("Averaged over the three real runs on four T4 GPUs, cache reuse follows how much each policy knows about the cache: about fifty-two to fifty-three percent for the two cache-blind policies, about two thirds for swiftserve, and about eighty-six percent for prefix-aware. Median time to first token falls with it for prefix-aware, which had the lowest median in every run. The tail latency was not consistently better, so no claim is made about it. The full table with per-run values is in the results section.");
  }

  // ============ 12. Module map ============
  {
    const s = addSlide("Implementation");
    title(s, "Implementation: module map");
    const rows = [
      ["app.py", "FastAPI app and the chat handler: admission, candidate snapshot, policy call, proxy"],
      ["policy.py", "The four policies, each with select() and a recorded decision"],
      ["prefix_index.py, tokenization.py", "Chained block hashes, the holder index, and the chat tokenizers"],
      ["state.py", "Per-replica state: in-flight count, latency estimate, circuit breaker, sessions"],
      ["proxy.py, resilience.py", "Streaming proxy with retries; circuit breaker and admission control"],
      ["metrics_scraper.py, replica_sidecar.py", "Reads each replica's load and GPU data; the node-side sidecar"],
      ["telemetry.py, events.py, dashboard_api.py", "Request traces, the event stream and the console's API"],
      ["ui/", "The web console: Playground, Load test, Compare, Live routing, Dashboard"],
    ];
    s.addTable([[head("File"), head("Role")], ...rows.map((r, i) => r.map((t, j) => cell(t, i, { bold: j === 0, fontFace: j === 0 ? "Courier New" : THEME.bodyFontFace, fontSize: j === 0 ? 11 : 12 })))],
      { x: 0.5, y: 1.2, w: 9, colW: [3.2, 5.8], border: { type: "solid", pt: 0.5, color: C.accent4 }, rowH: [0.38, 0.46, 0.46, 0.46, 0.46, 0.46, 0.46, 0.46, 0.46], margin: [0.04, 0.1, 0.04, 0.1], objectName: "Module map table" });
    s.addNotes("This is the map of the code. The router is a FastAPI application in app.py. The policies are in policy.py and are independent of the web layer. The prefix index and tokenizers support the prefix-aware policy. State holds what the router knows about each replica. The proxy and resilience modules handle streaming, retries, the circuit breaker and admission control. Telemetry, events and the dashboard API power the console, and ui holds the console itself.");
  }

  // ============ 13. Request path ============
  {
    const s = addSlide("Implementation");
    title(s, "Implementation: the path of one request");
    const steps = [
      ["Receive", "chat_completions in app.py reads the body and session id and opens a trace (telemetry.begin)."],
      ["Admit", "admission.try_acquire: at most 256 requests in flight, otherwise the request is rejected."],
      ["Snapshot", "The state of every replica is read before the policy runs and recorded with the decision."],
      ["Choose", "policy.select (or select_with_prediction) returns a replica; a half-open replica may get the request as a probe."],
      ["Forward", "forward_chat_completion sends it, retrying connection errors on the same replica (_send_with_retry)."],
      ["Stream", "The response is relayed chunk by chunk while a StreamTimer measures TTFT and ITL."],
      ["Account", "Completion, failure or client disconnect is recorded once; counters and latency averages are updated."],
    ];
    steps.forEach(([h, d], i) => {
      const y = 1.2 + i * 0.57;
      numberedRow(s, i, y - 0.06, 0.4);
      text(s, h, { x: 1.1, y, w: 1.4, h: 0.48, fontSize: 14, bold: true, valign: "middle" });
      text(s, d, { x: 2.55, y, w: 6.95, h: 0.5, fontSize: 12, color: C.text2, valign: "middle" });
    });
    s.addNotes("Follow one request through the code. The handler reads the request and opens a trace. Admission control rejects it if too many requests are in flight. The state of every replica is read before the policy runs, so the console shows what the policy saw. The policy returns a replica, with a special case that gives a recovering replica a probe request. The proxy sends the request, retrying connection errors on the same replica, then relays the response while a timer measures time to first token and inter-token latency. Finally the outcome is recorded exactly once, whether it completed, failed or the client disconnected.");
  }

  // ============ 14. Protection ============
  {
    const s = addSlide("Implementation");
    title(s, "Implementation: protection and state");
    const cards = [
      ["Circuit breaker", "resilience.CircuitBreaker", "Five consecutive failures open it. After 10 seconds, doubling up to 120, one probe request tests the replica (half-open)."],
      ["Admission control", "resilience.AdmissionController", "Caps requests in flight at 256 and answers with an error beyond it, instead of queueing without bound."],
      ["Retries", "proxy._send_with_retry", "Connection errors are retried on the same replica. There is no failover to another replica yet."],
      ["Metrics scraper", "metrics_scraper.py", "Every 2 seconds reads each replica's /health and /metrics; the sidecar also reports GPU memory and temperature."],
    ];
    cards.forEach(([h, c, d], i) => {
      const x = 0.5 + (i % 2) * 4.6, y = 1.25 + Math.floor(i / 2) * 1.95;
      card(s, x, y, 4.4, 1.8, `Protection card ${i + 1}`);
      text(s, h, { x: x + 0.2, y: y + 0.12, w: 4.0, h: 0.35, fontSize: 16, bold: true });
      text(s, c, { x: x + 0.2, y: y + 0.5, w: 4.0, h: 0.3, fontFace: "Courier New", fontSize: 10, color: C.accent1 });
      text(s, d, { x: x + 0.2, y: y + 0.85, w: 4.0, h: 0.9, fontSize: 13, color: C.text2 });
    });
    s.addNotes("Four mechanisms protect the router and feed the policies. The circuit breaker stops sending traffic to a failing replica and probes it again after a cooldown that doubles up to two minutes. Admission control caps concurrency at 256. Retries stay on the same replica, which is a known limitation because there is no failover. The metrics scraper reads each replica's load every two seconds, and the sidecar on each node adds GPU memory and temperature.");
  }

  // ============ 15. Observability and console ============
  {
    const s = addSlide("Implementation");
    title(s, "Implementation: events and the console");
    leadBullets(s, [
      ["Events.", "Every step of a request is published to an in-process EventBus (a ring buffer with bounded subscriber queues) that never blocks or raises."],
      ["Traces.", "A RequestTrace per request records the selected, dispatched and served replica separately, with the decision, retries and timings."],
      ["Streaming to the browser.", "The console subscribes to /api/events/stream (server-sent events) and can resume from the last event it saw."],
      ["Console.", "Playground, Load test, Compare, Live routing and Dashboard are plain JavaScript modules with no framework."],
      ["Compare.", "POST /api/policy swaps in a fresh policy (off by default; the dev launcher enables it on localhost). The Compare tab runs one seeded workload through each policy."],
    ], { x: 0.5, y: 1.2, w: 9, h: 2.9, fontSize: 13, paraSpaceAfter: 6 });
    ["request.received", "routing.decision", "dispatch", "first_token", "request.completed"].forEach((e, i) => {
      const x = 0.5 + i * 1.82;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 4.35, w: 1.6, h: 0.6, rectRadius: 0.06, fill: { color: i === 1 ? C.accent1 : C.text1 }, line: { color: i === 1 ? C.accent1 : C.text1, width: 0 }, objectName: `Event ${i + 1}` });
      text(s, e, { x, y: 4.35, w: 1.6, h: 0.6, fontFace: "Courier New", fontSize: 10, bold: true, color: C.background1, align: "center", valign: "middle" });
      if (i < 4) arrow(s, x + 1.62, 4.65, x + 1.8, 4.65, C.accent4, `Event arrow ${i + 1}`);
    });
    text(s, "Events published for one request", { x: 0.5, y: 4.05, w: 5, h: 0.28, fontSize: 12, color: C.text2 });
    s.addNotes("The observability layer is what makes the claims checkable. An in-process event bus carries every step of every request, and a trace object keeps selected, dispatched and served replica separate. The console subscribes over server-sent events and can resume after a reconnect. It has five tabs written in plain JavaScript. The Compare tab depends on one extra endpoint that swaps the active policy; it is disabled unless the launcher turns it on, and it refuses to switch while requests are in flight.");
  }

  // ============ 16. Testing and verification ============
  {
    const s = addSlide("Implementation");
    title(s, "Implementation: testing and verification");
    const cols = [
      ["Automated tests", C.accent1, ["371 tests pass, plus a browser end-to-end test.", "Replicas in the tests are mocked, or fake servers with real prefix caching and no model.", "They check routing, attribution, retries, the event stream, the console and the policy switch."]],
      ["Observed on real GPUs", C.accent3, ["Four Colab T4 nodes behind tunnels, vLLM with Qwen2.5-3B-Instruct.", "Predicted and reported cached tokens matched on a test request.", "Three comparison runs of all four policies, 60 requests each."]],
      ["Not verified", C.accent5, ["Failover between replicas (not implemented).", "Why the tail latency of prefix_aware varied between runs.", "Behaviour with other models, other cache sizes or much longer runs."]],
    ];
    cols.forEach(([h, col, items], i) => {
      const x = 0.5 + i * 3.1;
      card(s, x, 1.25, 2.8, 3.4, `Testing panel ${i + 1}`);
      text(s, h, { x: x + 0.2, y: 1.38, w: 2.45, h: 0.4, fontFace: THEME.headFontFace, fontSize: 16, bold: true, color: col });
      text(s, items.map((t, k) => ({ text: t, options: { bullet: true, breakLine: k < items.length - 1 } })), { x: x + 0.2, y: 1.9, w: 2.45, h: 3.1, fontSize: 12, paraSpaceAfter: 6 });
    });
    s.addNotes("Be explicit about what each kind of evidence means. The automated tests use mocked or fake replicas, so they prove the router's logic, not GPU performance. The real-GPU evidence is a short list: a matching cache prediction on a test request, and three comparison runs of all four policies. Anything outside that, such as failover, the cause of the tail-latency variation, or other models, is not verified and is listed as such.");
  }

  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log("wrote", OUT);
})();
