const $ = (id) => document.getElementById(id);
const moduleInfo = {
  tutor: {
    title: "搜题讲解", description: "把题目或卡住的步骤发来，我们一次只解决一个关键点。",
    suggestions: ["这道题第一步该从哪里入手？", "我卡在换元这一步了，可以给个提示吗？"]
  },
  mastery: {
    title: "掌握分析", description: "结合答题记录和你的复习习惯，找出值得优先投入的知识点。",
    suggestions: ["请分析我目前的薄弱点。", "样本较少的知识点有哪些？"]
  },
  roadmap: {
    title: "路线规划", description: "结合目标、考试时间、每周可用时间和 KT 状态安排复习。",
    suggestions: ["帮我规划接下来 4 周的高数复习。", "安排一个包含复盘和错题回访的计划。"]
  },
  practice: {
    title: "出题判题", description: "生成同类型练习；可以随时询问提示，也可以勾选“这是我的作答”提交判题。",
    suggestions: ["给我出一道定积分应用的基础题。", "来一道中值定理相关练习。"]
  },
  library: {
    title: "PDF 资料库", description: "管理用于检索和讲题参考的复习资料。"
  },
  knowledge: {
    title: "KT 掌握记录", description: "每个知识点累计完成 4 题后更新一次掌握度。"
  }
};
const memoryLabels = {
  study_goal: "复习目标",
  study_schedule: "时间安排",
  learning_preference: "讲解偏好",
  recurring_difficulty: "常见困难"
};
const sessionId = getSessionId();
let activeModule = "tutor";
let practicePending = false;
let requestSequence = 0;

