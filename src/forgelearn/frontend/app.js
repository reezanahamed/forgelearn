/*
 * ForgeLearn — the learning method in the browser (Phase 7).
 *
 * The page walks a learner through the method:
 *
 *   topic → interview (a few adaptive questions) → mission + ladder → build a rung (watched
 *   live) → teach-back gate → unlock the next rung.
 *
 * One composer, three roles: it asks the topic, takes an interview answer, or
 * takes a teach-back explanation — app.js sets its placeholder and shows/hides it
 * per stage. The LADDER rail (left) shows the mission, the rungs with their
 * status, and progress vs day one; the WORKSPACE (right) shows the files the
 * agent wrote and a Run button. Building a rung streams over SSE and reuses the
 * exact turn-rendering the earlier phases built.
 *
 * The learning SESSION id comes from the server (POST /api/learn/start) and is
 * the same id the workspace endpoints key on, so a rung's files land where Run
 * and the file tree read them.
 */

const AGENTS_PATH = "/api/agents";
const FILES_PATH = "/api/files";
const FILE_PATH = "/api/file";
const RUN_PATH = "/api/run";
const LEARN_START = "/api/learn/start";
const LEARN_INTERVIEW = "/api/learn/interview";
const LEARN_SESSION = "/api/learn/session";
const LEARN_BUILD = "/api/learn/build";
const LEARN_TEACHBACK = "/api/learn/teachback";
const LEARN_EXPORT = "/api/learn/export";

// The last session id, stored client-side so a returning learner resumes it
// (the sessions themselves are persisted server-side; Phase 8).
const SESSION_KEY = "forgelearn.session";

// Kinds that end a stream: on these we close it and finish the turn.
const TERMINAL = new Set(["done", "error"]);
// Setup/lifecycle noise hidden so the teaching feed stays clean.
const HIDDEN = new Set(["system"]);
// Small glyphs labelling each action kind at a glance.
const ICONS = { file_write: "\u{1F4C4}", command: "❯", tool: "·", tool_result: "↳" };

// Human-readable status for each rung, shown as a badge in the ladder rail.
const RUNG_STATUS = {
  locked: "locked",
  active: "up next",
  built: "built",
  complete: "done",
};

const chat = document.getElementById("chat");
const intro = document.getElementById("intro");
const form = document.getElementById("composer");
const promptEl = document.getElementById("prompt");
const agentEl = document.getElementById("agent");
const sendBtn = document.getElementById("send");
const runBtn = document.getElementById("run");
const filesEl = document.getElementById("files");
const viewer = document.getElementById("viewer");
const viewerName = document.getElementById("viewer-name");
const viewerBody = document.getElementById("viewer-body");
const ladderEl = document.getElementById("ladder");
const missionEl = document.getElementById("mission");
const rungsEl = document.getElementById("rungs");
const progressEl = document.getElementById("progress");
const exportBtn = document.getElementById("export");

// The whole client state. `stage` decides what a composer submit does.
const state = {
  session: null, // server-issued learning session id (also the workspace id)
  stage: "new", // new | interview | ladder | building | teachback | complete
  questions: [], // interview questions
  answers: [], // answers gathered so far
  qIndex: 0, // which interview question we're on
  projects: [], // the ladder rungs
  activeProjectId: null, // the rung currently being built / taught back
};

// The one in-flight SSE stream, or null when idle.
let source = null;

/** Create an element with a class and optional text, in one call. */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/** Keep the newest content in view as the conversation grows. */
function scrollToEnd() {
  chat.scrollTop = chat.scrollHeight;
}

/** Append the learner's message as a right-aligned bubble. */
function addUserMessage(text) {
  const msg = el("div", "msg user");
  msg.append(el("div", "bubble", text));
  chat.append(msg);
  scrollToEnd();
}

/** Append a tutor message (question, announcement, verdict) as a left card. */
function addTutorMessage(text) {
  const msg = el("div", "msg tutor");
  msg.append(el("div", "tutor-body", text));
  chat.append(msg);
  scrollToEnd();
  return msg;
}

/**
 * Append an animated "thinking…" indicator while the AI works. Returns the node;
 * call .remove() on it once the response (or an error) arrives.
 */
function showThinking(text) {
  const msg = el("div", "msg tutor thinking");
  const body = el("div", "tutor-body");
  body.append(el("span", null, (text || "Thinking") + " "));
  const dots = el("span", "dots");
  dots.innerHTML = "<span>.</span><span>.</span><span>.</span>";
  body.append(dots);
  msg.append(body);
  chat.append(msg);
  scrollToEnd();
  return msg;
}

