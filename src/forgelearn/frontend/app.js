/*
 * ForgeLearn, guided-lesson redesign.
 *
 * Flow: topic -> interview -> a syllabus of lessons -> per lesson:
 *   teach (a plain concept + an interactive widget) -> a quick check ->
 *   watch the AI demo a worked example -> build YOUR OWN version in the editor
 *   (with Hint and Check my work) -> the next lesson unlocks.
 *
 * The middle column carries the lesson conversation; the right column is the
 * workspace + a code editor you type in. Widgets are the AI's self-contained HTML
 * rendered in a sandboxed iframe.
 */

const AGENTS_PATH = "/api/agents";
const FILES_PATH = "/api/files";
const FILE_PATH = "/api/file";
const FILE_RAW_PATH = "/api/file/raw";
const FILE_SAVE_PATH = "/api/file/save";
const RUN_PATH = "/api/run";
const C_START = "/api/course/start";
const C_INTERVIEW = "/api/course/interview";
const C_SESSION = "/api/course/session";
const C_SESSIONS = "/api/course/sessions";
const C_OPEN = "/api/course/open";
const C_CHECK = "/api/course/check";
const C_DEMO = "/api/course/demo";
const C_BUILD = "/api/course/build";
const C_HINT = "/api/course/hint";

const SESSION_KEY = "forgelearn.session";
const GRADE_KEY = "forgelearn.grade";

const TERMINAL = new Set(["done", "error"]);
const HIDDEN = new Set(["system"]);
const ICONS = { file_write: "\u{1F4C4}", command: "❯", tool: "·", tool_result: "↳" };
const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico"]);
const STATUS_LABEL = { locked: "locked", active: "up next", built: "built", complete: "done" };
const STAGE_LABEL = {
  new: "just started", interview: "in the interview", syllabus: "picking a lesson",
  learning: "in a lesson", complete: "completed",
};

const chat = document.getElementById("chat");
const intro = document.getElementById("intro");
const form = document.getElementById("composer");
const promptEl = document.getElementById("prompt");
const agentEl = document.getElementById("agent");
const gradeEl = document.getElementById("grade");
const sendBtn = document.getElementById("send");
const runBtn = document.getElementById("run");
const hintBtn = document.getElementById("hint");
const filesEl = document.getElementById("files");
const railEl = document.getElementById("syllabus");
const missionEl = document.getElementById("mission");
const lessonsEl = document.getElementById("lessons");
const progressEl = document.getElementById("progress");
const exportBtn = document.getElementById("export");
const editorWrap = document.getElementById("editor-wrap");
const editorEl = document.getElementById("editor");
const editorName = document.getElementById("editor-name");
const saveBtn = document.getElementById("save");
const checkWorkBtn = document.getElementById("check-work");
const viewer = document.getElementById("viewer");
const viewerName = document.getElementById("viewer-name");
const viewerContent = document.getElementById("viewer-content");
const viewerClose = document.getElementById("viewer-close");

const state = {
  session: null,
  stage: "new", // new | interview | syllabus | learning | complete
  questions: [],
  answers: [],
  qIndex: 0,
  lessons: [],
  activeLessonId: null,
  checkKind: null, // "mcq" | "short" while awaiting a check answer
};

let source = null; // the one in-flight SSE stream

/* --- Tiny DOM helpers ------------------------------------------------------ */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
function scrollToEnd() {
  requestAnimationFrame(() => (chat.scrollTop = chat.scrollHeight));
}
function addUserMessage(text) {
  const m = el("div", "msg user");
  m.append(el("div", "bubble", text));
  chat.append(m);
  scrollToEnd();
}
function addTutorMessage(text) {
  const m = el("div", "msg tutor");
  m.append(el("div", "tutor-body", text));
  chat.append(m);
  scrollToEnd();
  return m;
}
function showThinking(text) {
  const m = el("div", "msg tutor thinking");
  const b = el("div", "tutor-body");
  b.append(el("span", null, (text || "Thinking") + " "));
  const d = el("span", "dots");
  d.innerHTML = "<span>.</span><span>.</span><span>.</span>";
  b.append(d);
  m.append(b);
  chat.append(m);
  scrollToEnd();
  return m;
}
function addErrorMessage(text) {
  const m = el("div", "msg tutor error");
  m.append(el("div", "tutor-body", text));
  chat.append(m);
  scrollToEnd();
  return m;
}
function currentGrade() {
  return parseInt(gradeEl && gradeEl.value, 10) || 7;
}
function friendlyError(raw) {
  const r = (raw || "").toLowerCase();
  if (r.includes("not logged in") || r.includes("/login") || r.includes("unauthorized") || r.includes("authenticat"))
    return "The AI engine isn't signed in. Open a terminal and run `claude` (or `codex`) to log in, then try again.";
  if (r.includes("command not found") || r.includes("no such file") || r.includes("enoent"))
    return "The AI engine CLI wasn't found. Install `claude` or `codex` so it runs from your terminal, then try again.";
  if (r.includes("timed out") || r.includes("timeout"))
    return "The AI engine took too long and timed out. Please try again.";
  return null;
}