function getSessionId() {
  let id = localStorage.getItem("studyAgentSessionId");
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem("studyAgentSessionId", id);
  }
  return id;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function escapeHtml(text) {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function renderMarkdown(source) {
  const codeBlocks = [];
  let escaped = escapeHtml(String(source || "").replace(/\r\n/g, "\n"));
  escaped = escaped.replace(/```([^\n]*)\n([\s\S]*?)```/g, (_, language, code) => {
    const token = `\u0000BLOCK${codeBlocks.length}\u0000`;
    const langClass = /^[a-zA-Z0-9_+-]{1,20}$/.test(language.trim())
      ? ` class="language-${language.trim()}"`
      : "";
    codeBlocks.push(`<pre><code${langClass}>${code.replace(/\n$/, "")}</code></pre>`);
    return token;
  });

  const inline = (text) => {
    const inlineCodes = [];
    let html = text.replace(/`([^`\n]+)`/g, (_, code) => {
      const token = `\u0000INLINE${inlineCodes.length}\u0000`;
      inlineCodes.push(`<code>${code}</code>`);
      return token;
    });
    html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
      try {
        const url = new URL(href.replaceAll("&amp;", "&"), window.location.origin);
        if (url.protocol !== "https:" && url.protocol !== "http:") return label;
        return `<a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      } catch {
        return label;
      }
    });
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/__(.+?)__/g, "<strong>$1</strong>")
      .replace(/~~(.+?)~~/g, "<del>$1</del>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    return html.replace(/\u0000INLINE(\d+)\u0000/g, (_, index) => inlineCodes[Number(index)]);
  };

  const blocks = [];
  let paragraph = [];
  let listType = "";
  let listItems = [];
  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (listItems.length) {
      blocks.push(`<${listType}>${listItems.map((item) => `<li>${inline(item)}</li>`).join("")}</${listType}>`);
      listItems = [];
      listType = "";
    }
  };

  const lines = escaped.split("\n");
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.includes("|") && index + 1 < lines.length
      && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[index + 1])
      && lines[index + 1].includes("-")) {
      flushParagraph();
      flushList();
      const cells = (row) => row.trim().replace(/^\||\|$/g, "")
        .split("|").map((cell) => cell.trim());
      const headers = cells(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes("|")) {
        rows.push(cells(lines[index]));
        index += 1;
      }
      index -= 1;
      blocks.push(
        `<div class="table-wrap"><table><thead><tr>${headers.map((cell) => `<th>${inline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${headers.map((_, cellIndex) => `<td>${inline(row[cellIndex] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
      );
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    const list = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(.+)$/);
    if (!line.trim() || heading || list || /^-{3,}$/.test(line) || line.startsWith("&gt; ")) {
      flushParagraph();
      if (!list) flushList();
      if (!line.trim()) continue;
      if (heading) {
        const level = Math.min(heading[1].length + 1, 4);
        blocks.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      } else if (list) {
        const type = /^\s*\d/.test(line) ? "ol" : "ul";
        if (listType && listType !== type) flushList();
        listType = type;
        listItems.push(list[1]);
      } else if (/^-{3,}$/.test(line)) {
        blocks.push("<hr>");
      } else if (line.startsWith("&gt; ")) {
        blocks.push(`<blockquote>${inline(line.slice(5))}</blockquote>`);
      }
      continue;
    }
    paragraph.push(line);
  }
  flushParagraph();
  flushList();
  return blocks.join("").replace(/\u0000BLOCK(\d+)\u0000/g, (_, index) => codeBlocks[Number(index)]);
}

function addMessage(role, content, options = {}) {
  const row = document.createElement("article");
  row.className = `message-row ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "我" : "∑";
  const body = document.createElement("div");
  body.className = "message-body";
  const rendered = document.createElement("div");
  rendered.className = "message-content";
  if (role === "user") rendered.textContent = content;
  else rendered.innerHTML = renderMarkdown(content);
  body.append(rendered);
  if (options.sources?.length) {
    const sources = document.createElement("div");
    sources.className = "sources";
    sources.textContent = `参考资料：${options.sources.join("、")}`;
    body.append(sources);
  }
  row.append(avatar, body);
  $("chatMessages").append(row);
  return row;
}

function showWelcome(module) {
  const info = moduleInfo[module];
  const welcome = document.createElement("div");
  welcome.className = "welcome";
  const logo = document.createElement("div");
  logo.className = "welcome-logo";
  logo.textContent = module === "tutor" ? "∑" : module === "practice" ? "▤" : "✦";
  const title = document.createElement("h2");
  title.textContent = `开始${info.title}`;
  const description = document.createElement("p");
  description.textContent = info.description;
  welcome.append(logo, title, description);
  const suggestions = document.createElement("div");
  suggestions.className = "suggestions";
  for (const text of info.suggestions || []) {
    const button = document.createElement("button");
    button.className = "suggestion";
    button.textContent = text;
    button.onclick = () => {
      $("messageInput").value = text;
      $("messageInput").focus();
    };
    suggestions.append(button);
  }
  welcome.append(suggestions);
  $("chatMessages").append(welcome);
}

function updateComposer(module) {
  $("moduleTitle").textContent = moduleInfo[module].title;
  $("moduleDescription").textContent = moduleInfo[module].description;
  $("moduleEyebrow").textContent = module === "library" || module === "knowledge" ? "学习资料" : "学习对话";
  $("chatView").hidden = module === "library" || module === "knowledge";
  $("libraryView").hidden = module !== "library";
  $("knowledgeView").hidden = module !== "knowledge";
  $("clearChatBtn").hidden = module === "library" || module === "knowledge";
  $("topicInput").hidden = module !== "tutor" && module !== "practice";
  document.querySelector('label[for="topicInput"]').hidden = $("topicInput").hidden;
  $("roadmapOptions").hidden = module !== "roadmap";
  $("answerToggle").hidden = module !== "practice" || !practicePending;
  $("isAnswer").checked = false;
  $("messageInput").placeholder = module === "practice"
    ? "输入练习主题，或询问当前题目的提示…"
    : "输入题目、问题或你现在卡住的地方…";
  $("composerHint").textContent = module === "practice" && practicePending
    ? "需要提示直接提问；提交答案前请勾选上方选项"
    : "Enter 发送 · Shift + Enter 换行";
}

async function loadModule(module) {
  activeModule = module;
  practicePending = false;
  const requestId = ++requestSequence;
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.module === module);
  });
  updateComposer(module);
  if (module === "library") {
    await refreshDocuments();
    return;
  }
  if (module === "knowledge") {
    await refreshKnowledge();
    return;
  }
  $("chatMessages").replaceChildren();
  try {
    const history = await api(`/api/chat/${module}?session_id=${encodeURIComponent(sessionId)}`);
    if (requestId !== requestSequence) return;
    practicePending = history.practice_pending;
    updateComposer(module);
    if (history.messages.length === 0) showWelcome(module);
    for (const message of history.messages) addMessage(message.role, message.content);
    $("chatScroll").scrollTop = $("chatScroll").scrollHeight;
  } catch (error) {
    addMessage("assistant", `读取历史对话失败：${error.message}`);
  }
}

async function refreshMemories() {
  const target = $("memoryList");
  try {
    const data = await api(`/api/memory?session_id=${encodeURIComponent(sessionId)}`);
    if (!data.memories.length) {
      target.innerHTML = '<span class="memory-empty">暂时还没有长期记忆</span>';
      return;
    }
    target.replaceChildren(...data.memories.map((item) => {
      const row = document.createElement("div");
      row.className = "memory-item";
      const top = document.createElement("div");
      top.className = "memory-item-top";
      const category = document.createElement("span");
      category.className = "memory-category";
      category.textContent = memoryLabels[item.category] || item.category;
      const remove = document.createElement("button");
      remove.className = "memory-delete";
      remove.type = "button";
      remove.title = "删除这条记忆";
      remove.setAttribute("aria-label", "删除这条记忆");
      remove.textContent = "×";
      remove.onclick = async () => {
        try {
          await api(`/api/memory/${item.id}?session_id=${encodeURIComponent(sessionId)}`, { method: "DELETE" });
          await refreshMemories();
        } catch (error) { alert(error.message); }
      };
      top.append(category, remove);
      const content = document.createElement("p");
      content.className = "memory-content";
      content.textContent = item.content;
      row.append(top, content);
      return row;
    }));
  } catch (error) {
    target.textContent = `记忆读取失败：${error.message}`;
  }
}

function addTyping() {
  const row = document.createElement("article");
  row.className = "message-row assistant";
  row.id = "typingIndicator";
  row.innerHTML = '<div class="avatar">∑</div><div class="message-body"><div class="typing"><span></span><span></span><span></span></div></div>';
  $("chatMessages").append(row);
  $("chatScroll").scrollTop = $("chatScroll").scrollHeight;
}

async function sendMessage(messageOverride = "") {
  const message = (messageOverride || $("messageInput").value).trim();
  if (!message) return;
  const module = activeModule;
  const intent = module === "practice" && $("isAnswer").checked ? "answer" : "message";
  $("messageInput").value = "";
  $("messageInput").style.height = "auto";
  $("isAnswer").checked = false;
  const welcome = document.querySelector(".welcome");
  if (welcome) welcome.remove();
  addMessage("user", message);
  addTyping();
  $("sendBtn").disabled = true;
  $("chatScroll").scrollTop = $("chatScroll").scrollHeight;
  try {
    const hoursValue = Number($("hours").value);
    const response = await api(`/api/chat/${module}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        message,
        topic: $("topicInput").value.trim(),
        exam_date: $("examDate").value,
        hours_per_week: Number.isFinite(hoursValue) && hoursValue > 0 ? hoursValue : null,
        intent
      })
    });
    $("typingIndicator")?.remove();
    if (module === activeModule) {
      addMessage("assistant", response.message);
      practicePending = Boolean(response.metadata?.practice_pending);
      updateComposer(module);
      $("chatScroll").scrollTop = $("chatScroll").scrollHeight;
    }
    if (response.memory_updated || response.memory_error) await refreshMemories();
    if (response.memory_error) {
      console.warn("跨模块记忆未更新：", response.memory_error);
    }
    if (response.summary_error) {
      console.warn("本模块历史摘要未更新：", response.summary_error);
    }
  } catch (error) {
    $("typingIndicator")?.remove();
    if (module === activeModule) addMessage("assistant", `请求没有完成：${error.message}`);
  } finally {
    $("sendBtn").disabled = false;
    if (module === activeModule) $("messageInput").focus();
  }
}

async function refreshDocuments() {
  const data = await api("/api/rag/documents");
  const target = $("documents");
  target.replaceChildren();
  if (!data.documents.length) {
    const empty = document.createElement("li");
    empty.textContent = "还没有上传 PDF 资料。";
    target.append(empty);
    return;
  }
  for (const doc of data.documents) {
    const row = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = `${doc.source} · ${doc.chunk_count} 段`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "移除";
    remove.onclick = async () => {
      if (!confirm(`确定移除“${doc.source}”吗？`)) return;
      try {
        await api(`/api/rag/documents/${encodeURIComponent(doc.source)}`, { method: "DELETE" });
        await refreshDocuments();
      } catch (error) { alert(error.message); }
    };
    row.append(name, remove);
    target.append(row);
  }
}

async function refreshKnowledge() {
  const data = await api("/api/knowledge");
  const target = $("knowledgeList");
  target.replaceChildren();
  if (!data.knowledge.length) {
    const empty = document.createElement("div");
    empty.className = "knowledge-empty";
    empty.textContent = "暂时没有答题数据。完成练习判题后，知识点掌握情况会显示在这里。";
    target.append(empty);
    return;
  }
  for (const item of data.knowledge) {
    const percent = Math.round(item.mastery * 100);
    const row = document.createElement("div");
    row.className = "knowledge-item";
    row.innerHTML = `<div class="knowledge-head"><strong></strong><span>${percent}%</span></div><div class="progress"><span style="width:${percent}%"></span></div><div class="knowledge-meta"></div>`;
    row.querySelector("strong").textContent = item.topic;
    row.querySelector(".knowledge-meta").textContent =
      `掌握度 ${percent}% · 置信度 ${Math.round(item.confidence * 100)}% · 已纳入 ${item.total_attempts} 题 · 待更新 ${item.pending_attempts} 题`;
    target.append(row);
  }
}

document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => loadModule(item.dataset.module));
});

$("chatForm").addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage();
});
$("messageInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});
$("messageInput").addEventListener("input", () => {
  $("messageInput").style.height = "auto";
  $("messageInput").style.height = `${Math.min($("messageInput").scrollHeight, 170)}px`;
});
$("clearChatBtn").addEventListener("click", async () => {
  if (!confirm("清空当前模块的对话与摘要？跨模块共享记忆和 KT 数据不会删除。")) return;
  try {
    await api(`/api/chat/${activeModule}?session_id=${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    await loadModule(activeModule);
  } catch (error) { alert(error.message); }
});
$("uploadForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("pdfFile").files[0];
  if (!file) {
    $("uploadStatus").textContent = "请先选择一个 PDF 文件。";
    return;
  }
  $("uploadBtn").disabled = true;
  $("uploadStatus").textContent = "正在读取并建立索引…";
  try {
    const form = new FormData();
    form.append("file", file);
    const data = await api("/api/rag/upload", { method: "POST", body: form });
    $("uploadStatus").textContent = `已索引 ${data.pages} 页，拆分为 ${data.chunks} 段。`;
    await refreshDocuments();
  } catch (error) {
    $("uploadStatus").textContent = `上传失败：${error.message}`;
  } finally {
    $("uploadBtn").disabled = false;
  }
});
$("refreshKnowledgeBtn").addEventListener("click", async () => {
  try { await refreshKnowledge(); }
  catch (error) { alert(error.message); }
});
$("analyzeKnowledgeBtn").addEventListener("click", async () => {
  await loadModule("mastery");
  $("messageInput").value = "请结合我的 KT 答题记录分析薄弱点，并指出建议优先复习的知识点。";
  await sendMessage();
});

Promise.all([api("/api/health"), refreshMemories()])
  .then(([health]) => {
    $("health").textContent = `在线 · KT 每 ${health.kt_batch_size} 题更新`;
    $("healthDot").classList.add("online");
  })
  .catch((error) => {
    $("health").textContent = "服务连接失败";
    console.error(error);
  });
loadModule(activeModule);
