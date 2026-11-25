"use strict";

const el = (id) => document.getElementById(id);
const node = (tag, value, className) => {
  const item = document.createElement(tag);
  if (value != null) item.textContent = value;
  if (className) item.className = className;
  return item;
};

let courses = [];
let selectedCourse = "";
let selectedLecture = "";
let detail = null;
let view = "summary";
let libraryVersion = 0;
let detailVersion = 0;
let lastStatus = null;
let statusNotice = "";
let statusRevision = 0;
let syncing = false;
let polling = false;
let subscriptionVersion = 0;

async function request(path, body) {
  const options = {cache: "no-store"};
  if (body !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify(body);
  }
  const response = await fetch("/api/" + path, options);
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || "本地服务返回 " + response.status);
  return data;
}

function placeholder(title, description = "", label = "", action = null) {
  el("note").hidden = true;
  el("placeholder").hidden = false;
  el("placeholder-title").textContent = title;
  el("placeholder-description").textContent = description;
  const button = el("placeholder-action");
  button.hidden = !action;
  button.textContent = label;
  button.onclick = action;
}

function showLectureList(lectures) {
  el("lecture-count").textContent = lectures.length;
  el("lectures").replaceChildren(...lectures.map((lecture) => {
    const id = String(lecture.sub_id);
    const button = node("button", null, "lecture");
    button.type = "button";
    button.setAttribute("aria-current", String(id === selectedLecture));
    const title = lecture.sub_title || "课次 " + id;
    button.title = title;
    button.append(node("span", title, "lecture-title"));
    const meta = [lecture.date, lecture.has_summary ? "" : lecture.error_msg ? "处理出错" : "待生成摘要"].filter(Boolean).join(" · ");
    if (meta) button.append(node("span", meta, "lecture-meta"));
    button.onclick = () => {
      selectedLecture = id;
      showLectureList(lectures);
      loadLecture(id);
    };
    return button;
  }));
}

async function showCourse(id, keepSelection) {
  selectedCourse = id;
  const course = courses.find(c => String(c.course_id) === id);
  const lectures = course?.lectures || [];
  if (!keepSelection || !lectures.some(l => String(l.sub_id) === selectedLecture)) {
    const first = lectures.find(l => l.has_summary) || lectures[0];
    selectedLecture = first ? String(first.sub_id) : "";
  }
  el("courses").value = id;
  showLectureList(lectures);
  if (selectedLecture) {
    await loadLecture(selectedLecture);
  } else {
    detailVersion++;
    detail = null;
    if (course) placeholder("这门课还没有课次", "检查新课次并解析所选课次后，可以在这里阅读笔记。");
    else placeholder("还没有课程笔记", "在「管理订阅」中填写课程 ID，点击「检查新课次」，再选择要解析的课次。", "管理订阅", openSubscriptions);
  }
}

async function loadLibrary() {
  const version = ++libraryVersion;
  try {
    const data = await request("library");
    if (version !== libraryVersion) return;
    courses = data.courses;
    const options = courses.map(c => new Option(c.title || "课程 " + c.course_id, c.course_id));
    el("courses").replaceChildren(...(options.length ? options : [new Option("暂无课程", "")]));
    el("courses").disabled = !courses.length;
    el("course-count").textContent = courses.length;
    const previous = courses.some(c => String(c.course_id) === selectedCourse);
    await showCourse(previous ? selectedCourse : courses.length ? String(courses[0].course_id) : "", previous);
  } catch (err) {
    if (version === libraryVersion) placeholder("课程库暂时无法打开", err.message, "重试", loadLibrary);
  }
}

async function loadLecture(id) {
  const version = ++detailVersion;
  if (String(detail?.sub_id) !== id) placeholder("正在读取课次…");
  try {
    const data = await request("lectures/" + encodeURIComponent(id));
    if (version !== detailVersion) return;
    detail = data;
    el("course-title").textContent = detail.course_title || "课程 " + detail.course_id;
    el("lecture-title").textContent = detail.sub_title || "课次 " + id;
    el("lecture-byline").textContent = [detail.teacher, detail.date].filter(Boolean).join(" · ");
    el("export").href = "/api/lectures/" + encodeURIComponent(id) + "/markdown";
    el("export").download = "iCourse-" + id + ".md";
    el("placeholder").hidden = true;
    el("note").hidden = false;
    showView(view);
  } catch (err) {
    if (version === detailVersion) placeholder("无法读取本节课", err.message, "重试", () => loadLecture(id));
  }
}