/* --- Networking ------------------------------------------------------------ */

async function postJSON(url, payload) {
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const raw = data.error || resp.statusText;
      addErrorMessage(friendlyError(raw) || "Something went wrong: " + raw);
      return null;
    }
    return data;
  } catch {
    addErrorMessage("Could not reach the server. Is `forgelearn` still running?");
    return null;
  }
}

/* --- Stage 1: topic + interview ------------------------------------------- */

function composerRole(show, placeholder) {
  form.hidden = !show;
  if (placeholder) promptEl.placeholder = placeholder;
  if (show) promptEl.focus();
}

async function startLearning(topic) {
  addUserMessage(topic);
  const t = showThinking("Reading your goal and preparing a few questions");
  const data = await postJSON(C_START, { topic, grade: currentGrade() });
  t.remove();
  if (!data) return;
  state.session = data.id;
  rememberSession(data.id);
  state.questions = data.interview_questions || [];
  state.answers = [];
  state.qIndex = 0;
  state.stage = "interview";
  addTutorMessage("Great. A few quick questions so I can tailor this to you.");
  askNextQuestion();
}

function askNextQuestion() {
  if (state.qIndex >= state.questions.length) return submitInterview();
  addTutorMessage(`Question ${state.qIndex + 1} of ${state.questions.length}: ${state.questions[state.qIndex]}`);
  composerRole(true, "Type your answer…");
}
function recordAnswer(answer) {
  addUserMessage(answer);
  state.answers.push(answer);
  state.qIndex += 1;
  askNextQuestion();
}

async function submitInterview() {
  composerRole(false);
  const t = showThinking("Designing your course of lessons");
  const session = await postJSON(C_INTERVIEW, { session: state.session, answers: state.answers, grade: currentGrade() });
  t.remove();
  if (!session) return;
  applySession(session);
  addTutorMessage("Here's your course on the left. Click the first lesson to start.");
}

/* --- Syllabus rail --------------------------------------------------------- */

function applySession(session) {
  state.stage = session.stage;
  state.lessons = session.lessons || [];
  state.activeLessonId = session.active_lesson_id;
  renderRail(session);
  exportBtn.hidden = !session.mission;
}

function renderRail(session) {
  railEl.hidden = false;
  missionEl.textContent = session.mission || "";
  lessonsEl.textContent = "";
  (session.lessons || []).forEach((le, i) => {
    const li = el("li", "lesson " + le.status);
    const head = el("div", "lesson-head");
    head.append(el("span", "lesson-title", `${i + 1}. ${le.title}`));
    head.append(el("span", "lesson-badge " + le.status, STATUS_LABEL[le.status] || le.status));
    li.append(head);
    if (le.goal) li.append(el("div", "lesson-goal", le.goal));
    if (le.status === "active" || le.status === "complete") {
      li.classList.add("clickable");
      li.addEventListener("click", () => openLesson(le.id));
    }
    lessonsEl.append(li);
  });
  progressEl.textContent = "";
  (session.progress || []).forEach((e) => progressEl.append(el("li", "progress-item", `${e.on}: ${e.note}`)));
}

/* --- Stage 2: open a lesson (teach) --------------------------------------- */

