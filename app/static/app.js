(() => {
  const PASSCODE_KEY = "family_ops_passcode";
  const MEMBER_TOKEN_KEY = "family_ops_member_token";
  const SESSION_KEY_PREFIX = "family_ops_chat_session_id";
  const DEVICE_LOCATION_KEY = "family_ops_device_location";

  const initialUrlParams = new URLSearchParams(window.location.search);
  const initialUrlMemberToken = (initialUrlParams.get("m") || "").trim();
  if (initialUrlMemberToken) {
    localStorage.setItem(MEMBER_TOKEN_KEY, initialUrlMemberToken);
  } else if (initialUrlParams.get("admin") === "1") {
    localStorage.removeItem(MEMBER_TOKEN_KEY);
  }

  let appState = {
    accessMode: "admin", // "admin" | "member"
    lockedMember: null,
    household: {},
    members: [],
    events: [],
    items: [],
    gcal: {},
    gcalCalendars: [],
    chatSessions: [],
    messagesBySession: {}, // { [sessionId]: Array<message> }
    selectedEventParticipants: [],
    calendarFilter: "ALL",
    calendarHorizon: "14D",
    pendingAttachments: [], // [{ id, filename, mime_type, data_base64, preview_url }]
    sessionId: `fam-session-${Math.random().toString(36).slice(2, 8)}`,
  };

  function getSessionStorageKey() {
    const mid = (appState.lockedMember && appState.lockedMember.member_id) || "default";
    return `${SESSION_KEY_PREFIX}_${mid}`;
  }

  function persistActiveSessionId(sid) {
    localStorage.setItem(getSessionStorageKey(), sid);
  }

  // Per-session in-flight tracker: Map<sessionId, { controller: AbortController, statusText: string }>
  const inflightBySession = new Map();

  let mediaRecorder = null;
  let recordingChunks = [];
  let recordingStream = null;
  let recordingTimerId = null;
  let recordingStartedAt = 0;
  let recordingStopPromise = null;
  let suppressPendingRenderOnStop = false;
  let isSubmittingChatForm = false;

  function getStoredPasscode() {
    return localStorage.getItem(PASSCODE_KEY) || "";
  }

  function getCachedDeviceLocation() {
    try {
      const raw = localStorage.getItem(DEVICE_LOCATION_KEY);
      if (!raw) return "";
      const parsed = JSON.parse(raw);
      if (!parsed || !parsed.coordsText || !parsed.ts) return "";
      if (Date.now() - Number(parsed.ts) > 6 * 3600 * 1000) return "";
      return String(parsed.coordsText);
    } catch (_err) {
      return "";
    }
  }

  function messageNeedsNearMeLocation(text) {
    return /\b(perto\s+de\s+mim|perto\s+daqui|aqui\s+perto|por\s+aqui|pr[óo]ximo\s+(?:a|de)\s+mim|na\s+minha\s+regi[ãa]o|na\s+minha\s+cidade|onde\s+estou|near\s+me|nearby)\b/i.test(
      String(text || "")
    );
  }

  function requestDeviceGeolocationOnce() {
    if (!navigator.geolocation || typeof navigator.geolocation.getCurrentPosition !== "function") {
      return Promise.resolve("");
    }
    return new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          const lat = Number(pos.coords.latitude).toFixed(4);
          const lng = Number(pos.coords.longitude).toFixed(4);
          const coordsText = `lat=${lat}, lng=${lng}`;
          try {
            localStorage.setItem(
              DEVICE_LOCATION_KEY,
              JSON.stringify({ coordsText, ts: Date.now() })
            );
          } catch (_err) {
            // Ignore storage quota errors
          }
          resolve(coordsText);
        },
        () => resolve(""),
        { enableHighAccuracy: false, timeout: 8000, maximumAge: 1800000 }
      );
    });
  }

  function getActiveMemberToken() {
    const liveUrlTok = (new URLSearchParams(window.location.search).get("m") || "").trim();
    if (liveUrlTok) return liveUrlTok;
    if (getStoredPasscode()) return "";
    return localStorage.getItem(MEMBER_TOKEN_KEY) || "";
  }

  async function apiFetch(path, options = {}) {
    const headers = Object.assign(
      {
        "Content-Type": "application/json",
        "X-Household-Passcode": getStoredPasscode(),
        "X-Member-Token": getActiveMemberToken(),
      },
      options.headers || {}
    );
    const resp = await fetch(path, Object.assign({}, options, { headers, cache: "no-store" }));
    if (resp.status === 401) {
      if (!new URLSearchParams(window.location.search).has("m")) {
        localStorage.removeItem(MEMBER_TOKEN_KEY);
      }
      document.getElementById("passcode-modal").classList.add("visible");
      throw new Error("UNAUTHORIZED");
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = data.detail || data;
      const err = new Error(typeof detail === "string" ? detail : detail.message || "Falha na requisição");
      err.detail = detail;
      throw err;
    }
    return data;
  }

  function getActiveMember() {
    if (appState.lockedMember && appState.lockedMember.name) {
      return appState.lockedMember;
    }
    return appState.members[0] || { member_id: "default", name: "Família", avatar: "" };
  }

  function switchTab(targetId) {
    const resolvedTarget =
      targetId === "view-setup" && appState.accessMode !== "admin"
        ? "view-concierge"
        : targetId;
    document.querySelectorAll(".nav-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.target === resolvedTarget);
    });
    document.querySelectorAll(".view-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === resolvedTarget);
    });
    const gearBtn = document.getElementById("btn-open-setup");
    if (gearBtn) {
      gearBtn.classList.toggle("active", resolvedTarget === "view-setup");
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

  const RECURRENCE_LABELS = {
    WEEKLY: "Semanal",
    BIWEEKLY: "Quinzenal",
    DAILY: "Diário",
    MONTHLY: "Mensal",
  };

  function formatTime(isoStr) {
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return isoStr;
    return d.toLocaleTimeString(appState.household.preferred_language || "pt-BR", {
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function formatDayHeading(datePart) {
    const d = new Date(`${datePart}T12:00:00`);
    if (Number.isNaN(d.getTime())) return datePart;
    return d.toLocaleDateString(appState.household.preferred_language || "pt-BR", {
      weekday: "long",
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  }

  function getHouseholdTimezone() {
    return (appState.household && appState.household.timezone) || "America/Sao_Paulo";
  }

  function getHouseholdTodayYmd() {
    try {
      return new Intl.DateTimeFormat("en-CA", {
        timeZone: getHouseholdTimezone(),
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(new Date());
    } catch (_err) {
      const now = new Date();
      const y = now.getFullYear();
      const m = String(now.getMonth() + 1).padStart(2, "0");
      const d = String(now.getDate()).padStart(2, "0");
      return `${y}-${m}-${d}`;
    }
  }

  function addDaysToYmd(ymd, days) {
    const base = new Date(`${ymd}T12:00:00Z`);
    if (Number.isNaN(base.getTime())) return ymd;
    base.setUTCDate(base.getUTCDate() + days);
    return base.toISOString().slice(0, 10);
  }

  function isEventAllDay(ev) {
    if (ev.is_all_day) return true;
    const s = String(ev.start_iso || "");
    const e = String(ev.end_iso || "");
    if (s.endsWith("T00:00:00") && (e.endsWith("T23:59:00") || e.endsWith("T23:59:59"))) {
      return true;
    }
    return false;
  }

  function setDrawerOpen(drawerId, btnId, open) {
    const drawer = document.getElementById(drawerId);
    const btn = document.getElementById(btnId);
    if (!drawer || !btn) return;
    drawer.classList.toggle("open", open);
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function prefillDefaultEventDatetimes() {
    const startEl = document.getElementById("evt-start");
    const endEl = document.getElementById("evt-end");
    if (!startEl || !endEl || startEl.value) return;
    const todayYmd = getHouseholdTodayYmd();
    const nextHour = Math.min(22, new Date().getHours() + 1);
    const endHour = Math.min(23, nextHour + 1);
    startEl.value = `${todayYmd}T${String(nextHour).padStart(2, "0")}:00`;
    endEl.value = `${todayYmd}T${String(endHour).padStart(2, "0")}:00`;
  }

  function renderHeaderAndSwitchers() {
    const hh = appState.household || {};
    const isConfigured = Boolean(hh.is_configured);
    const isAdmin = appState.accessMode === "admin";
    let todayFormatted = "";
    try {
      todayFormatted = new Date().toLocaleDateString(hh.preferred_language || "pt-BR", {
        timeZone: getHouseholdTimezone(),
        weekday: "short",
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
      });
    } catch (_err) {
      todayFormatted = new Date().toLocaleDateString(hh.preferred_language || "pt-BR");
    }

    document.getElementById("household-title").textContent = hh.household_name || "Family Ops Concierge";
    const gcalSuffix =
      appState.gcal && appState.gcal.active_sync
        ? ` | 📅 Google Agenda: ${appState.gcal.calendar_summary || appState.gcal.calendar_id || "Conectada"}`
        : "";
    document.getElementById("household-rules-summary").textContent = isConfigured
      ? `Hoje: ${todayFormatted}${gcalSuffix}`
      : `Hoje: ${todayFormatted} | Configuração inicial da família`;

    const navEl = document.getElementById("primary-nav");
    const gearBtn = document.getElementById("btn-open-setup");
    const closeSetupBtn = document.getElementById("btn-close-setup");
    const welcomeBanner = document.getElementById("setup-welcome-banner");
    if (navEl) navEl.style.display = isConfigured ? "grid" : "none";
    if (gearBtn) gearBtn.style.display = isAdmin ? "inline-grid" : "none";
    if (closeSetupBtn) closeSetupBtn.style.display = isConfigured && isAdmin ? "inline-flex" : "none";
    if (welcomeBanner) welcomeBanner.classList.toggle("visible", !isConfigured && isAdmin);

    const active = getActiveMember();
    const lockedPillEl = document.getElementById("locked-user-pill");
    if (lockedPillEl) {
      lockedPillEl.textContent = isConfigured && appState.members.length ? active.name : "";
    }

    if (!appState.selectedEventParticipants.length && active.name) {
      appState.selectedEventParticipants = [active.name];
    }
    renderParticipantSelectorChips();

    document.querySelectorAll(".item-assigned-select").forEach((assignSelect) => {
      const prevVal = assignSelect.value;
      assignSelect.innerHTML = `<option value="Anyone">Qualquer pessoa da família</option>`;
      appState.members.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m.name;
        opt.textContent = m.name;
        assignSelect.appendChild(opt);
      });
      if (prevVal && Array.from(assignSelect.options).some((o) => o.value === prevVal)) {
        assignSelect.value = prevVal;
      }
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
    allBtn.textContent = "[All] Toda a Família";
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
      btn.textContent = `[${m.name}]`;
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
    const todayYmd = getHouseholdTodayYmd();
    const cutoff14dYmd = addDaysToYmd(todayYmd, 14);

    // Strictly exclude dates from the past (events whose end date is before today in household timezone)
    const upcomingEvents = appState.events.filter((ev) => {
      const endDay = String(ev.end_iso || ev.start_iso || "").slice(0, 10);
      return endDay >= todayYmd;
    });

    const horizonEl = document.getElementById("calendar-horizon-filters");
    if (horizonEl) {
      horizonEl.innerHTML = "";
      const btn14 = document.createElement("button");
      btn14.type = "button";
      btn14.className = `filter-chip ${appState.calendarHorizon === "14D" ? "active" : ""}`;
      btn14.textContent = "Próximos 14 dias";
      btn14.addEventListener("click", () => {
        appState.calendarHorizon = "14D";
        renderCalendar();
      });

      const btnAll = document.createElement("button");
      btnAll.type = "button";
      btnAll.className = `filter-chip ${appState.calendarHorizon === "ALL" ? "active" : ""}`;
      btnAll.textContent = `Todos os futuros (${upcomingEvents.length})`;
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
    allChip.textContent = "Toda a Família";
    allChip.addEventListener("click", () => {
      appState.calendarFilter = "ALL";
      renderCalendar();
    });
    filterEl.appendChild(allChip);

    appState.members.forEach((m) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = `filter-chip ${appState.calendarFilter === m.name ? "active" : ""}`;
      chip.textContent = m.name;
      chip.addEventListener("click", () => {
        appState.calendarFilter = m.name;
        renderCalendar();
      });
      filterEl.appendChild(chip);
    });

    const container = document.getElementById("calendar-events-container");
    container.innerHTML = "";

    const filtered = upcomingEvents.filter((ev) => {
      const startDay = String(ev.start_iso || "").slice(0, 10);
      if (appState.calendarHorizon === "14D" && startDay > cutoff14dYmd) {
        return false;
      }
      if (appState.calendarFilter === "ALL") return true;
      const p = (ev.participants || "").toLowerCase();
      return (
        p === "all" ||
        p === "whole family" ||
        p === "toda a família" ||
        p === "toda a familia" ||
        p.includes(appState.calendarFilter.toLowerCase())
      );
    });

    if (!filtered.length) {
      container.innerHTML = `<div class="empty-state">Nenhum compromisso futuro nesta visualização. Toque em <strong>+</strong> acima para adicionar ou peça ao Concierge IA!</div>`;
      return;
    }

    let currentDay = "";
    filtered.forEach((ev) => {
      const rawDayPart = ev.start_iso.slice(0, 10);
      const dayPart = rawDayPart < todayYmd ? todayYmd : rawDayPart;
      if (dayPart !== currentDay) {
        currentDay = dayPart;
        const h = document.createElement("div");
        h.className = "day-heading";
        h.textContent = formatDayHeading(dayPart);
        container.appendChild(h);
      }

      const row = document.createElement("div");
      row.className = "event-row";
      const allDay = isEventAllDay(ev);
      const isRecurring = ev.recurrence && ev.recurrence !== "NONE" && ev.series_id;

      row.innerHTML = `
        <div class="event-time-col">
          ${
            allDay
              ? `<span class="all-day-badge">Dia inteiro</span>`
              : `<div>${formatTime(ev.start_iso)} - ${formatTime(ev.end_iso)}</div>`
          }
        </div>
        <div class="event-main-col">
          <div class="event-title">${escapeHtml(ev.title)}</div>
          <div class="event-meta">
            <span>👥 ${escapeHtml(ev.participants === "All" ? "Toda a Família" : ev.participants)}</span>
            ${ev.location ? `<span>📍 ${escapeHtml(ev.location)}</span>` : ""}
            ${isRecurring ? `<span>🔁 ${escapeHtml(RECURRENCE_LABELS[ev.recurrence] || ev.recurrence)}</span>` : ""}
            <span>✍️ Adicionado por ${escapeHtml(ev.created_by || "Família")}</span>
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
      delBtn.textContent = isRecurring ? "Remover 1x" : "Remover";
      delBtn.addEventListener("click", async () => {
        await apiFetch(`/api/events/${encodeURIComponent(ev.event_id)}`, { method: "DELETE" });
        await loadFullState();
      });
      actionWrap.appendChild(delBtn);

      if (isRecurring) {
        const delSeriesBtn = document.createElement("button");
        delSeriesBtn.type = "button";
        delSeriesBtn.className = "btn btn-ghost-danger";
        delSeriesBtn.textContent = "Remover Série";
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
      needEl.innerHTML = `<div class="empty-state">Nenhuma tarefa pendente. Toque em <strong>+</strong> acima para adicionar!</div>`;
    } else {
      needs.forEach((item) => needEl.appendChild(buildItemRow(item)));
    }

    if (!wants.length) {
      wantEl.innerHTML = `<div class="empty-state">Nenhum item na lista de desejos ainda. Toque em <strong>+</strong> acima para adicionar filmes, passeios ou ideias em família!</div>`;
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

    const ownerDisplay = !item.assigned_to || item.assigned_to === "Anyone" ? "Qualquer pessoa" : item.assigned_to;
    const textCol = document.createElement("div");
    textCol.innerHTML = `
      <div class="item-title">${escapeHtml(item.title)}</div>
      <div class="item-sub">
        ${item.deadline_or_closing_date ? `<span class="deadline-pill">Até ${escapeHtml(item.deadline_or_closing_date)}</span>` : ""}
        <span>Responsável: ${escapeHtml(ownerDisplay)}</span> ·
        <span>Adicionado por ${escapeHtml(item.created_by || "Família")}</span>
      </div>
    `;

    wrap.appendChild(cb);
    wrap.appendChild(textCol);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn btn-ghost-danger";
    delBtn.textContent = "✕";
    delBtn.title = "Excluir item";
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

  function syncAdminMemberDropdown(preferredAdminId = "") {
    const sel = document.getElementById("cfg-admin-member");
    if (!sel) return;
    const currentChoice = preferredAdminId || sel.value || (appState.household && appState.household.admin_member_id) || "";
    const rows = Array.from(document.querySelectorAll(".member-editor-row"));
    sel.innerHTML = "";
    rows.forEach((r, idx) => {
      const mid = r.dataset.memberId;
      const mname = r.querySelector(".m-name").value.trim() || `Membro ${idx + 1}`;
      const opt = document.createElement("option");
      opt.value = mid;
      opt.textContent = mname;
      if ((currentChoice && mid === currentChoice) || (!currentChoice && idx === 0)) {
        opt.selected = true;
      }
      sel.appendChild(opt);
    });
  }

  function renderSetupForm() {
    if (appState.accessMode !== "admin") return;
    const hh = appState.household || {};
    const cfgNameEl = document.getElementById("cfg-name");
    if (document.activeElement !== cfgNameEl) {
      cfgNameEl.value = hh.household_name || "";
    }
    document.getElementById("cfg-language").value = hh.preferred_language || "pt-BR";
    document.getElementById("cfg-timezone").value = hh.timezone || "America/Sao_Paulo";
    const passEl = document.getElementById("cfg-passcode");
    passEl.value = "";
    passEl.required = !hh.has_passcode;
    passEl.placeholder = hh.has_passcode
      ? "PIN ativo (deixe em branco para manter)"
      : "Crie um PIN do Administrador (obrigatório)";
    const passLabelEl = document.getElementById("cfg-passcode-label");
    if (passLabelEl) {
      passLabelEl.textContent = hh.has_passcode
        ? "PIN do Administrador (ativo)"
        : "PIN do Administrador (obrigatório)";
    }

    const listEl = document.getElementById("setup-members-list");
    listEl.innerHTML = "";
    if (appState.members.length > 0) {
      appState.members.forEach((m) => appendMemberSetupRow(m));
    } else {
      appendMemberSetupRow({ role: "Parent" });
    }
    syncAdminMemberDropdown(hh.admin_member_id || "");

    renderGcalSetupCard();
  }

  function renderGcalSetupCard() {
    const g = appState.gcal || {};
    const statusPill = document.getElementById("gcal-status-pill");
    const linkInput = document.getElementById("gcal-link-input");
    const pickerGroup = document.getElementById("gcal-picker-group");
    const connectBtn = document.getElementById("btn-gcal-connect");
    const saveBtn = document.getElementById("btn-gcal-save");
    const disconnectBtn = document.getElementById("btn-gcal-disconnect");

    if (!statusPill) return;

    if (g.active_sync) {
      statusPill.className = "gcal-status-pill connected";
      const acct = g.connected_email ? ` (${g.connected_email})` : "";
      statusPill.textContent = `● Conectado: ${g.calendar_summary || "Agenda da Família"}${acct}`;
    } else if (g.is_connected) {
      statusPill.className = "gcal-status-pill connected";
      statusPill.textContent = `● Conectado${g.connected_email ? `: ${g.connected_email}` : ""}`;
    } else {
      statusPill.className = "gcal-status-pill";
      statusPill.textContent = "Não conectado";
    }

    if (linkInput && document.activeElement !== linkInput) {
      linkInput.value = g.calendar_link_input || g.calendar_id || "";
    }

    if (connectBtn) {
      connectBtn.textContent = g.is_connected
        ? "Trocar Conta Google"
        : "Conectar e Autorizar com o Google";
    }
    if (saveBtn) {
      saveBtn.style.display = g.is_connected ? "inline-flex" : "none";
    }
    if (disconnectBtn) {
      disconnectBtn.style.display = g.is_connected ? "inline-flex" : "none";
    }
    if (pickerGroup) {
      pickerGroup.style.display = g.is_connected && appState.gcalCalendars.length > 0 ? "flex" : "none";
    }
  }

  async function refreshGcalCalendarsDropdown() {
    if (!appState.gcal || !appState.gcal.is_connected) return;
    const selectEl = document.getElementById("gcal-calendar-picker");
    const pickerGroup = document.getElementById("gcal-picker-group");
    if (!selectEl) return;
    try {
      const data = await apiFetch("/api/gcal/calendars");
      appState.gcalCalendars = data.calendars || [];
      selectEl.innerHTML = `<option value="">-- Escolha uma agenda da sua conta --</option>`;
      appState.gcalCalendars.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.calendar_id;
        opt.textContent = `${c.summary}${c.primary ? " (Pessoal)" : ""}`;
        if (appState.gcal && c.calendar_id === appState.gcal.calendar_id) {
          opt.selected = true;
        }
        selectEl.appendChild(opt);
      });
      if (pickerGroup) {
        pickerGroup.style.display = appState.gcalCalendars.length > 0 ? "flex" : "none";
      }
    } catch (_err) {
      // Keep UI clean; dropdown stays hidden if listing fails
    }
  }

  async function shareMemberInviteLink(row) {
    clearBanner("setup-feedback");
    const mId = row.dataset.memberId;
    const typedName = row.querySelector(".m-name").value.trim();
    if (!typedName) {
      showBanner("setup-feedback", "Digite o nome do familiar antes de compartilhar o link.", "error");
      row.querySelector(".m-name").focus();
      return;
    }

    let existingMember = appState.members.find((m) => m.member_id === mId);
    const needsSave =
      !appState.household.is_configured ||
      !existingMember ||
      !existingMember.invite_token ||
      existingMember.name !== typedName;

    if (needsSave) {
      const hhName = document.getElementById("cfg-name").value.trim();
      const newPass = document.getElementById("cfg-passcode").value.trim();
      if (hhName.length < 2) {
        showBanner("setup-feedback", "Preencha o Nome da Família acima antes de compartilhar links.", "error");
        document.getElementById("cfg-name").focus();
        return;
      }
      if (!appState.household.has_passcode && !newPass) {
        showBanner("setup-feedback", "Defina um PIN do Administrador acima antes de enviar links aos familiares.", "error");
        document.getElementById("cfg-passcode").focus();
        return;
      }
      const ok = await saveHouseholdSetupFromForm(false);
      if (!ok) return;
      existingMember =
        appState.members.find((m) => m.member_id === mId) ||
        appState.members.find((m) => m.name.toLowerCase() === typedName.toLowerCase());
    }

    if (!existingMember || !existingMember.invite_token) {
      showBanner("setup-feedback", "Salve as configurações primeiro para gerar o link deste membro.", "error");
      return;
    }

    const inviteUrl = `${window.location.origin}/?m=${encodeURIComponent(existingMember.invite_token)}`;
    const famName = appState.household.household_name || "Family Ops Concierge";

    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(inviteUrl);
      } catch (_err) {
        // Ignore clipboard errors when native share succeeds
      }
    }

    if (navigator.share) {
      try {
        await navigator.share({
          title: `${famName} - ${existingMember.name}`,
          text: `Oi ${existingMember.name}! Aqui está seu link pessoal da agenda e listas da nossa família (${famName}):`,
          url: inviteUrl,
        });
        showBanner("setup-feedback", `Link exclusivo de ${existingMember.name} compartilhado!`, "success");
        return;
      } catch (_err) {
        // Fallback to copied link banner below
      }
    }

    showBanner(
      "setup-feedback",
      `Link exclusivo de ${existingMember.name} copiado! Envie no WhatsApp: ${inviteUrl}`,
      "success"
    );
  }

  function appendMemberSetupRow(member = {}) {
    const listEl = document.getElementById("setup-members-list");
    const row = document.createElement("div");
    row.className = "member-editor-row";
    row.dataset.memberId = member.member_id || `mem-${Math.random().toString(36).slice(2, 10)}`;

    row.innerHTML = `
      <input type="text" class="m-name" value="${escapeAttr(member.name || "")}" placeholder="Nome (ex.: Lucas)" required />
      <select class="m-role" aria-label="Relação familiar">
        <option value="Parent" ${member.role === "Parent" ? "selected" : ""}>Pai / Mãe</option>
        <option value="Teen" ${member.role === "Teen" ? "selected" : ""}>Adolescente</option>
        <option value="Child" ${member.role === "Child" ? "selected" : ""}>Criança</option>
        <option value="Relative" ${member.role === "Relative" ? "selected" : ""}>Familiar</option>
      </select>
      <button type="button" class="m-share-btn m-share" title="Compartilhar link exclusivo deste familiar" aria-label="Compartilhar link exclusivo deste familiar">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="18" cy="5" r="3"></circle>
          <circle cx="6" cy="12" r="3"></circle>
          <circle cx="18" cy="19" r="3"></circle>
          <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"></line>
          <line x1="15.41" y1="6.51" x2="8.59" y2="10.49"></line>
        </svg>
      </button>
      <button type="button" class="m-remove-btn m-remove" title="Remover membro" aria-label="Remover membro">✕</button>
    `;

    row.querySelector(".m-name").addEventListener("input", () => syncAdminMemberDropdown());
    row.querySelector(".m-share").addEventListener("click", () => shareMemberInviteLink(row));
    row.querySelector(".m-remove").addEventListener("click", () => {
      if (document.querySelectorAll(".member-editor-row").length > 1) {
        row.remove();
        syncAdminMemberDropdown();
      }
    });
    listEl.appendChild(row);
    syncAdminMemberDropdown();
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function escapeAttr(str) {
    return escapeHtml(str);
  }

  function renderAll() {
    renderHeaderAndSwitchers();
    renderCalendar();
    renderLists();
    if (appState.accessMode === "admin") {
      renderSetupForm();
    }
    renderPendingAttachments();
    renderChatSessionsList();
    syncComposerControls();
  }

  async function loadFullState(initialBootstrap = false) {
    const data = await apiFetch("/api/state?include_completed=true");
    appState.accessMode = data.access_mode || "admin";
    appState.lockedMember = data.locked_member || null;
    appState.household = data.household;
    appState.members = data.members;
    appState.events = data.events;
    appState.items = data.items;
    appState.gcal = data.gcal || appState.gcal || {};
    appState.chatSessions = data.chat_sessions || [];

    const storedSid = localStorage.getItem(getSessionStorageKey());
    if (storedSid && initialBootstrap) {
      appState.sessionId = storedSid;
    }

    if (initialBootstrap) {
      const params = new URLSearchParams(window.location.search);
      const isAdmin = appState.accessMode === "admin";
      if (isAdmin && params.get("gcal_connected") === "1") {
        switchTab("view-setup");
        showBanner(
          "gcal-feedback",
          "Conta Google conectada! Confirme ou escolha a Agenda da Família abaixo.",
          "success"
        );
        window.history.replaceState({}, "", window.location.pathname);
      } else if (isAdmin && params.get("gcal_error")) {
        switchTab("view-setup");
        showBanner("gcal-feedback", `Falha na autenticação Google: ${params.get("gcal_error")}`, "error");
        window.history.replaceState({}, "", window.location.pathname);
      } else if (isAdmin && (!appState.household || !appState.household.is_configured)) {
        switchTab("view-setup");
      } else {
        switchTab("view-concierge");
      }
      if (isAdmin && appState.gcal && appState.gcal.is_connected) {
        refreshGcalCalendarsDropdown();
      }
    }

    if (initialBootstrap) {
      const existingMatch = appState.chatSessions.find((s) => s.session_id === appState.sessionId);
      if (existingMatch) {
        await loadSessionTranscript(appState.sessionId);
      } else if (appState.chatSessions.length > 0) {
        appState.sessionId = appState.chatSessions[0].session_id;
        persistActiveSessionId(appState.sessionId);
        await loadSessionTranscript(appState.sessionId);
      } else {
        persistActiveSessionId(appState.sessionId);
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
        img.alt = att.filename || "Imagem anexada";
        chip.appendChild(img);
        const label = document.createElement("span");
        label.textContent = att.filename || "Imagem / convite";
        chip.appendChild(label);
      } else if (att.mime_type.startsWith("audio/")) {
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.src = att.preview_url;
        chip.appendChild(audio);
      } else {
        const docLabel = document.createElement("span");
        docLabel.textContent = `📄 ${att.filename || "Anexo"}`;
        chip.appendChild(docLabel);
      }

      const rmBtn = document.createElement("button");
      rmBtn.type = "button";
      rmBtn.className = "attachment-remove";
      rmBtn.textContent = "✕";
      rmBtn.title = "Remover anexo";
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
      reader.onerror = () => reject(new Error("Não foi possível ler o arquivo."));
      reader.readAsDataURL(blob);
    });
  }

  async function stageFiles(fileList) {
    clearBanner("chat-feedback");
    for (const file of Array.from(fileList || [])) {
      const mime = (file.type || "").toLowerCase();
      if (!mime.startsWith("image/") && !mime.startsWith("audio/") && mime !== "application/pdf") {
        showBanner("chat-feedback", `Arquivo '${file.name}' não suportado. Envie uma imagem, áudio ou PDF.`, "error");
        continue;
      }
      if (file.size > 12 * 1024 * 1024) {
        showBanner("chat-feedback", `O arquivo '${file.name}' excede o limite de 12 MB.`, "error");
        continue;
      }
      const { dataUrl, base64Part } = await fileOrBlobToBase64(file);
      appState.pendingAttachments.push({
        id: `att-${Math.random().toString(36).slice(2, 9)}`,
        filename: file.name || "anexo",
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
    const timerEl = document.getElementById("mic-timer-label");
    if (micBtn) micBtn.classList.remove("btn-recording");
    if (timerEl) timerEl.textContent = "";
  }

  function finalizeActiveRecording(silentStage = false) {
    if (!mediaRecorder && !recordingStopPromise) {
      return Promise.resolve(null);
    }
    const pendingPromise = recordingStopPromise;
    if (silentStage) {
      suppressPendingRenderOnStop = true;
    }
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
      try {
        mediaRecorder.stop();
      } catch (_err) {
        // Ignore if recorder already transitioned to inactive
      }
    }
    return pendingPromise || Promise.resolve(null);
  }

  async function toggleMicRecording() {
    clearBanner("chat-feedback");
    const micBtn = document.getElementById("btn-mic-record");
    const timerEl = document.getElementById("mic-timer-label");

    if (mediaRecorder && mediaRecorder.state === "recording") {
      await finalizeActiveRecording(false);
      return;
    }

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showBanner(
        "chat-feedback",
        "O microfone não está disponível neste navegador. Use https:// ou anexe um áudio pelo ícone de clipe.",
        "error"
      );
      return;
    }

    try {
      recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chosenMime = pickSupportedAudioMime();
      recordingChunks = [];
      suppressPendingRenderOnStop = false;
      const recorder = chosenMime
        ? new MediaRecorder(recordingStream, { mimeType: chosenMime })
        : new MediaRecorder(recordingStream);
      mediaRecorder = recorder;

      recorder.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) {
          recordingChunks.push(ev.data);
        }
      };

      let resolveStop;
      recordingStopPromise = new Promise((resolve) => {
        resolveStop = resolve;
      });

      recorder.onstop = async () => {
        try {
          const actualMime = (recorder.mimeType || "audio/webm").split(";", 1)[0];
          stopMicStream();
          if (!recordingChunks.length) {
            resolveStop(null);
            return;
          }
          const ext = actualMime.includes("mp4") || actualMime.includes("aac")
            ? "m4a"
            : actualMime.includes("ogg")
            ? "ogg"
            : "webm";
          const blob = new Blob(recordingChunks, { type: actualMime });
          const { dataUrl, base64Part } = await fileOrBlobToBase64(blob);
          const stagedVoice = {
            id: `voice-${Math.random().toString(36).slice(2, 9)}`,
            filename: `mensagem-de-voz.${ext}`,
            mime_type: actualMime,
            data_base64: base64Part,
            preview_url: dataUrl,
          };
          appState.pendingAttachments.push(stagedVoice);
          if (!suppressPendingRenderOnStop) {
            renderPendingAttachments();
            document.getElementById("btn-send-chat").focus();
          }
          resolveStop(stagedVoice);
        } catch (_err) {
          resolveStop(null);
        } finally {
          suppressPendingRenderOnStop = false;
          recordingStopPromise = null;
          if (mediaRecorder === recorder) {
            mediaRecorder = null;
          }
        }
      };

      recorder.onerror = () => {
        stopMicStream();
        suppressPendingRenderOnStop = false;
        recordingStopPromise = null;
        if (mediaRecorder === recorder) {
          mediaRecorder = null;
        }
        resolveStop(null);
      };

      recorder.start();
      recordingStartedAt = Date.now();
      micBtn.classList.add("btn-recording");
      if (timerEl) timerEl.textContent = "0:00";
      recordingTimerId = setInterval(() => {
        const sec = Math.floor((Date.now() - recordingStartedAt) / 1000);
        const mm = Math.floor(sec / 60);
        const ss = String(sec % 60).padStart(2, "0");
        if (timerEl) timerEl.textContent = `${mm}:${ss}`;
      }, 500);
    } catch (err) {
      stopMicStream();
      recordingStopPromise = null;
      mediaRecorder = null;
      showBanner(
        "chat-feedback",
        `Não foi possível acessar o microfone (${err.message}). Verifique a permissão do navegador ou anexe um áudio.`,
        "error"
      );
    }
  }

  // Wire Navigation Tabs & Header Gear Icon
  document.querySelectorAll(".nav-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!appState.household || !appState.household.is_configured) {
        switchTab("view-setup");
        return;
      }
      switchTab(btn.dataset.target);
    });
  });

  document.getElementById("btn-open-setup").addEventListener("click", () => {
    const setupPanel = document.getElementById("view-setup");
    if (setupPanel.classList.contains("active") && appState.household && appState.household.is_configured) {
      switchTab("view-concierge");
    } else {
      switchTab("view-setup");
    }
  });

  document.getElementById("btn-close-setup").addEventListener("click", () => {
    if (appState.household && appState.household.is_configured) {
      switchTab("view-concierge");
    }
  });

  // Wire Collapsible '+' Drawers for Calendar Events & Lists
  const toggleEventBtn = document.getElementById("btn-toggle-event-form");
  if (toggleEventBtn) {
    toggleEventBtn.addEventListener("click", () => {
      const drawer = document.getElementById("event-form-drawer");
      const nextOpen = !drawer.classList.contains("open");
      setDrawerOpen("event-form-drawer", "btn-toggle-event-form", nextOpen);
      if (nextOpen) {
        prefillDefaultEventDatetimes();
        document.getElementById("evt-title").focus();
      }
    });
  }
  const cancelEventBtn = document.getElementById("btn-cancel-event-form");
  if (cancelEventBtn) {
    cancelEventBtn.addEventListener("click", () => {
      setDrawerOpen("event-form-drawer", "btn-toggle-event-form", false);
    });
  }

  const toggleNeedBtn = document.getElementById("btn-toggle-need-form");
  if (toggleNeedBtn) {
    toggleNeedBtn.addEventListener("click", () => {
      const drawer = document.getElementById("need-item-drawer");
      const nextOpen = !drawer.classList.contains("open");
      setDrawerOpen("need-item-drawer", "btn-toggle-need-form", nextOpen);
      if (nextOpen) {
        document.getElementById("need-item-title").focus();
      }
    });
  }
  const cancelNeedBtn = document.getElementById("btn-cancel-need-form");
  if (cancelNeedBtn) {
    cancelNeedBtn.addEventListener("click", () => {
      setDrawerOpen("need-item-drawer", "btn-toggle-need-form", false);
    });
  }

  const toggleWantBtn = document.getElementById("btn-toggle-want-form");
  if (toggleWantBtn) {
    toggleWantBtn.addEventListener("click", () => {
      const drawer = document.getElementById("want-item-drawer");
      const nextOpen = !drawer.classList.contains("open");
      setDrawerOpen("want-item-drawer", "btn-toggle-want-form", nextOpen);
      if (nextOpen) {
        document.getElementById("want-item-title").focus();
      }
    });
  }
  const cancelWantBtn = document.getElementById("btn-cancel-want-form");
  if (cancelWantBtn) {
    cancelWantBtn.addEventListener("click", () => {
      setDrawerOpen("want-item-drawer", "btn-toggle-want-form", false);
    });
  }

  function collectSetupFormMembers() {
    const rows = Array.from(document.querySelectorAll(".member-editor-row"));
    return rows
      .map((r) => ({
        member_id: r.dataset.memberId || null,
        name: r.querySelector(".m-name").value.trim(),
        role: r.querySelector(".m-role").value,
      }))
      .filter((m) => m.name.length >= 1);
  }

  // Wire End-User Google Calendar Link & Google Approval Controls
  async function saveGcalLinkSetting(enableSync = true) {
    const linkVal = document.getElementById("gcal-link-input").value.trim();
    const res = await apiFetch("/api/gcal/config", {
      method: "PUT",
      body: JSON.stringify({
        enabled: enableSync,
        calendar_link_or_id: linkVal,
      }),
    });
    appState.gcal = res.gcal || {};
    appState.events = res.events || appState.events;
    renderAll();
    return res;
  }

  const gcalSaveBtn = document.getElementById("btn-gcal-save");
  if (gcalSaveBtn) {
    gcalSaveBtn.addEventListener("click", async () => {
      clearBanner("gcal-feedback");
      try {
        await saveGcalLinkSetting(true);
        showBanner("gcal-feedback", "Google Agenda da família atualizada!", "success");
      } catch (err) {
        showBanner("gcal-feedback", err.message, "error");
      }
    });
  }

  const gcalConnectBtn = document.getElementById("btn-gcal-connect");
  if (gcalConnectBtn) {
    gcalConnectBtn.addEventListener("click", async () => {
      clearBanner("gcal-feedback");
      try {
        // Preserve any typed first-time household setup before leaving the page for Google OAuth
        const typedName = document.getElementById("cfg-name").value.trim();
        const typedMembers = collectSetupFormMembers();
        if ((!appState.household || !appState.household.is_configured) && typedName.length >= 2 && typedMembers.length >= 1) {
          const newPass = document.getElementById("cfg-passcode").value.trim();
          const adminSel = document.getElementById("cfg-admin-member");
          await apiFetch("/api/household/setup", {
            method: "PUT",
            body: JSON.stringify({
              household_name: typedName,
              preferred_language: document.getElementById("cfg-language").value,
              timezone: document.getElementById("cfg-timezone").value.trim(),
              sports_music_buffer_min: Number((appState.household && appState.household.sports_music_buffer_min) ?? 30),
              admin_member_id: adminSel ? adminSel.value : typedMembers[0].member_id,
              new_passcode: newPass || null,
              clear_passcode: false,
              members: typedMembers,
            }),
          });
          localStorage.removeItem(MEMBER_TOKEN_KEY);
          if (newPass) {
            localStorage.setItem(PASSCODE_KEY, newPass);
          }
        }
        await saveGcalLinkSetting(true);
        const startRes = await apiFetch("/api/gcal/oauth/start");
        window.location.href = startRes.auth_url;
      } catch (err) {
        showBanner("gcal-feedback", err.message, "error");
      }
    });
  }

  const gcalPickerEl = document.getElementById("gcal-calendar-picker");
  if (gcalPickerEl) {
    gcalPickerEl.addEventListener("change", async () => {
      if (gcalPickerEl.value) {
        document.getElementById("gcal-link-input").value = gcalPickerEl.value;
        clearBanner("gcal-feedback");
        try {
          await saveGcalLinkSetting(true);
          showBanner("gcal-feedback", "Agenda ativa da família alterada!", "success");
        } catch (err) {
          showBanner("gcal-feedback", err.message, "error");
        }
      }
    });
  }

  const gcalDisconnectBtn = document.getElementById("btn-gcal-disconnect");
  if (gcalDisconnectBtn) {
    gcalDisconnectBtn.addEventListener("click", async () => {
      clearBanner("gcal-feedback");
      try {
        const res = await apiFetch("/api/gcal/disconnect", { method: "POST" });
        appState.gcal = res.gcal || {};
        appState.gcalCalendars = [];
        appState.events = res.events || [];
        renderAll();
        showBanner("gcal-feedback", "Google Agenda desconectada.", "success");
      } catch (err) {
        showBanner("gcal-feedback", err.message, "error");
      }
    });
  }

  // Wire Add Event Form (no category field; auto-collapses drawer on save)
  document.getElementById("add-event-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearBanner("event-feedback");
    if (!appState.selectedEventParticipants.length) {
      showBanner("event-feedback", "Selecione quem vai participar ([All] ou membro(s) da família).", "error");
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
            `Aviso de Conflito de Horário:\n\n${firstErr.detail.message}\n\n` +
            `Deseja agendar este compromisso mesmo assim?`
          );
          if (!accepted) {
            showBanner(
              "event-feedback",
              "O evento não foi adicionado. Ajuste a data/horário se quiser evitar o conflito ou envie novamente para confirmar.",
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
      setDrawerOpen("event-form-drawer", "btn-toggle-event-form", false);
      await loadFullState();
      const countMsg = res.occurrences_created > 1 ? ` (${res.occurrences_created} repetições adicionadas)` : "";
      const overlapMsg = res.overlap_warnings && res.overlap_warnings.length ? " (conflito confirmado)" : "";
      showBanner("event-feedback", `Compromisso agendado na Agenda da Família${countMsg}${overlapMsg}!`, "success");
    } catch (err) {
      const hint = err.detail && err.detail.recovery_hint ? ` Dica: ${err.detail.recovery_hint}` : "";
      showBanner("event-feedback", `${err.message}${hint}`, "error");
    }
  });

  // Wire Separate Collapsible '+' Forms for Need-To-Do and Want-To-Do
  async function handleAddListItem(itemType, titleInputId, deadlineInputId, assignedSelectId, drawerId, btnId, formEl) {
    clearBanner("item-feedback");
    const active = getActiveMember();
    try {
      await apiFetch("/api/items", {
        method: "POST",
        body: JSON.stringify({
          title: document.getElementById(titleInputId).value.trim(),
          item_type: itemType,
          deadline_or_closing_date: document.getElementById(deadlineInputId).value || null,
          assigned_to: document.getElementById(assignedSelectId).value,
          created_by: active.name,
        }),
      });
      formEl.reset();
      setDrawerOpen(drawerId, btnId, false);
      await loadFullState();
      showBanner("item-feedback", "Salvo na lista compartilhada!", "success");
    } catch (err) {
      showBanner("item-feedback", err.message, "error");
    }
  }

  document.getElementById("add-need-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await handleAddListItem(
      "NEED_TO_DO",
      "need-item-title",
      "need-item-deadline",
      "need-item-assigned",
      "need-item-drawer",
      "btn-toggle-need-form",
      e.target
    );
  });

  document.getElementById("add-want-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await handleAddListItem(
      "WANT_TO_DO",
      "want-item-title",
      "want-item-deadline",
      "want-item-assigned",
      "want-item-drawer",
      "btn-toggle-want-form",
      e.target
    );
  });

  async function saveHouseholdSetupFromForm(navigateAfterSave = true) {
    clearBanner("setup-feedback");
    const members = collectSetupFormMembers();
    if (!members.length) {
      showBanner("setup-feedback", "Adicione pelo menos um membro da família.", "error");
      return false;
    }

    const newPass = document.getElementById("cfg-passcode").value.trim();
    const adminSel = document.getElementById("cfg-admin-member");
    const adminMemberId = adminSel ? adminSel.value : members[0].member_id;

    try {
      await apiFetch("/api/household/setup", {
        method: "PUT",
        body: JSON.stringify({
          household_name: document.getElementById("cfg-name").value.trim(),
          preferred_language: document.getElementById("cfg-language").value,
          timezone: document.getElementById("cfg-timezone").value.trim(),
          sports_music_buffer_min: Number((appState.household && appState.household.sports_music_buffer_min) ?? 30),
          admin_member_id: adminMemberId || null,
          new_passcode: newPass || null,
          clear_passcode: false,
          members,
        }),
      });
      localStorage.removeItem(MEMBER_TOKEN_KEY);
      if (newPass) {
        localStorage.setItem(PASSCODE_KEY, newPass);
      }
      await loadFullState();
      if (navigateAfterSave) {
        switchTab("view-concierge");
      } else {
        showBanner("setup-feedback", "Configurações da família salvas!", "success");
      }
      return true;
    } catch (err) {
      showBanner("setup-feedback", err.message, "error");
      return false;
    }
  }

  // Wire Setup Form
  document.getElementById("btn-add-member-row").addEventListener("click", () => {
    appendMemberSetupRow({ role: "Child" });
  });

  document.getElementById("household-setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await saveHouseholdSetupFromForm(true);
  });

  document.getElementById("btn-clear-passcode").addEventListener("click", async (e) => {
    e.preventDefault();
    const members = collectSetupFormMembers();
    if (!members.length) return;
    const adminSel = document.getElementById("cfg-admin-member");
    await apiFetch("/api/household/setup", {
      method: "PUT",
      body: JSON.stringify({
        household_name: document.getElementById("cfg-name").value.trim() || "Nossa Família",
        preferred_language: document.getElementById("cfg-language").value,
        timezone: document.getElementById("cfg-timezone").value.trim(),
        sports_music_buffer_min: Number((appState.household && appState.household.sports_music_buffer_min) ?? 30),
        admin_member_id: adminSel ? adminSel.value : null,
        clear_passcode: true,
        members,
      }),
    });
    localStorage.removeItem(PASSCODE_KEY);
    localStorage.removeItem(MEMBER_TOKEN_KEY);
    await loadFullState();
    showBanner("setup-feedback", "PIN removido.", "success");
  });

  const resetHouseholdBtn = document.getElementById("btn-reset-household");
  if (resetHouseholdBtn) {
    resetHouseholdBtn.addEventListener("click", async () => {
      const ok = window.confirm(
        "Redefinir as configurações da família, eventos locais, listas e conversas para o estado inicial de novo usuário?"
      );
      if (!ok) return;
      try {
        await apiFetch("/api/household/reset", { method: "POST" });
        localStorage.removeItem(PASSCODE_KEY);
        localStorage.removeItem(MEMBER_TOKEN_KEY);
        localStorage.removeItem(DEVICE_LOCATION_KEY);
        appState.messagesBySession = {};
        appState.chatSessions = [];
        appState.sessionId = `fam-session-${Math.random().toString(36).slice(2, 8)}`;
        persistActiveSessionId(appState.sessionId);
        await loadFullState(true);
      } catch (err) {
        showBanner("setup-feedback", err.message, "error");
      }
    });
  }

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

  const DEFAULT_WELCOME_TEXT = "Como posso ajudar?";

  function setMobileHistoryOpen(open) {
    const rail = document.getElementById("chat-history-rail");
    const btn = document.getElementById("btn-mobile-history");
    if (!rail || !btn) return;
    rail.classList.toggle("mobile-open", open);
    btn.classList.toggle("active", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function syncComposerControls() {
    const isBusy = inflightBySession.has(appState.sessionId);
    const sendBtn = document.getElementById("btn-send-chat");
    const stopBtn = document.getElementById("btn-stop-request");
    const chatTitleEl = document.getElementById("active-chat-title");

    if (sendBtn) {
      sendBtn.disabled = isBusy;
      sendBtn.classList.toggle("is-busy", isBusy);
    }
    if (stopBtn) {
      stopBtn.style.display = isBusy ? "inline-grid" : "none";
    }
    if (chatTitleEl) {
      const meta = appState.chatSessions.find((s) => s.session_id === appState.sessionId);
      chatTitleEl.textContent =
        meta && meta.title && meta.title !== "New Chat" && meta.title !== "Nova Conversa"
          ? meta.title
          : "Concierge IA";
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
        title: "Nova Conversa",
        status: inflightBySession.has(appState.sessionId) ? "running" : "idle",
        message_count: (appState.messagesBySession[appState.sessionId] || []).length,
        last_preview: "Nova conversa",
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
      titleDiv.textContent =
        !sess.title || sess.title === "New Chat" ? "Nova Conversa" : sess.title;
      main.appendChild(titleDiv);

      const metaDiv = document.createElement("div");
      metaDiv.className = "session-item-meta";
      if (isRunningLocal) {
        const badge = document.createElement("span");
        badge.className = "session-status-badge running";
        badge.textContent = "⏳ Pensando...";
        metaDiv.appendChild(badge);
      } else if (needsApproval) {
        const badge = document.createElement("span");
        badge.className = "session-status-badge approval";
        badge.textContent = "🛡️ Aprovação";
        metaDiv.appendChild(badge);
      }
      const countSpan = document.createElement("span");
      const count = Number(sess.message_count || 0);
      countSpan.textContent = count > 0 ? `${count} msg${count === 1 ? "" : "s"}` : "Vazia";
      metaDiv.appendChild(countSpan);
      main.appendChild(metaDiv);

      row.appendChild(main);

      if (count > 0 || sessionsToRender.length > 1) {
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "session-delete-btn";
        delBtn.textContent = "✕";
        delBtn.title = "Excluir conversa";
        delBtn.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          await deleteChatSession(sess.session_id);
        });
        row.appendChild(delBtn);
      }

      row.addEventListener("click", async () => {
        setMobileHistoryOpen(false);
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
    persistActiveSessionId(targetSessionId);
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
    persistActiveSessionId(newId);
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

    // 0. On-demand city / location prompt
    if (/\b(qual\s+(?:sua\s+|a\s+sua\s+)?cidade|cidade\s+ou\s+bairro|sua\s+localiza[çc][ãa]o|which\s+city|your\s+location)\b/i.test(combined)) {
      return [
        { label: "📍 Usar minha localização", useGeolocation: true },
        { label: "✏️ Digitar cidade/bairro...", prefill: "" },
      ];
    }

    // 1. Participant selection question
    if (
      /\b(which\s+family\s+member|who\s+will\s+(?:be\s+)?(?:attend|participat|go)|who\s+is\s+(?:attending|participating|going)|participants?|quem\s+vai|quem\s+da\s+fam[íi]lia|qual\s+membro)\b/i.test(
        combined
      )
    ) {
      const members = appState.members || [];
      members.slice(0, 4).forEach((m) => {
        chips.push({
          label: `Apenas ${m.name}`,
          prompt: `Apenas ${m.name} vai participar.`,
        });
      });
      const kids = members.filter((m) => m.role === "Teen" || m.role === "Child");
      if (kids.length >= 2) {
        const kidNames = kids.map((k) => k.name).join(" e ");
        chips.push({
          label: kidNames,
          prompt: `${kidNames} vão participar.`,
        });
      }
      chips.push({
        label: "[All] Toda a Família",
        prompt: "[All] Toda a família vai participar.",
      });
      return chips.slice(0, 5);
    }

    // 2. Overlap / arrival-buffer verbal confirmation question
    if (/\b(anyway|despite\s+the\s+overlap|overlap|arrival[- ]buffer|mesmo\s+assim|conflito)\b/i.test(combined)) {
      return [
        { label: "✓ Sim, agendar mesmo assim", prompt: "Sim, pode agendar mesmo assim." },
        { label: "🕒 Sugerir horário livre", prompt: "Pode sugerir um horário livre sem conflito nesse dia?" },
        { label: "✕ Pular por enquanto", prompt: "Pular este agendamento por enquanto." },
      ];
    }

    // 3. Linked Need-To-Do errand / gift reminder question
    if (/\b(need-to-do|errand|buy\s+a?\s*gift|birthday\s+gift|shopping|wish\s*list|tarefa|presente)\b/i.test(combined)) {
      return [
        { label: "✓ Sim, adicionar às Tarefas", prompt: "Sim, adicione esse item à nossa lista de Tarefas Pendentes." },
        { label: "✏️ Personalizar tarefa...", prefill: "Adicionar às Tarefas: " },
        { label: "✕ Não precisa", prompt: "Não precisa adicionar tarefa extra agora, obrigado." },
      ];
    }

    // 4. Weekend / multi-slot plan selection
    if (/\b(weekend|saturday|sunday|proposed\s+plan|which\s+(?:slot|option|window)|fim\s+de\s+semana|s[áa]bado|domingo)\b/i.test(combined)) {
      return [
        { label: "✓ Ótimo, agendar esses horários", prompt: "Ótimo plano! Pode agendar esses horários sugeridos." },
        { label: "🕒 Sugerir outros horários", prompt: "Pode sugerir outros horários alternativos para esse plano?" },
        { label: "✏️ Ajustar um detalhe...", prefill: "Vamos ajustar um detalhe: " },
      ];
    }

    // 5. Generic question fallback
    if (extractedQuestion || /\?\s*$/.test(cleanText)) {
      return [
        { label: "✓ Sim, pode seguir", prompt: "Sim, pode seguir." },
        { label: "✏️ Deixe-me explicar...", prefill: "" },
        { label: "✕ Agora não", prompt: "Agora não, obrigado." },
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
            img.alt = att.filename || "Convite enviado";
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
            docTag.textContent = `${icon} ${att.filename || "Anexo"}`;
            mediaWrap.appendChild(docTag);
          }
        });
        bubble.appendChild(mediaWrap);
      }
      const captionDiv = document.createElement("div");
      captionDiv.textContent = msg.content || "";
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
    if (richBody.childNodes.length > 0) {
      bubble.appendChild(richBody);
    }

    const pendingConfs = msg.pending_confirmations || [];
    const hasUnresolvedHitl = pendingConfs.some((c) => !c.resolved);

    pendingConfs.forEach((conf) => {
      const hasWarn = Boolean(conf.warnings && conf.warnings.length);
      const card = document.createElement("div");
      card.className = `hitl-confirm-card${hasWarn ? " has-warning" : ""}`;

      const sum = document.createElement("div");
      sum.className = "hitl-confirm-summary";
      sum.textContent = conf.summary || `Confirmar ${conf.tool_name}`;
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
        warnNote.innerHTML = "<strong>Deseja agendar mesmo assim?</strong>";
        warnBox.appendChild(warnNote);

        card.appendChild(warnBox);
      }

      const actions = document.createElement("div");
      actions.className = "hitl-confirm-actions";

      if (conf.resolved) {
        const resolvedSpan = document.createElement("span");
        resolvedSpan.className = "hitl-status-pill";
        resolvedSpan.textContent = conf.resolution_label || "Decisão registrada";
        actions.appendChild(resolvedSpan);
      } else {
        const approveBtn = document.createElement("button");
        approveBtn.type = "button";
        approveBtn.className = "btn btn-primary";
        approveBtn.textContent = hasWarn ? "✓ Sim, Agendar" : "✓ Confirmar";

        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn btn-secondary";
        cancelBtn.textContent = "✕ Cancelar";

        const handleDecision = async (approved, followUpPrompt = "") => {
          approveBtn.disabled = true;
          cancelBtn.disabled = true;
          actions.innerHTML = `<span class="hitl-status-pill">${approved ? "Aprovando e salvando..." : "Cancelando ação..."}</span>`;
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
          altSlotBtn.textContent = "🕒 Sugerir outro horário livre";
          altSlotBtn.addEventListener("click", () => {
            altSlotBtn.disabled = true;
            handleDecision(false, "Pode sugerir um horário livre sem conflito nesse dia?");
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

        const chipsRow = document.createElement("div");
        chipsRow.className = "smart-chips-row";
        const busy = inflightBySession.has(sessionIdForActions);

        chipItems.forEach((item) => {
          const chipBtn = document.createElement("button");
          chipBtn.type = "button";
          chipBtn.className = "smart-reply-chip";
          chipBtn.disabled = busy;
          chipBtn.textContent = item.label;
          chipBtn.addEventListener("click", async () => {
            if (
              item.useGeolocation ||
              item.label.includes("📍") ||
              /usar\s+(?:minha\s+)?localiza[çc][ãa]o/i.test(item.label)
            ) {
              chipBtn.disabled = true;
              const coords = await requestDeviceGeolocationOnce();
              if (coords) {
                await sendChatMessage("Use minha localização atual para buscar opções perto de mim.", [], coords);
              } else {
                chipBtn.disabled = false;
                const inputEl = document.getElementById("chat-input");
                if (inputEl) {
                  inputEl.placeholder = "Digite sua cidade ou bairro (ex.: Campinas, SP)";
                  inputEl.focus();
                }
              }
              return;
            }
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
      pendingBubble.textContent = inflight.statusText || "Verificando a agenda e as listas da família...";
      messagesEl.appendChild(pendingBubble);
    }

    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  async function submitToolConfirmation(targetSessionId, confirmationCallId, confirmed) {
    const active = getActiveMember();
    const controller = new AbortController();
    inflightBySession.set(targetSessionId, {
      controller,
      statusText: confirmed ? "Aplicando atualização aprovada na agenda..." : "Registrando cancelamento...",
    });

    const localMsgs = appState.messagesBySession[targetSessionId] || [];
    localMsgs.forEach((m) => {
      (m.pending_confirmations || []).forEach((c) => {
        if (c.confirmation_call_id === confirmationCallId) {
          c.resolved = true;
          c.resolution_label = confirmed ? "✓ Aprovado" : "✕ Cancelado";
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
          content: `Erro ao enviar confirmação: ${err.message}`,
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

  async function sendChatMessage(text, stagedAttachments = [], explicitUserLocation = "") {
    clearBanner("chat-feedback");
    const targetSessionId = appState.sessionId;
    if (inflightBySession.has(targetSessionId)) {
      showBanner(
        "chat-feedback",
        "Esta conversa ainda está respondendo. Toque em '+' para iniciar outra conversa em paralelo ou no ícone de parar.",
        "error"
      );
      return;
    }

    const active = getActiveMember();
    const displayCaption = text
      || (stagedAttachments.some((a) => a.mime_type.startsWith("audio/"))
        ? "Enviou uma mensagem de voz"
        : "Enviou uma imagem/convite");

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
    let resolvedLocation = explicitUserLocation || getCachedDeviceLocation() || "";
    const needsFirstTimeGps =
      !resolvedLocation &&
      !(appState.household && appState.household.default_location) &&
      messageNeedsNearMeLocation(text);

    inflightBySession.set(targetSessionId, {
      controller,
      statusText: needsFirstTimeGps
        ? "Solicitando sua localização pela 1ª vez..."
        : stagedAttachments.length
        ? "Lendo anexo e verificando a agenda da família..."
        : "Verificando a agenda e buscando informações...",
    });

    renderActiveSessionMessages();
    renderChatSessionsList();
    syncComposerControls();

    try {
      if (needsFirstTimeGps) {
        resolvedLocation = await requestDeviceGeolocationOnce();
        const activeInflight = inflightBySession.get(targetSessionId);
        if (activeInflight) {
          activeInflight.statusText = "Buscando opções perto de você...";
          if (appState.sessionId === targetSessionId) {
            renderActiveSessionMessages();
          }
        }
      }

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
          member_avatar: "",
          session_id: targetSessionId,
          user_location: resolvedLocation || null,
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
          content: "(Resposta interrompida. Você pode enviar uma nova mensagem a qualquer momento.)",
          models_used: [],
          tool_calls: [],
          pending_confirmations: [],
        });
      } else {
        msgList.push({
          role: "assistant",
          content: `Erro ao falar com o Concierge: ${err.message}`,
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
    if (isSubmittingChatForm) {
      return;
    }
    isSubmittingChatForm = true;
    try {
      if ((mediaRecorder && mediaRecorder.state !== "inactive") || recordingStopPromise) {
        await finalizeActiveRecording(true);
      }
      const input = document.getElementById("chat-input");
      const text = input.value.trim();
      const attachmentsToSend = [...appState.pendingAttachments];
      if (!text && !attachmentsToSend.length) {
        input.focus();
        return;
      }
      input.value = "";
      appState.pendingAttachments = [];
      renderPendingAttachments();
      await sendChatMessage(text, attachmentsToSend);
    } finally {
      isSubmittingChatForm = false;
    }
  });

  const newChatBtn = document.getElementById("btn-new-chat");
  if (newChatBtn) {
    newChatBtn.addEventListener("click", () => {
      setMobileHistoryOpen(false);
      startNewChatSession();
    });
  }

  const mobileHistoryBtn = document.getElementById("btn-mobile-history");
  if (mobileHistoryBtn) {
    mobileHistoryBtn.addEventListener("click", () => {
      const rail = document.getElementById("chat-history-rail");
      setMobileHistoryOpen(rail ? !rail.classList.contains("mobile-open") : false);
    });
  }

  const mobileNewChatBtn = document.getElementById("btn-mobile-new-chat");
  if (mobileNewChatBtn) {
    mobileNewChatBtn.addEventListener("click", () => {
      setMobileHistoryOpen(false);
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

  // Auto-sync shared state across multiple family phones when unlocking or returning to the app
  let lastAutoSyncAt = Date.now();
  async function syncOnPhoneForeground() {
    if (document.visibilityState && document.visibilityState !== "visible") return;
    if (Date.now() - lastAutoSyncAt < 4000) return;
    if (inflightBySession.size > 0) return;
    const setupPanel = document.getElementById("view-setup");
    if (setupPanel && setupPanel.classList.contains("active")) return;
    lastAutoSyncAt = Date.now();
    try {
      await loadFullState(false);
    } catch (_err) {
      // Keep UI quiet on background refresh
    }
  }
  document.addEventListener("visibilitychange", syncOnPhoneForeground);
  window.addEventListener("focus", syncOnPhoneForeground);

  // Admin PIN Unlock Modal
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
      localStorage.removeItem(MEMBER_TOKEN_KEY);
      if (new URLSearchParams(window.location.search).has("m")) {
        window.history.replaceState({}, "", window.location.pathname);
      }
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