/** Append a clearly-marked error card (distinct from a normal tutor message). */
function addErrorMessage(text) {
  const msg = el("div", "msg tutor error");
  msg.append(el("div", "tutor-body", text));
  chat.append(msg);
  scrollToEnd();
  return msg;
}

/**
 * Turn a raw backend/agent error into clear, actionable guidance. The engine is a
 * separate CLI (claude/codex) the user runs and authenticates in their own
 * terminal, so its failures need translating into "here's what to do". Returns a
 * friendly string, or null when there's no better wording than the raw message.
 */
function friendlyError(raw) {
  const r = (raw || "").toLowerCase();
  if (
    r.includes("not logged in") ||
    r.includes("/login") ||
    r.includes("logged out") ||
    r.includes("unauthorized") ||
    r.includes("authenticat")
  ) {
    return (
      "The AI engine isn't signed in. This sign-in happens in your terminal, not " +
      "here: open a terminal and run `claude` (for Claude Code) or `codex` (for " +
      "OpenAI Codex), complete the login, then come back and send your topic again."
    );
  }
  if (r.includes("api key") || r.includes("api_key") || r.includes("invalid key")) {
    return (
      "The AI engine rejected its API key. Check the key or login for your agent " +
      "CLI (claude or codex) in your terminal, then try again."
    );
  }
  if (r.includes("command not found") || r.includes("no such file") || r.includes("enoent")) {
    return (
      "The AI engine CLI wasn't found. Install `claude` (Claude Code) or `codex` " +
      "(OpenAI Codex) so it runs from your terminal, then try again."
    );
  }
  if (r.includes("credit") || r.includes("quota") || r.includes("rate limit") || r.includes("insufficient")) {
    return "The AI engine hit a usage or billing limit. Check your provider account, then try again. Details: " + raw;
  }
  if (r.includes("timed out") || r.includes("timeout")) {
    return "The AI engine took too long and timed out. Try again; a simpler topic or a faster model can help.";
  }
  return null;
}

/* --- Stage 1: topic + interview ------------------------------------------- */

/** Show or hide the composer and set its placeholder for the current role. */
function composerRole(show, placeholder) {
  form.hidden = !show;
  if (placeholder) promptEl.placeholder = placeholder;
  if (show) promptEl.focus();
}

/** Begin: send the topic, get interview questions, ask the first. */
async function startLearning(topic) {
  addUserMessage(topic);
  const thinking = showThinking("Reading your goal and preparing a few questions");
  const data = await postJSON(LEARN_START, { topic });
  thinking.remove();
  if (!data) return;

  state.session = data.id;
  rememberSession(data.id);
  state.questions = data.interview_questions || [];
  state.answers = [];
  state.qIndex = 0;
  state.stage = "interview";
  addTutorMessage(
    "Great. A few quick questions so I can tailor this to you.",
  );
  askNextQuestion();
}

/** Show the next interview question, or submit once all are answered. */
function askNextQuestion() {
  if (state.qIndex >= state.questions.length) {
    submitInterview();
    return;
  }
  const q = state.questions[state.qIndex];
  addTutorMessage(`Question ${state.qIndex + 1} of ${state.questions.length}: ${q}`);
  composerRole(true, "Type your answer…");
}

/** Record an interview answer and advance. */
function recordAnswer(answer) {
  addUserMessage(answer);
  state.answers.push(answer);
  state.qIndex += 1;
  askNextQuestion();
}

/** Submit the interview answers; render the mission + ladder. */
async function submitInterview() {
  composerRole(false);
  const thinking = showThinking("Designing your ladder of projects");
  const session = await postJSON(LEARN_INTERVIEW, {
    session: state.session,
    answers: state.answers,
  });
  thinking.remove();
  if (!session) return;
  applySession(session);
  addTutorMessage(
    "Here's your mission and ladder on the left. Press Build on the first " +
      "project when you're ready, and I'll build it with you.",
  );
}

/* --- Stage 2: the ladder rail --------------------------------------------- */

/** Update client state + the ladder rail from a full session payload. */
function applySession(session) {
  state.stage = session.stage;
  state.projects = session.projects || [];
  state.activeProjectId = session.current_project_id;
  renderLadder(session);
  // Once there's a mission, the session is worth keeping — offer the export.
  exportBtn.hidden = !session.mission;
}