// Read the common formatting used by course summaries. All source content is
// inserted as text; links, HTML, and image URLs never load external resources.
function inline(parent, source) {
  const tokens = /(`[^`\n]+`|\$\$[^\n]*?\$\$|\$[^$\n]+\$|\*\*[^*\n]+\*\*)/g;
  let offset = 0;
  for (const match of source.matchAll(tokens)) {
    parent.append(source.slice(offset, match.index));
    const token = match[0];
    if (token.startsWith("**")) parent.append(node("strong", token.slice(2, -2)));
    else if (token.startsWith("`")) parent.append(node("code", token.slice(1, -1)));
    else parent.append(node("span", token, "formula"));
    offset = match.index + token.length;
  }
  parent.append(source.slice(offset));
}

function formatted(tag, source) {
  const elem = node(tag);
  inline(elem, source);
  return elem;
}

function markdown(source) {
  const fragment = document.createDocumentFragment();
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const listItem = /^\s*([-+*]|\d+[.)])\s+(.+)/;
  const separator = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/;
  const cells = line => line.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim());
  const isHeading = line => /^#{1,6}\s+/.test(line);
  const isRule = line => /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line);
  const isTable = i => lines[i].includes("|") && separator.test(lines[i + 1] || "");
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    const trim = line.trim();
    if (!trim) { i++; continue; }
    if (trim.startsWith("```") || trim === "$$" || trim === "\\[") {
      const fence = trim.startsWith("```") ? "```" : trim === "$$" ? "$$" : "\\]";
      const math = fence !== "```";
      const block = [];
      if (math) block.push(trim);
      i++;
      while (i < lines.length && !(math ? lines[i].trim() === fence : lines[i].trim().startsWith(fence))) block.push(lines[i++]);
      if (i < lines.length) {
        if (math) block.push(fence);
        i++;
      }
      fragment.append(node(math ? "div" : "pre", block.join("\n"), math ? "math-block" : ""));
    } else if (isHeading(line)) {
      const [marks, ...rest] = line.split(/\s+/);
      fragment.append(formatted("h" + Math.min(4, Math.max(2, marks.length)), rest.join(" ")));
      i++;
    } else if (isRule(line)) {
      fragment.append(node("hr"));
      i++;
    } else if (listItem.test(line)) {
      const ordered = /^\d/.test(line.match(listItem)[1]);
      const list = node(ordered ? "ol" : "ul");
      if (ordered) list.start = parseInt(line.match(listItem)[1], 10);
      while (i < lines.length) {
        const match = lines[i].match(listItem);
        if (!match || /^\d/.test(match[1]) !== ordered) break;
        list.append(formatted("li", match[2]));
        i++;
      }
      fragment.append(list);
    } else if (isTable(i)) {
      const wrapper = node("div", null, "table-scroll");
      const table = node("table");
      const row = (line, tag) => {
        const tr = node("tr");
        cells(line).forEach(cell => tr.append(formatted(tag, cell)));
        table.append(tr);
      };
      row(line, "th");
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) row(lines[i++], "td");
      wrapper.append(table);
      fragment.append(wrapper);
    } else if (trim.startsWith(">")) {
      const block = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) block.push(lines[i++].trim().replace(/^>\s?/, ""));
      fragment.append(formatted("blockquote", block.join("\n")));
    } else {
      const paragraph = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !isHeading(lines[i]) && !isRule(lines[i]) &&
        !listItem.test(lines[i]) && !isTable(i) && lines[i].trim() !== "\\[" && !/^(?:```|\$\$$|>)/.test(lines[i].trim())) paragraph.push(lines[i++]);
      fragment.append(formatted("p", paragraph.join("\n")));
    }
  }
  return fragment;
}

const tabs = [...document.querySelectorAll("[role=tab]")];
function showView(next) {
  view = next;
  tabs.forEach(tab => {
    const active = tab.dataset.view === view;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  const panel = el("content");
  panel.setAttribute("aria-labelledby", "tab-" + view);
  panel.replaceChildren();
  if (!detail) return;
  if (view === "summary" && detail.summary) panel.append(markdown(detail.summary));
  else if (view === "transcript" && detail.transcript) panel.append(node("p", detail.transcript, "plaintext"));
  else if (view === "pages" && detail.pages?.length) {
    detail.pages.forEach(page => {
      const section = node("section", null, "ppt-page");
      const status = page.ocr_status === "pending" ? "等待识别" : "本页暂无识别文字";
      section.append(node("h2", "第 " + page.page_num + " 页"), node("p", page.text || status, "plaintext"));
      panel.append(section);
    });
  } else {
    panel.append(node("p", {summary: "本节课暂无摘要。", transcript: "本节课暂无转录。", pages: "本节课暂无 PPT 识别结果。"}[view], "subtle"));
  }
  if (view !== "transcript" && window.renderMathInElement) {
    window.renderMathInElement(panel, {
      delimiters: [
        {left: "$$", right: "$$", display: true},
        {left: "\\[", right: "\\]", display: true},
        {left: "\\(", right: "\\)", display: false},
        {left: "$", right: "$", display: false},
      ],
      throwOnError: false,
      trust: false,
    });
  }
}
tabs.forEach((tab, i) => {
  tab.onclick = () => showView(tab.dataset.view);
  tab.onkeydown = event => {
    const positions = {ArrowLeft: (i + 2) % 3, ArrowRight: (i + 1) % 3, Home: 0, End: 2};
    if (!(event.key in positions)) return;
    event.preventDefault();
    const target = tabs[positions[event.key]];
    target.focus();
    showView(target.dataset.view);
  };
});

async function openSubscriptions() {
  statusNotice = "";
  if (lastStatus) renderStatus(lastStatus);
  const version = ++subscriptionVersion;
  const dialog = el("subscription-dialog");
  el("ids").value = "";
  el("ids").disabled = true;
  el("save").disabled = true;
  el("form-status").textContent = "正在读取订阅…";
  if (!dialog.open) dialog.showModal();
  try {
    const data = await request("subscriptions");
    if (version !== subscriptionVersion || !dialog.open) return;
    el("ids").value = data.course_ids.join("\n");
    el("ids").disabled = false;
    el("save").disabled = false;
    el("form-status").textContent = "";
    el("ids").focus();
  } catch (err) {
    if (version === subscriptionVersion) el("form-status").textContent = err.message;
  }
}
el("subscriptions").onsubmit = async event => {
  event.preventDefault();
  const ids = [...new Set(el("ids").value.trim().split(/[\s,，、]+/).filter(Boolean))];
  if (ids.some(id => !/^\d+$/.test(id))) {
    el("form-status").textContent = "课程 ID 只包含数字，请检查输入。";
    return;
  }
  const version = subscriptionVersion;
  el("save").disabled = true;
  el("ids").disabled = true;
  el("form-status").textContent = "正在保存…";
  try {
    await request("subscriptions", {course_ids: ids});
    if (version === subscriptionVersion) {
      el("subscription-dialog").close();
      statusNotice = "已保存订阅 · " + ids.length + " 门课程";
      if (lastStatus) renderStatus(lastStatus);
    }
  } catch (err) {
    if (version === subscriptionVersion) el("form-status").textContent = err.message;
  } finally {
    if (version === subscriptionVersion) {
      el("save").disabled = false;
      el("ids").disabled = false;
    }
  }
};
el("edit-subs").onclick = openSubscriptions;
el("cancel").onclick = el("close-dialog").onclick = () => el("subscription-dialog").close();
el("courses").onchange = e => showCourse(e.target.value, false);

const stages = {login: "登录 iCourse", discover: "检查课程", prepare: "准备资料", transcribe: "语音转录", ocr: "识别 PPT", summarize: "生成摘要", done: "完成", failed: "处理失败"};
let plan = null;
let selected = new Set();
let planLoading = false;
let planRevision = 0;
let providers = [];
let providerId = "";
let providerBusy = false;
let profileRevision = 0;

function renderStatus(status) {
  const time = status.finished_at && new Date(status.finished_at);
  const when = time && !Number.isNaN(time.getTime()) ? time.toLocaleString("zh-CN", {hour12: false}) : "";
  const fallback = status.running ? "正在执行任务…" : "本地服务已就绪";
  el("status").textContent = statusNotice || [status.message || fallback, status.running ? "" : when].filter(Boolean).join(" · ");
  el("sync").disabled = status.running || syncing;
  el("sync").textContent = status.phase === "discovering" ? "检查中…" : "检查新课次";
  el("open-pending").hidden = !status.plan_id;
  el("open-pending").disabled = status.running || syncing;
  el("open-pending").textContent = "待选课次 · " + (status.pending_count || 0);
  document.body.classList.toggle("busy", status.running || syncing);

  const p = status.progress || {};
  const discovery = status.operation === "discover" || status.phase === "discovering" || p.stage === "discover";
  const showProgress = status.running || Boolean(status.progress && ["finished", "error"].includes(status.phase));
  el("job").hidden = !showProgress;
  if (showProgress) {
    el("job-title").textContent = status.running ? (discovery ? "正在检查订阅课程" : "正在解析所选课次") :
      (status.phase === "error" ? "任务已结束" : discovery ? "课程检查已结束" : "解析已结束");
    const total = Number(p.total) || 0;
    const count = Number(p.completed) || 0;
    const meter = el("job-progress");
    if (total > 0) {
      meter.max = total;
      meter.value = count;
    } else meter.removeAttribute("value");
    el("job-count").textContent = total > 0 ? count + " / " + total + (discovery ? " 门课程" : " 节课次") : "";
    const parts = [p.course_title || (p.course_id && "课程 " + p.course_id), p.sub_title || (p.sub_id && "课次 " + p.sub_id),
      stages[p.stage] || p.stage, p.detail, discovery ? "" : status.provider_label];
    el("job-detail").textContent = [...new Set(parts.filter(Boolean))].join(" · ");
    el("job-read").hidden = status.running || !courses.some(c => c.lectures.some(l => l.has_summary));
  }
  syncSelection();
}

function acceptStatus(status) {
  const before = lastStatus;
  if (before && ["running", "started_at", "finished_at", "phase", "message"].some(k => before[k] !== status[k])) statusNotice = "";
  lastStatus = status;
  renderStatus(status);
  const ended = !status.running && (!before || before.running || before.finished_at !== status.finished_at || before.phase !== status.phase);
  if (ended && before) loadLibrary().then(() => renderStatus(lastStatus));
  const newPlan = status.phase === "ready" && status.plan_id &&
    (!before || before.phase === "discovering" || before.plan_id !== status.plan_id);
  if (newPlan && !document.querySelector("dialog[open]")) openPending();
  else if (el("pending-dialog").open && plan && plan.plan_id !== status.plan_id) openPending();
}

async function pollStatus() {
  if (polling || syncing) return;
  polling = true;
  const revision = statusRevision;
  try {
    const status = await request("status");
    if (revision === statusRevision && !syncing) acceptStatus(status);
  } catch (err) {
    if (revision === statusRevision) el("status").textContent = "无法连接本地服务 · " + err.message;
  } finally {
    polling = false;
  }
}

el("sync").onclick = async () => {
  if (syncing || lastStatus?.running) return;
  statusRevision++;
  statusNotice = "";
  syncing = true;
  el("sync").disabled = true;
  el("status").textContent = "正在开始检查课次…";
  try {
    const status = await request("discover", {});
    syncing = false;
    acceptStatus(status);
  } catch (err) {
    syncing = false;
    statusNotice = err.message;
    renderStatus(lastStatus || {running: false});
  }
};

el("job-read").onclick = async () => {
  const progress = lastStatus?.progress || {};
  const matches = courses.flatMap(c => c.lectures.filter(l => l.has_summary).map(l => ({course: c, lecture: l})));
  const recent = matches.find(({lecture}) => String(lecture.sub_id) === String(progress.sub_id)) ||
    matches.sort((a, b) => String(b.lecture.processed_at || "").localeCompare(String(a.lecture.processed_at || "")))[0];
  if (recent) {
    selectedLecture = String(recent.lecture.sub_id);
    view = "summary";
    await showCourse(String(recent.course.course_id), true);
    el("reader").scrollIntoView();
  }
};

function selectionKey(l) { return JSON.stringify([String(l.course_id), String(l.sub_id)]); }
function syncSelection() {
  if (!el("pending-dialog").open) return;
  const total = plan?.lectures.length || 0;
  const locked = planLoading || syncing || Boolean(lastStatus?.running);
  el("pick-all").disabled = locked || !total || selected.size === total;
  el("pick-none").disabled = locked || !selected.size;
  el("pending-count").textContent = planLoading ? "正在读取课次…" : "已选 " + selected.size + " / " + total + " 节";
  el("parse-selected").textContent = "解析所选 " + selected.size + " 节";
  el("parse-selected").disabled = locked || providerBusy || !selected.size || !providerId;
  el("parse-profile").disabled = locked || providerBusy || !providers.length;
  el("configure-profile").disabled = syncing || planLoading || providerBusy;
  for (const group of el("pending-list").querySelectorAll(".pending-group")) {
    const items = [...group.querySelectorAll("input[data-key]")];
    let count = 0;
    for (const input of items) {
      input.checked = selected.has(input.dataset.key);
      input.disabled = locked;
      if (input.checked) count++;
    }
    const toggle = group.querySelector(".pending-course-head input");
    toggle.checked = count === items.length;
    toggle.indeterminate = count > 0 && count < items.length;
    toggle.disabled = locked;
  }
}

function renderPlan() {
  const list = el("pending-list");
  list.replaceChildren();
  if (!plan) return;
  const groups = new Map();
  for (const lecture of plan.lectures) {
    const id = String(lecture.course_id);
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(lecture);
  }
  if (!groups.size) list.append(node("p", "当前没有待解析的课次。", "subtle"));
  for (const [id, lectures] of groups) {
    const titles = new Set();
    const repeatedTitles = new Set();
    for (const lecture of lectures) {
      if (titles.has(lecture.sub_title)) repeatedTitles.add(lecture.sub_title);
      titles.add(lecture.sub_title);
    }
    const group = node("section", null, "pending-group");
    const header = node("div", null, "pending-course-head");
    const label = node("label");
    const courseToggle = node("input");
    courseToggle.type = "checkbox";
    const title = lectures[0].course_title || "课程 " + id;
    courseToggle.setAttribute("aria-label", "全选" + title);
    label.append(courseToggle, title);
    header.append(label, node("small", lectures.length + " 节"));
    group.append(header);
    courseToggle.onchange = () => {
      for (const l of lectures) {
        if (courseToggle.checked) selected.add(selectionKey(l));
        else selected.delete(selectionKey(l));
      }
      syncSelection();
    };
    for (const l of lectures) {
      const row = node("label", null, "pending-row");
      const input = node("input");
      input.type = "checkbox";
      input.dataset.key = selectionKey(l);
      const text = node("span", l.sub_title || "课次 " + l.sub_id);
      const meta = [l.date, repeatedTitles.has(l.sub_title) && "录播 ID " + l.sub_id,
        l.error_msg || (l.error_stage && "上次处理失败")].filter(Boolean).join(" · ");
      if (meta) text.append(node("small", meta));
      input.onchange = () => {
        if (input.checked) selected.add(input.dataset.key);
        else selected.delete(input.dataset.key);
        syncSelection();
      };
      row.append(input, text);
      group.append(row);
    }
    list.append(group);
  }
  for (const failure of plan.failed_courses || []) {
    list.append(node("p", "课程 " + failure.course_id + " 检查失败 · " + failure.message, "pending-warning"));
  }
  syncSelection();
}

async function openPending() {
  const revision = ++planRevision;
  const dialog = el("pending-dialog");
  planLoading = true;
  plan = null;
  selected.clear();
  el("pending-message").textContent = "";
  if (!dialog.open) dialog.showModal();
  el("pending-list").replaceChildren();
  syncSelection();
  const [planResult, profileResult] = await Promise.allSettled([request("plan"), request("providers")]);
  if (revision !== planRevision || !dialog.open) return;
  planLoading = false;
  if (planResult.status === "fulfilled") plan = planResult.value;
  if (profileResult.status === "fulfilled") applyProviders(profileResult.value);
  else applyProviders({providers: [], selected_id: null});
  el("pending-message").textContent = [planResult, profileResult].filter(r => r.status === "rejected").map(r => r.reason.message).join(" · ");
  renderPlan();
  syncSelection();
}
el("open-pending").onclick = openPending;
el("pick-all").onclick = () => { selected = new Set(plan.lectures.map(selectionKey)); syncSelection(); };
el("pick-none").onclick = () => { selected.clear(); syncSelection(); };
el("parse-selected").onclick = async () => {
  if (el("parse-selected").disabled || !plan) return;
  const lectures = plan.lectures.filter(l => selected.has(selectionKey(l))).map(({course_id, sub_id}) => ({course_id, sub_id}));
  statusRevision++;
  statusNotice = "";
  syncing = true;
  syncSelection();
  el("pending-message").textContent = "正在提交所选课次…";
  try {
    const status = await request("process", {plan_id: plan.plan_id, lectures, provider_id: providerId});
    syncing = false;
    el("pending-dialog").close();
    acceptStatus(status);
  } catch (err) {
    syncing = false;
    el("pending-message").textContent = err.message;
    syncSelection();
    pollStatus();
  }
};

// Presets fill editable fields. Requests always go to the local service.
const presets = [
  {name: "自定义兼容接口", url: "", model: ""},
  {name: "ModelScope", url: "https://api-inference.modelscope.cn/v1/", model: "deepseek-ai/DeepSeek-V4.1-Flash"},
  {name: "DeepSeek", url: "https://api.deepseek.com/v1", model: "deepseek-v4-flash"},
  {name: "Gemini", url: "https://generativelanguage.googleapis.com/v1beta/openai/", model: "gemini-2.5-flash"},
  {name: "OpenAI", url: "https://api.openai.com/v1", model: ""},
  {name: "Ollama（本机）", url: "http://127.0.0.1:11434/v1", model: ""},
];
el("profile-preset").replaceChildren(...presets.map((p,i) => new Option(p.name, String(i))));
const profileLabel = p => p.name + " · " + p.model;
function applyProviders(data) {
  providers = data.providers;
  providerId = providers.some(p => p.id === data.selected_id) ? data.selected_id : "";
  const list = el("parse-profile");
  list.replaceChildren(...providers.map(p => new Option(profileLabel(p), p.id)));
  if (!providerId) list.prepend(new Option(providers.length ? "选择要使用的摘要 API" : "尚未配置摘要 API", ""));
  list.value = providerId;
  const active = providers.find(p => p.id === providerId);
  el("parse-model-hint").textContent = active ? active.base_url : providers.length ? "请选择本次解析使用的 API 和模型。" : "先在「管理 API」中保存一个配置，再提交解析。";
  syncSelection();
}
el("parse-profile").onchange = async event => {
  const id = event.target.value;
  if (!id || id === providerId) return;
  providerBusy = true;
  syncSelection();
  el("pending-message").textContent = "";
  try {
    applyProviders(await request("providers/select", {id}));
  } catch (err) {
    event.target.value = providerId;
    el("pending-message").textContent = err.message;
  } finally {
    providerBusy = false;
    syncSelection();
  }
};

let editedProfile = "";
let profileSaving = false;
function showProfile(id) {
  const form = el("profile-form");
  const current = providers.find(p => p.id === id);
  editedProfile = current?.id || "";
  el("profiles").value = editedProfile;
  form.reset();
  el("profile-key").value = "";
  el("profile-message").textContent = "";
  const preset = presets.findIndex(p => p.url && current && p.url.replace(/\/$/, "") === current.base_url.replace(/\/$/, ""));
  el("profile-preset").value = String(preset === -1 ? 0 : preset);
  el("profile-name").value = current?.name || "";
  el("profile-url").value = current?.base_url || "";
  el("profile-model").value = current?.model || "";
  const readOnly = current?.source === "env";
  for (const field of form.querySelectorAll("input,select")) field.disabled = readOnly;
  el("profile-key").required = !current;
  el("profile-key").placeholder = current ? "已配置密钥" : "输入 API 密钥";
  el("key-hint").textContent = readOnly ? "此配置来自 .env。可在文件中修改，或新增独立配置。" :
    current ? "留空沿用已存密钥。修改 API 地址时需重新填写密钥。" :
    "密钥保存在本机。免鉴权的本机服务可以填写 local。";
  el("save-profile").hidden = readOnly;
  el("use-profile").hidden = !current || current.id === providerId;
  el("delete-profile").hidden = !current || readOnly;
  el("save-profile").textContent = "保存配置";
}
function refreshProfileList(preferredId) {
  const list = el("profiles");
  const options = providers.map(p => new Option(profileLabel(p) + (p.id === providerId ? "（当前使用）" : ""), p.id));
  list.replaceChildren(...options, new Option("新增配置", ""));
  showProfile(preferredId && providers.some(p => p.id === preferredId) ? preferredId : "");
}
async function openProfiles() {
  const revision = ++profileRevision;
  const dialog = el("profiles-dialog");
  if (!dialog.open) dialog.showModal();
  el("profile-form").hidden = true;
  el("new-profile").disabled = true;
  el("profiles").disabled = true;
  el("profiles").replaceChildren(new Option("正在读取配置…", ""));
  try {
    const data = await request("providers");
    if (revision !== profileRevision || !dialog.open) return;
    applyProviders(data);
    el("profile-form").hidden = false;
    el("new-profile").disabled = false;
    el("profiles").disabled = false;
    refreshProfileList(providerId || providers[0]?.id);
  } catch (err) {
    if (revision === profileRevision) el("profiles").replaceChildren(new Option(err.message, ""));
  }
}
el("profiles").onchange = e => showProfile(e.target.value);
el("new-profile").onclick = () => { showProfile(""); el("profile-preset").focus(); };
el("edit-models").onclick = openProfiles;
el("configure-profile").onclick = openProfiles;
el("profile-preset").onchange = e => {
  const preset = presets[Number(e.target.value)];
  el("profile-url").value = preset.url;
  el("profile-model").value = preset.model;
  if (!el("profile-name").value || presets.some(p => p.name === el("profile-name").value)) {
    el("profile-name").value = preset.url ? preset.name : "";
  }
};

async function saveProfile(action) {
  if (profileSaving) return;
  const current = providers.find(p => p.id === editedProfile);
  if (action === "delete" && !window.confirm("删除「" + current.name + "」及其在本机保存的密钥？")) return;
  const body = action === "save" ? {
    name: el("profile-name").value.trim(), base_url: el("profile-url").value.trim(), model: el("profile-model").value.trim(),
    ...(current && {id: editedProfile}), ...(el("profile-key").value.trim() && {api_key: el("profile-key").value.trim()}),
  } : {id: editedProfile};
  const route = action === "save" ? "providers" : "providers/" + action;
  const revision = profileRevision;
  profileSaving = true;
  el("profile-form").inert = true;
  el("profiles").disabled = el("new-profile").disabled = true;
  el("profile-message").textContent = "正在保存…";
  try {
    const data = await request(route, body);
    applyProviders(data);
    if (revision === profileRevision && el("profiles-dialog").open) {
      const chosen = action === "delete" ? providerId || providers[0]?.id : current?.id || providerId;
      refreshProfileList(chosen);
      el("profile-message").textContent = action === "delete" ? "已删除配置。" : action === "select" ? "已设为当前使用的 API。" : "配置已保存。";
    }
  } catch (err) {
    if (revision === profileRevision) el("profile-message").textContent = err.message;
  } finally {
    profileSaving = false;
    el("profile-form").inert = false;
    if (revision === profileRevision) el("profiles").disabled = el("new-profile").disabled = false;
  }
}
el("profile-form").onsubmit = e => { e.preventDefault(); saveProfile("save"); };
el("use-profile").onclick = () => saveProfile("select");
el("delete-profile").onclick = () => saveProfile("delete");
for (const button of document.querySelectorAll("[data-close]")) {
  button.onclick = () => el(button.dataset.close).close();
}
el("profiles-dialog").addEventListener("close", () => {
  el("profile-key").value = "";
  profileRevision++;
});
el("pending-dialog").addEventListener("close", () => { planRevision++; selected.clear(); });
loadLibrary().then(() => { if (lastStatus) renderStatus(lastStatus); });
pollStatus();
window.setInterval(pollStatus, 1800);