async function openLesson(lessonId) {
  if (source) return;
  const t = showThinking("Opening the lesson");
  const session = await postJSON(C_OPEN, { session: state.session, lesson: lessonId, grade: currentGrade() });
  t.remove();
  if (!session) return;
  applySession(session);
  const lesson = state.lessons.find((l) => l.id === lessonId);
  if (lesson) renderLesson(lesson);
}

function renderLesson(lesson) {
  state.activeLessonId = lesson.id;
  addTutorMessage(`Lesson: ${lesson.title}`);
  if (lesson.concept) addTutorMessage(lesson.concept);
  if (lesson.widget && lesson.widget.html) renderWidget(lesson.widget);
  if (lesson.check) renderCheck(lesson.check);
  else startDemoPrompt(lesson);
}

function renderWidget(widget) {
  const card = el("div", "widget-card");
  if (widget.title) card.append(el("div", "widget-title", widget.title));
  if (widget.caption) card.append(el("div", "widget-caption", widget.caption));
  const frame = document.createElement("iframe");
  frame.className = "widget-frame";
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("title", widget.title || "interactive");
  frame.srcdoc = widget.html;
  card.append(frame);
  chat.append(card);
  scrollToEnd();
}

function renderCheck(check) {
  addTutorMessage("Quick check: " + check.question);
  if (check.kind === "mcq" && (check.options || []).length) {
    state.checkKind = "mcq";
    const box = el("div", "options");
    check.options.forEach((opt) => {
      const b = el("button", "option", opt);
      b.addEventListener("click", () => {
        box.querySelectorAll("button").forEach((x) => (x.disabled = true));
        submitCheck(opt);
      });
      box.append(b);
    });
    chat.append(box);
    scrollToEnd();
    composerRole(false);
  } else {
    state.checkKind = "short";
    composerRole(true, "Type your answer…");
  }
}

async function submitCheck(answer) {
  addUserMessage(answer);
  state.checkKind = null;
  composerRole(false);
  const t = showThinking("Checking your answer");
  const data = await postJSON(C_CHECK, {
    session: state.session, lesson: state.activeLessonId, answer, grade: currentGrade(),
  });
  t.remove();
  if (!data) return;
  const card = el("div", "verdict " + (data.correct ? "pass" : "fail"));
  card.append(el("div", "verdict-badge", data.correct ? "Correct ✓" : "Not quite"));
  if (data.feedback) card.append(el("p", "verdict-feedback", data.feedback));
  if (data.explanation) card.append(el("p", "verdict-explain", data.explanation));
  chat.append(card);
  scrollToEnd();
  const lesson = state.lessons.find((l) => l.id === state.activeLessonId);
  startDemoPrompt(lesson);
}

/* --- Stage 3: the AI demo -------------------------------------------------- */

function startDemoPrompt(lesson) {
  const msg = addTutorMessage("Now watch me build a worked example. Then you'll build your own.");
  const btn = el("button", "cta", "Watch the demo");
  btn.addEventListener("click", () => {
    btn.disabled = true;
    startDemo(lesson ? lesson.id : state.activeLessonId);
  });
  msg.querySelector(".tutor-body").append(document.createElement("br"), btn);
  scrollToEnd();
}

function startDemo(lessonId) {
  if (source) return;
  const agent = agentEl.value;
  const url = C_DEMO + "?session=" + encodeURIComponent(state.session) +
    "&lesson=" + encodeURIComponent(lessonId) +
    "&grade=" + encodeURIComponent(currentGrade()) +
    (agent ? "&agent=" + encodeURIComponent(agent) : "");
  runStream(url, "Worked example", (endState) => {
    if (endState === "done") startBuild(lessonId);
    else refreshSession();
  });
}

/* --- Stage 4: your build --------------------------------------------------- */

async function startBuild(lessonId) {
  await refreshSession();
  const lesson = state.lessons.find((l) => l.id === lessonId);
  addTutorMessage(
    "Your turn 🔨 " + (lesson ? lesson.build_task : "Build your own version.") +
      " Write it in the editor on the right, Save, then press Check my work. Stuck? Press Hint.",
  );
  editorName.value = lesson && lesson.domain_type === "interactive" ? "index.html" : "main.py";
  editorEl.value = "";
  editorWrap.hidden = false;
  hintBtn.disabled = false;
  checkWorkBtn.disabled = false;
  editorEl.focus();
}

