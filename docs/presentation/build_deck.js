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
const OUT = process.env.DECK_OUT || path.join(HERE, "SwiftServe_Presentation.pptx");

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
pres.title = "Cache-Aware Request Routing for LLM Replicas";
pres.subject = "Routing requests across GPU replicas to reuse the prefix cache";
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

// Measurements written by:  python scripts/demo.py --replicas ... --policies ... --json-out docs/presentation/results.json
// Simulated runs (fake replicas) are never put in the deck unless ALLOW_SIMULATED=1 is set for layout testing.
const RESULTS_PATH = process.env.RESULTS_JSON || path.join(HERE, "results.json");
let RESULTS = null;
if (fs.existsSync(RESULTS_PATH)) {
  RESULTS = JSON.parse(fs.readFileSync(RESULTS_PATH, "utf8"));
  if (RESULTS.simulated && !process.env.ALLOW_SIMULATED) {
    console.warn("results.json comes from simulated replicas; the comparison slide is left out.");
    RESULTS = null;
  }
}


(async () => {
  for (const s of ["Context", "Approach", "Evidence", "Close"]) pres.addSection({ title: s });

  const numberedRow = (s, i, y, d = 0.5) => {
    s.addShape(pres.shapes.OVAL, { x: 0.5, y: y + 0.1, w: d, h: d, fill: { color: C.accent1 }, line: { color: C.accent1, width: 0 }, objectName: `Number ${i + 1}` });
    text(s, String(i + 1), { x: 0.5, y: y + 0.1, w: d, h: d, fontSize: 16, bold: true, color: C.background1, align: "center", valign: "middle" });
  };

  // ============ 1. Title ============
  {
    const s = addSlide("Context", "DARK");
    text(s, "Cache-Aware Request Routing for LLM Replicas", { x: 0.7, y: 1.35, w: 6.2, h: 1.9, fontFace: THEME.headFontFace, fontSize: 38, bold: true, color: C.background1 });
    text(s, "Sending each request to the GPU that already holds the beginning of its prompt, without overloading it", { x: 0.7, y: 3.45, w: 5.7, h: 0.9, fontSize: 18, color: C.accent4 });
    [[7.35, 1.55], [8.55, 1.55], [7.35, 2.75], [8.55, 2.75]].forEach(([x, y], i) => {
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: 1.0, h: 1.0, rectRadius: 0.1, fill: { color: i === 0 ? C.accent1 : C.text2 }, line: { color: i === 0 ? C.accent1 : C.text2, width: 0 }, objectName: `Replica tile ${i + 1}` });
      text(s, "GPU " + "ABCD"[i], { x, y: y + 0.35, w: 1.0, h: 0.3, fontSize: 14, bold: true, color: C.background1, align: "center" });
    });
    s.addNotes("This project is a request router that sits in front of several GPU servers running the same language model. When a prompt shares its beginning with an earlier prompt, the router sends it to the GPU that already processed that beginning, so the work is not repeated, unless that GPU is too busy. The highlighted tile represents the one GPU that holds the cached prefix.");
  }

  // ============ 2. Problem ============
  {
    const s = addSlide("Context");
    title(s, "Each GPU keeps its own cache");
    const paras = [
      ["Prefill reads the whole prompt. ", "The model processes every prompt token and stores the attention keys and values it will reuse while generating. That store is the KV cache, and the work grows with prompt length."],
      ["Prefix caching skips repeated work. ", "vLLM keeps the KV cache of finished prompts in blocks of 16 tokens. A new prompt that begins with the same full blocks skips prefill for them. A 1,600-token prefix is 100 blocks."],
      ["Caches are not shared. ", "Only the replica that handled the earlier prompt holds it. A balancer that ignores this may send the request to a replica that must prefill everything again."],
    ];
    text(s, paras.map(([b, t], i) => [{ text: b, options: { bold: true } }, { text: t, options: { breakLine: i < paras.length - 1 } }]).flat(), { x: 0.5, y: 1.3, w: 4.7, h: 3.8, fontSize: 14, paraSpaceAfter: 10 });
    card(s, 5.4, 1.3, 4.1, 3.75, "Problem diagram panel");
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 6.0, y: 1.5, w: 2.9, h: 0.6, rectRadius: 0.08, fill: { color: C.text1 }, line: { color: C.text1, width: 0 }, objectName: "Request box" });
    text(s, "Request with a shared prefix", { x: 6.0, y: 1.5, w: 2.9, h: 0.6, fontSize: 14, bold: true, color: C.background1, align: "center", valign: "middle" });
    [5.6, 6.6, 7.6, 8.6].forEach((x, i) => {
      arrow(s, 7.45, 2.1, x + 0.4, 3.0, i === 0 ? C.accent1 : C.accent4, `Arrow to GPU ${"ABCD"[i]}`);
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 3.0, w: 0.8, h: 0.8, rectRadius: 0.08, fill: { color: i === 0 ? C.accent1 : C.background1 }, line: { color: i === 0 ? C.accent1 : C.accent4, width: 1 }, objectName: `GPU ${"ABCD"[i]}` });
      text(s, "GPU " + "ABCD"[i], { x, y: 3.0, w: 0.8, h: 0.8, fontSize: 14, bold: true, color: i === 0 ? C.background1 : C.text1, align: "center", valign: "middle" });
      text(s, i === 0 ? "prefix cached" : "no cache", { x: x - 0.1, y: 3.88, w: 1.0, h: 0.3, fontSize: 11, color: i === 0 ? C.accent1 : C.accent4, align: "center", bold: i === 0 });
    });
    text(s, "Only one replica can skip the repeated prefill. Where the request lands decides whether the cache is used.", { x: 5.65, y: 4.25, w: 3.6, h: 0.65, fontSize: 14, color: C.text2, align: "center" });
    s.addNotes("Prefill is the cost of reading the prompt, and it grows with prompt length. vLLM can reuse earlier prefill work if a new prompt begins with exactly the same tokens, in whole blocks of sixteen. Each GPU keeps its own cache, so a repeated prompt that lands on the wrong replica gets no benefit.");
  }

  // ============ 3. Value of a hit ============
  {
    const s = addSlide("Context");
    title(s, "What a cache hit is worth");
    [["2,061 ms", "time to first token, empty cache"], ["74 ms", "time to first token, prefix cached"], ["1,600 of 1,617", "prompt tokens reused from cache"]].forEach(([n, l], i) => {
      const x = 0.5 + i * 3.1;
      card(s, x, 1.3, 2.8, 1.55, `Stat card ${i + 1}`);
      text(s, n, { x: x + 0.15, y: 1.45, w: 2.5, h: 0.75, fontFace: THEME.headFontFace, fontSize: i === 2 ? 26 : 34, bold: true, color: i === 1 ? C.accent1 : C.text1, valign: "middle", align: "center" });
      text(s, l, { x: x + 0.15, y: 2.2, w: 2.5, h: 0.55, fontSize: 14, color: C.text2, align: "center" });
    });
    text(s, "Time to first token", { x: 0.5, y: 3.1, w: 3, h: 0.3, fontSize: 14, bold: true });
    const maxW = 7.2;
    s.addShape(pres.shapes.RECTANGLE, { x: 2.0, y: 3.5, w: maxW, h: 0.4, fill: { color: C.accent5 }, line: { color: C.accent5, width: 0 }, objectName: "Cold bar" });
    s.addShape(pres.shapes.RECTANGLE, { x: 2.0, y: 4.05, w: maxW * (74 / 2061), h: 0.4, fill: { color: C.accent1 }, line: { color: C.accent1, width: 0 }, objectName: "Warm bar" });
    text(s, "Cold", { x: 0.5, y: 3.5, w: 1.4, h: 0.4, fontSize: 14, valign: "middle" });
    text(s, "Cached", { x: 0.5, y: 4.05, w: 1.4, h: 0.4, fontSize: 14, valign: "middle" });
    text(s, "74 ms", { x: 2.0 + maxW * (74 / 2061) + 0.12, y: 4.05, w: 1.2, h: 0.4, fontSize: 14, bold: true, valign: "middle", color: C.accent1 });
    text(s, "One Tesla T4 running vLLM with Qwen2.5-3B-Instruct. The same system prompt of about 1,600 tokens was sent in three requests; only the first met an empty cache. The saving is the prefill of the matched blocks, so it appears in time to first token, not in the time per generated token.", { x: 0.5, y: 4.6, w: 9, h: 0.7, fontSize: 12, color: C.text2 });
    s.addNotes("This is a direct measurement on one GPU node, calling the model server without the router. The first request had to process the whole prompt and took about two seconds to produce its first token. The next two reused sixteen hundred of the sixteen hundred and seventeen prompt tokens and took about seventy-four milliseconds. The bars are drawn to scale. A cache hit removes prefill work, so it shortens time to first token; generation speed afterward is unchanged.");
  }

  // ============ 4. Idea ============
  {
    const s = addSlide("Approach");
    title(s, "The idea");
    text(s, "Maximise cache reuse while keeping every request inside its response-time target.", { x: 0.5, y: 1.3, w: 9, h: 0.9, fontFace: THEME.headFontFace, fontSize: 24 });
    const items = [
      [fa.FaDatabase, "Remember", "The router keeps an index from prompt-block hashes to the replicas that hold them, updated as it routes."],
      [fa.FaSearch, "Match", "For each request it counts how many leading blocks every replica holds. The longest match is the best candidate."],
      [fa.FaShieldAlt, "Protect", "A latency estimate rules out a candidate that would miss the target, so cache affinity never creates a hot spot on its own."],
    ];
    for (let i = 0; i < items.length; i++) {
      const x = 0.5 + i * 3.1;
      card(s, x, 2.45, 2.8, 2.6, `Principle card ${i + 1}`);
      await iconCircle(s, items[i][0], x + 0.2, 2.6, 0.6, HEX.accent1, `Principle ${i + 1}`);
      text(s, items[i][1], { x: x + 0.2, y: 3.3, w: 2.4, h: 0.35, fontSize: 18, bold: true });
      text(s, items[i][2], { x: x + 0.2, y: 3.7, w: 2.45, h: 1.3, fontSize: 14, color: C.text2 });
    }
    s.addNotes("The objective has two sides. Reuse as much cached work as possible, but never at the price of missing the response-time target. The three parts map onto that: an index that remembers where blocks live, a match that prefers the longest shared prefix, and a latency estimate that vetoes a candidate that is too busy.");
  }

  // ============ 5. Trade-off ============
  {
    const s = addSlide("Approach");
    title(s, "Why it is a trade-off");
    const cols = [
      ["Stick to the cache", C.accent3, "Gain: prefill is skipped for every matched block.", "Risk: popular prefixes pile onto one replica, its queue grows and latency rises."],
      ["Spread evenly", C.accent5, "Gain: queues stay balanced.", "Risk: every replica sees every prefix, so each cache holds copies of everything and evicts useful blocks. In this setup a node caches only about 6,000 tokens."],
      ["Balance both", C.accent1, "Use the longest match unless the latency estimate says that replica would miss the target.", "Then fall back to the least-loaded replica."],
    ];
    cols.forEach(([h, col, a, b], i) => {
      const x = 0.5 + i * 3.1;
      card(s, x, 1.3, 2.8, 3.75, `Trade-off card ${i + 1}`);
      s.addShape(pres.shapes.OVAL, { x: x + 0.2, y: 1.5, w: 0.45, h: 0.45, fill: { color: col }, line: { color: col, width: 0 }, objectName: `Trade-off marker ${i + 1}` });
      text(s, h, { x: x + 0.8, y: 1.5, w: 1.9, h: 0.45, fontSize: 17, bold: true, valign: "middle" });
      text(s, [{ text: a, options: { breakLine: true } }, { text: b }], { x: x + 0.2, y: 2.15, w: 2.4, h: 2.8, fontSize: 14, color: C.text2, paraSpaceAfter: 10 });
    });
    s.addNotes("Routing only by cache sends popular prompts to the same replica, which then queues. Routing only by load destroys locality, and with small caches the replicas keep evicting each other's blocks. In this setup each node holds about six thousand tokens of KV cache, so a handful of long system prompts cannot all stay cached on every node. The router has to balance the two, which is why it combines a match with a latency estimate.");
  }

  // ============ 6. Architecture ============
  {
    const s = addSlide("Approach");
    title(s, "System architecture");
    card(s, 0.5, 1.4, 2.0, 2.5, "Console panel");
    text(s, "Web console", { x: 0.65, y: 1.52, w: 1.7, h: 0.35, fontSize: 16, bold: true });
    text(s, ["Playground", "Load test", "Live routing", "Dashboard"].map((t, i, a) => ({ text: t, options: { breakLine: i < a.length - 1 } })), { x: 0.65, y: 1.95, w: 1.7, h: 1.6, fontSize: 14, color: C.text2, paraSpaceAfter: 6 });
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 3.1, y: 1.15, w: 3.1, h: 3.1, rectRadius: 0.08, fill: { color: C.text1 }, line: { color: C.text1, width: 0 }, shadow: shadow(), objectName: "Router panel" });
    text(s, "Router (FastAPI)", { x: 3.3, y: 1.25, w: 2.7, h: 0.35, fontSize: 16, bold: true, color: C.background1 });
    ["Admission control", "Routing policy", "Streaming proxy"].forEach((t, i) => {
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 3.3, y: 1.7 + i * 0.82, w: 2.7, h: 0.66, rectRadius: 0.06, fill: { color: i === 1 ? C.accent1 : C.text2 }, line: { color: i === 1 ? C.accent1 : C.text2, width: 0 }, objectName: t });
      text(s, t, { x: 3.3, y: 1.7 + i * 0.82, w: 2.7, h: 0.66, fontSize: 14, bold: true, color: C.background1, align: "center", valign: "middle" });
    });
    ["A", "B", "C", "D"].forEach((r, i) => {
      const y = 1.0 + i * 0.85;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 7.35, y, w: 2.15, h: 0.7, rectRadius: 0.06, fill: { color: C.background2 }, line: { color: C.accent4, width: 1 }, objectName: `Replica ${r}` });
      text(s, [{ text: `Replica ${r}`, options: { bold: true, breakLine: true } }, { text: "T4 GPU · sidecar · vLLM" }], { x: 7.35, y, w: 2.15, h: 0.7, fontSize: 12, align: "center", valign: "middle" });
    });
    arrow(s, 2.55, 2.7, 3.05, 2.7, C.text1, "Console to router");
    arrow(s, 6.25, 2.7, 7.3, 2.7, C.text1, "Router to replicas");
    text(s, "HTTPS tunnels", { x: 6.1, y: 2.95, w: 1.3, h: 0.5, fontSize: 11, color: C.text2, align: "center" });
    text(s, [{ text: "Router: ", options: { bold: true } }, { text: "chooses a replica and streams its response back. ", options: {} }, { text: "Replicas: ", options: { bold: true } }, { text: "vLLM with an OpenAI-compatible API, behind a small sidecar that also reports GPU use. ", options: {} }, { text: "Console: ", options: { bold: true } }, { text: "shows each routing step live from the router's event stream." }], { x: 0.5, y: 4.45, w: 9, h: 0.85, fontSize: 14, color: C.text2 });
    s.addNotes("The console sends requests to the router. The router applies admission control, runs the routing policy to pick a replica, and streams the response back, retrying connection errors. Each replica is a Colab T4 running vLLM, with a small sidecar process in front that also reports the GPU's memory and temperature. The nodes have no public address, so each is reached through an HTTPS tunnel. In parallel the router publishes an event for every step so the console can show what happened.");
  }

  // ============ 7. How the router decides ============
  {
    const s = addSlide("Approach");
    title(s, "How the router decides");
    const steps = [
      ["Split the prompt", "Tokenize the chat with the model's own tokenizer and cut it into 16-token blocks. Only full blocks are cacheable, as in vLLM."],
      ["Chain the hashes", "hash(block i) combines hash(block i-1) with the tokens of block i, so each hash identifies the whole prefix up to that block."],
      ["Find the longest match", "Count the leading blocks each replica holds. A match is eligible at 50% of the prompt or 256 tokens. Longest wins; ties go to the shorter queue."],
      ["Check the target", "Estimate latency from queue depth, recent average latency and batch capacity. A replica that would exceed the target is skipped."],
      ["Fall back to load", "With no usable match, pick the least-loaded replica. The chosen replica's blocks are recorded at once, so concurrent requests follow."],
    ];
    steps.forEach(([h, d], i) => {
      const y = 1.18 + i * 0.8;
      numberedRow(s, i, y - 0.04);
      text(s, h, { x: 1.2, y, w: 2.45, h: 0.74, fontSize: 15, bold: true, valign: "middle" });
      text(s, d, { x: 3.75, y, w: 5.75, h: 0.74, fontSize: 14, color: C.text2, valign: "middle" });
    });
    s.addNotes("Walk through the five steps. The prompt is split into blocks of sixteen tokens, matching vLLM. Each block's hash includes the previous hash, so a single hash stands for everything before it. A match counts only if it covers at least half the prompt or at least 256 tokens, so tiny overlaps do not drag traffic around. The latency estimate is a queueing model fed by queue depth and recent latency. After the choice, the prompt's blocks are recorded against that replica immediately; that is optimistic, and it is why concurrent requests with the same prefix gather on one replica.");
  }

  // ============ 8. Worked example ============
  {
    const s = addSlide("Approach");
    title(s, "Worked example");
    text(s, "Illustrative numbers chosen to show the arithmetic. A 600-token prompt contains 37 full blocks.", { x: 0.5, y: 1.1, w: 9, h: 0.35, fontSize: 14, color: C.text2 });
    const head = (t) => ({ text: t, options: { bold: true, color: C.background1, fill: { color: C.text1 }, fontSize: 13, fontFace: THEME.bodyFontFace, valign: "middle" } });
    const cell = (t, o = {}) => ({ text: t, options: { fontSize: 13, fontFace: THEME.bodyFontFace, color: C.text1, valign: "middle", ...o } });
    const rows = [
      ["A", "30", "30 of 37 = 81% (480 tokens)", "1.4 s", "Eligible, longest match"],
      ["B", "12", "12 of 37 = 32% (192 tokens)", "0.9 s", "Not eligible: under 50% and under 256 tokens"],
      ["C", "0", "0%", "0.8 s", "No match"],
      ["D", "0", "0%", "0.5 s", "No match, least loaded"],
    ];
    s.addTable([[head("Replica"), head("Blocks held"), head("Match"), head("Estimate"), head("Result")],
      ...rows.map((r, i) => r.map((t, j) => cell(t, { bold: j === 0, fill: { color: i === 0 ? "D8EFEA" : i % 2 ? C.background1 : C.background2 } })))],
      { x: 0.5, y: 1.55, w: 9, colW: [0.9, 1.1, 2.9, 1.0, 3.1], border: { type: "solid", pt: 0.5, color: C.accent4 }, rowH: [0.38, 0.42, 0.55, 0.42, 0.42], margin: [0.04, 0.1, 0.04, 0.1], objectName: "Worked example table" });
    [["Target 3,000 ms", "A is chosen. 480 of 600 prompt tokens skip prefill.", C.accent1], ["If A's estimate were 3,400 ms", "A is vetoed. D, the least-loaded replica, is chosen and gets no cache benefit.", C.accent5]].forEach(([h, d, col], i) => {
      const x = 0.5 + i * 4.6;
      card(s, x, 3.95, 4.4, 1.2, `Outcome card ${i + 1}`);
      text(s, h, { x: x + 0.2, y: 4.05, w: 4.0, h: 0.35, fontSize: 16, bold: true, color: col });
      text(s, d, { x: x + 0.2, y: 4.45, w: 4.0, h: 0.65, fontSize: 14, color: C.text2 });
    });
    s.addNotes("This example uses invented numbers to show the mechanism; it is not a measurement. A 600-token prompt holds 37 full 16-token blocks. Replica A holds the first 30 blocks, which is 81 percent of the prompt and 480 tokens, so it is eligible. Replica B holds 12 blocks, which is below both thresholds, so its partial match is ignored. If A's latency estimate fits the target, A wins. If it does not, the match is vetoed and the least-loaded replica is used instead.");
  }

  // ============ 9. Reliability and state ============
  {
    const s = addSlide("Approach");
    title(s, "State and protection");
    const items = [
      [fa.FaTachometerAlt, "Per-replica state", "Requests in flight, a running average of latency, and queue depth and cache usage read from each replica's metrics."],
      [fa.FaDoorOpen, "Admission control", "At most 256 requests are in flight. Beyond that the router answers with an error instead of queueing without bound."],
      [fa.FaBolt, "Circuit breaker", "Five consecutive failures open the circuit. After 10 seconds, doubling up to 120, a single probe request tests the replica."],
      [fa.FaRedo, "Retries", "Connection errors are retried on the same replica. There is no failover to another replica yet."],
    ];
    for (let i = 0; i < items.length; i++) {
      const x = 0.5 + (i % 2) * 4.6, y = 1.3 + Math.floor(i / 2) * 1.95;
      card(s, x, y, 4.4, 1.75, `State card ${i + 1}`);
      await iconCircle(s, items[i][0], x + 0.2, y + 0.2, 0.55, HEX.accent1, `State ${i + 1}`);
      text(s, items[i][1], { x: x + 0.95, y: y + 0.2, w: 3.3, h: 0.55, fontSize: 17, bold: true, valign: "middle" });
      text(s, items[i][2], { x: x + 0.2, y: y + 0.88, w: 4.0, h: 0.8, fontSize: 14, color: C.text2 });
    }
    s.addNotes("The routing policy depends on per-replica state: how many requests are in flight, recent latency, and queue depth and cache usage scraped from each replica. Admission control caps concurrency at 256. The circuit breaker stops sending traffic to a failing replica and probes it again after a cooldown that grows. Connection errors are retried on the same replica; failing over to a different replica is not implemented yet.");
  }

  // ============ 10. Policies compared ============
  {
    const s = addSlide("Approach");
    title(s, "Four policies compared");
    const head = (t) => ({ text: t, options: { bold: true, color: C.background1, fill: { color: C.text1 }, fontSize: 13, fontFace: THEME.bodyFontFace, valign: "middle" } });
    const cell = (t, o = {}) => ({ text: t, options: { fontSize: 12, fontFace: THEME.bodyFontFace, color: C.text1, valign: "middle", ...o } });
    const rows = [
      ["round_robin", "Nothing. Replicas take turns.", "Simple and perfectly even.", "Ignores both load and cache. Every replica ends up caching every prefix."],
      ["least_connections", "Requests currently in flight on each replica.", "Adapts as load changes.", "Ignores which replica holds the cache."],
      ["swiftserve", "Session affinity, then a prefix trie over whole messages.", "Keeps a conversation on one replica.", "Needs two shared messages. Single-turn traffic falls back to load."],
      ["prefix_aware", "16-token block hashes, a load estimate and the response-time target.", "Reuses shared prefixes across different users and sessions.", "The index is a belief: it can go stale after cache evictions or failures."],
    ];
    s.addTable([[head("Policy"), head("What it looks at"), head("Strength"), head("Weakness")],
      ...rows.map((r, i) => r.map((t, j) => cell(t, { bold: j === 0, fill: { color: i % 2 ? C.background1 : C.background2 } })))],
      { x: 0.5, y: 1.3, w: 9, colW: [1.7, 2.6, 2.3, 2.4], border: { type: "solid", pt: 0.5, color: C.accent4 }, rowH: [0.4, 0.85, 0.8, 0.95, 0.95], margin: [0.05, 0.1, 0.05, 0.1], objectName: "Policy comparison table" });
    s.addNotes("These are the four routing policies in the router. Round robin and least connections are the baselines: neither knows anything about the cache. Swiftserve is the earlier policy that tracks conversations and a trie over whole messages, so it needs at least two shared messages before it can match. Prefix-aware matches at the level of 16-token blocks, so it can reuse a shared system prompt across different users and conversations. The next slides show how each is measured.");
  }

  // ============ 11. Observability ============
  {
    const s = addSlide("Evidence");
    title(s, "Every request is traceable");
    imageFit(s, path.join(HERE, "assets/request_decision.png"), 0.5, 1.25, 5.6, 2.7, "Routing decision screenshot");
    imageFit(s, path.join(HERE, "assets/request_tokens.png"), 0.5, 4.05, 5.6, 1.2, "Cache tokens screenshot");
    const items = [
      "Selected, dispatched and served replica, kept separate because retries can make them differ",
      "The routing decision and the reason for it",
      "Time to first token, inter-token latency and total time at the router",
      "Cached tokens reported by the engine, next to the router's prediction",
    ];
    text(s, items.map((t, i) => ({ text: t, options: { bullet: true, breakLine: i < items.length - 1 } })), { x: 6.4, y: 1.3, w: 3.1, h: 2.9, fontSize: 14, paraSpaceAfter: 8 });
    text(s, "Events: received, routing decision, dispatch, first token, completed, streamed live to the console.", { x: 6.4, y: 4.3, w: 3.1, h: 0.9, fontSize: 12, color: C.text2 });
    s.addNotes("Each request gets an identifier, and the router publishes an event for every step: received, routing decision, dispatch, first token and completed. The console joins those events with what the browser measured. The screenshot shows one request. The policy chose the replica because it held the prompt's first block, and the router's predicted cache reuse matched what vLLM reported afterward.");
  }

  // ============ 12. Metrics ============
  {
    const s = addSlide("Evidence");
    title(s, "How latency is measured");
    s.addShape(pres.shapes.LINE, { x: 1.5, y: 2.15, w: 7.2, h: 0, line: { color: C.accent4, width: 2 }, objectName: "Timeline" });
    [[1.5, "Request received"], [3.7, "First token"], [8.2, "Last token"]].forEach(([x, l], i) => {
      s.addShape(pres.shapes.OVAL, { x: x - 0.1, y: 2.05, w: 0.2, h: 0.2, fill: { color: C.accent1 }, line: { color: C.accent1, width: 0 }, objectName: `Mark ${i + 1}` });
      text(s, l, { x: x - 0.9, y: 2.35, w: 1.8, h: 0.3, fontSize: 14, bold: true, align: "center" });
    });
    text(s, "TTFT", { x: 1.5, y: 1.55, w: 2.2, h: 0.35, fontSize: 16, bold: true, color: C.accent1, align: "center" });
    text(s, "ITL: gaps between tokens", { x: 3.7, y: 1.55, w: 4.5, h: 0.35, fontSize: 16, bold: true, color: C.accent3, align: "center" });
    const defs = [
      ["TTFT", "Time to first token", "Request arrival at the router to the first chunk that contains text. It includes routing, queueing and prefill."],
      ["ITL", "Inter-token latency", "(last text time − first text time) ÷ (text chunks − 1). A mean, because tunnels deliver chunks in bursts."],
      ["Cache reuse", "Cached ÷ prompt tokens", "From the engine's usage report, compared with the router's prediction. Zero means a miss."],
    ];
    defs.forEach(([k, h, d], i) => {
      const x = 0.5 + i * 3.1;
      card(s, x, 3.0, 2.8, 2.15, `Definition card ${i + 1}`);
      text(s, k, { x: x + 0.2, y: 3.12, w: 2.4, h: 0.35, fontSize: 18, bold: true, color: C.accent1 });
      text(s, h, { x: x + 0.2, y: 3.5, w: 2.4, h: 0.3, fontSize: 14, bold: true });
      text(s, d, { x: x + 0.2, y: 3.85, w: 2.45, h: 1.25, fontSize: 13, color: C.text2 });
    });
    s.addNotes("Time to first token is how long the user waits before any text appears; it contains routing, queueing and the prefill, which is exactly what caching shortens. Inter-token latency describes the pace once generation starts. Tunnels can bunch several tokens into one chunk, so single gaps are unreliable, but the mean over the whole span is not. Cache reuse is read from the engine's own usage report, so it is a measurement and not an estimate. Non-streaming requests have no first token to observe.");
  }

  // ============ 13. Measured through the router ============
  {
    const s = addSlide("Evidence");
    title(s, "A run through the router on four GPUs");
    s.addChart(pres.charts.BAR, [{ name: "Requests", labels: ["GPU A", "GPU B", "GPU C", "GPU D"], values: [18, 12, 22, 8] }], {
      x: 0.5, y: 1.25, w: 4.6, h: 3.1, barDir: "col", chartColors: [HEX.accent1], showTitle: true, title: "Requests served per replica", titleFontSize: 14, titleColor: HEX.dk1, titleFontFace: "+mn-lt",
      showValue: true, dataLabelPosition: "outEnd", dataLabelFontSize: 12, dataLabelColor: HEX.dk1, dataLabelFontFace: "+mn-lt",
      catAxisLabelColor: HEX.dk2, valAxisLabelColor: HEX.dk2, catAxisLabelFontSize: 12, valAxisLabelFontSize: 11, catAxisLabelFontFace: "+mn-lt", valAxisLabelFontFace: "+mn-lt",
      valGridLine: { color: "E1E4EA", size: 0.5 }, catGridLine: { style: "none" }, showLegend: false, valAxisMinVal: 0,
    });
    [["60 of 60", "requests completed, concurrency 8"], ["374 / 4,944 ms", "time to first token, median / 95th percentile"], ["34.2 ms", "mean inter-token latency under load"], ["16 and 16", "cached tokens predicted, and reported"]].forEach(([n, l], i) => {
      const y = 1.25 + i * 0.78;
      card(s, 5.4, y, 4.1, 0.68, `Result card ${i + 1}`);
      text(s, n, { x: 5.55, y, w: 1.75, h: 0.68, fontFace: THEME.headFontFace, fontSize: 16, bold: true, valign: "middle", color: C.accent1 });
      text(s, l, { x: 7.3, y, w: 2.1, h: 0.68, fontSize: 12, valign: "middle", color: C.text2 });
    });
    text(s, [{ text: "What this shows: ", options: { bold: true } }, { text: "requests flow through the router to four real GPUs, load spreads across them, and the router's cache prediction matched the engine. " }, { text: "What it does not show: ", options: { bold: true } }, { text: "prompts were only 40 tokens, so little could be cached; this is not a cache benchmark." }], { x: 0.5, y: 4.5, w: 9, h: 0.75, fontSize: 13, color: C.text2 });
    s.addNotes("This run sent sixty requests at a concurrency of eight through the router to the four GPUs, using the prefix-aware policy. All sixty completed, and the work spread over all four replicas. The 95th percentile time to first token is far above the median; the likely causes are the first requests reaching cold replicas and queueing, but that has not been broken down. The prompts were very short, so this run shows that routing and measurement work end to end. It is not evidence of a cache speedup. The fair comparison is on the next slides.");
  }

  // ============ 14. Experiment protocol ============
  {
    const s = addSlide("Evidence");
    title(s, "How the policies are compared");
    const head = (t) => ({ text: t, options: { bold: true, color: C.background1, fill: { color: C.text1 }, fontSize: 13, fontFace: THEME.bodyFontFace, valign: "middle" } });
    const cell = (t, o = {}) => ({ text: t, options: { fontSize: 12, fontFace: THEME.bodyFontFace, color: C.text1, valign: "middle", ...o } });
    const rows = [
      ["1  Cluster", "Four T4 nodes, same model and vLLM settings, tunnels reachable", "Hardware and engine are identical for every policy"],
      ["2  Workload", "6 long system prompts (about 1,500 tokens) shared by 60 sessions, concurrency 8", "More shared prefixes than one node can cache, so placement matters"],
      ["3  Isolation", "Each policy gets its own prompt namespace: a marker at the front changes every block hash", "No policy inherits another policy's warm cache"],
      ["4  Run", "The console's Compare tab (or scripts/demo.py) runs round_robin, least_connections, swiftserve and prefix_aware in turn", "The same requests go through every policy"],
      ["5  Repeat", "Run again with another seed and the policy order reversed", "Shows the ranking is not an ordering or noise effect"],
      ["6  Read", "TTFT median and 95th percentile, cache reuse, goodput, spread across replicas", "TTFT shows the prefill saving; spread shows load balance"],
    ];
    s.addTable([[head("Step"), head("What is done"), head("Why")],
      ...rows.map((r, i) => r.map((t, j) => cell(t, { bold: j === 0, fill: { color: i % 2 ? C.background1 : C.background2 } })))],
      { x: 0.5, y: 1.2, w: 9, colW: [1.35, 4.35, 3.3], border: { type: "solid", pt: 0.5, color: C.accent4 }, rowH: [0.36, 0.6, 0.6, 0.66, 0.6, 0.6, 0.6], margin: [0.04, 0.1, 0.04, 0.1], objectName: "Experiment protocol table" });
    s.addNotes("This is the protocol for a fair comparison. The same four GPUs and settings are used for every policy. The workload has six long shared system prompts, which is more than one node's cache can hold, so a policy that places requests well should show a higher cache reuse. Each policy runs in its own prompt namespace so none of them benefits from a cache warmed by an earlier run. The comparison is then repeated with a different seed and the policy order reversed to check the ranking holds. The command is: python scripts/demo.py with the four replica URLs, the four policies, and the json-out option. Policies run one after another, so this is a controlled demonstration rather than a publishable benchmark.");
  }

  // ============ 15. Comparison results (only with real measurements) ============
  // Accepts scripts/demo.py --json-out ("policies": true_cache_ratio, replica_spread, goodput) or the
