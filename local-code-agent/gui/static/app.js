(() => {
  const transcript = document.getElementById("transcript");
  const emptyState = document.getElementById("empty-state");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("send-btn");
  const reindexBtn = document.getElementById("reindex-btn");
  const statusDot = document.getElementById("status-dot");
  const modelTag = document.getElementById("model-tag");
  const projectPath = document.getElementById("project-path");

  let currentTurnBody = null;
  let currentTextEl = null;
  let currentRawText = "";
  let turnInFlight = false;
  let planCardEl = null;

  // ---------- helpers ----------

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function renderMarkdownLite(raw) {
    // Minimal, deliberately non-exhaustive markdown: fenced code blocks, inline code,
    // and bold. Everything else stays as plain (already-escaped) text. Re-parses the
    // full accumulated message on every delta, which is cheap at chat-message sizes.
    const escaped = escapeHtml(raw);
    const parts = escaped.split(/```/);
    let html = "";
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {
        let block = parts[i];
        const firstNewline = block.indexOf("\n");
        let code = block;
        if (firstNewline !== -1) {
          const maybeLang = block.slice(0, firstNewline).trim();
          if (/^[A-Za-z0-9_+-]*$/.test(maybeLang)) {
            code = block.slice(firstNewline + 1);
          }
        }
        code = code.replace(/\n$/, "");
        html += `<pre class="codeblock"><code>${code}</code></pre>`;
      } else {
        let seg = parts[i];
        seg = seg.replace(/`([^`\n]+)`/g, "<code>$1</code>");
        seg = seg.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
        html += seg;
      }
    }
    return html;
  }

  function scrollToBottom() {
    transcript.scrollTop = transcript.scrollHeight;
  }

  function hideEmptyState() {
    if (emptyState) emptyState.style.display = "none";
  }

  function ensureTurn() {
    if (currentTurnBody) return currentTurnBody;
    hideEmptyState();
    const tpl = document.getElementById("tpl-turn");
    const node = tpl.content.cloneNode(true);
    const msg = node.querySelector(".msg-agent");
    transcript.appendChild(msg);
    currentTurnBody = msg.querySelector(".turn-body");
    return currentTurnBody;
  }

  function endTurn() {
    currentTurnBody = null;
    currentTextEl = null;
    currentRawText = "";
    turnInFlight = false;
    sendBtn.disabled = false;
  }

  function renderDiff(diffText) {
    const pre = document.createElement("pre");
    pre.className = "permission-diff";
    const lines = diffText.split("\n");
    for (const line of lines) {
      const span = document.createElement("span");
      if (line.startsWith("+") && !line.startsWith("+++")) span.className = "diff-add";
      else if (line.startsWith("-") && !line.startsWith("---")) span.className = "diff-del";
      else if (line.startsWith("@@")) span.className = "diff-hunk";
      span.textContent = line;
      pre.appendChild(span);
      pre.appendChild(document.createTextNode("\n"));
    }
    return pre;
  }

  // ---------- event handlers ----------

  function onContentDelta(delta) {
    const body = ensureTurn();
    if (!currentTextEl) {
      currentTextEl = document.createElement("div");
      currentTextEl.className = "agent-text";
      body.appendChild(currentTextEl);
      currentRawText = "";
    }
    currentRawText += delta;
    currentTextEl.innerHTML = renderMarkdownLite(currentRawText);
    scrollToBottom();
  }

  function onContentDone() {
    currentTextEl = null;
    currentRawText = "";
  }

  function onToolCall(name, args) {
    const body = ensureTurn();
    const tpl = document.getElementById("tpl-tool-call");
    const node = tpl.content.cloneNode(true);
    node.querySelector(".tool-call-name").textContent = name;
    let argsStr = "";
    try { argsStr = JSON.stringify(args); } catch (e) { argsStr = ""; }
    node.querySelector(".tool-call-args").textContent = argsStr.length > 120 ? argsStr.slice(0, 120) + "…" : argsStr;
    body.appendChild(node);
    currentTextEl = null;
    currentRawText = "";
    scrollToBottom();
  }

  function onToolResult(name, text) {
    const body = ensureTurn();
    const tpl = document.getElementById("tpl-tool-result");
    const node = tpl.content.cloneNode(true);
    node.querySelector("summary").textContent = `${name} result`;
    node.querySelector(".tool-result-body").textContent = text;
    body.appendChild(node);
    scrollToBottom();
  }

  function onPermissionRequest(ev) {
    const body = ensureTurn();
    const tpl = document.getElementById("tpl-permission");
    const node = tpl.content.cloneNode(true);
    const card = node.querySelector(".permission-card");
    card.querySelector(".permission-kind").textContent = ev.kind.replace("_", " ");
    card.querySelector(".permission-message").textContent = ev.message || "";
    card.querySelector(".permission-danger").textContent = ev.danger || "";

    if (ev.diff) {
      const placeholder = card.querySelector(".permission-diff");
      const rendered = renderDiff(ev.diff);
      placeholder.replaceWith(rendered);
    }

    card.querySelectorAll(".perm-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const decision = btn.getAttribute("data-decision");
        fetch("/api/permission_response", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: ev.id, decision }),
        });
        card.classList.add("resolved");
        const resolvedLabel = card.querySelector(".permission-resolved");
        const labelMap = { y: "Allowed once", session: "Allowed for this session", n: "Denied" };
        resolvedLabel.textContent = labelMap[decision] || decision;
        resolvedLabel.hidden = false;
      });
    });

    body.appendChild(node);
    currentTextEl = null;
    currentRawText = "";
    scrollToBottom();
  }

  function onStatus(message, isError) {
    const body = ensureTurn();
    const tpl = document.getElementById("tpl-status");
    const node = tpl.content.cloneNode(true);
    const line = node.querySelector(".status-line");
    line.textContent = message;
    if (isError) line.style.color = "var(--danger)";
    body.appendChild(node);
    currentTextEl = null;
    currentRawText = "";
    scrollToBottom();
  }

  function onPlanUpdate(steps) {
    if (!planCardEl) {
      const body = ensureTurn();
      const tpl = document.getElementById("tpl-plan");
      const node = tpl.content.cloneNode(true);
      planCardEl = node.querySelector(".plan-card");
      body.appendChild(node);
      currentTextEl = null;
      currentRawText = "";
    }

    const stepsContainer = planCardEl.querySelector(".plan-steps");
    stepsContainer.innerHTML = "";
    const stepTpl = document.getElementById("tpl-plan-step");
    steps.forEach((s, i) => {
      const stepNode = stepTpl.content.cloneNode(true);
      const stepEl = stepNode.querySelector(".plan-step");
      const status = s.status || "pending";
      stepEl.classList.add(status);
      const icon = status === "completed" ? "✔" : status === "in_progress" ? "▶" : "○";
      stepEl.querySelector(".plan-step-icon").textContent = icon;
      stepEl.querySelector(".plan-step-desc").textContent = `${i + 1}. ${s.description || ""}`;
      stepsContainer.appendChild(stepNode);
    });
    scrollToBottom();
  }

  // ---------- SSE ----------

  function onReindexProgress(current, total) {
    reindexBtn.disabled = true;
    if (total > 0) {
      reindexBtn.textContent = `Indexing ${current}/${total}`;
    } else {
      reindexBtn.textContent = "Indexing…";
    }
  }

  function onReindexDone(files, chunks) {
    reindexBtn.disabled = false;
    reindexBtn.textContent = "Reindex";
    if (files !== null && files !== undefined) {
      reindexBtn.title = `${files} files, ${chunks} chunks indexed`;
    }
  }

  function connectEvents() {
    const es = new EventSource("/events");
    es.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch (err) { return; }

      switch (ev.type) {
        case "content_delta": onContentDelta(ev.delta); break;
        case "content_done": onContentDone(); break;
        case "tool_call": onToolCall(ev.name, ev.args); break;
        case "tool_result": onToolResult(ev.name, ev.text); break;
        case "permission_request": onPermissionRequest(ev); break;
        case "status": onStatus(ev.message, false); break;
        case "plan_update": onPlanUpdate(ev.steps || []); break;
        case "blocked": onStatus(ev.message, true); break;
        case "error": onStatus(ev.message, true); break;
        case "turn_complete": endTurn(); break;
        case "reindex_progress": onReindexProgress(ev.current, ev.total); break;
        case "reindex_done": onReindexDone(ev.files, ev.chunks); break;
        default: break;
      }
    };
    es.onerror = () => {
      // EventSource auto-reconnects; nothing to do here besides letting it retry.
    };
  }

  // ---------- composer ----------

  function sendMessage() {
    const text = input.value.trim();
    if (!text || turnInFlight) return;

    hideEmptyState();
    const tpl = document.getElementById("tpl-user-msg");
    const node = tpl.content.cloneNode(true);
    node.querySelector(".msg-bubble").textContent = text;
    transcript.appendChild(node);
    scrollToBottom();

    input.value = "";
    input.style.height = "auto";
    turnInFlight = true;
    sendBtn.disabled = true;

    fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    }).catch(() => {
      onStatus("Could not reach the local agent server.", true);
      endTurn();
    });
  }

  sendBtn.addEventListener("click", sendMessage);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  });

  reindexBtn.addEventListener("click", () => {
    reindexBtn.disabled = true;
    reindexBtn.textContent = "Indexing…";
    fetch("/api/reindex", { method: "POST" }).catch(() => {
      reindexBtn.disabled = false;
      reindexBtn.textContent = "Reindex";
    });
  });

  // ---------- status polling ----------

  async function pollStatus() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      const ready = data.ollama_reachable && data.chat_model_ready && data.embed_model_ready;
      statusDot.className = "dot " + (ready ? "ok" : "bad");
      modelTag.textContent = data.chat_model + (ready ? "" : " (not ready)");
      projectPath.textContent = data.project_root;
      if (!reindexBtn.disabled && data.stats) {
        reindexBtn.title = `${data.stats.files} files, ${data.stats.chunks} chunks indexed`;
      }
    } catch (e) {
      statusDot.className = "dot bad";
      modelTag.textContent = "server unreachable";
    }
  }

  pollStatus();
  setInterval(pollStatus, 5000);
  connectEvents();
})();
