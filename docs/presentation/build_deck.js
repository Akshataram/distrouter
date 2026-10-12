// Builds SwiftServe_Presentation.pptx.
//
//   NODE_PATH=<dir containing node_modules with pptxgenjs, react-icons, react, react-dom, sharp> \
//     node docs/presentation/build_deck.js
//
// Numbers on the results slides come from measurements recorded in
// docs/COMPLETE_PROJECT_WALKTHROUGH.md. The policy-comparison slide is built only
// when docs/presentation/results.json contains measurements for at least two policies.

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
const OUT = path.join(HERE, "SwiftServe_Presentation.pptx");

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

const RESULTS = JSON.parse(fs.readFileSync(path.join(HERE, "results.json"), "utf8"));

(async () => {
  // sections are declared in slide order
  for (const s of ["Context", "Approach", "Evidence", "Close"]) pres.addSection({ title: s });

  // ============ 1. Title ============
  {
    const s = addSlide("Context", "DARK");
    text(s, "Cache-Aware Request Routing for LLM Replicas", { x: 0.7, y: 1.35, w: 6.2, h: 1.9, fontFace: THEME.headFontFace, fontSize: 38, bold: true, color: C.background1, valign: "top" });
    text(s, "Sending each request to the GPU that already holds the beginning of its prompt", { x: 0.7, y: 3.45, w: 5.6, h: 0.9, fontSize: 18, color: C.accent4 });
    // four replica tiles, one highlighted as the replica holding the cached prefix
    const pos = [[7.35, 1.55], [8.55, 1.55], [7.35, 2.75], [8.55, 2.75]];
    pos.forEach(([x, y], i) => {
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: 1.0, h: 1.0, rectRadius: 0.1, fill: { color: i === 0 ? C.accent1 : C.text2 }, line: { color: i === 0 ? C.accent1 : C.text2, width: 0 }, objectName: `Replica tile ${i + 1}` });
      text(s, "GPU " + "ABCD"[i], { x, y: y + 0.35, w: 1.0, h: 0.3, fontSize: 14, bold: true, color: C.background1, align: "center" });
    });
    s.addNotes("This project is a request router that sits in front of several GPU servers running the same language model. The goal is simple: when a prompt shares its beginning with an earlier prompt, send it to the GPU that already processed that beginning, so the work is not repeated. The highlighted tile on the right represents the one GPU that holds the cached prefix.");
  }

  // ============ 2. Problem ============
  {
    const s = addSlide("Context");
    title(s, "Each GPU keeps its own cache");
    const paras = [
      ["Prefill reads the whole prompt. ", "The model processes every prompt token and stores the attention state it will reuse while generating, called the KV cache."],
      ["Prefix caching skips repeated work. ", "vLLM keeps the KV cache for finished prompts. A new prompt that starts with the same full 16-token blocks skips that part of prefill."],
      ["Caches are not shared. ", "With several replicas, only the one that handled the earlier prompt holds it. A balancer that ignores this sends the request to a replica that must start from scratch."],
    ];
    text(s, paras.map(([b, t], i) => [
      { text: b, options: { bold: true, breakLine: false } },
      { text: t, options: { breakLine: i < paras.length - 1 } },
    ]).flat(), { x: 0.5, y: 1.35, w: 4.6, h: 3.6, fontSize: 14, paraSpaceAfter: 10 });
    // diagram: one request, four replicas, only A holds the prefix
    card(s, 5.4, 1.3, 4.1, 3.75, "Problem diagram panel");
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 6.0, y: 1.5, w: 2.9, h: 0.6, rectRadius: 0.08, fill: { color: C.text1 }, line: { color: C.text1, width: 0 }, objectName: "Request box" });
    text(s, "Request with a shared prefix", { x: 6.0, y: 1.5, w: 2.9, h: 0.6, fontSize: 14, bold: true, color: C.background1, align: "center", valign: "middle" });
    const gx = [5.6, 6.6, 7.6, 8.6];
    gx.forEach((x, i) => {
      arrow(s, 7.45, 2.1, x + 0.4, 3.0, i === 0 ? C.accent1 : C.accent4, `Arrow to GPU ${"ABCD"[i]}`);
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 3.0, w: 0.8, h: 0.8, rectRadius: 0.08, fill: { color: i === 0 ? C.accent1 : C.background1 }, line: { color: i === 0 ? C.accent1 : C.accent4, width: 1 }, objectName: `GPU ${"ABCD"[i]}` });
      text(s, "GPU " + "ABCD"[i], { x, y: 3.0, w: 0.8, h: 0.8, fontSize: 14, bold: true, color: i === 0 ? C.background1 : C.text1, align: "center", valign: "middle" });
      text(s, i === 0 ? "prefix cached" : "no cache", { x: x - 0.1, y: 3.88, w: 1.0, h: 0.3, fontSize: 11, color: i === 0 ? C.accent1 : C.accent4, align: "center", bold: i === 0 });
    });
    text(s, "Only one replica can skip the repeated prefill. Where the request lands decides whether the cache is used.", { x: 5.65, y: 4.25, w: 3.6, h: 0.65, fontSize: 14, color: C.text2, align: "center" });
    s.addNotes("Explain the two phases of generating text. Prefill is the cost of reading the prompt, and it grows with prompt length. vLLM can reuse earlier prefill work if a new prompt begins with exactly the same tokens, in whole blocks of sixteen. The catch is that each GPU keeps its own cache. If four replicas serve traffic and a repeated prompt lands on the wrong one, the saved work is not available.");
  }

  // ============ 3. What a hit is worth ============
  {
    const s = addSlide("Context");
    title(s, "What a cache hit is worth");
    const stats = [["2,061 ms", "time to first token, empty cache"], ["74 ms", "time to first token, prefix cached"], ["1,600 of 1,617", "prompt tokens reused from cache"]];
    stats.forEach(([n, l], i) => {
      const x = 0.5 + i * 3.1;
      card(s, x, 1.3, 2.8, 1.55, `Stat card ${i + 1}`);
      text(s, n, { x: x + 0.15, y: 1.45, w: 2.5, h: 0.75, fontFace: THEME.headFontFace, fontSize: i === 2 ? 26 : 34, bold: true, color: i === 1 ? C.accent1 : C.text1, valign: "middle", align: "center" });
      text(s, l, { x: x + 0.15, y: 2.2, w: 2.5, h: 0.55, fontSize: 14, color: C.text2, align: "center" });
    });
    // proportional bars
    text(s, "Time to first token", { x: 0.5, y: 3.15, w: 3, h: 0.3, fontSize: 14, bold: true });
    const maxW = 7.2;
    s.addShape(pres.shapes.RECTANGLE, { x: 2.0, y: 3.55, w: maxW, h: 0.42, fill: { color: C.accent5 }, line: { color: C.accent5, width: 0 }, objectName: "Cold bar" });
    s.addShape(pres.shapes.RECTANGLE, { x: 2.0, y: 4.15, w: maxW * (74 / 2061), h: 0.42, fill: { color: C.accent1 }, line: { color: C.accent1, width: 0 }, objectName: "Warm bar" });
    text(s, "Cold", { x: 0.5, y: 3.55, w: 1.4, h: 0.42, fontSize: 14, valign: "middle" });
    text(s, "Cached", { x: 0.5, y: 4.15, w: 1.4, h: 0.42, fontSize: 14, valign: "middle" });
    text(s, "74 ms", { x: 2.0 + maxW * (74 / 2061) + 0.12, y: 4.15, w: 1.2, h: 0.42, fontSize: 14, bold: true, valign: "middle", color: C.accent1 });
    text(s, "Measured on one Tesla T4 running vLLM with Qwen2.5-3B-Instruct. The same system prompt of about 1,600 tokens was sent in three requests; only the first met an empty cache.", { x: 0.5, y: 4.75, w: 8.4, h: 0.55, fontSize: 12, color: C.text2 });
    s.addNotes("This is a direct measurement on one of the GPU nodes, calling the model server without the router. The first request had to process the whole prompt and took about two seconds to produce the first token. The next two requests reused sixteen hundred of the sixteen hundred and seventeen prompt tokens and took about seventy-four milliseconds. The bars are drawn to scale. This gap is what a cache-aware router tries to capture.");
  }

  // ============ 4. Idea ============
  {
    const s = addSlide("Approach");
    title(s, "The idea");
    text(s, "Send each request to the replica that already holds the start of its prompt, unless that replica is too busy.", { x: 0.5, y: 1.3, w: 9, h: 1.05, fontFace: THEME.headFontFace, fontSize: 24, color: C.text1 });
    const items = [
      [fa.FaDatabase, "Remember", "The router keeps an index of which replica holds which blocks of which prompts."],
      [fa.FaSearch, "Match", "The replica with the longest shared prefix is the best candidate."],
      [fa.FaShieldAlt, "Protect", "A latency estimate rules out a candidate that would miss the response-time target."],
    ];
    for (let i = 0; i < items.length; i++) {
      const x = 0.5 + i * 3.1;
      card(s, x, 2.6, 2.8, 2.35, `Principle card ${i + 1}`);
      await iconCircle(s, items[i][0], x + 0.2, 2.8, 0.6, HEX.accent1, `Principle ${i + 1}`);
      text(s, items[i][1], { x: x + 0.2, y: 3.5, w: 2.4, h: 0.35, fontSize: 18, bold: true });
      text(s, items[i][2], { x: x + 0.2, y: 3.9, w: 2.4, h: 0.95, fontSize: 14, color: C.text2 });
    }
    s.addNotes("The idea has three parts. First, remember: the router tracks which replica has seen which prompt blocks. Second, match: among the replicas, prefer the one with the longest shared prefix. Third, protect: a cached replica that is overloaded is worse than an idle one, so a latency estimate can veto the match.");
  }

  // ============ 5. Architecture ============
  {
    const s = addSlide("Approach");
    title(s, "System architecture");
    // browser console
    card(s, 0.5, 1.5, 2.0, 2.4, "Console panel");
    text(s, "Web console", { x: 0.65, y: 1.62, w: 1.7, h: 0.35, fontSize: 16, bold: true });
    text(s, ["Playground", "Load test", "Live routing", "Dashboard"].map((t, i, a) => ({ text: t, options: { breakLine: i < a.length - 1 } })), { x: 0.65, y: 2.05, w: 1.7, h: 1.6, fontSize: 14, color: C.text2, paraSpaceAfter: 6 });
    // router
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 3.1, y: 1.15, w: 3.1, h: 3.1, rectRadius: 0.08, fill: { color: C.text1 }, line: { color: C.text1, width: 0 }, shadow: shadow(), objectName: "Router panel" });
    text(s, "Router", { x: 3.3, y: 1.25, w: 2.7, h: 0.35, fontSize: 16, bold: true, color: C.background1 });
    ["Admission control", "Routing policy", "Proxy with retries"].forEach((t, i) => {
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 3.3, y: 1.7 + i * 0.82, w: 2.7, h: 0.66, rectRadius: 0.06, fill: { color: i === 1 ? C.accent1 : C.text2 }, line: { color: i === 1 ? C.accent1 : C.text2, width: 0 }, objectName: t });
      text(s, t, { x: 3.3, y: 1.7 + i * 0.82, w: 2.7, h: 0.66, fontSize: 14, bold: true, color: C.background1, align: "center", valign: "middle" });
    });
    // replicas
    ["A", "B", "C", "D"].forEach((r, i) => {
      const y = 1.0 + i * 0.85;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 7.35, y, w: 2.15, h: 0.7, rectRadius: 0.06, fill: { color: C.background2 }, line: { color: C.accent4, width: 1 }, objectName: `Replica ${r}` });
      text(s, [{ text: `Replica ${r}`, options: { bold: true, breakLine: true } }, { text: "T4 GPU · vLLM" }], { x: 7.35, y, w: 2.15, h: 0.7, fontSize: 13, align: "center", valign: "middle" });
    });
    arrow(s, 2.55, 2.7, 3.05, 2.7, C.text1, "Console to router");
    arrow(s, 6.25, 2.7, 7.3, 2.7, C.text1, "Router to replicas");
    text(s, "HTTPS tunnels", { x: 6.1, y: 2.95, w: 1.3, h: 0.5, fontSize: 11, color: C.text2, align: "center" });
    text(s, "Requests flow left to right. Each routing step is also published as an event that the console draws live.", { x: 0.5, y: 4.6, w: 8.4, h: 0.6, fontSize: 14, color: C.text2 });
    s.addNotes("The browser console sends requests to the router. The router applies admission control, runs the routing policy to pick a replica, and proxies the request, retrying connection errors. Each replica is a Colab T4 GPU running vLLM, reached through an HTTPS tunnel because the nodes have no public address. In parallel, the router publishes an event for every step so the console can show what happened to each request.");
  }

  // ============ 6. How routing decides ============
  {
    const s = addSlide("Approach");
    title(s, "How the router decides");
    const steps = [
      ["Split the prompt", "The chat is tokenized and cut into 16-token blocks, matching the block size vLLM caches."],
      ["Chain the hashes", "Each block's hash includes the hash before it, so one hash identifies an entire prefix."],
      ["Find the longest match", "The index lists which replicas hold each hash. A match counts if it covers half the prompt or at least 256 tokens."],
      ["Check the response-time target", "A latency estimate from queue depth and recent latency rules out a replica that would be too slow."],
      ["Fall back to load", "With no usable match, the least-loaded replica is chosen. The chosen replica's index then records the prompt's blocks."],
    ];
    steps.forEach(([h, d], i) => {
      const y = 1.25 + i * 0.78;
      s.addShape(pres.shapes.OVAL, { x: 0.5, y: y + 0.08, w: 0.5, h: 0.5, fill: { color: C.accent1 }, line: { color: C.accent1, width: 0 }, objectName: `Step ${i + 1} number` });
      text(s, String(i + 1), { x: 0.5, y: y + 0.08, w: 0.5, h: 0.5, fontSize: 16, bold: true, color: C.background1, align: "center", valign: "middle" });
      text(s, h, { x: 1.2, y, w: 2.55, h: 0.66, fontSize: 15, bold: true, valign: "middle" });
      text(s, d, { x: 3.85, y, w: 5.65, h: 0.66, fontSize: 14, color: C.text2, valign: "middle" });
    });
    s.addNotes("Walk through the five steps. The prompt is split into blocks of sixteen tokens. Each block is hashed together with the previous hash, which means a single hash stands for everything before it too. The router looks those hashes up in its index to see which replica holds the longest run. A match is only used if it is large enough to matter and if the replica is predicted to answer within the response-time target. Otherwise the router picks the least-loaded replica. After choosing, it records the prompt's blocks against that replica so that similar requests follow.");
  }

  // ============ 7. Policies compared ============
  {
    const s = addSlide("Approach");
    title(s, "Four policies compared");
    const head = (t) => ({ text: t, options: { bold: true, color: C.background1, fill: { color: C.text1 }, fontSize: 13, fontFace: THEME.bodyFontFace, valign: "middle" } });
    const cell = (t, o = {}) => ({ text: t, options: { fontSize: 12, fontFace: THEME.bodyFontFace, color: C.text1, valign: "middle", ...o } });
    const rows = [
      ["round_robin", "Nothing. Replicas take turns.", "Simple and perfectly even.", "Ignores both load and cache."],
      ["least_connections", "Requests currently in flight on each replica.", "Adapts as load changes.", "Ignores which replica holds the cache."],
      ["swiftserve", "Session affinity, then a prefix trie over whole messages.", "Keeps a conversation on one replica.", "Needs two shared messages. Single-turn traffic falls back to load."],
      ["prefix_aware", "16-token block hashes, a load estimate and the response-time target.", "Reuses shared prefixes across different users.", "The index can go stale after cache evictions or failures."],
    ];
    s.addTable([
      [head("Policy"), head("What it looks at"), head("Strength"), head("Weakness")],
      ...rows.map((r, i) => r.map((t, j) => cell(t, { bold: j === 0, fill: { color: i % 2 ? C.background1 : C.background2 }, ...(i === 3 ? { fill: { color: C.background2 } } : {}) }))),
    ], { x: 0.5, y: 1.3, w: 9, colW: [1.7, 2.6, 2.3, 2.4], border: { type: "solid", pt: 0.5, color: C.accent4 }, rowH: [0.4, 0.8, 0.8, 0.95, 0.95], margin: [0.05, 0.1, 0.05, 0.1], objectName: "Policy comparison table" });
    s.addNotes("These are the four routing policies implemented in the router. Round robin and least connections are the baselines: neither knows anything about the cache. Swiftserve is the earlier policy, which tracks conversations and a trie of whole messages, so it needs at least two shared messages before it can match. Prefix-aware matches at the level of 16-token blocks, so it can reuse a shared system prompt across different users and different conversations.");
  }

  // ============ 8. Observability ============
  {
    const s = addSlide("Evidence");
    title(s, "Every request is traceable");
    imageFit(s, path.join(HERE, "assets/request_decision.png"), 0.5, 1.25, 5.6, 2.7, "Routing decision screenshot");
    imageFit(s, path.join(HERE, "assets/request_tokens.png"), 0.5, 4.05, 5.6, 1.2, "Cache tokens screenshot");
    const items = [
      "Which replica was selected, dispatched to and served the request",
      "The routing decision and the reason for it",
      "Time to first token, inter-token latency and total time, measured at the router",
      "Cached tokens reported by the engine, next to the router's prediction",
    ];
    text(s, items.map((t, i) => ({ text: t, options: { bullet: true, breakLine: i < items.length - 1 } })), { x: 6.4, y: 1.4, w: 3.1, h: 3.7, fontSize: 14, paraSpaceAfter: 10 });
    s.addNotes("Each request gets an identifier, and the router records what it decided and what happened. The screenshot shows one request: the policy chose the replica because it held the prompt's first block, and the router's predicted cache reuse matched what vLLM reported afterward. Selected, dispatched and served are kept separate because they can differ when a request is retried.");
  }

  // ============ 9. Metrics ============
  {
    const s = addSlide("Evidence");
    title(s, "How latency is measured");
    // timeline
    s.addShape(pres.shapes.LINE, { x: 1.5, y: 2.15, w: 7.2, h: 0, line: { color: C.accent4, width: 2 }, objectName: "Timeline" });
    const marks = [[1.5, "Request received"], [3.7, "First token"], [8.2, "Last token"]];
    marks.forEach(([x, l], i) => {
      s.addShape(pres.shapes.OVAL, { x: x - 0.1, y: 2.05, w: 0.2, h: 0.2, fill: { color: C.accent1 }, line: { color: C.accent1, width: 0 }, objectName: `Mark ${i + 1}` });
      text(s, l, { x: x - 0.9, y: 2.35, w: 1.8, h: 0.3, fontSize: 14, bold: true, align: "center" });
    });
    text(s, "TTFT", { x: 1.5, y: 1.55, w: 2.2, h: 0.35, fontSize: 16, bold: true, color: C.accent1, align: "center" });
    text(s, "ITL: gaps between tokens", { x: 3.7, y: 1.55, w: 4.5, h: 0.35, fontSize: 16, bold: true, color: C.accent3, align: "center" });
    const defs = [
      ["TTFT", "Time to first token", "From the request arriving at the router to the first response chunk that contains text."],
      ["ITL", "Inter-token latency", "Time between the first and last text chunks divided by the number of gaps. Reported as a mean, because tunnels deliver chunks in bursts."],
      ["Cache reuse", "Prompt tokens reused", "Taken from the engine's own usage report and compared with the router's prediction."],
    ];
    defs.forEach(([k, h, d], i) => {
      const x = 0.5 + i * 3.1;
      card(s, x, 3.0, 2.8, 2.15, `Definition card ${i + 1}`);
      text(s, k, { x: x + 0.2, y: 3.12, w: 2.4, h: 0.35, fontSize: 18, bold: true, color: C.accent1 });
      text(s, h, { x: x + 0.2, y: 3.5, w: 2.4, h: 0.3, fontSize: 14, bold: true });
      text(s, d, { x: x + 0.2, y: 3.85, w: 2.4, h: 1.25, fontSize: 13, color: C.text2 });
    });
    s.addNotes("Time to first token is how long the user waits before anything appears; it contains the prefill, which is exactly what caching shortens. Inter-token latency describes the pace once generation starts. Network tunnels can bunch several tokens into one chunk, so single gaps are not reliable, but the mean over the whole span is. Cache reuse comes from the engine's usage report, so it is a measurement and not an estimate.");
  }

  // ============ 10. Measured through the router ============
  {
    const s = addSlide("Evidence");
    title(s, "A run through the router on four GPUs");
    s.addChart(pres.charts.BAR, [{ name: "Requests", labels: ["GPU A", "GPU B", "GPU C", "GPU D"], values: [18, 12, 22, 8] }], {
      x: 0.5, y: 1.25, w: 4.6, h: 3.1, barDir: "col", chartColors: [HEX.accent1], showTitle: true, title: "Requests served per replica", titleFontSize: 14, titleColor: HEX.dk1, titleFontFace: "+mn-lt",
      showValue: true, dataLabelPosition: "outEnd", dataLabelFontSize: 12, dataLabelColor: HEX.dk1, dataLabelFontFace: "+mn-lt",
      catAxisLabelColor: HEX.dk2, valAxisLabelColor: HEX.dk2, catAxisLabelFontSize: 12, valAxisLabelFontSize: 11, catAxisLabelFontFace: "+mn-lt", valAxisLabelFontFace: "+mn-lt",
      valGridLine: { color: "E1E4EA", size: 0.5 }, catGridLine: { style: "none" }, showLegend: false, valAxisMinVal: 0,
    });
    const stats = [["60 of 60", "requests completed, concurrency 8"], ["374 / 4,944 ms", "time to first token, median / 95th percentile"], ["34.2 ms", "mean inter-token latency under load"], ["16 and 16", "cached tokens predicted, and reported"]];
    stats.forEach(([n, l], i) => {
      const y = 1.25 + i * 0.78;
      card(s, 5.4, y, 4.1, 0.68, `Result card ${i + 1}`);
      text(s, n, { x: 5.55, y, w: 1.75, h: 0.68, fontFace: THEME.headFontFace, fontSize: 16, bold: true, valign: "middle", color: C.accent1 });
      text(s, l, { x: 7.3, y, w: 2.1, h: 0.68, fontSize: 12, valign: "middle", color: C.text2 });
    });
    text(s, "The prompts in this run were only 40 tokens, so very little could be cached. It shows load spreading across four real GPUs and the router's prediction matching the engine. It is not a cache benchmark.", { x: 0.5, y: 4.5, w: 9, h: 0.7, fontSize: 14, color: C.text2 });
    s.addNotes("This run sent sixty requests at a concurrency of eight through the router to the four Colab GPUs, using the prefix-aware policy. All sixty completed. The distribution shows work spreading over all four replicas. The 95th percentile time to first token is much higher than the median, which points to the first requests reaching cold replicas and to queueing; that has not been broken down further. The prompts were very short, so this run is evidence that routing and measurement work end to end, not evidence of a cache speedup.");
  }

  // ============ 11. Policy comparison (only with measurements) ============
  const pol = (RESULTS.comparison && RESULTS.comparison.policies) || {};
  const names = ["round_robin", "least_connections", "swiftserve", "prefix_aware"].filter((n) => pol[n]);
  if (names.length >= 2) {
    const s = addSlide("Evidence");
    title(s, "Policy comparison");
    const v = (k) => names.map((n) => pol[n][k]);
    s.addChart(pres.charts.BAR, [
      { name: "TTFT median (ms)", labels: names, values: v("ttft_p50") },
      { name: "TTFT 95th percentile (ms)", labels: names, values: v("ttft_p95") },
    ], {
      x: 0.5, y: 1.25, w: 5.3, h: 3.3, barDir: "col", barGrouping: "clustered", chartColors: [HEX.accent1, HEX.accent2], showLegend: true, legendPos: "b", legendFontSize: 12, legendColor: HEX.dk2, legendFontFace: "+mn-lt",
      showValue: true, dataLabelPosition: "outEnd", dataLabelFontSize: 11, dataLabelColor: HEX.dk1, dataLabelFontFace: "+mn-lt", catAxisLabelColor: HEX.dk2, valAxisLabelColor: HEX.dk2, catAxisLabelFontSize: 12, valAxisLabelFontSize: 11,
      catAxisLabelFontFace: "+mn-lt", valAxisLabelFontFace: "+mn-lt", valGridLine: { color: "E1E4EA", size: 0.5 }, catGridLine: { style: "none" }, valAxisMinVal: 0,
    });
    const head = (t) => ({ text: t, options: { bold: true, color: C.background1, fill: { color: C.text1 }, fontSize: 12, fontFace: THEME.bodyFontFace } });
    const cell = (t, o = {}) => ({ text: String(t), options: { fontSize: 12, fontFace: THEME.bodyFontFace, color: C.text1, ...o } });
    s.addTable([[head("Policy"), head("Cache reuse"), head("Tokens/s")],
      ...names.map((n, i) => [cell(n, { bold: true, fill: { color: i % 2 ? C.background1 : C.background2 } }), cell(pol[n].cache_reuse_pct + " %", { fill: { color: i % 2 ? C.background1 : C.background2 } }), cell(pol[n].tokens_per_s, { fill: { color: i % 2 ? C.background1 : C.background2 } })])],
      { x: 6.1, y: 1.4, w: 3.4, colW: [1.6, 0.95, 0.85], border: { type: "solid", pt: 0.5, color: C.accent4 }, margin: [0.05, 0.08, 0.05, 0.08], objectName: "Comparison table" });
    if (RESULTS.comparison.workload) text(s, RESULTS.comparison.workload, { x: 0.5, y: 4.7, w: 9, h: 0.55, fontSize: 12, color: C.text2 });
    s.addNotes("All policies ran the same workload on the same four replicas, with a different leading word in the shared system prompt each time so that no policy benefited from a cache warmed by an earlier run.");
  }

  // ============ 12. Demo ============
  {
    const s = addSlide("Evidence");
    title(s, "Live demonstration");
    const steps = [["Dashboard", "four GPUs with memory and temperature reported by each node"], ["Playground", "one request twice: cold, then with the prefix cached"], ["Load test", "sixty concurrent requests spread across the replicas"]];
    steps.forEach(([h, d], i) => {
      const x = 0.5 + i * 3.1;
      card(s, x, 1.25, 2.8, 1.45, `Demo step ${i + 1}`);
      s.addShape(pres.shapes.OVAL, { x: x + 0.15, y: 1.37, w: 0.4, h: 0.4, fill: { color: C.accent1 }, line: { color: C.accent1, width: 0 }, objectName: `Demo step ${i + 1} number` });
      text(s, String(i + 1), { x: x + 0.15, y: 1.37, w: 0.4, h: 0.4, fontSize: 14, bold: true, color: C.background1, align: "center", valign: "middle" });
      text(s, h, { x: x + 0.7, y: 1.37, w: 2.0, h: 0.4, fontSize: 16, bold: true, valign: "middle" });
      text(s, d, { x: x + 0.15, y: 1.88, w: 2.5, h: 0.75, fontSize: 14, color: C.text2 });
    });
    imageFit(s, path.join(HERE, "assets/dashboard_gpus.png"), 0.5, 3.0, 9, 2.0, "Dashboard screenshot");
    s.addNotes("Run the demonstration in this order. Start on the dashboard to show the real GPUs. Then use the playground to send the same long system prompt twice and point at the cached token count and the time to first token. Finish with the load test and the live routing view to show concurrent requests being spread across replicas.");
  }

  // ============ 13. Limits and next steps ============
  {
    const s = addSlide("Close");
    title(s, "Limits and next steps");
    const cols = [
      ["Current limits", HEX.accent5, ["A failed request can leave a stale entry in the router's cache index.", "Connection errors retry the same replica; there is no failover to another one.", "Latency measured through tunnels includes network delay.", "Results so far come from short runs on a single model."]],
      ["Next steps", HEX.accent1, ["Run every policy on one long shared-prefix workload and report the differences.", "Rebuild the index from the engine's cache events instead of predictions.", "Fail over to another replica on connection errors.", "Keep the index across router restarts."]],
    ];
    cols.forEach(([h, col, items], i) => {
      const x = 0.5 + i * 4.6;
      card(s, x, 1.3, 4.4, 3.35, `${h} panel`);
      text(s, h, { x: x + 0.25, y: 1.45, w: 3.9, h: 0.4, fontFace: THEME.headFontFace, fontSize: 20, bold: true, color: i === 0 ? C.accent5 : C.accent1 });
      text(s, items.map((t, k) => ({ text: t, options: { bullet: true, breakLine: k < items.length - 1 } })), { x: x + 0.25, y: 2.0, w: 3.9, h: 3.0, fontSize: 14, paraSpaceAfter: 8 });
    });
    s.addNotes("Be direct about the limitations. The index is a belief about what each replica holds, and it can drift after cache evictions or failed requests. Retries stay on the same replica. The measurements so far are short and on one model. The next steps address each of these, starting with a fair comparison of all four policies on the same workload.");
  }

  // ============ 14. Summary ============
  {
    const s = addSlide("Close", "DARK");
    text(s, "Summary", { x: 0.7, y: 0.6, w: 8.6, h: 0.8, fontFace: THEME.headFontFace, fontSize: 34, bold: true, color: C.background1 });
    const pts = [
      ["Caches are per replica", "A balancer that ignores them wastes the work vLLM could reuse."],
      ["Route by prefix, protect by latency", "Hash prompts in 16-token blocks, pick the longest match, and veto replicas that would be too slow."],
      ["Measure every step", "Each request records its decision, its replica and its timings, so claims can be checked."],
    ];
    pts.forEach(([h, d], i) => {
      const y = 1.7 + i * 1.15;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.7, y, w: 8.6, h: 0.95, rectRadius: 0.08, fill: { color: C.text2 }, line: { color: C.text2, width: 0 }, objectName: `Summary card ${i + 1}` });
      text(s, h, { x: 0.95, y, w: 3.4, h: 0.95, fontSize: 15, bold: true, color: C.background1, valign: "middle" });
      text(s, d, { x: 4.5, y, w: 4.6, h: 0.95, fontSize: 14, color: "D5DAE3", valign: "middle" });
    });
    s.addNotes("Close with the three points. Caches are per replica. Routing by prefix and protecting with a latency check lets the router reuse cached work without overloading a replica. And every request is measured, so the claims in this presentation can be checked against the recorded data.");
  }

  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log("wrote", OUT);
})();