// console's Compare tab "Export JSON" ("results": cache_reuse, spread).
const rawPol = (RESULTS && (RESULTS.policies || RESULTS.results)) || {};
const pol = Object.fromEntries(Object.entries(rawPol).map(([k, v]) => [k, { ...v, true_cache_ratio: v.true_cache_ratio ?? v.cache_reuse ?? null, replica_spread: v.replica_spread ?? v.spread ?? null, goodput: v.goodput ?? null }]));
const hasGoodput = Object.values(pol).some((v) => v.goodput !== null);
  const names = ["round_robin", "least_connections", "swiftserve", "prefix_aware"].filter((n) => pol[n]);
  if (names.length >= 2) {
    const s = addSlide("Evidence");
    title(s, "Measured policy comparison");
    const v = (k) => names.map((n) => Math.round(pol[n][k] ?? 0));
    s.addChart(pres.charts.BAR, [
      { name: "TTFT median (ms)", labels: names, values: v("ttft_p50") },
      { name: "TTFT 95th percentile (ms)", labels: names, values: v("ttft_p95") },
    ], {
      x: 0.5, y: 1.2, w: 5.0, h: 3.2, barDir: "col", barGrouping: "clustered", chartColors: [HEX.accent1, HEX.accent2], showLegend: true, legendPos: "b", legendFontSize: 12, legendColor: HEX.dk2, legendFontFace: "+mn-lt",
      showValue: true, dataLabelPosition: "outEnd", dataLabelFontSize: 11, dataLabelColor: HEX.dk1, dataLabelFontFace: "+mn-lt", catAxisLabelColor: HEX.dk2, valAxisLabelColor: HEX.dk2, catAxisLabelFontSize: 11, valAxisLabelFontSize: 11,
      catAxisLabelFontFace: "+mn-lt", valAxisLabelFontFace: "+mn-lt", valGridLine: { color: "E1E4EA", size: 0.5 }, catGridLine: { style: "none" }, valAxisMinVal: 0,
    });
    const pct = (x) => (x === null || x === undefined ? "n/a" : (x * 100).toFixed(0) + " %");
    const head = (t) => ({ text: t, options: { bold: true, color: C.background1, fill: { color: C.text1 }, fontSize: 12, fontFace: THEME.bodyFontFace } });
    const cell = (t, i, o = {}) => ({ text: String(t), options: { fontSize: 12, fontFace: THEME.bodyFontFace, color: C.text1, fill: { color: i % 2 ? C.background1 : C.background2 }, ...o } });
    const spreadTxt = (n) => (pol[n].replica_spread === null ? "n/a" : pol[n].replica_spread.toFixed(2));
    s.addTable([[head("Policy"), head("Cache reuse"), ...(hasGoodput ? [head("Goodput")] : []), head("Max : mean load")],
      ...names.map((n, i) => [cell(n, i, { bold: true }), cell(pct(pol[n].true_cache_ratio), i), ...(hasGoodput ? [cell(pct(pol[n].goodput), i)] : []), cell(spreadTxt(n), i)])],
      { x: 5.7, y: 1.3, w: 3.8, colW: hasGoodput ? [1.6, 0.75, 0.65, 0.8] : [1.8, 1.0, 1.0], border: { type: "solid", pt: 0.5, color: C.accent4 }, margin: [0.05, 0.06, 0.05, 0.06], objectName: "Comparison table" });
    const ok = names.filter((n) => pol[n].ttft_p50);
    const best = ok.reduce((a, b) => (pol[a].ttft_p50 <= pol[b].ttft_p50 ? a : b));
    const worst = ok.filter((n) => n !== best).reduce((a, b) => (pol[a].ttft_p50 >= pol[b].ttft_p50 ? a : b));
    const w = RESULTS.workload || RESULTS.config || {};
    const tenants = w.num_apps ?? w.tenants, sysTok = w.system_prompt_tokens ?? w.tokens, reqs = w.sessions ?? w.n, conc = w.concurrency ?? w.conc;
    const bestP95 = ok.filter((n) => pol[n].ttft_p95).reduce((a, b) => (pol[a].ttft_p95 <= pol[b].ttft_p95 ? a : b));
    const tail = bestP95 === best ? "" : ` Its 95th percentile was ${Math.round(pol[best].ttft_p95)} ms, against ${Math.round(pol[bestP95].ttft_p95)} ms for ${bestP95}.`;
    text(s, `${best} had the lowest median TTFT, ${(pol[worst].ttft_p50 / pol[best].ttft_p50).toFixed(1)}x lower than ${worst}.${tail} Single run: ${reqs} requests per policy, ${tenants} tenants with system prompts of about ${sysTok} tokens, concurrency ${conc}, ${RESULTS.replicas || 4} replicas, policies run one after another.`, { x: 0.5, y: 4.55, w: 9, h: 0.7, fontSize: 13, color: C.text2 });
    s.addNotes("All policies ran the same workload on the same replicas, one after another, each in its own prompt namespace so that none benefited from a cache warmed by an earlier run. Read time to first token first, because that is where prefill savings show up. Cache reuse is the share of prompt tokens the engines reported as cached. Goodput is the share of requests that met both the time-to-first-token and per-token targets. Max-to-mean load shows how evenly requests spread: 1.0 is perfectly even, higher means one replica took more than its share. Compare the median and the 95th percentile separately: a policy can have the best median and a worse tail, because concentrating requests on the replicas that hold a cache can also concentrate queueing. The policies ran one after another in a single run, so this is a controlled demonstration; repeating it with another seed and the order reversed checks that the ranking holds.");
  }

  // ============ 16. Demo ============
  {
    const s = addSlide("Evidence");
    title(s, "Live demonstration");
    [["Dashboard", "four GPUs with memory and temperature reported by each node"], ["Playground", "one request twice: cold, then with the prefix cached"], ["Compare", "the same multi-tenant workload through all four policies"], ["Live routing", "requests spreading across the replicas as they run"]].forEach(([h, d], i) => {
      const x = 0.5 + i * 2.28;
      card(s, x, 1.25, 2.1, 1.6, `Demo step ${i + 1}`);
      s.addShape(pres.shapes.OVAL, { x: x + 0.12, y: 1.37, w: 0.4, h: 0.4, fill: { color: C.accent1 }, line: { color: C.accent1, width: 0 }, objectName: `Demo step ${i + 1} number` });
      text(s, String(i + 1), { x: x + 0.12, y: 1.37, w: 0.4, h: 0.4, fontSize: 14, bold: true, color: C.background1, align: "center", valign: "middle" });
      text(s, h, { x: x + 0.6, y: 1.37, w: 1.45, h: 0.4, fontSize: 15, bold: true, valign: "middle" });
      text(s, d, { x: x + 0.12, y: 1.87, w: 1.9, h: 0.95, fontSize: 13, color: C.text2 });
    });
    imageFit(s, path.join(HERE, "assets/dashboard_gpus.png"), 0.5, 3.1, 9, 2.0, "Dashboard screenshot");
    s.addNotes("Run the demonstration in this order. Start on the dashboard to show the real GPUs and their memory and temperature. Then use the playground to send the same long system prompt twice and point at the cached token count and the time to first token. Open the Compare tab and run all four policies on the same workload; the table shows the difference side by side. Keep the live routing view open while it runs to show requests spreading across replicas.");
  }

  // ============ 17. Limits and next steps ============
  {
    const s = addSlide("Close");
    title(s, "Limits and next steps");
    const cols = [
      ["Current limits", ["The router's index is a belief. A failed request can leave a stale entry, and the index does not see engine evictions.", "Connection errors retry the same replica; there is no failover to another one.", "Latency through tunnels includes network delay.", "Policies are compared one after another on one model, so results are a controlled demonstration."]],
      ["Next steps", ["Repeat the comparison with several seeds and report the spread.", "Rebuild the index from the engine's cache events instead of predictions.", "Fail over to another replica on connection errors.", "Keep the index across router restarts."]],
    ];
    cols.forEach(([h, items], i) => {
      const x = 0.5 + i * 4.6;
      card(s, x, 1.3, 4.4, 3.85, `${h} panel`);
      text(s, h, { x: x + 0.25, y: 1.45, w: 3.9, h: 0.4, fontFace: THEME.headFontFace, fontSize: 20, bold: true, color: i === 0 ? C.accent5 : C.accent1 });
      text(s, items.map((t, k) => ({ text: t, options: { bullet: true, breakLine: k < items.length - 1 } })), { x: x + 0.25, y: 2.0, w: 3.9, h: 3.0, fontSize: 14, paraSpaceAfter: 8 });
    });
    s.addNotes("Be direct about the limitations. The index is a belief about what each replica holds, and it can drift after evictions or failed requests. Retries stay on the same replica. The comparison runs policies one after another, so it is a controlled demonstration rather than a publishable benchmark. The next steps address each of these.");
  }

  // ============ 18. Summary ============
  {
    const s = addSlide("Close", "DARK");
    text(s, "Summary", { x: 0.7, y: 0.6, w: 8.6, h: 0.8, fontFace: THEME.headFontFace, fontSize: 34, bold: true, color: C.background1 });
    [["Caches are per replica", "A balancer that ignores them wastes prefill work that vLLM could reuse."],
     ["Route by prefix, protect by latency", "Hash prompts in 16-token blocks, take the longest eligible match, and skip replicas that would miss the target."],
     ["Measure every step", "Each request records its decision, its replica and its timings, so every claim can be checked against data."]].forEach(([h, d], i) => {
      const y = 1.7 + i * 1.15;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.7, y, w: 8.6, h: 0.95, rectRadius: 0.08, fill: { color: C.text2 }, line: { color: C.text2, width: 0 }, objectName: `Summary card ${i + 1}` });
      text(s, h, { x: 0.95, y, w: 3.4, h: 0.95, fontSize: 15, bold: true, color: C.background1, valign: "middle" });
      text(s, d, { x: 4.5, y, w: 4.6, h: 0.95, fontSize: 14, color: "D5DAE3", valign: "middle" });
    });
    s.addNotes("Close with the three points. Caches are per replica. Routing by prefix and protecting with a latency check lets the router reuse cached work without overloading a replica. And every request is measured, so the claims in this presentation can be checked against recorded data.");
  }

  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log("wrote", OUT, "(comparison slide:", names.length >= 2 ? "included" : "not included, no measurements", ")");
})();