async function saveEditor() {
  if (!state.session) return false;
  const path = (editorName.value || "main.py").trim();
  const data = await postJSON(FILE_SAVE_PATH, { session: state.session, path, content: editorEl.value });
  if (data) refreshFiles();
  return !!data;
}

async function checkMyWork() {
  if (source) return;
  if (!(await saveEditor())) return;
  const t = showThinking("Looking over what you built");
  const data = await postJSON(C_BUILD, { session: state.session, lesson: state.activeLessonId, grade: currentGrade() });
  t.remove();
  if (!data) return;
  const card = el("div", "verdict " + (data.passed ? "pass" : "fail"));
  card.append(el("div", "verdict-badge", data.passed ? "Passed ✓" : "Not yet, keep going"));
  if (data.feedback) card.append(el("p", "verdict-feedback", data.feedback));
  if (data.hints && data.hints.length) {
    card.append(el("div", "verdict-label", "Try this:"));
    const ul = el("ul", "probes");
    data.hints.forEach((h) => ul.append(el("li", null, h)));
    card.append(ul);
  }
  if (data.progress_note) card.append(el("p", "verdict-progress", data.progress_note));
  chat.append(card);
  scrollToEnd();
  if (data.session) applySession(data.session);
  if (data.passed) {
    editorWrap.hidden = true;
    hintBtn.disabled = true;
    checkWorkBtn.disabled = true;
    if (data.next_lesson_id) {
      const next = (data.session.lessons || []).find((l) => l.id === data.next_lesson_id);
      addTutorMessage("Lesson complete 🎉 Next up: " + (next ? next.title : "the next lesson") + ". Click it on the left when ready.");
    } else {
      addTutorMessage("🎉 You've finished every lesson. Look how far you've come from day one.");
    }
  }
}

async function getHint() {
  if (source) return;
  await saveEditor();
  const t = showThinking("Thinking of a hint");
  const data = await postJSON(C_HINT, { session: state.session, lesson: state.activeLessonId, grade: currentGrade() });
  t.remove();
  if (data && data.hint) addTutorMessage("💡 " + data.hint);
}

/* --- SSE turn rendering (demo + run) -------------------------------------- */

function startAssistantTurn(title) {
  const msg = el("div", "msg assistant");
  const head = el("div", "turn-head");
  head.append(el("span", null, title));
  const status = el("span", "status building");
  status.innerHTML = '<span class="dots"><span>.</span><span>.</span><span>.</span></span>';
  head.append(status);
  const body = el("div", "turn-body");
  msg.append(head, body);
  const loading = el("div", "turn-loading");
  loading.append(el("span", "spinner"));
  loading.append(el("span", null, "Starting up… the AI is getting to work"));
  body.append(loading);
  chat.append(msg);
  scrollToEnd();
  const clearLoading = () => loading.parentNode && loading.remove();
  return {
    addEvent(evt) {
      const kind = evt.kind || "system";
      if (HIDDEN.has(kind)) return;
      clearLoading();
      if (kind === "narration") body.append(el("div", "ev narration", evt.text || ""));
      else if (kind === "done" || kind === "error") { if (evt.text) body.append(el("div", "turn-foot", evt.text)); }
      else body.append(renderStep(kind, evt));
      scrollToEnd();
    },
    finish(s) {
      clearLoading();
      status.className = "status " + s;
      status.textContent = s === "error" ? "error" : "done";
      scrollToEnd();
    },
  };
}
function renderStep(kind, evt) {
  const row = el("div", "ev step " + kind);
  if (evt.is_error) row.classList.add("is-error");
  row.append(el("span", "icon", ICONS[kind] || ICONS.tool));
  row.append(el("span", "step-text", kind === "file_write" && evt.path ? evt.path : evt.text || ""));
  return row;
}
function runStream(url, title, onFinish) {
  const turn = startAssistantTurn(title);
  let finished = false;
  setBusy(true);
  source = new EventSource(url);
  const done = (endState) => {
    endStream();
    refreshFiles();
    if (onFinish) onFinish(endState);
  };
  source.onmessage = (frame) => {
    let evt;
    try { evt = JSON.parse(frame.data); } catch { return; }
    turn.addEvent(evt);
    if (TERMINAL.has(evt.kind)) {
      finished = true;
      const endState = evt.kind === "error" ? "error" : "done";
      turn.finish(endState);
      done(endState);
    }
  };
  source.onerror = () => {
    if (finished) return;
    turn.addEvent({ kind: "error", text: "connection to the server was lost" });
    turn.finish("error");
    done("error");
  };
}
function setBusy(busy) {
  sendBtn.disabled = busy;
  promptEl.disabled = busy;
  agentEl.disabled = busy;
  if (busy) { runBtn.disabled = true; hintBtn.disabled = true; checkWorkBtn.disabled = true; }
}
function endStream() {
  if (source) { source.close(); source = null; }
  sendBtn.disabled = false;
  promptEl.disabled = false;
}