/** Render the mission, rungs (with a Build button on the active one), progress. */
function renderLadder(session) {
  ladderEl.hidden = false;
  missionEl.textContent = session.mission || "";

  rungsEl.textContent = "";
  (session.projects || []).forEach((p, i) => {
    const li = el("li", "rung " + p.status);
    const head = el("div", "rung-head");
    head.append(el("span", "rung-title", `${i + 1}. ${p.you_build}`));
    head.append(el("span", "rung-badge " + p.status, RUNG_STATUS[p.status] || p.status));
    li.append(head);
    li.append(el("div", "rung-learn", "Learn: " + p.you_learn));

    // The active, not-yet-built rung gets a Build button (idle only).
    if (p.status === "active") {
      const build = el("button", "rung-build", "Build");
      build.disabled = source !== null;
      build.addEventListener("click", () => buildRung(p.id));
      li.append(build);
    }
    rungsEl.append(li);
  });

  progressEl.textContent = "";
  (session.progress || []).forEach((entry) => {
    progressEl.append(el("li", "progress-item", `${entry.on}: ${entry.note}`));
  });
}

/* --- Stage 3: build a rung (watched live) --------------------------------- */

/** Build the given rung: stream the agent's work, then open the teach-back. */
function buildRung(projectId) {
  if (source) return; // one stream at a time
  state.activeProjectId = projectId;
  state.stage = "building";
  const project = state.projects.find((p) => p.id === projectId);
  addTutorMessage(`Building: ${project ? project.you_build : projectId}`);

  const agent = agentEl.value; // "" → server default
  const url =
    LEARN_BUILD +
    "?session=" + encodeURIComponent(state.session) +
    "&project=" + encodeURIComponent(projectId) +
    (agent ? "&agent=" + encodeURIComponent(agent) : "");
  runStream(url, project ? project.you_build : "Build", (endState) => {
    if (endState === "done") openTeachBack(projectId);
    else refreshSession(); // failed build — re-enable the Build button
  });
}

/* --- Stage 4: the teach-back gate ----------------------------------------- */

/** Prompt the learner to explain what they built. */
async function openTeachBack(projectId) {
  await refreshSession(); // reflect the BUILT status server-side
  state.stage = "teachback";
  state.activeProjectId = projectId;
  const project = state.projects.find((p) => p.id === projectId);
  addTutorMessage(
    "Now teach it back: in your own words, explain how " +
      (project ? `"${project.you_learn}"` : "this") +
      " actually works. Explaining it is how it sticks.",
  );
  composerRole(true, "Explain how it works, in your own words…");
}

/** Submit an explanation; render the verdict; unlock or ask to try again. */
async function submitTeachBack(explanation) {
  addUserMessage(explanation);
  composerRole(false);
  const thinking = showThinking("Thinking about your explanation");
  const data = await postJSON(LEARN_TEACHBACK, {
    session: state.session,
    project: state.activeProjectId,
    explanation,
  });
  thinking.remove();
  if (!data) return;

  renderVerdict(data);
  if (data.session) applySession(data.session);

  if (data.passed) {
    if (data.next_project_id) {
      const next = (data.session.projects || []).find((p) => p.id === data.next_project_id);
      addTutorMessage(
        "Unlocked the next project" +
          (next ? `: ${next.you_build}` : "") +
          ". Press Build on the left when you're ready.",
      );
      state.stage = "ladder";
    } else {
      addTutorMessage("🎉 You've completed every project on your ladder. Look how far you've come from day one.");
      state.stage = "complete";
    }
  } else {
    // Not passed: stay in teach-back so the learner can refine and resubmit.
    state.stage = "teachback";
    composerRole(true, "Refine your explanation and try again…");
  }
}

/** Render the judge's verdict: pass/fail badge, feedback, probes, notes. */
function renderVerdict(data) {
  const card = el("div", "verdict " + (data.passed ? "pass" : "fail"));
  card.append(el("div", "verdict-badge", data.passed ? "Passed ✓" : "Not yet, keep going"));
  if (data.feedback) card.append(el("p", "verdict-feedback", data.feedback));
  if (data.probes && data.probes.length) {
    card.append(el("div", "verdict-label", "Sharpen these:"));
    const ul = el("ul", "probes");
    data.probes.forEach((p) => ul.append(el("li", null, p)));
    card.append(ul);
  }
  if (data.progress_note) card.append(el("p", "verdict-progress", data.progress_note));
  if (data.storage_note) card.append(el("p", "verdict-storage", "Make it stick: " + data.storage_note));
  chat.append(card);
  scrollToEnd();
}

