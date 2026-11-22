(() => {
  const PASSCODE_KEY = "family_ops_passcode";
  const ACTIVE_MEMBER_KEY = "family_ops_active_member_id";
  const SESSION_KEY = "family_ops_chat_session_id";

  let appState = {
    household: {},
    members: [],
    events: [],
    items: [],
    chatSessions: [],
    messagesBySession: {}, // { [sessionId]: Array<message> }
    activeMemberId: localStorage.getItem(ACTIVE_MEMBER_KEY) || "",
    selectedEventParticipants: [],
    calendarFilter: "ALL",
    calendarHorizon: "ALL",
    pendingAttachments: [], // [{ id, filename, mime_type, data_base64, preview_url }]
    sessionId: localStorage.getItem(SESSION_KEY) || `fam-session-${Math.random().toString(36).slice(2, 8)}`,
  };
  localStorage.setItem(SESSION_KEY, appState.sessionId);

  // Per-session in-flight tracker: Map<sessionId, { controller: AbortController, statusText: string }>
  const inflightBySession = new Map();

  let mediaRecorder = null;
  let recordingChunks = [];
  let recordingStream = null;
  let recordingTimerId = null;
  let recordingStartedAt = 0;

  function getStoredPasscode() {
    return localStorage.getItem(PASSCODE_KEY) || "";
  }

  async function apiFetch(path, options = {}) {
    const headers = Object.assign(
      { "Content-Type": "application/json", "X-Household-Passcode": getStoredPasscode() },
      options.headers || {}
    );
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    if (resp.status === 401) {
      document.getElementById("passcode-modal").classList.add("visible");
      throw new Error("UNAUTHORIZED");
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = data.detail || data;
      const err = new Error(typeof detail === "string" ? detail : detail.message || "Request failed");
      err.detail = detail;
      throw err;
    }
    return data;
  }

  function getActiveMember() {
    const found = appState.members.find((m) => m.member_id === appState.activeMemberId);
    return found || appState.members[0] || { member_id: "default", name: "Family", avatar: "🙂" };
  }

  function switchTab(targetId) {
    document.querySelectorAll(".nav-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.target === targetId);
    });
    document.querySelectorAll(".view-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === targetId);
    });
    const gearBtn = document.getElementById("btn-open-setup");
    if (gearBtn) {
      gearBtn.classList.toggle("active", targetId === "view-setup");
    }
  }

  function showBanner(elId, message, kind = "error") {
    const el = document.getElementById(elId);
    if (!el) return;
    el.textContent = message;
    el.className = `feedback-banner visible ${kind}`;
    if (kind === "success") {
      setTimeout(() => el.classList.remove("visible"), 4500);
    }
  }

  function clearBanner(elId) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.className = "feedback-banner";
    el.textContent = "";
  }

  function formatTime(isoStr) {
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return isoStr;
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function formatDayHeading(datePart) {
    const d = new Date(`${datePart}T12:00:00`);
    if (Number.isNaN(d.getTime())) return datePart;
    return d.toLocaleDateString(appState.household.preferred_language || undefined, {
      weekday: "long",
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  }

  function renderHeaderAndSwitchers() {
    const hh = appState.household;
    const todayFormatted = new Date().toLocaleDateString(hh.preferred_language || "en", {
      weekday: "short",
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    });
    document.getElementById("household-title").textContent = hh.household_name || "Family Ops Concierge";
    document.getElementById("household-rules-summary").textContent =
      `Today: ${todayFormatted} | Arrival buffer: ${hh.sports_music_buffer_min ?? 30} min`;

    const active = getActiveMember();
    appState.activeMemberId = active.member_id;
    localStorage.setItem(ACTIVE_MEMBER_KEY, active.member_id);

    const pillsEl = document.getElementById("active-member-pills");
    pillsEl.innerHTML = "";
    appState.members.forEach((m) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `member-pill ${m.member_id === active.member_id ? "active" : ""}`;
      btn.textContent = `${m.avatar || "🙂"} ${m.name}`;
      btn.addEventListener("click", () => {
        appState.activeMemberId = m.member_id;
        appState.selectedEventParticipants = [m.name];
        localStorage.setItem(ACTIVE_MEMBER_KEY, m.member_id);
        renderAll();
      });
      pillsEl.appendChild(btn);
    });

    document.getElementById("chat-speaker-indicator").textContent =
      `Speaking as ${active.avatar || "🙂"} ${active.name} - send text, voice (🎤), or an invitation image (📷 / Ctrl+V).`;

    if (!appState.selectedEventParticipants.length && active.name) {
      appState.selectedEventParticipants = [active.name];
    }
    renderParticipantSelectorChips();

    const assignSelect = document.getElementById("item-assigned");
    assignSelect.innerHTML = `<option value="Anyone">Anyone in Family</option>`;
    appState.members.forEach((m) => {
      const opt2 = document.createElement("option");
      opt2.value = m.name;
      opt2.textContent = `${m.avatar || ""} ${m.name}`;
      assignSelect.appendChild(opt2);
    });
  }

  function renderParticipantSelectorChips() {
    const chipsWrap = document.getElementById("evt-participants-chips");
    if (!chipsWrap) return;
    chipsWrap.innerHTML = "";

    const isAll = appState.selectedEventParticipants.includes("All");
    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = `filter-chip ${isAll ? "active" : ""}`;
    allBtn.textContent = "[All] Whole Family";
    allBtn.addEventListener("click", () => {
      appState.selectedEventParticipants = ["All"];
      renderParticipantSelectorChips();
    });
    chipsWrap.appendChild(allBtn);

    appState.members.forEach((m) => {
      const selected = !isAll && appState.selectedEventParticipants.includes(m.name);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `filter-chip ${selected ? "active" : ""}`;
      btn.textContent = `${m.avatar || ""} [${m.name}]`;
      btn.addEventListener("click", () => {
        let next = appState.selectedEventParticipants.filter((p) => p !== "All");
        if (next.includes(m.name)) {
          next = next.filter((p) => p !== m.name);
        } else {
          next.push(m.name);
        }
        appState.selectedEventParticipants = next;
        renderParticipantSelectorChips();
      });
      chipsWrap.appendChild(btn);
    });
  }

  function renderCalendar() {
    const horizonEl = document.getElementById("calendar-horizon-filters");
    if (horizonEl) {
      horizonEl.innerHTML = "";
      const btn14 = document.createElement("button");
      btn14.type = "button";
      btn14.className = `filter-chip ${appState.calendarHorizon === "14D" ? "active" : ""}`;
      btn14.textContent = "Next 14 Days";
      btn14.addEventListener("click", () => {
        appState.calendarHorizon = "14D";
        renderCalendar();
      });

      const btnAll = document.createElement("button");
      btnAll.type = "button";
      btnAll.className = `filter-chip ${appState.calendarHorizon === "ALL" ? "active" : ""}`;
      btnAll.textContent = `All Scheduled (${appState.events.length})`;
      btnAll.addEventListener("click", () => {
        appState.calendarHorizon = "ALL";
        renderCalendar();
      });

      horizonEl.appendChild(btn14);
      horizonEl.appendChild(btnAll);
    }

    const filterEl = document.getElementById("calendar-member-filters");
    filterEl.innerHTML = "";

    const allChip = document.createElement("button");
    allChip.type = "button";
    allChip.className = `filter-chip ${appState.calendarFilter === "ALL" ? "active" : ""}`;
    allChip.textContent = "All Family";
    allChip.addEventListener("click", () => {
      appState.calendarFilter = "ALL";
      renderCalendar();
    });
    filterEl.appendChild(allChip);

    appState.members.forEach((m) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = `filter-chip ${appState.calendarFilter === m.name ? "active" : ""}`;
      chip.textContent = `${m.avatar || ""} ${m.name}`;
      chip.addEventListener("click", () => {
        appState.calendarFilter = m.name;
        renderCalendar();
      });
      filterEl.appendChild(chip);
    });

    const container = document.getElementById("calendar-events-container");
    container.innerHTML = "";

    const now = new Date();
    const cutoff14d = new Date(now.getTime() + 14 * 24 * 60 * 60 * 1000);

    const filtered = appState.events.filter((ev) => {
      if (appState.calendarHorizon === "14D") {
        const dt = new Date(ev.start_iso);
        if (!Number.isNaN(dt.getTime()) && dt > cutoff14d) {
          return false;
        }
      }
      if (appState.calendarFilter === "ALL") return true;
      const p = (ev.participants || "").toLowerCase();
      return p === "all" || p === "whole family" || p.includes(appState.calendarFilter.toLowerCase());
    });

    if (!filtered.length) {
      container.innerHTML = `<div class="empty-state">No events in this view. Add an event above, switch horizon to All Scheduled, or upload an invitation to the AI Concierge!</div>`;
      return;
    }

    let currentDay = "";
    filtered.forEach((ev) => {
      const dayPart = ev.start_iso.slice(0, 10);
      if (dayPart !== currentDay) {
        currentDay = dayPart;
        const h = document.createElement("div");
        h.className = "day-heading";
        h.textContent = formatDayHeading(dayPart);
        container.appendChild(h);
      }

      const row = document.createElement("div");
      row.className = "event-row";
      const hasBuffer = ev.buffer_start_iso && ev.buffer_start_iso !== ev.start_iso;
      const isRecurring = ev.recurrence && ev.recurrence !== "NONE" && ev.series_id;

      row.innerHTML = `
        <div class="event-time-col">
          <div>${formatTime(ev.start_iso)} - ${formatTime(ev.end_iso)}</div>
          ${hasBuffer ? `<span class="buffer-tag">Buffer ${formatTime(ev.buffer_start_iso)}</span>` : ""}
        </div>
        <div class="event-main-col">
          <div class="event-title">${escapeHtml(ev.title)}</div>
          <div class="event-meta">
            <span>👥 ${escapeHtml(ev.participants === "All" ? "All Family" : ev.participants)}</span>
            ${ev.location ? `<span>📍 ${escapeHtml(ev.location)}</span>` : ""}
            ${isRecurring ? `<span>🔁 ${escapeHtml(ev.recurrence)}</span>` : ""}
            <span>🏷️ ${escapeHtml(ev.category)}</span>
            <span>✍️ Added by ${escapeHtml(ev.created_by || "Family")}</span>
            ${ev.notes ? `<span>📝 ${escapeHtml(ev.notes)}</span>` : ""}
          </div>
        </div>
      `;

      const actionWrap = document.createElement("div");
      actionWrap.style.display = "flex";
      actionWrap.style.gap = "4px";
      actionWrap.style.flexWrap = "wrap";

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn btn-ghost-danger";
      delBtn.textContent = isRecurring ? "Remove 1x" : "Remove";
      delBtn.addEventListener("click", async () => {
        await apiFetch(`/api/events/${encodeURIComponent(ev.event_id)}`, { method: "DELETE" });
        await loadFullState();
      });
      actionWrap.appendChild(delBtn);

      if (isRecurring) {
        const delSeriesBtn = document.createElement("button");
        delSeriesBtn.type = "button";
        delSeriesBtn.className = "btn btn-ghost-danger";
        delSeriesBtn.textContent = "Remove Series";
        delSeriesBtn.addEventListener("click", async () => {
          await apiFetch(`/api/events/${encodeURIComponent(ev.event_id)}?delete_series=true`, {
            method: "DELETE",
          });
          await loadFullState();
        });
        actionWrap.appendChild(delSeriesBtn);
      }

      row.appendChild(actionWrap);
      container.appendChild(row);
    });
  }

  function renderLists() {
    const needEl = document.getElementById("need-todo-list");
    const wantEl = document.getElementById("want-todo-list");
    needEl.innerHTML = "";
    wantEl.innerHTML = "";

    const needs = appState.items.filter((i) => i.item_type === "NEED_TO_DO");
    const wants = appState.items.filter((i) => i.item_type === "WANT_TO_DO");

    if (!needs.length) {
      needEl.innerHTML = `<div class="empty-state">No open errands. Add obligations or prep tasks above!</div>`;
    } else {
      needs.forEach((item) => needEl.appendChild(buildItemRow(item)));
    }

    if (!wants.length) {
      wantEl.innerHTML = `<div class="empty-state">No wish-list items yet. Add movies, outings, or family ideas!</div>`;
    } else {
      wants.forEach((item) => wantEl.appendChild(buildItemRow(item)));
    }
  }

  function buildItemRow(item) {
    const row = document.createElement("div");
    row.className = `item-row ${item.status === "DONE" ? "done" : ""}`;

    const wrap = document.createElement("div");
    wrap.className = "item-check-wrap";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = item.status === "DONE";
    cb.addEventListener("change", async () => {
      await apiFetch(`/api/items/${encodeURIComponent(item.item_id)}`, {
        method: "PATCH",
        body: JSON.stringify({ status: cb.checked ? "DONE" : "OPEN" }),
      });
      await loadFullState();
    });

    const textCol = document.createElement("div");
    textCol.innerHTML = `
      <div class="item-title">${escapeHtml(item.title)}</div>
      <div class="item-sub">
        ${item.deadline_or_closing_date ? `<span class="deadline-pill">By ${escapeHtml(item.deadline_or_closing_date)}</span>` : ""}
        <span>Owner: ${escapeHtml(item.assigned_to || "Anyone")}</span> ·
        <span>Added by ${escapeHtml(item.created_by || "Family")}</span>
      </div>
    `;

    wrap.appendChild(cb);
    wrap.appendChild(textCol);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn btn-ghost-danger";
    delBtn.textContent = "✕";
    delBtn.title = "Delete item";
    delBtn.addEventListener("click", async () => {
      await apiFetch(`/api/items/${encodeURIComponent(item.item_id)}`, {
        method: "PATCH",
        body: JSON.stringify({ delete: true }),
      });
      await loadFullState();
    });

    row.appendChild(wrap);
    row.appendChild(delBtn);
    return row;
  }

  function renderSetupForm() {
    const hh = appState.household;
    document.getElementById("cfg-name").value = hh.household_name || "";
    document.getElementById("cfg-language").value = hh.preferred_language || "en";
    document.getElementById("cfg-timezone").value = hh.timezone || "America/Sao_Paulo";
    document.getElementById("cfg-buffer").value = hh.sports_music_buffer_min ?? 30;
    document.getElementById("cfg-passcode").value = "";
    document.getElementById("cfg-passcode").placeholder = hh.has_passcode
      ? "Passcode active (leave blank to keep)"
      : "Optional PIN for family phones";

    const listEl = document.getElementById("setup-members-list");
    listEl.innerHTML = "";
    appState.members.forEach((m) => appendMemberSetupRow(m));
  }

  function appendMemberSetupRow(member = {}) {
    const listEl = document.getElementById("setup-members-list");
    const row = document.createElement("div");
    row.className = "member-editor-row";
    row.dataset.memberId = member.member_id || "";

    row.innerHTML = `
      <input type="text" class="m-avatar" value="${escapeAttr(member.avatar || "🙂")}" maxlength="4" title="Emoji avatar" />
      <input type="text" class="m-name" value="${escapeAttr(member.name || "")}" placeholder="Member name (e.g. Kid1)" required />
      <select class="m-role">
        <option value="Parent" ${member.role === "Parent" ? "selected" : ""}>Parent</option>
        <option value="Teen" ${member.role === "Teen" ? "selected" : ""}>Teen</option>
        <option value="Child" ${member.role === "Child" ? "selected" : ""}>Child</option>
        <option value="Relative" ${member.role === "Relative" ? "selected" : ""}>Relative</option>
      </select>
      <input type="text" class="m-notes" value="${escapeAttr(member.routine_notes || "")}" placeholder="Routine/activities (e.g. Band rehearsal, soccer)" />
      <button type="button" class="btn btn-ghost-danger m-remove">Remove</button>
    `;

    row.querySelector(".m-remove").addEventListener("click", () => {
      if (document.querySelectorAll(".member-editor-row").length > 1) {
        row.remove();
      }
    });
    listEl.appendChild(row);
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function escapeAttr(str) {
    return escapeHtml(str).replace(/"/g, "&quot;");
  }

  function renderAll() {
    renderHeaderAndSwitchers();
    renderCalendar();
    renderLists();
    renderSetupForm();
    renderPendingAttachments();
    renderChatSessionsList();
    syncComposerControls();
  }

  async function loadFullState(initialBootstrap = false) {
    const data = await apiFetch("/api/state?include_completed=true");
    appState.household = data.household;
    appState.members = data.members;
    appState.events = data.events;
    appState.items = data.items;
    appState.chatSessions = data.chat_sessions || [];

    if (initialBootstrap) {
      const existingMatch = appState.chatSessions.find((s) => s.session_id === appState.sessionId);
      if (existingMatch) {
        await loadSessionTranscript(appState.sessionId);
      } else if (appState.chatSessions.length > 0) {
        appState.sessionId = appState.chatSessions[0].session_id;
        localStorage.setItem(SESSION_KEY, appState.sessionId);
        await loadSessionTranscript(appState.sessionId);
      } else {
        renderActiveSessionMessages();
      }
    }
    renderAll();
  }

  // --- MULTIMODAL ATTACHMENT & VOICE RECORDING HELPERS ---

  function renderPendingAttachments() {
    const strip = document.getElementById("chat-attachment-strip");
    if (!strip) return;
    strip.innerHTML = "";
    if (!appState.pendingAttachments.length) {
      strip.classList.remove("visible");
      return;
    }
    strip.classList.add("visible");

    appState.pendingAttachments.forEach((att) => {
      const chip = document.createElement("div");
      chip.className = "attachment-chip";

      if (att.mime_type.startsWith("image/")) {
        const img = document.createElement("img");
        img.src = att.preview_url;
        img.alt = att.filename || "Uploaded image";
        chip.appendChild(img);
        const label = document.createElement("span");
        label.textContent = att.filename || "Invitation image";
        chip.appendChild(label);
      } else if (att.mime_type.startsWith("audio/")) {
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.src = att.preview_url;
        chip.appendChild(audio);
      } else {
        const docLabel = document.createElement("span");
        docLabel.textContent = `📄 ${att.filename || "Attachment"}`;
        chip.appendChild(docLabel);
      }

      const rmBtn = document.createElement("button");
      rmBtn.type = "button";
      rmBtn.className = "attachment-remove";
      rmBtn.textContent = "✕";
      rmBtn.title = "Remove attachment";
      rmBtn.addEventListener("click", () => {
        appState.pendingAttachments = appState.pendingAttachments.filter((x) => x.id !== att.id);
        renderPendingAttachments();
      });
      chip.appendChild(rmBtn);
      strip.appendChild(chip);
    });
  }

  function fileOrBlobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = String(reader.result || "");
        const base64Part = dataUrl.includes(",") ? dataUrl.split(",", 2)[1] : dataUrl;
        resolve({ dataUrl, base64Part });
      };
      reader.onerror = () => reject(new Error("Could not read file."));
      reader.readAsDataURL(blob);
    });
  }

  async function stageFiles(fileList) {
    clearBanner("chat-feedback");
    for (const file of Array.from(fileList || [])) {
      const mime = (file.type || "").toLowerCase();
      if (!mime.startsWith("image/") && !mime.startsWith("audio/") && mime !== "application/pdf") {
        showBanner("chat-feedback", `Unsupported file '${file.name}'. Please upload an image, audio clip, or PDF.`, "error");
        continue;
      }
      if (file.size > 12 * 1024 * 1024) {
        showBanner("chat-feedback", `File '${file.name}' exceeds the 12 MB limit.`, "error");
        continue;
      }
      const { dataUrl, base64Part } = await fileOrBlobToBase64(file);
      appState.pendingAttachments.push({
        id: `att-${Math.random().toString(36).slice(2, 9)}`,
        filename: file.name || "attachment",
        mime_type: mime.split(";", 1)[0],
        data_base64: base64Part,
        preview_url: dataUrl,
      });
    }
    renderPendingAttachments();
    document.getElementById("chat-input").focus();
  }

  function pickSupportedAudioMime() {
    if (typeof MediaRecorder === "undefined") return "";
    const candidates = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/mp4",
      "audio/ogg;codecs=opus",
    ];
    for (const c of candidates) {
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(c)) {
        return c;
      }
    }
    return "";
  }

  function stopMicStream() {
    if (recordingTimerId) {
      clearInterval(recordingTimerId);
      recordingTimerId = null;
    }
    if (recordingStream) {
      recordingStream.getTracks().forEach((t) => t.stop());
      recordingStream = null;
    }
    const micBtn = document.getElementById("btn-mic-record");
    micBtn.classList.remove("btn-recording");
    micBtn.textContent = "🎤 Speak";
  }

  async function toggleMicRecording() {
    clearBanner("chat-feedback");
    const micBtn = document.getElementById("btn-mic-record");

    if (mediaRecorder && mediaRecorder.state === "recording") {
      mediaRecorder.stop();
      return;
    }

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showBanner(
        "chat-feedback",
        "Microphone access is unavailable in this browser context. Use http://localhost:8080 or https://, or attach an audio file via '📷 Invite / Image'.",
        "error"
      );
      return;
    }

    try {
      recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chosenMime = pickSupportedAudioMime();
      recordingChunks = [];
      mediaRecorder = chosenMime
        ? new MediaRecorder(recordingStream, { mimeType: chosenMime })
        : new MediaRecorder(recordingStream);

      mediaRecorder.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) {
          recordingChunks.push(ev.data);
        }
      };

      mediaRecorder.onstop = async () => {
        const actualMime = (mediaRecorder.mimeType || "audio/webm").split(";", 1)[0];
        stopMicStream();
        if (!recordingChunks.length) return;
        const blob = new Blob(recordingChunks, { type: actualMime });
        const { dataUrl, base64Part } = await fileOrBlobToBase64(blob);
        appState.pendingAttachments.push({
          id: `voice-${Math.random().toString(36).slice(2, 9)}`,
          filename: "voice-message.webm",
          mime_type: actualMime,
          data_base64: base64Part,
          preview_url: dataUrl,
        });
        renderPendingAttachments();
        document.getElementById("btn-send-chat").focus();
      };

      mediaRecorder.start();
      recordingStartedAt = Date.now();
      micBtn.classList.add("btn-recording");
      micBtn.textContent = "⏹ Stop (0:00)";
      recordingTimerId = setInterval(() => {
        const sec = Math.floor((Date.now() - recordingStartedAt) / 1000);
        const mm = Math.floor(sec / 60);
        const ss = String(sec % 60).padStart(2, "0");
        micBtn.textContent = `⏹ Stop (${mm}:${ss})`;
      }, 500);
    } catch (err) {
      stopMicStream();
      showBanner(
        "chat-feedback",
        `Could not access microphone (${err.message}). Check browser/Remote Desktop mic permission, or upload an audio clip via '📷 Invite / Image'.`,
        "error"
      );
    }
  }

  // Wire Navigation Tabs & Header Gear Icon
  document.querySelectorAll(".nav-tab").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.target));
  });

  document.getElementById("btn-open-setup").addEventListener("click", () => {
    const setupPanel = document.getElementById("view-setup");
    if (setupPanel.classList.contains("active")) {
      switchTab("view-concierge");
    } else {
      switchTab("view-setup");
    }
  });

  document.getElementById("btn-close-setup").addEventListener("click", () => {
    switchTab("view-concierge");
  });

  // Wire Add Event Form
  document.getElementById("add-event-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearBanner("event-feedback");
    if (!appState.selectedEventParticipants.length) {
      showBanner("event-feedback", "Please select who is participating ([All] or specific family member(s)).", "error");
      return;
    }
    const active = getActiveMember();
    const startRaw = document.getElementById("evt-start").value;
    const endRaw = document.getElementById("evt-end").value;
    const startIso = startRaw.length === 16 ? `${startRaw}:00` : startRaw;
    const endIso = endRaw.length === 16 ? `${endRaw}:00` : endRaw;
    const recurrence = document.getElementById("evt-recurrence").value;
    const participantsStr = appState.selectedEventParticipants.includes("All")
      ? "All"
      : appState.selectedEventParticipants.join(", ");

    const eventPayload = {
      title: document.getElementById("evt-title").value.trim(),
      category: document.getElementById("evt-category").value,
      start_iso: startIso,
      end_iso: endIso,
      participants: participantsStr,
      location: document.getElementById("evt-location").value.trim(),
      recurrence,
      occurrences: recurrence === "NONE" ? 1 : 30,
      notes: document.getElementById("evt-notes").value.trim(),
      created_by: active.name,
      allow_overlap: false,
    };

    try {
      let res;
      try {
        res = await apiFetch("/api/events", {
          method: "POST",
          body: JSON.stringify(eventPayload),
        });
      } catch (firstErr) {
        if (firstErr.detail && firstErr.detail.error_code === "OVERLAP_CONFIRMATION_REQUIRED") {
          const accepted = window.confirm(
            `Schedule or 30-Min Arrival-Buffer Overlap Notice:\n\n${firstErr.detail.message}\n\n` +
            `Overlaps are allowed on the Family Calendar (e.g. parallel kid activities). Do you want to add this event anyway?`
          );
          if (!accepted) {
            showBanner(
              "event-feedback",
              "Event was not added. Adjust the date/time if you want to avoid the overlap, or submit again to confirm.",
              "error"
            );
            return;
          }
          res = await apiFetch("/api/events", {
            method: "POST",
            body: JSON.stringify(Object.assign({}, eventPayload, { allow_overlap: true })),
          });
        } else {
          throw firstErr;
        }
      }
      e.target.reset();
      await loadFullState();
      const countMsg = res.occurrences_created > 1 ? ` (${res.occurrences_created} repetitions added)` : "";
      const overlapMsg = res.overlap_warnings && res.overlap_warnings.length ? " (overlap confirmed)" : "";
      showBanner("event-feedback", `Event scheduled on the shared Family Calendar${countMsg}${overlapMsg}!`, "success");
    } catch (err) {
      const hint = err.detail && err.detail.recovery_hint ? ` Tip: ${err.detail.recovery_hint}` : "";
      showBanner("event-feedback", `${err.message}${hint}`, "error");
    }
  });

  // Wire Add Item Form
  document.getElementById("add-item-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearBanner("item-feedback");
    const active = getActiveMember();
    try {
      await apiFetch("/api/items", {
        method: "POST",
        body: JSON.stringify({
          title: document.getElementById("item-title").value.trim(),
          item_type: document.getElementById("item-type").value,
          deadline_or_closing_date: document.getElementById("item-deadline").value || null,
          assigned_to: document.getElementById("item-assigned").value,
          created_by: active.name,
        }),
      });
      e.target.reset();
      await loadFullState();
      showBanner("item-feedback", "Saved to shared list!", "success");
    } catch (err) {
      showBanner("item-feedback", err.message, "error");
    }
  });

  // Wire Setup Form
  document.getElementById("btn-add-member-row").addEventListener("click", () => {
    appendMemberSetupRow({ avatar: "🌟", role: "Child" });
  });

  document.getElementById("household-setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearBanner("setup-feedback");
    const rows = Array.from(document.querySelectorAll(".member-editor-row"));
    const members = rows.map((r) => ({
      member_id: r.dataset.memberId || null,
      avatar: r.querySelector(".m-avatar").value.trim() || "🙂",
      name: r.querySelector(".m-name").value.trim(),
      role: r.querySelector(".m-role").value,
      routine_notes: r.querySelector(".m-notes").value.trim(),
    }));

    const newPass = document.getElementById("cfg-passcode").value.trim();
    try {
      await apiFetch("/api/household/setup", {
        method: "PUT",
        body: JSON.stringify({
          household_name: document.getElementById("cfg-name").value.trim(),
          preferred_language: document.getElementById("cfg-language").value,
          timezone: document.getElementById("cfg-timezone").value.trim(),
          sports_music_buffer_min: Number(document.getElementById("cfg-buffer").value || 30),
          new_passcode: newPass || null,
          clear_passcode: false,
          members,
        }),
      });
      if (newPass) {
        localStorage.setItem(PASSCODE_KEY, newPass);
      }
      await loadFullState();
      showBanner("setup-feedback", "Family configuration saved! Everyone's profile pills are updated above.", "success");
    } catch (err) {
      showBanner("setup-feedback", err.message, "error");
    }
  });

  document.getElementById("btn-clear-passcode").addEventListener("click", async (e) => {
    e.preventDefault();
    const rows = Array.from(document.querySelectorAll(".member-editor-row"));
    const members = rows.map((r) => ({
      member_id: r.dataset.memberId || null,
      avatar: r.querySelector(".m-avatar").value.trim() || "🙂",
      name: r.querySelector(".m-name").value.trim(),
      role: r.querySelector(".m-role").value,
      routine_notes: r.querySelector(".m-notes").value.trim(),
    }));
    await apiFetch("/api/household/setup", {
      method: "PUT",
      body: JSON.stringify({
        household_name: document.getElementById("cfg-name").value.trim() || "Our Family",
        preferred_language: document.getElementById("cfg-language").value,
        timezone: document.getElementById("cfg-timezone").value.trim(),
        sports_music_buffer_min: Number(document.getElementById("cfg-buffer").value || 30),
        clear_passcode: true,
        members,
      }),
    });
    localStorage.removeItem(PASSCODE_KEY);
    await loadFullState();
    showBanner("setup-feedback", "Passcode lock removed.", "success");
  });

  // Wire Multimodal Controls (Upload, Mic, Paste, Drag-and-Drop)
  const fileInput = document.getElementById("chat-file-input");
  document.getElementById("btn-attach-media").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    if (fileInput.files && fileInput.files.length) {
      await stageFiles(fileInput.files);
      fileInput.value = "";
    }
  });

  document.getElementById("btn-mic-record").addEventListener("click", toggleMicRecording);

  document.getElementById("chat-input").addEventListener("paste", async (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    const mediaFiles = [];
    for (const item of items) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) mediaFiles.push(f);
      }
    }
    if (mediaFiles.length) {
      e.preventDefault();
      await stageFiles(mediaFiles);
    }
  });

  const dropzone = document.getElementById("chat-dropzone");
  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("drag-over");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag-over"));
  dropzone.addEventListener("drop", async (e) => {
    e.preventDefault();
    dropzone.classList.remove("drag-over");
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      await stageFiles(e.dataTransfer.files);
    }
  });

  // --- MULTI-SESSION CHAT HISTORY & NON-BLOCKING CONCURRENCY ---

  const DEFAULT_WELCOME_TEXT =
    "Hi! I'm your Family Ops Concierge. You can type below, speak with your microphone (🎤 Speak), or upload/paste a photo of a party invitation or school flyer (📷 Invite / Image) so we can organize your family schedule together!";

  function syncComposerControls() {
    const isBusy = inflightBySession.has(appState.sessionId);
    const sendBtn = document.getElementById("btn-send-chat");
    const stopBtn = document.getElementById("btn-stop-request");
    const chatTitleEl = document.getElementById("active-chat-title");

    if (sendBtn) {
      sendBtn.disabled = isBusy;
      sendBtn.textContent = isBusy ? "Thinking..." : "Send";
    }
    if (stopBtn) {
      stopBtn.style.display = isBusy ? "inline-flex" : "none";
    }
    if (chatTitleEl) {
      const meta = appState.chatSessions.find((s) => s.session_id === appState.sessionId);
      chatTitleEl.textContent =
        meta && meta.title && meta.title !== "New Chat"
          ? meta.title
          : "Family Ops AI Concierge";
    }
  }

  function renderChatSessionsList() {
    const listEl = document.getElementById("chat-sessions-list");
    if (!listEl) return;
    listEl.innerHTML = "";

    const sessionsToRender = [...appState.chatSessions];
    if (!sessionsToRender.some((s) => s.session_id === appState.sessionId)) {
      sessionsToRender.unshift({
        session_id: appState.sessionId,
        title: "New Chat",
        status: inflightBySession.has(appState.sessionId) ? "running" : "idle",
        message_count: (appState.messagesBySession[appState.sessionId] || []).length,
        last_preview: "Fresh conversation",
      });
    }

    sessionsToRender.forEach((sess) => {
      const row = document.createElement("div");
      const isActive = sess.session_id === appState.sessionId;
      const isRunningLocal = inflightBySession.has(sess.session_id) || sess.status === "running";
      const needsApproval = !isRunningLocal && sess.status === "awaiting_approval";
      row.className = `session-item ${isActive ? "active" : ""}`;

      const main = document.createElement("div");
      main.className = "session-item-main";

      const titleDiv = document.createElement("div");
      titleDiv.className = "session-item-title";
      titleDiv.textContent = sess.title || "New Chat";
      main.appendChild(titleDiv);

      const metaDiv = document.createElement("div");
      metaDiv.className = "session-item-meta";
      if (isRunningLocal) {
        const badge = document.createElement("span");
        badge.className = "session-status-badge running";
        badge.textContent = "⏳ Thinking...";
        metaDiv.appendChild(badge);
      } else if (needsApproval) {
        const badge = document.createElement("span");
        badge.className = "session-status-badge approval";
        badge.textContent = "🛡️ Approval";
        metaDiv.appendChild(badge);
      }
      const countSpan = document.createElement("span");
      const count = Number(sess.message_count || 0);
      countSpan.textContent = count > 0 ? `${count} msg${count === 1 ? "" : "s"}` : "Empty";
      metaDiv.appendChild(countSpan);
      main.appendChild(metaDiv);

      row.appendChild(main);

      if (count > 0 || sessionsToRender.length > 1) {
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "session-delete-btn";
        delBtn.textContent = "✕";
        delBtn.title = "Delete chat session";
        delBtn.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          await deleteChatSession(sess.session_id);
        });
        row.appendChild(delBtn);
      }

      row.addEventListener("click", async () => {
        if (appState.sessionId === sess.session_id) return;
        await switchChatSession(sess.session_id);
      });

      listEl.appendChild(row);
    });
  }

  async function loadSessionTranscript(targetSessionId) {
    try {
      const detail = await apiFetch(`/api/chat/sessions/${encodeURIComponent(targetSessionId)}`);
      appState.messagesBySession[targetSessionId] = detail.messages || [];
    } catch (err) {
      if (!appState.messagesBySession[targetSessionId]) {
        appState.messagesBySession[targetSessionId] = [];
      }
    }
    if (appState.sessionId === targetSessionId) {
      renderActiveSessionMessages();
    }
  }

  async function switchChatSession(targetSessionId) {
    clearBanner("chat-feedback");
    appState.sessionId = targetSessionId;
    localStorage.setItem(SESSION_KEY, targetSessionId);
    renderChatSessionsList();
    syncComposerControls();
    await loadSessionTranscript(targetSessionId);
    document.getElementById("chat-input").focus();
  }

  async function startNewChatSession() {
    clearBanner("chat-feedback");
    const newId = `fam-session-${Math.random().toString(36).slice(2, 8)}`;
    appState.sessionId = newId;
    appState.messagesBySession[newId] = [];
    localStorage.setItem(SESSION_KEY, newId);
    renderActiveSessionMessages();
    renderChatSessionsList();
    syncComposerControls();
    document.getElementById("chat-input").focus();
  }

  async function deleteChatSession(targetSessionId) {
    const inflight = inflightBySession.get(targetSessionId);
    if (inflight) {
      inflight.controller.abort();
      inflightBySession.delete(targetSessionId);
    }
    delete appState.messagesBySession[targetSessionId];
    try {
      const res = await apiFetch(`/api/chat/sessions/${encodeURIComponent(targetSessionId)}`, {
        method: "DELETE",
      });
      appState.chatSessions = res.chat_sessions || [];
    } catch (err) {
      appState.chatSessions = appState.chatSessions.filter((s) => s.session_id !== targetSessionId);
    }

    if (appState.sessionId === targetSessionId) {
      if (appState.chatSessions.length > 0) {
        await switchChatSession(appState.chatSessions[0].session_id);
      } else {
        await startNewChatSession();
      }
    } else {
      renderChatSessionsList();
      syncComposerControls();
    }
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function escapeRegExp(str) {
    return String(str || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function formatInlineRichHtml(rawStr) {
    let html = escapeHtml(rawStr);
    // Inline code spans
    html = html.replace(/`([^`]+)`/g, '<code class="chat-inline-code">$1</code>');
    // Bold spans
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // Bracketed participant tags such as [All], [Kid1], [Papa, Mama]
    const knownNames = (appState.members || [])
      .map((m) => escapeRegExp(escapeHtml(m.name)))
      .filter(Boolean);
    const tokenAlternation = ["All", ...knownNames].join("|");
    if (tokenAlternation) {
      const tagRegex = new RegExp(
        `\\[((?:${tokenAlternation})(?:,\\s*(?:${tokenAlternation}))*)\\]`,
        "g"
      );
      html = html.replace(tagRegex, '<span class="chat-member-tag">[$1]</span>');
    }
    return html;
  }

  function isPositiveNonWarningLine(line) {
    return /\b(no\s+overlap|without\s+overlap|without\s+overlapping|conflict[- ]free|no\s+conflict|zero\s+conflict|sem\s+conflito)\b/i.test(
      line
    );
  }

  function isWarningLine(line) {
    const trimmed = line.trim();
    if (!trimmed) return false;
    if (
      /^(?:\*\*)?(?:⚠️\s*)?(?:warning|overlap\s+warning|overlap\s+notice|buffer\s+warning|buffer\s+notice|heads\s+up|caution|aten[çc][ãa]o|aviso)\s*:/i.test(
        trimmed
      ) ||
      trimmed.startsWith("⚠️")
    ) {
      return true;
    }
    if (isPositiveNonWarningLine(trimmed)) {
      return false;
    }
    return /\b(overlaps?\s+with|direct\s+time\s+overlap|arrival[- ]buffer\s+overlap|30[- ]min(?:ute)?\s+(?:sports\/music\s+)?(?:arrival[- ]?)?buffer\s+overlap|schedule\s+conflict|conflita\s+com|sobreposi[çc][ãa]o)\b/i.test(
      trimmed
    );
  }

  function stripWarningPrefix(line) {
    return line
      .trim()
      .replace(/^[-*•]\s*/, "")
      .replace(/^(?:\*\*)?(?:⚠️\s*)?(?:warning|overlap\s+warning|overlap\s+notice|buffer\s+warning|buffer\s+notice|heads\s+up|caution|aten[çc][ãa]o|aviso)(?:\*\*)?\s*:\s*/i, "")
      .replace(/^⚠️\s*/, "")
      .trim();
  }

  function isExplicitActionLine(line) {
    const trimmed = line.trim();
    return (
      trimmed.startsWith("👉") ||
      trimmed.startsWith("❓") ||
      /^(?:\*\*)?(?:👉\s*|❓\s*)?(?:action\s+needed|your\s+input|your\s+decision|question|a[çc][ãa]o\s+necess[áa]ria|pergunta)(?:\*\*)?\s*:/i.test(
        trimmed
      )
    );
  }

  function stripActionPrefix(line) {
    return line
      .trim()
      .replace(/^[-*•]\s*/, "")
      .replace(/^(?:\*\*)?(?:👉\s*|❓\s*)?(?:action\s+needed|your\s+input|your\s+decision|question|a[çc][ãa]o\s+necess[áa]ria|pergunta)(?:\*\*)?\s*:\s*/i, "")
      .replace(/^(?:👉|❓)\s*/, "")
      .replace(/^\*\*([\s\S]+)\*\*$/, "$1")
      .trim();
  }

  function extractQuickRepliesAndCleanText(rawContent) {
    const quickReplies = [];
    const cleaned = String(rawContent || "")
      .replace(/\[QUICK_REPLIES:\s*([^\]]+)\]/gi, (_match, group1) => {
        String(group1 || "")
          .split("|")
          .map((s) => s.trim())
          .filter(Boolean)
          .forEach((chip) => {
            if (!quickReplies.includes(chip)) {
              quickReplies.push(chip);
            }
          });
        return "";
      })
      .trim();
    return { cleanedText: cleaned, explicitChips: quickReplies.slice(0, 5) };
  }

  function inferSmartReplyChips(cleanText, extractedQuestion) {
    const combined = `${cleanText}\n${extractedQuestion || ""}`;
    const chips = [];

    // 1. Participant selection question
    if (
      /\b(which\s+family\s+member|who\s+will\s+(?:be\s+)?(?:attend|participat|go)|who\s+is\s+(?:attending|participating|going)|participants?|quem\s+vai|qual\s+membro)\b/i.test(
        combined
      )
    ) {
      const members = appState.members || [];
      members.slice(0, 4).forEach((m) => {
        chips.push({
          label: `${m.avatar || "🙂"} Only ${m.name}`,
          prompt: `Only ${m.name} will participate.`,
        });
      });
      const kids = members.filter((m) => m.role === "Teen" || m.role === "Child");
      if (kids.length >= 2) {
        const kidNames = kids.map((k) => k.name).join(" and ");
        chips.push({
          label: `🧒 Both ${kidNames}`,
          prompt: `${kidNames} will participate.`,
        });
      }
      chips.push({
        label: "👨‍👩‍👧‍👦 [All] Whole Family",
        prompt: "[All] The whole family will participate.",
      });
      return chips.slice(0, 5);
    }

    // 2. Overlap / arrival-buffer verbal confirmation question
    if (/\b(anyway|despite\s+the\s+overlap|overlap|arrival[- ]buffer|mesmo\s+assim)\b/i.test(combined)) {
      return [
        { label: "✓ Yes, schedule it anyway", prompt: "Yes, please schedule it anyway." },
        { label: "🕒 Suggest a free time slot", prompt: "Please suggest a conflict-free time slot on that day instead." },
        { label: "✕ Skip for now", prompt: "Skip scheduling this event for now." },
      ];
    }

    // 3. Linked Need-To-Do errand / gift reminder question
    if (/\b(need-to-do|errand|buy\s+a?\s*gift|birthday\s+gift|shopping|wish\s*list|tarefa|presente)\b/i.test(combined)) {
      return [
        { label: "✓ Yes, add to Need-To-Do", prompt: "Yes, please add that item to our Need-To-Do errands list." },
        { label: "✏️ Customize errand...", prefill: "Add to Need-To-Do: " },
        { label: "✕ No extra errand needed", prompt: "No thanks, no extra errand needed right now." },
      ];
    }

    // 4. Weekend / multi-slot plan selection
    if (/\b(weekend|saturday|sunday|proposed\s+plan|which\s+(?:slot|option|window)|fim\s+de\s+semana|s[áa]bado|domingo)\b/i.test(combined)) {
      return [
        { label: "✓ Looks great, book these slots", prompt: "That plan looks great! Please schedule those proposed slots." },
        { label: "🕒 Try different times", prompt: "Can you suggest alternative time windows for this plan?" },
        { label: "✏️ Adjust one detail...", prefill: "Let's adjust the plan: " },
      ];
    }

    // 5. Generic question fallback
    if (extractedQuestion || /\?\s*$/.test(cleanText)) {
      return [
        { label: "✓ Yes, go ahead", prompt: "Yes, please go ahead." },
        { label: "✏️ Let me clarify...", prefill: "" },
        { label: "✕ No thanks", prompt: "No thanks." },
      ];
    }

    return [];
  }

  function createWarningCalloutEl(warningLineText) {
    const box = document.createElement("div");
    box.className = "chat-warning-callout";

    const icon = document.createElement("div");
    icon.className = "chat-warning-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "⚠️";
    box.appendChild(icon);

    const body = document.createElement("div");
    body.className = "chat-warning-body";

    const lbl = document.createElement("div");
    lbl.className = "chat-warning-label";
    lbl.textContent = "Schedule / Household Warning";
    body.appendChild(lbl);

    const txt = document.createElement("div");
    txt.innerHTML = formatInlineRichHtml(stripWarningPrefix(warningLineText));
    body.appendChild(txt);

    box.appendChild(body);
    return box;
  }

  function createActionCalloutEl(questionText) {
    const box = document.createElement("div");
    box.className = "chat-action-callout";

    const icon = document.createElement("div");
    icon.className = "chat-action-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "?";
    box.appendChild(icon);

    const body = document.createElement("div");
    body.className = "chat-action-body";

    const lbl = document.createElement("div");
    lbl.className = "chat-action-label";
    lbl.textContent = "Action / Confirmation Requested";
    body.appendChild(lbl);

    const q = document.createElement("div");
    q.className = "chat-action-question";
    q.innerHTML = formatInlineRichHtml(stripActionPrefix(questionText));
    body.appendChild(q);

    box.appendChild(body);
    return box;
  }

  function renderRichAssistantContent(rawContent) {
    const container = document.createElement("div");
    container.className = "assistant-rich-body";

    const { cleanedText, explicitChips } = extractQuickRepliesAndCleanText(rawContent);
    if (!cleanedText) {
      return { container, explicitChips, detectedQuestion: "" };
    }

    const rawLines = cleanedText.split(/\r?\n/);
    // Fallback detection: if no line uses explicit `👉 Action Needed:`, check if the final non-empty line ends with `?`
    let hasExplicitAction = rawLines.some((l) => isExplicitActionLine(l));
    let autoPromotedLineIdx = -1;
    let autoPromotedQuestionSuffix = "";

    if (!hasExplicitAction) {
      for (let i = rawLines.length - 1; i >= 0; i -= 1) {
        const candidate = rawLines[i].trim();
        if (!candidate) continue;
        if (candidate.endsWith("?") && !isWarningLine(candidate) && !/^[-*•\d]/.test(candidate)) {
          // If the line has multiple sentences, split out the final question sentence if needed
          const sentenceMatch = candidate.match(/^([\s\S]+?[.!])\s+([^.!?]+\?)$/);
          if (sentenceMatch && sentenceMatch[2].length >= 12) {
            rawLines[i] = sentenceMatch[1].trim();
            autoPromotedQuestionSuffix = sentenceMatch[2].trim();
          } else {
            autoPromotedLineIdx = i;
          }
        }
        break;
      }
    }

    let currentListEl = null;
    let currentListType = null;
    let detectedQuestion = "";

    const flushList = () => {
      if (currentListEl) {
        container.appendChild(currentListEl);
        currentListEl = null;
        currentListType = null;
      }
    };

    rawLines.forEach((rawLine, idx) => {
      const trimmed = rawLine.trim();
      if (!trimmed) {
        flushList();
        return;
      }

      if (idx === autoPromotedLineIdx || isExplicitActionLine(trimmed)) {
        flushList();
        detectedQuestion = stripActionPrefix(trimmed);
        container.appendChild(createActionCalloutEl(trimmed));
        return;
      }

      if (isWarningLine(trimmed)) {
        flushList();
        container.appendChild(createWarningCalloutEl(trimmed));
        return;
      }

      const headingMatch = trimmed.match(/^#{1,3}\s+(.+)$/);
      if (headingMatch) {
        flushList();
        const h = document.createElement("div");
        h.className = "chat-section-heading";
        h.innerHTML = formatInlineRichHtml(headingMatch[1]);
        container.appendChild(h);
        return;
      }

      const ulMatch = trimmed.match(/^[-*•]\s+(.+)$/);
      if (ulMatch) {
        if (currentListType !== "ul") {
          flushList();
          currentListEl = document.createElement("ul");
          currentListEl.className = "chat-rich-list";
          currentListType = "ul";
        }
        const li = document.createElement("li");
        li.innerHTML = formatInlineRichHtml(ulMatch[1]);
        currentListEl.appendChild(li);
        return;
      }

      const olMatch = trimmed.match(/^\d+\.\s+(.+)$/);
      if (olMatch) {
        if (currentListType !== "ol") {
          flushList();
          currentListEl = document.createElement("ol");
          currentListEl.className = "chat-rich-list";
          currentListType = "ol";
        }
        const li = document.createElement("li");
        li.innerHTML = formatInlineRichHtml(olMatch[1]);
        currentListEl.appendChild(li);
        return;
      }

      flushList();
      const p = document.createElement("p");
      p.className = "chat-paragraph";
      p.innerHTML = formatInlineRichHtml(trimmed);
      container.appendChild(p);
    });

    flushList();

    if (autoPromotedQuestionSuffix) {
      detectedQuestion = stripActionPrefix(autoPromotedQuestionSuffix);
      container.appendChild(createActionCalloutEl(autoPromotedQuestionSuffix));
    }

    return { container, explicitChips, detectedQuestion };
  }

  function buildMessageBubbleElement(msg, sessionIdForActions, isLatestAssistantMsg = false) {
    const bubble = document.createElement("div");
    bubble.className = `msg-bubble ${msg.role === "user" ? "user" : "assistant"}`;

    if (msg.role === "user") {
      const atts = msg.attachments || [];
      if (atts.length) {
        const mediaWrap = document.createElement("div");
        mediaWrap.className = "bubble-media-list";
        atts.forEach((att) => {
          if (att.preview_url && att.mime_type && att.mime_type.startsWith("image/")) {
            const img = document.createElement("img");
            img.className = "bubble-media-img";
            img.src = att.preview_url;
            img.alt = att.filename || "Uploaded invitation";
            mediaWrap.appendChild(img);
          } else if (att.preview_url && att.mime_type && att.mime_type.startsWith("audio/")) {
            const audio = document.createElement("audio");
            audio.className = "bubble-media-audio";
            audio.controls = true;
            audio.src = att.preview_url;
            mediaWrap.appendChild(audio);
          } else {
            const icon = att.mime_type && att.mime_type.startsWith("audio/") ? "🎤" : "📎";
            const docTag = document.createElement("div");
            docTag.textContent = `${icon} ${att.filename || "Attachment"}`;
            mediaWrap.appendChild(docTag);
          }
        });
        bubble.appendChild(mediaWrap);
      }
      const captionDiv = document.createElement("div");
      const prefix = msg.speaker_name
        ? `${msg.speaker_avatar ? msg.speaker_avatar + " " : ""}${msg.speaker_name}: `
        : "";
      captionDiv.textContent = `${prefix}${msg.content || ""}`;
      bubble.appendChild(captionDiv);
      return bubble;
    }

    // Assistant bubble
    const models = msg.models_used || [];
    const tools = msg.tool_calls || [];
    if (models.length || tools.length) {
      const traceRow = document.createElement("div");
      traceRow.className = "tool-trace-row";
      models.forEach((m) => {
        const mb = document.createElement("span");
        mb.className = "tool-badge";
        mb.textContent = `🧠 ${m}`;
        traceRow.appendChild(mb);
      });
      tools.forEach((tc) => {
        const b = document.createElement("span");
        b.className = "tool-badge";
        b.textContent = `⚡ ${tc}`;
        traceRow.appendChild(b);
      });
      bubble.appendChild(traceRow);
    }

    const { container: richBody, explicitChips, detectedQuestion } = renderRichAssistantContent(
      msg.content || ""
    );
    bubble.appendChild(richBody);

    const pendingConfs = msg.pending_confirmations || [];
    const hasUnresolvedHitl = pendingConfs.some((c) => !c.resolved);

    pendingConfs.forEach((conf) => {
      const hasWarn = Boolean(conf.warnings && conf.warnings.length);
      const card = document.createElement("div");
      card.className = `hitl-confirm-card${hasWarn ? " has-warning" : ""}`;

      const hdr = document.createElement("div");
      hdr.className = "hitl-confirm-header";
      hdr.textContent = hasWarn
        ? "⚠️ Schedule / Arrival-Buffer Overlap Warning"
        : "🛡️ Human-in-the-Loop Confirmation Required";
      card.appendChild(hdr);

      const sum = document.createElement("div");
      sum.className = "hitl-confirm-summary";
      sum.textContent = conf.summary || `Confirm ${conf.tool_name}`;
      card.appendChild(sum);

      if (hasWarn) {
        const warnBox = document.createElement("div");
        warnBox.className = "hitl-warning-box";

        const warnList = document.createElement("ul");
        warnList.className = "hitl-warning-list";
        conf.warnings.forEach((w) => {
          const li = document.createElement("li");
          li.textContent = w;
          warnList.appendChild(li);
        });
        warnBox.appendChild(warnList);

        const warnNote = document.createElement("div");
        warnNote.className = "hitl-warning-note";
        warnNote.innerHTML =
          "<strong>Do you want to add this entry to the Family Calendar anyway?</strong> Parallel activities are allowed when confirmed.";
        warnBox.appendChild(warnNote);

        card.appendChild(warnBox);
      }

      const actions = document.createElement("div");
      actions.className = "hitl-confirm-actions";

      if (conf.resolved) {
        const resolvedSpan = document.createElement("span");
        resolvedSpan.className = "hitl-status-pill";
        resolvedSpan.textContent = conf.resolution_label || "Decision recorded";
        actions.appendChild(resolvedSpan);
      } else {
        const approveBtn = document.createElement("button");
        approveBtn.type = "button";
        approveBtn.className = "btn btn-primary";
        approveBtn.textContent = hasWarn ? "✓ Yes, Add Event Anyway" : "✓ Approve & Run";

        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn btn-secondary";
        cancelBtn.textContent = "✕ Cancel";

        const handleDecision = async (approved, followUpPrompt = "") => {
          approveBtn.disabled = true;
          cancelBtn.disabled = true;
          actions.innerHTML = `<span class="hitl-status-pill">${approved ? "Approving & saving..." : "Cancelling action..."}</span>`;
          await submitToolConfirmation(sessionIdForActions, conf.confirmation_call_id, approved);
          if (followUpPrompt) {
            await sendChatMessage(followUpPrompt);
          }
        };

        approveBtn.addEventListener("click", () => handleDecision(true));
        cancelBtn.addEventListener("click", () => handleDecision(false));

        actions.appendChild(approveBtn);
        actions.appendChild(cancelBtn);

        if (hasWarn) {
          const altSlotBtn = document.createElement("button");
          altSlotBtn.type = "button";
          altSlotBtn.className = "smart-reply-chip";
          altSlotBtn.textContent = "🕒 Suggest a free slot instead";
          altSlotBtn.addEventListener("click", () => {
            altSlotBtn.disabled = true;
            handleDecision(false, "Please suggest a conflict-free time slot on that day instead.");
          });
          actions.appendChild(altSlotBtn);
        }
      }

      card.appendChild(actions);
      bubble.appendChild(card);
    });

    // Render Smart Answer Chips when the assistant asks a question and no unresolved HITL card already handles it
    if (isLatestAssistantMsg && !hasUnresolvedHitl) {
      const chipItems = explicitChips.length
        ? explicitChips.map((label) => ({ label, prompt: label }))
        : detectedQuestion
        ? inferSmartReplyChips(msg.content || "", detectedQuestion)
        : [];

      if (chipItems.length) {
        const chipsWrap = document.createElement("div");
        chipsWrap.className = "smart-chips-wrap";

        const chipsLabel = document.createElement("div");
        chipsLabel.className = "smart-chips-label";
        chipsLabel.textContent = "Quick answers (click to reply):";
        chipsWrap.appendChild(chipsLabel);

        const chipsRow = document.createElement("div");
        chipsRow.className = "smart-chips-row";
        const busy = inflightBySession.has(sessionIdForActions);

        chipItems.forEach((item) => {
          const chipBtn = document.createElement("button");
          chipBtn.type = "button";
          chipBtn.className = "smart-reply-chip";
          chipBtn.disabled = busy;
          chipBtn.textContent = item.label;
          chipBtn.addEventListener("click", () => {
            if (typeof item.prefill === "string") {
              const inputEl = document.getElementById("chat-input");
              if (inputEl) {
                inputEl.value = item.prefill;
                inputEl.focus();
              }
              return;
            }
            sendChatMessage(item.prompt);
          });
          chipsRow.appendChild(chipBtn);
        });

        chipsWrap.appendChild(chipsRow);
        bubble.appendChild(chipsWrap);
      }
    }

    return bubble;
  }

  function renderActiveSessionMessages() {
    const messagesEl = document.getElementById("chat-messages");
    if (!messagesEl) return;
    messagesEl.innerHTML = "";

    const sid = appState.sessionId;
    const history = appState.messagesBySession[sid] || [];
    const inflight = inflightBySession.get(sid);

    if (!history.length && !inflight) {
      const welcome = buildMessageBubbleElement(
        {
          role: "assistant",
          content: DEFAULT_WELCOME_TEXT,
          models_used: [],
          tool_calls: [],
          pending_confirmations: [],
        },
        sid,
        false
      );
      messagesEl.appendChild(welcome);
      return;
    }

    let lastAssistantIdx = -1;
    for (let i = history.length - 1; i >= 0; i -= 1) {
      if (history[i].role !== "user") {
        lastAssistantIdx = i;
        break;
      }
    }

    history.forEach((msg, idx) => {
      const isLatestAssistant = idx === lastAssistantIdx && !inflight;
      messagesEl.appendChild(buildMessageBubbleElement(msg, sid, isLatestAssistant));
    });

    if (inflight) {
      const pendingBubble = document.createElement("div");
      pendingBubble.className = "msg-bubble assistant";
      pendingBubble.textContent = inflight.statusText || "Checking household schedule & lists...";
      messagesEl.appendChild(pendingBubble);
    }

    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  async function submitToolConfirmation(targetSessionId, confirmationCallId, confirmed) {
    const active = getActiveMember();
    const controller = new AbortController();
    inflightBySession.set(targetSessionId, {
      controller,
      statusText: confirmed ? "Applying approved calendar update..." : "Recording cancellation...",
    });

    const localMsgs = appState.messagesBySession[targetSessionId] || [];
    localMsgs.forEach((m) => {
      (m.pending_confirmations || []).forEach((c) => {
        if (c.confirmation_call_id === confirmationCallId) {
          c.resolved = true;
          c.resolution_label = `${confirmed ? "✓ Approved" : "✕ Cancelled"} by ${active.name}`;
        }
      });
    });

    if (appState.sessionId === targetSessionId) {
      renderActiveSessionMessages();
    }
    renderChatSessionsList();
    syncComposerControls();

    try {
      const data = await apiFetch("/api/chat/confirm", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          confirmation_call_id: confirmationCallId,
          confirmed,
          member_id: active.member_id,
          member_name: active.name,
          session_id: targetSessionId,
        }),
      });
      appState.messagesBySession[targetSessionId] = data.session_messages || [];
      appState.household = data.household;
      appState.members = data.members;
      appState.events = data.events;
      appState.items = data.items;
      appState.chatSessions = data.chat_sessions || appState.chatSessions;
    } catch (err) {
      if (err.name !== "AbortError") {
        (appState.messagesBySession[targetSessionId] = appState.messagesBySession[targetSessionId] || []).push({
          role: "assistant",
          content: `Error submitting confirmation: ${err.message}`,
          models_used: [],
          tool_calls: [],
          pending_confirmations: [],
        });
      }
    } finally {
      inflightBySession.delete(targetSessionId);
      if (appState.sessionId === targetSessionId) {
        renderActiveSessionMessages();
      }
      renderAll();
    }
  }

  async function sendChatMessage(text, stagedAttachments = []) {
    clearBanner("chat-feedback");
    const targetSessionId = appState.sessionId;
    if (inflightBySession.has(targetSessionId)) {
      showBanner(
        "chat-feedback",
        "This chat is still thinking. Click '+ New Chat' on the left to ask something in parallel, or click '⏹ Stop'.",
        "error"
      );
      return;
    }

    const active = getActiveMember();
    const displayCaption = text
      || (stagedAttachments.some((a) => a.mime_type.startsWith("audio/"))
        ? "Sent a voice message"
        : "Attached an invitation/image");

    const optimisticUserMsg = {
      role: "user",
      speaker_name: active.name,
      speaker_avatar: active.avatar || "",
      content: displayCaption,
      attachments: stagedAttachments.map((a) => ({
        filename: a.filename,
        mime_type: a.mime_type,
        preview_url: a.preview_url,
      })),
    };
    if (!appState.messagesBySession[targetSessionId]) {
      appState.messagesBySession[targetSessionId] = [];
    }
    appState.messagesBySession[targetSessionId].push(optimisticUserMsg);

    if (!appState.chatSessions.some((s) => s.session_id === targetSessionId)) {
      appState.chatSessions.unshift({
        session_id: targetSessionId,
        title: displayCaption.slice(0, 44),
        status: "running",
        message_count: appState.messagesBySession[targetSessionId].length,
      });
    }

    const controller = new AbortController();
    inflightBySession.set(targetSessionId, {
      controller,
      statusText: stagedAttachments.length
        ? "Reading attachment & checking household schedule..."
        : "Checking household schedule & lists...",
    });

    renderActiveSessionMessages();
    renderChatSessionsList();
    syncComposerControls();

    try {
      const data = await apiFetch("/api/chat", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          message: text,
          attachments: stagedAttachments.map((a) => ({
            mime_type: a.mime_type,
            data_base64: a.data_base64,
            filename: a.filename,
          })),
          member_id: active.member_id,
          member_name: active.name,
          member_avatar: active.avatar || "🙂",
          session_id: targetSessionId,
        }),
      });

      const localWithPreviews = appState.messagesBySession[targetSessionId] || [];
      const serverMsgs = data.session_messages || [];
      // Preserve local dataURL previews for attachments uploaded during this browser session
      if (stagedAttachments.length && serverMsgs.length >= 2) {
        const userIdx = serverMsgs.length - 2;
        if (serverMsgs[userIdx] && serverMsgs[userIdx].role === "user") {
          serverMsgs[userIdx].attachments = optimisticUserMsg.attachments;
        }
      } else {
        void localWithPreviews;
      }

      appState.messagesBySession[targetSessionId] = serverMsgs;
      appState.household = data.household;
      appState.members = data.members;
      appState.events = data.events;
      appState.items = data.items;
      appState.chatSessions = data.chat_sessions || appState.chatSessions;
    } catch (err) {
      const msgList = appState.messagesBySession[targetSessionId] || [];
      if (err.name === "AbortError") {
        msgList.push({
          role: "assistant",
          content: "(Request stopped. You can send a new message anytime.)",
          models_used: [],
          tool_calls: [],
          pending_confirmations: [],
        });
      } else {
        msgList.push({
          role: "assistant",
          content: `Error talking to Concierge: ${err.message}`,
          models_used: [],
          tool_calls: [],
          pending_confirmations: [],
        });
      }
      appState.messagesBySession[targetSessionId] = msgList;
    } finally {
      inflightBySession.delete(targetSessionId);
      if (appState.sessionId === targetSessionId) {
        renderActiveSessionMessages();
      }
      renderAll();
    }
  }

  document.getElementById("chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (mediaRecorder && mediaRecorder.state === "recording") {
      showBanner("chat-feedback", "Click '⏹ Stop' first to finish your voice recording, then press Send.", "error");
      return;
    }
    const input = document.getElementById("chat-input");
    const text = input.value.trim();
    const attachmentsToSend = [...appState.pendingAttachments];
    if (!text && !attachmentsToSend.length) {
      showBanner("chat-feedback", "Type a message, record a voice note (🎤), or attach an image (📷).", "error");
      return;
    }
    input.value = "";
    appState.pendingAttachments = [];
    renderPendingAttachments();
    await sendChatMessage(text, attachmentsToSend);
  });

  document.querySelectorAll(".starter-chip").forEach((chip) => {
    chip.addEventListener("click", () => sendChatMessage(chip.dataset.prompt, []));
  });

  const newChatBtn = document.getElementById("btn-new-chat");
  if (newChatBtn) {
    newChatBtn.addEventListener("click", () => {
      startNewChatSession();
    });
  }

  const stopReqBtn = document.getElementById("btn-stop-request");
  if (stopReqBtn) {
    stopReqBtn.addEventListener("click", () => {
      const inflight = inflightBySession.get(appState.sessionId);
      if (inflight) {
        inflight.controller.abort();
      }
    });
  }

  // Passcode Unlock Modal
  document.getElementById("passcode-unlock-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const candidate = document.getElementById("unlock-passcode-input").value.trim();
    const resp = await fetch("/api/auth/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passcode: candidate }),
    });
    if (resp.ok) {
      localStorage.setItem(PASSCODE_KEY, candidate);
      document.getElementById("passcode-modal").classList.remove("visible");
      document.getElementById("passcode-error").classList.remove("visible");
      await loadFullState(true);
    } else {
      document.getElementById("passcode-error").classList.add("visible");
    }
  });

  // Initial Bootstrap
  (async () => {
    try {
      await loadFullState(true);
    } catch (err) {
      // Handled by 401 modal if passcode is set
    }
  })();
})();