/* --- Workspace: files, viewer, run ---------------------------------------- */

async function refreshFiles() {
  if (!state.session) return;
  let files = [];
  try {
    const resp = await fetch(FILES_PATH + "?session=" + encodeURIComponent(state.session));
    if (resp.ok) files = (await resp.json()).files || [];
  } catch { return; }
  filesEl.textContent = "";
  if (!files.length) {
    filesEl.append(el("li", "files-empty", "Files appear here as you build."));
    runBtn.disabled = true;
    return;
  }
  for (const f of files) {
    const li = el("li", "file");
    li.append(el("span", "file-name", f.path));
    li.append(el("span", "file-size", formatSize(f.size)));
    li.addEventListener("click", () => openFile(f.path));
    filesEl.append(li);
  }
  runBtn.disabled = source !== null;
}
async function openFile(path) {
  viewerName.textContent = path;
  viewerContent.textContent = "";
  const ext = (path.split(".").pop() || "").toLowerCase();
  if (IMAGE_EXTS.has(ext)) {
    const img = el("img", "viewer-img");
    img.alt = path;
    img.src = FILE_RAW_PATH + "?session=" + encodeURIComponent(state.session) + "&path=" + encodeURIComponent(path);
    viewerContent.append(img);
    viewer.hidden = false;
    return;
  }
  try {
    const resp = await fetch(FILE_PATH + "?session=" + encodeURIComponent(state.session) + "&path=" + encodeURIComponent(path));
    if (!resp.ok) return;
    const pre = el("pre", "viewer-pre");
    pre.textContent = (await resp.json()).content || "";
    viewerContent.append(pre);
    viewer.hidden = false;
  } catch { /* best-effort */ }
}
function closeViewer() { viewer.hidden = true; viewerContent.textContent = ""; }
function formatSize(b) {
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
  return (b / 1048576).toFixed(1) + " MB";
}

/* --- Session refresh + resume + past sessions ----------------------------- */

async function refreshSession() {
  if (!state.session) return;
  try {
    const resp = await fetch(C_SESSION + "?session=" + encodeURIComponent(state.session));
    if (resp.ok) applySession(await resp.json());
  } catch { /* best-effort */ }
}
function rememberSession(id) { try { localStorage.setItem(SESSION_KEY, id); } catch {} }
function lastSessionId() { try { return localStorage.getItem(SESSION_KEY); } catch { return null; } }
function forgetSession() { try { localStorage.removeItem(SESSION_KEY); } catch {} }

async function tryResume() {
  const id = lastSessionId();
  if (!id) return false;
  let session;
  try {
    const resp = await fetch(C_SESSION + "?session=" + encodeURIComponent(id));
    if (!resp.ok) { forgetSession(); return false; }
    session = await resp.json();
  } catch { return false; }
  if (intro) intro.remove();
  state.session = session.id;
  rememberSession(session.id);
  if (gradeEl && session.reading_grade) gradeEl.value = String(session.reading_grade);
  applySession(session);
  refreshFiles();
  addTutorMessage('Welcome back to "' + (session.mission || session.topic) + '". Pick a lesson on the left to keep going.');
  composerRole(false);
  return true;
}

