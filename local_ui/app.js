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
    selectedLecture = lectures.length ? String(lectures[0].sub_id) : "";
  }
  el("courses").value = id;
  showLectureList(lectures);
  if (selectedLecture) {
    await loadLecture(selectedLecture);
  } else {
    detailVersion++;
    detail = null;
    if (course) placeholder("这门课还没有课次", "同步完成后，可以在这里阅读已收录的课次。");
    else placeholder("还没有课程笔记", "在「管理订阅」中填写课程 ID，然后点击「立即同步」。", "管理订阅", openSubscriptions);
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

function renderStatus(status) {
  const finished = status.finished_at;
  const time = finished ? new Date(finished) : null;
  const when = time && !Number.isNaN(time.getTime()) ? time.toLocaleString("zh-CN", {hour12: false}) : "";
  const fallback = status.running ? "正在同步课程…" : "本地服务已就绪";
  el("status").textContent = statusNotice || [status.message || fallback, status.running ? "" : when].filter(Boolean).join(" · ");
  el("sync").disabled = status.running || syncing;
  el("sync").textContent = status.running || syncing ? "同步中…" : "立即同步";
  document.body.classList.toggle("busy", status.running || syncing);
}
async function pollStatus() {
  if (polling || syncing) return;
  polling = true;
  const revision = statusRevision;
  try {
    const status = await request("status");
    if (revision !== statusRevision || syncing) return;
    const finished = lastStatus && !status.running && (lastStatus.running || lastStatus.finished_at !== status.finished_at);
    if (lastStatus && ["running", "started_at", "finished_at", "message"].some(key => lastStatus[key] !== status[key])) statusNotice = "";
    lastStatus = status;
    renderStatus(status);
    if (finished) await loadLibrary();
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
  renderStatus({running: true, message: "正在启动同步…"});
  try {
    const status = await request("sync", {});
    lastStatus = status;
    syncing = false;
    renderStatus(status);
    if (!status.running) await loadLibrary();
  } catch (err) {
    syncing = false;
    statusNotice = err.message;
    renderStatus(lastStatus || {running: false});
  }
};
loadLibrary();
pollStatus();
window.setInterval(pollStatus, 3500);