/* --- SSE build stream (reused turn rendering) ----------------------------- */

/** Start a new assistant turn: a titled card whose body fills with events. */
function startAssistantTurn(title) {
  const msg = el("div", "msg assistant");
  const head = el("div", "turn-head");
  head.append(el("span", null, title));
  const status = el("span", "status building");
  status.innerHTML = '<span class="dots"><span>.</span><span>.</span><span>.</span></span>';
  head.append(status);
  const body = el("div", "turn-body");
  msg.append(head, body);

  // A spinner placeholder shown until the first real activity streams in, since
  // the agent can take a while to spin up before its first event arrives.
  const loading = el("div", "turn-loading");
  loading.append(el("span", "spinner"));
  loading.append(el("span", null, "Starting up… the AI is getting to work"));
  body.append(loading);

  chat.append(msg);
  scrollToEnd();

  const clearLoading = () => {
    if (loading.parentNode) loading.remove();
  };

  return {
    addEvent(evt) {
      const kind = evt.kind || "system";
      if (HIDDEN.has(kind)) return;
      clearLoading(); // first visible event: drop the spinner
      if (kind === "narration") {
        body.append(el("div", "ev narration", evt.text || ""));
      } else if (kind === "done" || kind === "error") {
        if (evt.text) body.append(el("div", "turn-foot", evt.text));
      } else {
        body.append(renderStep(kind, evt));
      }
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

/** Build a compact sub-step row for a file write / command / tool action. */
function renderStep(kind, evt) {
  const row = el("div", "ev step " + kind);
  if (evt.is_error) row.classList.add("is-error");
  row.append(el("span", "icon", ICONS[kind] || ICONS.tool));
  const label = kind === "file_write" && evt.path ? evt.path : evt.text || "";
  row.append(el("span", "step-text", label));
  return row;
}

/**
 * Open an SSE stream at `url`, render its events into a new turn, resolve via
 * `onFinish(state)`. Shared by Build and Run. Disables controls while in flight
 * and refreshes the workspace when it ends.
 */
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
    try {
      evt = JSON.parse(frame.data);
    } catch {
      return;
    }
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

/** Toggle the composer + Run + Build buttons while a stream is in flight. */
function setBusy(busy) {
  sendBtn.disabled = busy;
  promptEl.disabled = busy;
  agentEl.disabled = busy;
  if (busy) runBtn.disabled = true;
  rungsEl.querySelectorAll(".rung-build").forEach((b) => (b.disabled = busy));
}

/** Close the in-flight stream and re-enable the composer. */
function endStream() {
  if (source) {
    source.close();
    source = null;
  }
  sendBtn.disabled = false;
  promptEl.disabled = false;
}

/* --- Workspace: files, viewer, Run ---------------------------------------- */

/** Fetch the session's file tree and render it; enable Run if there are files. */
async function refreshFiles() {
  if (!state.session) return;
  let files = [];
  try {
    const resp = await fetch(FILES_PATH + "?session=" + encodeURIComponent(state.session));
    if (resp.ok) files = (await resp.json()).files || [];
  } catch {
    return;
  }
  filesEl.textContent = "";
  if (files.length === 0) {
    filesEl.append(el("li", "files-empty", "Files the AI builds appear here."));
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

/** Show one file's contents in the viewer pane. */
async function openFile(path) {
  try {
    const url =
      FILE_PATH +
      "?session=" + encodeURIComponent(state.session) +
      "&path=" + encodeURIComponent(path);
    const resp = await fetch(url);
    if (!resp.ok) return;
    const data = await resp.json();
    viewerName.textContent = path;
    viewerBody.textContent = data.content || "";
    viewer.hidden = false;
  } catch {
    /* viewing a file is best-effort */
  }
}

/** Human-readable byte size, e.g. "1.2 KB". */
function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

/* --- Networking + session refresh ----------------------------------------- */

/** POST JSON and return the parsed body, or null (surfacing an error card). */
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
  } catch (e) {
    addErrorMessage(
      "Could not reach the server. Make sure `forgelearn` is still running in your terminal, then try again.",
    );
    return null;
  }
}

/** Re-fetch the session and re-render the ladder (after a build/teach-back). */
async function refreshSession() {
  if (!state.session) return;
  try {
    const resp = await fetch(LEARN_SESSION + "?session=" + encodeURIComponent(state.session));
    if (resp.ok) applySession(await resp.json());
  } catch {
    /* best-effort */
  }
}

/* --- Provider dropdown ----------------------------------------------------- */

/** Populate the provider dropdown from /api/agents and preselect the default. */
async function loadAgents() {
  let data;
  try {
    const resp = await fetch(AGENTS_PATH);
    if (!resp.ok) return;
    data = await resp.json();
  } catch {
    return;
  }
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

/* --- Resume + export (Phase 8) -------------------------------------------- */

/** Persist the current session id so a return visit can resume it. */
function rememberSession(id) {
  try {
    localStorage.setItem(SESSION_KEY, id);
  } catch {
    /* private mode / storage disabled — resume just won't be offered */
  }
}

/** The last session id we saw, or null if none / storage unavailable. */
function lastSessionId() {
  try {
    return localStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

/** Forget the stored session (e.g. it no longer exists server-side). */
function forgetSession() {
  try {
    localStorage.removeItem(SESSION_KEY);
  } catch {
    /* nothing to do */
  }
}

/**
 * On load, try to resume the last session from the server. Returns true if a
 * session was restored, false to fall back to a fresh "what do you want to
 * learn?" start.
 */
async function tryResume() {
  const id = lastSessionId();
  if (!id) return false;
  let session;
  try {
    const resp = await fetch(LEARN_SESSION + "?session=" + encodeURIComponent(id));
    if (!resp.ok) {
      forgetSession();
      return false;
    }
    session = await resp.json();
  } catch {
    return false;
  }
  resumeSession(session);
  return true;
}

/** Rebuild the UI from a stored session and place the composer for its stage. */
function resumeSession(session) {
  if (intro) intro.remove();
  state.session = session.id;
  rememberSession(session.id);
  applySession(session);
  refreshFiles();

  const stage = session.stage;
  if (stage === "interview") {
    // Answers aren't persisted mid-interview; re-ask from the top.
    state.questions = session.interview_questions || [];
    state.answers = [];
    state.qIndex = 0;
    addTutorMessage("Welcome back. Let's pick up your interview.");
    askNextQuestion();
    return;
  }

  addTutorMessage(
    'Welcome back to "' + (session.mission || session.topic) + '". ' +
      "Your ladder and progress are on the left.",
  );

  if (stage === "teachback") {
    const project = state.projects.find((p) => p.id === state.activeProjectId);
    addTutorMessage(
      "You'd built this one. Teach it back to unlock the next: explain how " +
        (project ? `"${project.you_learn}"` : "it") + " works.",
    );
    composerRole(true, "Explain how it works, in your own words…");
  } else if (stage === "complete") {
    addTutorMessage("🎉 You'd finished every rung. Export it below to keep it.");
    composerRole(false);
  } else {
    // ladder / building: press Build on the active rung to continue.
    addTutorMessage("Press Build on the active project to keep going.");
    composerRole(false);
  }
}

// Export: download this session as a self-contained HTML file.
exportBtn.addEventListener("click", () => {
  if (!state.session) return;
  window.location.href = LEARN_EXPORT + "?session=" + encodeURIComponent(state.session);
});

/* --- Composer: one input, three roles ------------------------------------- */

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const value = promptEl.value.trim();
  if (!value || source) return;
  if (intro) intro.remove();
  promptEl.value = "";

  // ForgeLearn has no slash-commands. Users who saw the agent CLI's
  // "Please run /login" message sometimes type it here; guide them instead.
  if (value.startsWith("/")) {
    addErrorMessage(
      "ForgeLearn has no slash commands. A \"Please run /login\" message comes " +
        "from your coding-agent CLI, and that login happens in your terminal: run " +
        "`claude` (or `codex`) there to sign in, then come back and type what you " +
        "want to learn.",
    );
    return;
  }

  if (state.stage === "new") startLearning(value);
  else if (state.stage === "interview") recordAnswer(value);
  else if (state.stage === "teachback") submitTeachBack(value);
});

runBtn.addEventListener("click", () => {
  if (source || !state.session) return;
  runStream(RUN_PATH + "?session=" + encodeURIComponent(state.session), "Run");
});

// Start: fill the provider dropdown, then resume the last session if there is
// one, else show the composer for a fresh topic.
async function boot() {
  await loadAgents();
  const resumed = await tryResume();
  if (!resumed) composerRole(true, "What do you want to learn?");
}
boot();