async function showPastSessions() {
  if (!intro) return;
  let sessions = [];
  try {
    const resp = await fetch(C_SESSIONS);
    if (!resp.ok) return;
    sessions = (await resp.json()).sessions || [];
  } catch { return; }
  sessions = sessions.filter((s) => s.mission || s.stage !== "new").slice(0, 8);
  if (!sessions.length || !intro) return;
  const box = el("div", "resume-box");
  box.append(el("div", "resume-title", "Or pick up where you left off"));
  const list = el("ul", "resume-list");
  for (const s of sessions) {
    const li = el("li", "resume-item");
    li.append(el("span", "resume-topic", s.mission || s.topic || "Untitled"));
    li.append(el("span", "resume-meta", (STAGE_LABEL[s.stage] || s.stage) + " · " + shortDate(s.created_at)));
    li.addEventListener("click", () => openPastSession(s.id));
    list.append(li);
  }
  box.append(list);
  intro.append(box);
}
function shortDate(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }); } catch { return ""; }
}
async function openPastSession(id) {
  let session;
  try {
    const resp = await fetch(C_SESSION + "?session=" + encodeURIComponent(id));
    if (!resp.ok) { addErrorMessage("Could not open that session."); return; }
    session = await resp.json();
  } catch { addErrorMessage("Could not reach the server."); return; }
  if (intro) intro.remove();
  state.session = session.id;
  rememberSession(session.id);
  if (gradeEl && session.reading_grade) gradeEl.value = String(session.reading_grade);
  applySession(session);
  refreshFiles();
  addTutorMessage('Welcome back to "' + (session.mission || session.topic) + '". Pick a lesson on the left to keep going.');
}

/* --- Provider dropdown + grade -------------------------------------------- */

async function loadAgents() {
  let data;
  try {
    const resp = await fetch(AGENTS_PATH);
    if (!resp.ok) return;
    data = await resp.json();
  } catch { return; }
  const agents = data.agents || [];
  agentEl.textContent = "";
  for (const name of agents) {
    const opt = el("option", null, name);
    opt.value = name;
    if (name === data.default_agent) opt.selected = true;
    agentEl.append(opt);
  }
  agentEl.hidden = agents.length < 2;
}
function setUpGrade() {
  if (!gradeEl) return;
  try { const saved = localStorage.getItem(GRADE_KEY); if (saved) gradeEl.value = saved; } catch {}
  gradeEl.addEventListener("change", () => { try { localStorage.setItem(GRADE_KEY, gradeEl.value); } catch {} });
}

/* --- Event wiring ---------------------------------------------------------- */

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const value = promptEl.value.trim();
  if (!value || source) return;
  if (intro) intro.remove();
  promptEl.value = "";
  if (value.startsWith("/")) {
    addErrorMessage("ForgeLearn has no slash commands. A \"/login\" message is for your agent CLI in the terminal (run `claude` or `codex`), not here.");
    return;
  }
  if (state.stage === "new") startLearning(value);
  else if (state.stage === "interview") recordAnswer(value);
  else if (state.checkKind === "short") submitCheck(value);
});

runBtn.addEventListener("click", () => {
  if (source || !state.session) return;
  runStream(RUN_PATH + "?session=" + encodeURIComponent(state.session), "Run");
});
hintBtn.addEventListener("click", getHint);
saveBtn.addEventListener("click", saveEditor);
checkWorkBtn.addEventListener("click", checkMyWork);
editorEl.addEventListener("keydown", (e) => {
  // Ctrl/Cmd+S saves; Tab inserts two spaces instead of leaving the editor.
  if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); saveEditor(); }
  if (e.key === "Tab") {
    e.preventDefault();
    const s = editorEl.selectionStart, en = editorEl.selectionEnd;
    editorEl.value = editorEl.value.slice(0, s) + "  " + editorEl.value.slice(en);
    editorEl.selectionStart = editorEl.selectionEnd = s + 2;
  }
});
exportBtn.addEventListener("click", () => {
  if (state.session) window.location.href = "/api/learn/export?session=" + encodeURIComponent(state.session);
});
viewerClose.addEventListener("click", closeViewer);
viewer.addEventListener("click", (e) => { if (e.target === viewer) closeViewer(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !viewer.hidden) closeViewer(); });

/* --- Boot ------------------------------------------------------------------ */

async function boot() {
  setUpGrade();
  await loadAgents();
  const resumed = await tryResume();
  if (!resumed) { composerRole(true, "What do you want to learn?"); showPastSessions(); }
}
boot();
