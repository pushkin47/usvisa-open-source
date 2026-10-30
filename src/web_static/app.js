const state = {
  settings: [],
  ignoredPeriods: [],
  priorityPeriods: [],
  polling: null,
  lastPayload: null,
  lastProcess: null,
  lastMonitor: null,
  debugPayload: null,
  user: null,
  adminUserSettings: null,
  cityOptions: {},
  cities: [],
  authMode: "login",
  logUserFilter: "all",
  showLogEventTime: true,
  onboardingShownThisSession: false,
  editingIgnoreRange: null,
  editingPriorityRange: null,
  tour: {
    active: false,
    steps: [],
    index: 0,
    returnTab: "results",
    markSeenOnClose: true,
  },
};

const TOUR_COPY = {
  results: {
    title: "Результаты",
    text: "Здесь появляются актуальные даты, которые монитор нашел для записи, и время ближайшей проверки.",
  },
  logs: {
    title: "Логи запуска",
    text: "В этой вкладке видно, что происходит прямо сейчас: вход на сайт, запросы, ошибки и статус текущего процесса.",
  },
  ignore: {
    title: "Игнор-лист",
    text: "Тут можно исключить нежелательные периоды и задать приоритетные даты, на которые сервису можно пытаться записать вас автоматически.",
  },
  settings: {
    title: "Настройки",
    text: "Здесь администратор управляет параметрами мониторинга, таймингами и служебным поведением сервиса.",
  },
  users: {
    title: "Пользователи",
    text: "Во вкладке собраны аккаунты пользователей, их статусы и выбранные параметры записи.",
  },
  profile: {
    title: "Личный кабинет",
    text: "Сюда нужно вставить логин и пароль от ais.usvisa-info.com для собеседования в Астане. Без этого сервис не сможет работать с вашей записью.",
  },
  instruction: {
    title: "Инструкция",
    text: "Здесь хранится пошаговая памятка: DS-160, регистрация на сайте посольства, ввод логина и пароля и общая логика сервиса.",
  },
  summary: {
    title: "Сводка",
    text: "В этой вкладке собрана статистика проверок: сколько было удачных запусков, ошибок и найденных дат.",
  },
  errors: {
    title: "Ошибки",
    text: "Здесь администратор видит последние ошибки мониторинга, которые еще не попали в сводку.",
  },
  debug: {
    title: "Отладка",
    text: "Во вкладке лежат скриншоты, HTML и другие артефакты неудачных логинов для технической диагностики.",
  },
  cities: {
    title: "Астана",
    text: "Здесь настраиваются ссылки записи и API для Астаны (134).",
  },
};

const USER_STATUSES = [
  "не оплачен",
  "поиск включен",
  "поиск выключен",
  "записан на слот",
  "заблокирован",
  "удален",
];

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const MAX_ALLOWED_DATE = "2030-12-31";
const THEME_STORAGE_KEY = "usvisa-theme";
const LOG_EVENT_TIME_STORAGE_KEY = "usvisa-log-event-time";

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function formatDateWithSeconds(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ru-RU", {
    dateStyle: "medium",
    timeStyle: "medium",
  });
}

function formatLogEventTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value.replace("T", " ").replace(/\.\d+.*$/, "");
  }
  return date.toLocaleString("ru-RU", {
    dateStyle: "short",
    timeStyle: "medium",
  });
}

function nextCheckLabel(apiState) {
  if (!apiState.nextCheckAt) return "-";
  const seconds = apiState.secondsUntilNextCheck;
  if (seconds === 0) return "идет проверка";
  if (typeof seconds !== "number") return formatDate(apiState.nextCheckAt);
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}м ${rest}с`;
}

function sessionLabel(session) {
  if (!session) return "-";
  const parts = [];
  if (session.hasCookie || session.cookieCandidates > 0) parts.push("cookie");
  if (session.hasCsrfToken) parts.push("CSRF");
  if (session.hasSessionId) parts.push("Session-Id");
  return parts.length ? parts.join(" + ") : "нет данных";
}

function selectedRangeHours() {
  const value = $("#rangeHours").value;
  return value === "all" ? null : Number(value);
}

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(theme) {
  const nextTheme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = nextTheme;
  localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
  const button = $("#themeToggleBtn");
  if (button) {
    button.textContent = nextTheme === "dark" ? "Светлая тема" : "Тёмная тема";
  }
}

function toggleTheme() {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
}

function eventsInRange(events, hours) {
  if (!hours) return events || [];
  const cutoff = Date.now() - hours * 60 * 60 * 1000;
  return (events || []).filter((event) => {
    const time = new Date(event.at).getTime();
    return Number.isFinite(time) && time >= cutoff;
  });
}

function uniqueCount(values) {
  return new Set((values || []).filter(Boolean)).size;
}

function renderMetrics(apiState) {
  const summary = state.lastPayload?.summary || {};
  $("#successfulChecks").textContent = String(summary.successfulChecks ?? 0);
  $("#tokenErrors").textContent = String(summary.tokenErrors ?? 0);
  $("#browserErrors").textContent = String(summary.browserErrors ?? 0);
  $("#foundFreeDays").textContent = String(summary.foundFreeDays ?? 0);
  $("#actualFreeDays").textContent = String(summary.actualFreeDays ?? 0);
  $("#nextCheck").textContent = nextCheckLabel(apiState);
  $("#nextCheckAtAbsolute").textContent = formatDateWithSeconds(apiState.nextCheckAt);
}

function monitorLabel(monitor) {
  if (!monitor?.active) return "выключен";
  return `включен · ${monitor.interval_seconds}с`;
}

function autoPauseLabel(monitor) {
  if (monitor?.auto_paused && monitor?.paused_until) {
    return `на паузе до ${formatDateWithSeconds(monitor.paused_until)}`;
  }
  return "не активна";
}

function windowDayTemplate(item) {
  const details = item.times?.length ? `${item.times.length} времен` : "есть в /days";
  return `<div class="day-row found-date-row"><strong>${escapeHtml(item.date)}</strong><span class="muted">${details}</span></div>`;
}

function renderDateTagList(dates, emptyText) {
  if (!dates?.length) return `<div class="empty compact">${emptyText}</div>`;
  return `<div class="tag-list found-date-list">${dates.map((date) => `<span class="tag found-date-tag">${escapeHtml(date)}</span>`).join("")}</div>`;
}

function renderDatePayload(payload, emptyText = "Еще нет данных последнего ответа /days.") {
  if (!payload?.length) return `<div class="empty compact">${escapeHtml(emptyText)}</div>`;
  return `<div class="kv-list">${payload
    .map(
      (item) =>
        `<div class="kv-row"><strong>${escapeHtml(item.date)}</strong><span>${item.business_day ? "рабочий день" : "нерабочий день"}</span></div>`,
    )
    .join("")}</div>`;
}

function renderTimesMap(timesMap, emptyText) {
  const entries = Object.entries(timesMap || {});
  if (!entries.length) return `<div class="empty compact">${emptyText}</div>`;
  return `<div class="kv-list">${entries
    .map(([date, times]) => {
      const value = times?.length ? times.map((time) => escapeHtml(time)).join(", ") : "нет времени";
      return `<div class="kv-row found-date-row"><strong>${escapeHtml(date)}</strong><span>${value}</span></div>`;
    })
    .join("")}</div>`;
}

function renderCurrentWindows(apiState) {
  const active = apiState.currentActiveAppointments || [];
  $("#activeWindowDays").innerHTML = active.length
    ? active.map(windowDayTemplate).join("")
    : '<div class="empty compact">-</div>';
}

function renderLastCheck(apiState) {
  const dates = Array.isArray(apiState.periodFoundDates)
    ? [...new Set(apiState.periodFoundDates.filter(Boolean))].sort()
    : [];
  $("#datesDebugSnapshot").innerHTML = `
    <div class="focus-card">
      <span class="snapshot-label">Найденные даты за выбранный период</span>
      ${renderDateTagList(dates, "За выбранный период монитор не находил дат вне игнор-листа")}
    </div>
  `;
}

function renderErrors(apiState) {
  const errors = apiState.errorEvents || [];
  $("#clearErrorsBtn").hidden = true;
  if (apiState.readError) {
    $("#clearErrorsBtn").hidden = false;
    $("#errorsSummary").textContent = "Не удалось прочитать очередь ошибок.";
    $("#errorsBlock").innerHTML = `<div class="error-item">Не удалось прочитать state: ${escapeHtml(apiState.readError)}</div>`;
    return;
  }
  if (!errors.length) {
    $("#errorsSummary").textContent = "актуальных ошибок нет";
    $("#errorsBlock").innerHTML = "";
    return;
  }
  $("#clearErrorsBtn").hidden = false;
  $("#errorsSummary").textContent = "Последние ошибки мониторинга, которые еще не ушли в сводку.";
  $("#errorsBlock").innerHTML = `<h3>Ошибки в очереди дайджеста</h3>${errors
    .map(
      (item) =>
        `<div class="error-item"><strong>${formatDate(item.at)} · ${escapeHtml(item.type)}</strong><br>${escapeHtml(item.message)}</div>`,
    )
    .join("")}`;
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 Б";
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}

function renderDebugArtifacts(payload) {
  state.debugPayload = payload || null;
  $("#clearDebugArtifactsBtn").hidden = true;
  if (!payload) {
    $("#debugSummary").textContent = "Не удалось загрузить артефакты отладки.";
    $("#debugArtifacts").innerHTML = '<div class="empty">API отладки сейчас недоступен.</div>';
    return;
  }

  const artifacts = payload.artifacts || [];
  const path = payload.path ? `Папка: ${payload.path}` : "";
  $("#debugSummary").textContent = artifacts.length
    ? `${artifacts.length} артефакт(ов). ${path}`
    : payload.exists
      ? `Папка с артефактами есть, но пока пустая. ${path}`
      : `Папка с артефактами пока не создана. ${path}`;

  if (!artifacts.length) {
    $("#debugArtifacts").innerHTML = '<div class="empty">Пока нечего показывать: скриншоты и HTML появятся после неудачных браузерных логинов.</div>';
    return;
  }

  $("#clearDebugArtifactsBtn").hidden = false;

  $("#debugArtifacts").innerHTML = artifacts
    .map((artifact) => {
      const meta = [
        artifact.createdAt ? `<span class="tag">${escapeHtml(formatDateWithSeconds(artifact.createdAt))}</span>` : "",
        artifact.screenshotName ? `<span class="tag">${escapeHtml(artifact.screenshotName)}</span>` : "",
        artifact.htmlName ? `<span class="tag">${escapeHtml(artifact.htmlName)}</span>` : "",
      ]
        .filter(Boolean)
        .join("");

      const imageBlock = artifact.screenshotUrl
        ? `<a class="debug-image-link" href="${artifact.screenshotUrl}" target="_blank" rel="noreferrer">
            <img class="debug-image" src="${artifact.screenshotUrl}" alt="${escapeHtml(artifact.label || artifact.id)}">
          </a>`
        : '<div class="empty compact">Скриншот не сохранен.</div>';

      const htmlBlock = artifact.htmlPreview
        ? `<details class="debug-html-block">
            <summary>HTML превью${artifact.htmlTitle ? ` · ${escapeHtml(artifact.htmlTitle)}` : ""}</summary>
            <pre class="debug-code">${escapeHtml(artifact.htmlPreview)}</pre>
          </details>`
        : '<div class="empty compact">HTML-дамп не сохранен.</div>';

      const rawLink = artifact.htmlUrl
        ? `<a class="debug-link" href="${artifact.htmlUrl}" target="_blank" rel="noreferrer">Открыть raw HTML</a>`
        : "";
      const deleteAction = `<div class="debug-card-actions"><button class="secondary-button" data-delete-artifact="${escapeHtml(
        artifact.id,
      )}" type="button">Удалить артефакт</button></div>`;

      return `<article class="debug-card">
        <div class="debug-card-head">
          <div class="debug-card-copy">
            <h3>${escapeHtml(artifact.label || artifact.id)}</h3>
            <div class="tag-list">${meta}</div>
          </div>
          <div class="debug-card-stats">
            <span>PNG: ${escapeHtml(formatBytes(artifact.screenshotSize))}</span>
            <span>HTML: ${escapeHtml(formatBytes(artifact.htmlSize))}</span>
          </div>
        </div>
        ${imageBlock}
        <div class="debug-card-body">
          ${htmlBlock}
          ${rawLink}
          ${deleteAction}
        </div>
      </article>`;
    })
    .join("");

  $$("[data-delete-artifact]").forEach((button) => {
    button.addEventListener("click", () => {
      deleteDebugArtifact(button.dataset.deleteArtifact);
    });
  });
}

function fieldTemplate(field) {
  if (field.type === "checkbox") {
    const checked = String(field.value).toLowerCase() === "true" ? "checked" : "";
    const help = field.help ? `<small class="field-help">${escapeHtml(field.help)}</small>` : "";
    return `<label class="field-card field-card-toggle">
      <div class="field-copy">
        <span class="field-label">${field.label}</span>
        ${help}
      </div>
      <div class="toggle">
        <input data-key="${field.key}" type="checkbox" ${checked}>
      </div>
    </label>`;
  }

  if (field.multiline) {
    const help = field.help ? `<small class="field-help">${escapeHtml(field.help)}</small>` : "";
    return `<label class="field-card">
      <span class="field-label">${field.label}</span>
      ${help}
      <textarea data-key="${field.key}" spellcheck="false">${escapeHtml(field.value)}</textarea>
    </label>`;
  }

  const help = field.help ? `<small class="field-help">${escapeHtml(field.help)}</small>` : "";
  return `<label class="field-card">
    <span class="field-label">${field.label}</span>
    ${help}
    <input data-key="${field.key}" type="${field.type}" value="${escapeHtml(field.value)}" autocomplete="off">
  </label>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderSettings(groups) {
  state.settings = groups || [];
  $("#settingsGroups").innerHTML = state.settings
    .map(
      (group) => `<article class="settings-group">
        <h3>${escapeHtml(group.title)}</h3>
        <p class="muted">${escapeHtml(group.description)}</p>
        <div class="fields">${group.fields.map(fieldTemplate).join("")}</div>
      </article>`,
    )
    .join("");
}

function setAppVisible(isAuthenticated) {
  $("#authView").hidden = isAuthenticated;
  $("#appView").hidden = !isAuthenticated;
  if (!isAuthenticated) {
    state.onboardingShownThisSession = false;
  }
}

function activeTabName() {
  return $(".tab.active")?.dataset.tab || "results";
}

function canObserveRuns() {
  return state.user?.role === "admin";
}

function canViewLogs() {
  return state.user?.role === "admin";
}

function canViewAdminObserverTabs() {
  return state.user?.role === "admin";
}

function shouldAutoRefreshTab(name = activeTabName()) {
  return name === "results" || name === "logs";
}

function stopPolling() {
  if (!state.polling) return;
  clearInterval(state.polling);
  state.polling = null;
}

function ensurePolling() {
  stopPolling();
  if (!shouldAutoRefreshTab()) return;
  state.polling = setInterval(() => {
    if (!shouldAutoRefreshTab()) {
      stopPolling();
      return;
    }
    if (activeTabName() === "results") {
      loadCompactStatus();
      return;
    }
    if (activeTabName() === "logs") {
      loadProcessStatus();
      return;
    }
  }, 3000);
}

function setRoleVisibility() {
  const isAdmin = state.user?.role === "admin";
  const isUser = state.user?.role === "user";
  $$(".admin-only").forEach((item) => {
    item.hidden = !isAdmin;
  });
  $$(".account-only").forEach((item) => {
    item.hidden = false;
  });
  $$(".logs-observer-only").forEach((item) => {
    item.hidden = !canViewLogs();
  });
  $$(".admin-observer-only").forEach((item) => {
    item.hidden = !canViewAdminObserverTabs();
  });
  if (!isAdmin && ["settings", "users", "cities"].includes($(".tab.active")?.dataset.tab)) {
    activateTab("results");
  }
  if ((!canViewLogs() && $(".tab.active")?.dataset.tab === "logs")
    || (!canViewAdminObserverTabs() && ["errors", "debug"].includes($(".tab.active")?.dataset.tab))) {
    activateTab("results");
  }
}

function renderUser(user, cityOptions) {
  state.user = user;
  state.cityOptions = cityOptions || state.cityOptions || {};
  const roleLabel = user?.role === "admin" ? "админ" : user?.role === "demo" ? "демо" : "пользователь";
  $("#userBadge").textContent = user ? `${user.email} · ${roleLabel}` : "";
  setRoleVisibility();
  if (user?.profile) renderProfile(user.profile);
  maybeShowOnboarding(user);
}

function renderProfile(profile) {
  $("#embassyLogin").value = profile.embassyLogin || "";
  $("#embassyPassword").value = profile.embassyPassword || "";
  const cityOptions = state.cityOptions || {};
  $("#profileCity").innerHTML = Object.entries(cityOptions)
    .map(([value, option]) => {
      const inactive = option.active === false;
      const selectedInactive = value === (profile.city || "astana") && inactive;
      const label = `${option.label}${inactive ? " (неактивно)" : ""}`;
      return `<option value="${escapeHtml(value)}" ${inactive && !selectedInactive ? "disabled" : ""}>${escapeHtml(label)}</option>`;
    })
    .join("");
  $("#profileCity").value = profile.city || "astana";
}

function collectProfile() {
  return {
    embassyLogin: $("#embassyLogin").value,
    embassyPassword: $("#embassyPassword").value,
    city: $("#profileCity").value,
    ignoredDates: expandPeriodsToDates(state.ignoredPeriods),
  };
}

async function saveProfile(event) {
  event.preventDefault();
  $("#profileStatus").textContent = "Сохраняю...";
  const response = await fetch("/api/profile", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectProfile()),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    $("#profileStatus").textContent = data.error || "Не удалось сохранить";
    return;
  }
  $("#profileStatus").textContent = "Сохранено";
  state.user.profile = data.profile;
  renderProfile(data.profile);
}

async function loadUsers() {
  if (state.user?.role !== "admin") return;
  const response = await fetch("/api/users");
  if (!response.ok) return;
  const data = await response.json();
  $("#usersList").hidden = false;
  $("#userSettingsView").hidden = true;
  $("#usersList").innerHTML = data.users?.length
    ? data.users
        .map(
          (user) => `<article class="user-row">
            <div class="user-row-main">
              <strong>${escapeHtml(user.email)}</strong>
              <span class="muted">${escapeHtml(user.role)} · приоритет ${escapeHtml(user.priority)} · ${escapeHtml(user.city)} · facility ${escapeHtml(user.facilityId || "-")}</span>
              ${user.vpnProxyUrl ? `<span class="muted">proxy: ${escapeHtml(user.vpnProxyUrl)}</span>` : ""}
            </div>
            <div class="user-row-actions">
              <label class="user-status-field">
                <span class="muted">Статус</span>
                <select data-user-status="${escapeHtml(user.id)}">
                  ${USER_STATUSES.map(
                    (status) =>
                      `<option value="${escapeHtml(status)}" ${user.status === status ? "selected" : ""}>${escapeHtml(status)}</option>`,
                  ).join("")}
                </select>
              </label>
              <span class="tag user-meta-tag">${escapeHtml(user.ignoredDatesCount)} игнор-дат</span>
              ${user.role === "demo" ? "" : `<button class="secondary-button" data-open-user-settings="${escapeHtml(user.id)}" type="button">Настройки</button>`}
              ${user.id === state.user?.id ? "" : `<button class="danger-button" data-delete-user="${escapeHtml(user.id)}" data-user-email="${escapeHtml(user.email)}" type="button">Удалить</button>`}
            </div>
          </article>`,
        )
        .join("")
    : '<div class="empty compact">Пользователей пока нет.</div>';

  $$("[data-user-status]").forEach((select) => {
    select.dataset.previousValue = select.value;
    select.addEventListener("change", async () => {
      const userId = Number(select.dataset.userStatus);
      const status = select.value;
      const previous = select.dataset.previousValue || "";
      const ok = await updateUserStatus(userId, status);
      if (!ok) {
        select.value = previous;
        return;
      }
      select.dataset.previousValue = status;
    });
  });
  $$("[data-delete-user]").forEach((button) => {
    button.addEventListener("click", () => deleteUser(Number(button.dataset.deleteUser), button.dataset.userEmail || ""));
  });
  $$("[data-open-user-settings]").forEach((button) => {
    button.addEventListener("click", () => openUserSettings(Number(button.dataset.openUserSettings)));
  });
}

async function updateUserStatus(userId, status) {
  const response = await fetch("/api/users/status", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ userId, status }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert(data.error || "Не удалось обновить статус пользователя");
    return false;
  }
  await loadUsers();
  return true;
}

async function deleteUser(userId, email) {
  const confirmed = await confirmDeletion({
    title: "Удалить пользователя?",
    message: `Аккаунт ${email}, его профиль и все сессии будут удалены без возможности восстановления.`,
  });
  if (!confirmed) return;

  const response = await fetch(`/api/users/${userId}`, { method: "DELETE" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert(data.error || "Не удалось удалить пользователя");
    return;
  }
  await loadUsers();
}

async function openUserSettings(userId) {
  $("#usersList").hidden = true;
  $("#userSettingsView").hidden = false;
  $("#userSettingsStatus").textContent = "Загружаю...";
  const response = await fetch(`/api/users/${userId}/settings`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    $("#userSettingsStatus").textContent = data.error || "Не удалось открыть настройки";
    return;
  }
  state.adminUserSettings = {
    user: data.user,
    profile: data.profile,
    ignoredPeriods: Array.isArray(data.profile?.ignoredPeriods) ? data.profile.ignoredPeriods : [],
    priorityPeriods: Array.isArray(data.profile?.priorityPeriods) ? data.profile.priorityPeriods : [],
  };
  state.cityOptions = data.cityOptions || state.cityOptions || {};
  renderUserSettings();
}

function renderUserSettings() {
  const current = state.adminUserSettings;
  if (!current) return;
  const profile = current.profile || {};
  $("#userSettingsTitle").textContent = `Настройки: ${current.user.email}`;
  $("#userSettingsMeta").textContent = `${current.user.role} · ${current.user.status}`;
  $("#userSettingsEmbassyLogin").value = profile.embassyLogin || "";
  $("#userSettingsEmbassyPassword").value = "";
  $("#userSettingsVpnProxyUrl").value = profile.vpnProxyUrl || "";
  $("#userSettingsAppointmentUrl").value = profile.appointmentUrl || "";
  $("#userSettingsDatesApiUrl").value = profile.datesApiUrl || "";
  $("#userSettingsTimesApiUrl").value = profile.timesApiUrl || "";
  $("#userSettingsPreparationDays").value = String(Math.max(1, Number(profile.preparationDays ?? 1) || 1));
  $("#userSettingsCity").innerHTML = Object.entries(state.cityOptions || {})
    .map(([value, option]) => {
      const inactive = option.active === false;
      return `<option value="${escapeHtml(value)}" ${profile.city === value ? "selected" : ""}>${escapeHtml(option.label + (inactive ? " (неактивен)" : ""))}</option>`;
    })
    .join("");
  $("#userSettingsCity").value = profile.city || "astana";
  renderUserSettingsPeriods();
  $("#userSettingsStatus").textContent = "";
}

function periodChip(period, kind) {
  const label = period.start === period.end ? period.start : `${period.start} - ${period.end}`;
  return `<div class="date-chip-wrap">
    <span class="date-chip ${kind === "priority" ? "priority-chip" : ""}">${escapeHtml(label)}</span>
    <button class="date-chip-delete" data-user-settings-delete-${kind}="${escapeHtml(period.start)}|${escapeHtml(period.end)}" type="button">×</button>
  </div>`;
}

function renderUserSettingsPeriods() {
  const current = state.adminUserSettings;
  if (!current) return;
  $("#userSettingsIgnoredList").innerHTML = current.ignoredPeriods.length
    ? current.ignoredPeriods.map((period) => periodChip(period, "ignore")).join("")
    : '<div class="empty compact">Игнор-лист пуст.</div>';
  $("#userSettingsPriorityList").innerHTML = current.priorityPeriods.length
    ? current.priorityPeriods.map((period) => periodChip(period, "priority")).join("")
    : '<div class="empty compact">Приоритетный список пуст.</div>';
  $$("[data-user-settings-delete-ignore]").forEach((button) => {
    button.addEventListener("click", () => {
      const [start, end] = button.dataset.userSettingsDeleteIgnore.split("|");
      current.ignoredPeriods = current.ignoredPeriods.filter((period) => !(period.start === start && period.end === end));
      renderUserSettingsPeriods();
    });
  });
  $$("[data-user-settings-delete-priority]").forEach((button) => {
    button.addEventListener("click", () => {
      const [start, end] = button.dataset.userSettingsDeletePriority.split("|");
      current.priorityPeriods = current.priorityPeriods.filter((period) => !(period.start === start && period.end === end));
      renderUserSettingsPeriods();
    });
  });
}

function addUserSettingsPeriod(kind) {
  const current = state.adminUserSettings;
  if (!current) return;
  const startSelector = kind === "priority" ? "#userSettingsPriorityStart" : "#userSettingsIgnoreStart";
  const endSelector = kind === "priority" ? "#userSettingsPriorityEnd" : "#userSettingsIgnoreEnd";
  const period = periodFromInputs(startSelector, endSelector);
  if (!period) {
    $("#userSettingsStatus").textContent = "Проверь даты периода";
    return;
  }
  if (kind === "priority") {
    current.priorityPeriods = uniquePeriods([...current.priorityPeriods, period]);
    $("#userSettingsPriorityStart").value = "";
    $("#userSettingsPriorityEnd").value = "";
  } else {
    current.ignoredPeriods = uniquePeriods([...current.ignoredPeriods, period]);
    $("#userSettingsIgnoreStart").value = "";
    $("#userSettingsIgnoreEnd").value = "";
  }
  renderUserSettingsPeriods();
}

function collectUserSettings() {
  const current = state.adminUserSettings;
  const ignoredPeriods = uniquePeriods(current?.ignoredPeriods || []);
  const priorityPeriods = uniquePeriods(current?.priorityPeriods || []);
  return {
    embassyLogin: $("#userSettingsEmbassyLogin").value,
    embassyPassword: $("#userSettingsEmbassyPassword").value,
    city: $("#userSettingsCity").value,
    vpnProxyUrl: $("#userSettingsVpnProxyUrl").value,
    appointmentUrl: $("#userSettingsAppointmentUrl").value,
    datesApiUrl: $("#userSettingsDatesApiUrl").value,
    timesApiUrl: $("#userSettingsTimesApiUrl").value,
    preparationDays: Math.max(1, Number($("#userSettingsPreparationDays").value || 1)),
    ignoredPeriods,
    priorityPeriods,
    ignoredDates: expandPeriodsToDates(ignoredPeriods),
    priorityDates: expandPeriodsToDates(priorityPeriods),
  };
}

async function saveUserSettings() {
  const current = state.adminUserSettings;
  if (!current?.user?.id) return;
  $("#userSettingsStatus").textContent = "Сохраняю...";
  const response = await fetch(`/api/users/${current.user.id}/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectUserSettings()),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    $("#userSettingsStatus").textContent = data.error || "Не удалось сохранить";
    return;
  }
  state.adminUserSettings = {
    user: data.user,
    profile: data.profile,
    ignoredPeriods: Array.isArray(data.profile?.ignoredPeriods) ? data.profile.ignoredPeriods : [],
    priorityPeriods: Array.isArray(data.profile?.priorityPeriods) ? data.profile.priorityPeriods : [],
  };
  state.cityOptions = data.cityOptions || state.cityOptions || {};
  renderUserSettings();
  $("#userSettingsStatus").textContent = "Сохранено";
}

async function closeUserSettings() {
  state.adminUserSettings = null;
  $("#userSettingsView").hidden = true;
  $("#usersList").hidden = false;
  await loadUsers();
}

function onboardingStorageKey(user) {
  if (user?.email) return `usvisa_onboarding_seen:${String(user.email).toLowerCase()}`;
  if (user?.role === "demo") return "usvisa_onboarding_seen:demo";
  return "";
}

function tourSteps() {
  return $$(".tab")
    .filter((tab) => !tab.hidden)
    .map((tab) => {
      const copy = TOUR_COPY[tab.dataset.tab] || {
        title: tab.textContent?.trim() || "Раздел",
        text: "В этом разделе собраны связанные функции сервиса.",
      };
      return {
        tab: tab.dataset.tab,
        title: copy.title,
        text: copy.text,
      };
    });
}

function clearTourTarget() {
  $(".tour-target-active")?.classList.remove("tour-target-active");
}

function positionTourCard() {
  if (!state.tour.active) return;
  const step = state.tour.steps[state.tour.index];
  if (!step) return;
  const target = $(`.tab[data-tab="${step.tab}"]`);
  const card = $("#tourCard");
  if (!target || !card) return;

  target.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
  const rect = target.getBoundingClientRect();
  const cardRect = card.getBoundingClientRect();
  const gap = 18;
  const viewportPadding = 16;
  const preferredTop = rect.top - cardRect.height - gap;
  const top = preferredTop >= viewportPadding
    ? preferredTop
    : Math.min(window.innerHeight - cardRect.height - viewportPadding, rect.bottom + gap);
  const left = Math.min(
    window.innerWidth - cardRect.width - viewportPadding,
    Math.max(viewportPadding, rect.left + rect.width / 2 - cardRect.width / 2),
  );
  const pointerLeft = Math.min(
    cardRect.width - 28,
    Math.max(28, rect.left + rect.width / 2 - left),
  );
  card.style.top = `${Math.max(viewportPadding, top)}px`;
  card.style.left = `${left}px`;
  card.style.setProperty("--tour-pointer-left", `${pointerLeft}px`);
  card.dataset.placement = preferredTop >= viewportPadding ? "top" : "bottom";
}

function renderTourStep() {
  if (!state.tour.active) return;
  let step = state.tour.steps[state.tour.index];
  if (!step) {
    closeTour();
    return;
  }
  const tab = $(`.tab[data-tab="${step.tab}"]`);
  if (!tab || tab.hidden) {
    state.tour.steps = tourSteps();
    if (!state.tour.steps.length) {
      closeTour();
      return;
    }
    state.tour.index = Math.min(state.tour.index, state.tour.steps.length - 1);
    step = state.tour.steps[state.tour.index];
  }
  clearTourTarget();
  $(`.tab[data-tab="${step.tab}"]`)?.classList.add("tour-target-active");
  $("#tourCounter").textContent = `${state.tour.index + 1} / ${state.tour.steps.length}`;
  $("#tourTitle").textContent = step.title;
  $("#tourText").textContent = step.text;
  $("#tourPrevBtn").disabled = state.tour.index === 0;
  $("#tourNextBtn").textContent = state.tour.index === state.tour.steps.length - 1 ? "Завершить" : "Далее";
  requestAnimationFrame(positionTourCard);
}

function startTour({ force = false, markSeenOnClose = true } = {}) {
  if (!force && state.onboardingShownThisSession) return;
  const steps = tourSteps();
  if (!steps.length) {
    const dialog = $("#onboardingDialog");
    if (dialog && !dialog.open) dialog.showModal();
    return;
  }
  state.onboardingShownThisSession = true;
  state.tour.active = true;
  state.tour.steps = steps;
  state.tour.index = 0;
  state.tour.returnTab = $(".tab.active")?.dataset.tab || "results";
  state.tour.markSeenOnClose = markSeenOnClose;
  document.body.classList.add("tour-active");
  $("#tourOverlay").hidden = false;
  if ($("#onboardingDialog").open) $("#onboardingDialog").close();
  renderTourStep();
}

function closeTour({ markSeen = state.tour.markSeenOnClose } = {}) {
  if (!state.tour.active) return;
  clearTourTarget();
  state.tour.active = false;
  state.tour.steps = [];
  state.tour.index = 0;
  document.body.classList.remove("tour-active");
  $("#tourOverlay").hidden = true;
  $("#tourCard").style.top = "";
  $("#tourCard").style.left = "";
  $("#tourCard").style.removeProperty("--tour-pointer-left");
  if (markSeen) markOnboardingSeen();
  if (state.tour.returnTab && $(`.tab[data-tab="${state.tour.returnTab}"]`) && !$(`.tab[data-tab="${state.tour.returnTab}"]`).hidden) {
    activateTab(state.tour.returnTab);
  }
}

function nextTourStep() {
  if (!state.tour.active) return;
  if (state.tour.index >= state.tour.steps.length - 1) {
    closeTour();
    return;
  }
  state.tour.index += 1;
  renderTourStep();
}

function previousTourStep() {
  if (!state.tour.active || state.tour.index === 0) return;
  state.tour.index -= 1;
  renderTourStep();
}

function handleTourKeydown(event) {
  if (!state.tour.active) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeTour({ markSeen: true });
    return;
  }
  if (event.key === "ArrowRight" || event.key === "Enter") {
    event.preventDefault();
    nextTourStep();
    return;
  }
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    previousTourStep();
  }
}

function maybeShowOnboarding(user) {
  if (!["user", "demo", "admin"].includes(user?.role)) return;
  const key = onboardingStorageKey(user);
  if (!key) return;
  try {
    if (localStorage.getItem(key) === "1") return;
  } catch (_) {
    // ignore storage issues
  }
  startTour({ markSeenOnClose: true });
}

function markOnboardingSeen() {
  const key = onboardingStorageKey(state.user);
  if (!key) return;
  try {
    localStorage.setItem(key, "1");
  } catch (_) {
    // ignore storage issues
  }
}

function cityCardTemplate(city) {
  return `<article class="city-card" data-city-row="${escapeHtml(city.code)}">
    <div class="city-card-head">
      <div>
        <h3>${escapeHtml(city.label || city.code)}</h3>
        <p class="muted">Код: ${escapeHtml(city.code || "-")}</p>
      </div>
    </div>
    <div class="fields">
      <label class="field-card">
        <span class="field-label">Код</span>
        <input data-city-key="code" readonly type="text" value="${escapeHtml(city.code || "")}" autocomplete="off">
      </label>
      <label class="field-card">
        <span class="field-label">Название</span>
        <input data-city-key="label" readonly type="text" value="${escapeHtml(city.label || "")}" autocomplete="off">
      </label>
      <label class="field-card">
        <span class="field-label">Приоритет</span>
        <input data-city-key="priority" type="number" value="${escapeHtml(city.priority ?? 0)}" autocomplete="off">
      </label>
      <label class="field-card field-card-toggle">
        <div class="field-copy">
          <span class="field-label">Активен</span>
          <small class="field-help">Неактивный город не показывается пользователям для выбора записи.</small>
        </div>
        <div class="toggle">
          <input data-city-key="active" disabled type="checkbox" ${city.active ? "checked" : ""}>
        </div>
      </label>
      <label class="field-card">
        <span class="field-label">ID консульства</span>
        <input data-city-key="facilityId" readonly type="text" value="${escapeHtml(city.facilityId || "")}" autocomplete="off">
      </label>
      <label class="field-card">
        <span class="field-label">Страница записи</span>
        <input data-city-key="appointmentUrl" type="text" value="${escapeHtml(city.appointmentUrl || "")}" autocomplete="off">
      </label>
      <label class="field-card">
        <span class="field-label">API дат</span>
        <input data-city-key="datesApiUrl" type="text" value="${escapeHtml(city.datesApiUrl || "")}" autocomplete="off">
      </label>
      <label class="field-card">
        <span class="field-label">API времени</span>
        <input data-city-key="timesApiUrl" type="text" value="${escapeHtml(city.timesApiUrl || "")}" autocomplete="off">
      </label>
    </div>
  </article>`;
}

function renderCities(cities) {
  state.cities = Array.isArray(cities) ? cities : [];
  $("#citiesList").innerHTML = state.cities.length
    ? state.cities.map(cityCardTemplate).join("")
    : '<div class="empty compact">Города пока не заведены.</div>';

  $$("[data-remove-city]").forEach((button) => {
    button.addEventListener("click", () => {
      const code = button.dataset.removeCity;
      state.cities = state.cities.filter((city) => city.code !== code);
      renderCities(state.cities);
    });
  });
}

function collectCities() {
  return $$("[data-city-row]").map((row) => ({
    code: row.querySelector('[data-city-key="code"]')?.value || "",
    label: row.querySelector('[data-city-key="label"]')?.value || "",
    priority: Number(row.querySelector('[data-city-key="priority"]')?.value || 0),
    active: Boolean(row.querySelector('[data-city-key="active"]')?.checked),
    facilityId: row.querySelector('[data-city-key="facilityId"]')?.value || "",
    appointmentUrl: row.querySelector('[data-city-key="appointmentUrl"]')?.value || "",
    datesApiUrl: row.querySelector('[data-city-key="datesApiUrl"]')?.value || "",
    timesApiUrl: row.querySelector('[data-city-key="timesApiUrl"]')?.value || "",
  }));
}

async function saveCities(event) {
  event.preventDefault();
  $("#citiesStatus").textContent = "Сохраняю...";
  const response = await fetch("/api/cities", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cities: collectCities() }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    $("#citiesStatus").textContent = data.error || "Не удалось сохранить города";
    return;
  }
  $("#citiesStatus").textContent = "Сохранено";
  renderCities(data.cities || []);
  await loadStatus({ keepSettings: true });
}

async function loadDebugArtifacts() {
  const response = await fetch("/api/debug-artifacts");
  if (response.status === 401 || response.status === 403) return;
  if (!response.ok) {
    renderDebugArtifacts(null);
    return;
  }
  renderDebugArtifacts(await response.json());
}

async function clearErrors() {
  const response = await fetch("/api/errors/clear", { method: "POST" });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    alert(data.error || "Не удалось очистить ошибки");
    return;
  }
  await loadStatus({ keepSettings: true });
}

async function deleteDebugArtifact(artifactId) {
  if (!artifactId) return;
  const response = await fetch("/api/debug-artifacts/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ artifactId }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    alert(data.error || "Не удалось удалить артефакт");
    return;
  }
  await loadDebugArtifacts();
}

async function clearDebugArtifacts() {
  const response = await fetch("/api/debug-artifacts/delete-all", { method: "POST" });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    alert(data.error || "Не удалось удалить артефакты");
    return;
  }
  await loadDebugArtifacts();
}

async function checkAuth() {
  const response = await fetch("/api/auth/me");
  const data = await response.json();
  if (!data.user) {
    setAppVisible(false);
    return false;
  }
  setAppVisible(true);
  renderUser(data.user, data.cityOptions);
  activateTab("results");
  await loadStatus({ reason: "startup" });
  return true;
}

function renderAuthMode() {
  const isRegister = state.authMode === "register";
  $("#authTitle").textContent = isRegister ? "Регистрация" : "Вход";
  $("#authSubmitBtn").textContent = isRegister ? "Создать аккаунт" : "Войти";
  $("#authModeBtn").textContent = isRegister ? "Уже есть аккаунт" : "Создать аккаунт";
  $("#authPassword").autocomplete = isRegister ? "new-password" : "current-password";
  $("#termsSection").hidden = !isRegister;
  $("#termsAccepted").checked = false;
  $("#authStatus").textContent = "";
  $("#authStatus").classList.remove("error-text");
}

async function submitAuth(event) {
  event.preventDefault();
  if (state.authMode === "register" && !$("#termsAccepted").checked) {
    $("#authStatus").classList.add("error-text");
    $("#authStatus").textContent = "Для регистрации нужно согласиться с правилами использования сервиса";
    return;
  }
  $("#authStatus").classList.remove("error-text");
  $("#authStatus").textContent = state.authMode === "register" ? "Создаю аккаунт..." : "Вхожу...";
  const response = await fetch(state.authMode === "register" ? "/api/auth/register" : "/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: $("#authEmail").value,
      password: $("#authPassword").value,
      termsAccepted: $("#termsAccepted").checked,
    }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    $("#authStatus").classList.add("error-text");
    $("#authStatus").textContent = data.error || "Не удалось войти";
    return;
  }
  setAppVisible(true);
  renderUser(data.user, data.cityOptions);
  activateTab("results");
  await loadStatus({ reason: "login" });
  ensurePolling();
}

async function demoAccess() {
  $("#authStatus").classList.remove("error-text");
  $("#authStatus").textContent = "Открываю демо...";
  const response = await fetch("/api/auth/demo", { method: "POST" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    $("#authStatus").classList.add("error-text");
    $("#authStatus").textContent = data.error || "Не удалось открыть демо";
    return;
  }
  setAppVisible(true);
  renderUser(data.user, data.cityOptions);
  activateTab("results");
  await loadStatus({ reason: "login" });
  ensurePolling();
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" });
  state.user = null;
  stopPolling();
  setAppVisible(false);
}

function renderIgnoredDates(dates) {
  void dates;
  state.ignoredPeriods = Array.isArray(state.user?.profile?.ignoredPeriods) ? state.user.profile.ignoredPeriods : [];
  state.priorityPeriods = Array.isArray(state.user?.profile?.priorityPeriods) ? state.user.profile.priorityPeriods : [];
  if ($("#ignorePreparationDays")) {
    $("#ignorePreparationDays").value = String(Math.max(1, Number(state.user?.profile?.preparationDays ?? 1) || 1));
  }
  renderPreparationHint();
  const ranges = state.ignoredPeriods;
  $("#ignoredDateList").innerHTML = ranges.length
    ? ranges
        .map((range) => {
          const label =
            range.start === range.end
              ? range.start
              : `${range.start} - ${range.end}`;
          return `<div class="date-chip-wrap">
            <button class="date-chip" data-edit-range-start="${escapeHtml(range.start)}" data-edit-range-end="${escapeHtml(range.end)}" type="button">
              <span>${escapeHtml(label)}</span>
            </button>
            <button class="date-chip-delete" data-delete-range-start="${escapeHtml(range.start)}" data-delete-range-end="${escapeHtml(range.end)}" type="button">×</button>
          </div>`;
        })
        .join("")
    : '<div class="empty compact">Игнор-лист пуст.</div>';

  $$("[data-edit-range-start]").forEach((button) => {
    button.addEventListener("click", () => {
      beginIgnoreEdit(button.dataset.editRangeStart, button.dataset.editRangeEnd);
    });
  });
  $$("[data-delete-range-start]").forEach((button) => {
    button.addEventListener("click", async () => {
      const start = button.dataset.deleteRangeStart;
      const end = button.dataset.deleteRangeEnd;
      const label = start === end ? start : `${start} - ${end}`;
      if (!(await confirmPeriodDeletion(label))) return;
      if (
        state.editingIgnoreRange &&
        state.editingIgnoreRange.start === start &&
        state.editingIgnoreRange.end === end
      ) {
        clearIgnoreEdit();
      }
      saveIgnoredDates(
        state.ignoredPeriods.filter((item) => !(item.start === start && item.end === end)),
        state.priorityPeriods,
        "Игнор-лист сохранен",
        "#ignoreStatus",
      );
    });
  });

  const priorityRanges = state.priorityPeriods;
  $("#priorityDateList").innerHTML = priorityRanges.length
    ? priorityRanges
        .map((range) => {
          const label =
            range.start === range.end
              ? range.start
              : `${range.start} - ${range.end}`;
          return `<div class="date-chip-wrap">
            <button class="date-chip priority-chip" data-edit-priority-range-start="${escapeHtml(range.start)}" data-edit-priority-range-end="${escapeHtml(range.end)}" type="button">
              <span>${escapeHtml(label)}</span>
            </button>
            <button class="date-chip-delete" data-delete-priority-range-start="${escapeHtml(range.start)}" data-delete-priority-range-end="${escapeHtml(range.end)}" type="button">×</button>
          </div>`;
        })
        .join("")
    : '<div class="empty compact">Приоритетный список пуст.</div>';

  $$("[data-edit-priority-range-start]").forEach((button) => {
    button.addEventListener("click", () => {
      beginPriorityEdit(button.dataset.editPriorityRangeStart, button.dataset.editPriorityRangeEnd);
    });
  });
  $$("[data-delete-priority-range-start]").forEach((button) => {
    button.addEventListener("click", async () => {
      const start = button.dataset.deletePriorityRangeStart;
      const end = button.dataset.deletePriorityRangeEnd;
      const label = start === end ? start : `${start} - ${end}`;
      if (!(await confirmPeriodDeletion(label))) return;
      if (
        state.editingPriorityRange &&
        state.editingPriorityRange.start === start &&
        state.editingPriorityRange.end === end
      ) {
        clearPriorityEdit();
      }
      saveIgnoredDates(
        state.ignoredPeriods,
        state.priorityPeriods.filter((item) => !(item.start === start && item.end === end)),
        "Приоритетный список сохранен",
        "#priorityStatus",
      );
    });
  });

  updateIgnoreEditMode();
  updatePriorityEditMode();
}

function parseDateInput(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return null;
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day));
}

function formatDateInput(date) {
  return date.toISOString().slice(0, 10);
}

function formatLocalDateInput(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function datesBetween(startValue, endValue) {
  const start = parseDateInput(startValue);
  const end = parseDateInput(endValue);
  if (!start || !end || start > end) return [];
  const dates = [];
  const cursor = new Date(start);
  while (cursor <= end) {
    dates.push(formatDateInput(cursor));
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return dates;
}

function uniquePeriods(periods) {
  const seen = new Set();
  return periods.filter((period) => {
    if (!period?.start || !period?.end) return false;
    const key = `${period.start}|${period.end}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function expandPeriodsToDates(periods) {
  return [...new Set((periods || []).flatMap((period) => datesBetween(period.start, period.end)))].sort();
}

function currentMinDate() {
  const today = new Date();
  today.setDate(today.getDate() + 1);
  const year = today.getFullYear();
  const month = String(today.getMonth() + 1).padStart(2, "0");
  const day = String(today.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function applyDateBounds(input) {
  if (!input) return;
  input.min = currentMinDate();
  input.max = MAX_ALLOWED_DATE;
}

function periodFromInputs(startSelector, endSelector) {
  const startInput = $(startSelector);
  const endInput = $(endSelector);
  const start = startInput.value;
  const end = endInput.value;
  if (!start || !end) return null;
  if (start < startInput.min || end < endInput.min || start > startInput.max || end > endInput.max) return null;
  if (start > end) return null;
  return { start, end };
}

function focusDateInput(input) {
  if (!input) return;
  input.focus();
  if (typeof input.showPicker === "function") {
    try {
      input.showPicker();
      return;
    } catch {}
  }
  input.click();
}

function renderPreparationHint() {
  const target = $("#ignorePreparationHint");
  if (!target) return;
  const raw = Number($("#ignorePreparationDays")?.value ?? state.user?.profile?.preparationDays ?? 1);
  const effectiveDays = Number.isFinite(raw) ? Math.max(1, raw) : 1;
  const base = new Date();
  base.setHours(0, 0, 0, 0);
  base.setDate(base.getDate() + effectiveDays);
  const iso = formatLocalDateInput(base);
  target.textContent = `Сейчас автозапись разрешена начиная с ${iso}`;
}

function confirmPeriodDeletion(label) {
  return confirmDeletion({
    title: "Удалить период?",
    message: `Удалить период ${label}?`,
  });
}

function confirmDeletion({ title, message }) {
  return new Promise((resolve) => {
    const dialog = $("#confirmDialog");
    const heading = $("#confirmDialogTitle");
    const text = $("#confirmDialogText");
    const accept = $("#confirmDialogAccept");
    const cancel = $("#confirmDialogCancel");

    heading.textContent = title;
    text.textContent = message;

    const cleanup = (result) => {
      accept.removeEventListener("click", onAccept);
      cancel.removeEventListener("click", onCancel);
      dialog.removeEventListener("cancel", onCancel);
      if (dialog.open) dialog.close();
      resolve(result);
    };

    const onAccept = () => cleanup(true);
    const onCancel = (event) => {
      event?.preventDefault?.();
      cleanup(false);
    };

    accept.addEventListener("click", onAccept, { once: true });
    cancel.addEventListener("click", onCancel, { once: true });
    dialog.addEventListener("cancel", onCancel, { once: true });
    dialog.showModal();
  });
}

function updateIgnoreEditMode() {
  const isEditing = Boolean(state.editingIgnoreRange);
  $("#ignorePeriodSubmitBtn").textContent = isEditing ? "Сохранить период" : "Добавить период";
  $("#ignorePeriodForm").classList.toggle("editing", isEditing);
}

function updatePriorityEditMode() {
  const isEditing = Boolean(state.editingPriorityRange);
  $("#priorityPeriodSubmitBtn").textContent = isEditing ? "Сохранить период" : "Добавить период";
  $("#priorityPeriodForm").classList.toggle("editing", isEditing);
}

function clearIgnoreEdit() {
  state.editingIgnoreRange = null;
  $("#ignorePeriodStart").value = "";
  $("#ignorePeriodEnd").value = "";
  updateIgnoreEditMode();
}

function clearPriorityEdit() {
  state.editingPriorityRange = null;
  $("#priorityPeriodStart").value = "";
  $("#priorityPeriodEnd").value = "";
  updatePriorityEditMode();
}

function beginIgnoreEdit(start, end) {
  state.editingIgnoreRange = { start, end };
  $("#ignorePeriodStart").value = start;
  $("#ignorePeriodEnd").value = end;
  updateIgnoreEditMode();
  $("#ignoreStatus").textContent = "Редактируется выбранный период.";
}

function beginPriorityEdit(start, end) {
  state.editingPriorityRange = { start, end };
  $("#priorityPeriodStart").value = start;
  $("#priorityPeriodEnd").value = end;
  updatePriorityEditMode();
  $("#priorityStatus").textContent = "Редактируется выбранный период.";
}

function logLineUser(line) {
  const value = String(line || "");
  const prefixMatch = value.match(/^\[([^\]]+)\]/);
  if (prefixMatch) return prefixMatch[1];
  const startMatch = value.match(/^24\/7 monitor user (?:start|skipped): ([^ ]+)/);
  return startMatch ? startMatch[1] : "";
}

function logUsers(lines) {
  return [...new Set((lines || []).map(logLineUser).filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

function updateLogUserFilter(lines) {
  const select = $("#logUserFilter");
  if (!select) return;
  const users = logUsers(lines);
  state.logUserFilter = users.includes(state.logUserFilter) ? state.logUserFilter : "all";
  select.innerHTML = [
    '<option value="all">Все пользователи</option>',
    ...users.map((user) => `<option value="${escapeHtml(user)}">${escapeHtml(user)}</option>`),
  ].join("");
  select.value = state.logUserFilter;
  select.disabled = users.length === 0;
}

function filteredLogLines(lines) {
  if (state.logUserFilter === "all") return lines || [];
  return (lines || []).filter((line) => logLineUser(line) === state.logUserFilter);
}

function renderLogLine(line) {
  const value = String(line || "");
  const match = value.match(/^(\[[^\]]+\]\s*)?TIMING\s+utc=\S+\s+msk=(\S+)\s+(.*)$/);
  if (!match) return value;
  const userPrefix = match[1] || "";
  const eventTime = formatLogEventTime(match[2]);
  const rest = match[3] || "";
  return state.showLogEventTime
    ? `${userPrefix}[${eventTime}] ${rest}`
    : `${userPrefix}${rest}`;
}

function renderedLogLines(lines) {
  return filteredLogLines(lines).map(renderLogLine);
}

function renderProcess(process, monitor) {
  state.lastProcess = process || {};
  state.lastMonitor = monitor || {};
  const logs = $("#logs");
  const previousScrollTop = logs.scrollTop;
  const wasPinnedToBottom =
    logs.scrollHeight - logs.clientHeight - logs.scrollTop <= 32;

  const running = Boolean(process.running);
  const monitorEnabled = Boolean(monitor?.active || monitor?.task_alive || monitor?.auto_paused);
  $("#runCheckBtn").disabled = running;
  $("#runLoginBtn").disabled = running;
  $("#monitorToggleBtn").disabled = process.kind === "login" && running;
  $("#monitorToggleBtn").textContent = monitorEnabled ? "Стоп 24/7" : "Старт 24/7";
  $("#monitorToggleBtn").classList.toggle("danger-button", monitorEnabled);

  const name = process.kind === "login" ? "браузерный логин" : process.kind === "check" ? "проверка" : "процесс";
  if (running) {
    $("#processState").textContent = `Выполняется ${name}, старт: ${formatDate(process.started_at)}`;
  } else if (monitor?.auto_paused && monitor?.paused_until) {
    $("#processState").textContent = `Фоновый цикл на паузе до ${formatDateWithSeconds(monitor.paused_until)}`;
  } else if (monitor?.task_alive) {
    $("#processState").textContent = "Фоновый цикл 24/7 активен";
  } else if (process.started_at) {
    $("#processState").textContent = `${name} завершен с кодом ${process.returncode}, финиш: ${formatDate(process.finished_at)}`;
  } else {
    $("#processState").textContent = "Нет активного запуска";
  }
  const sleep = monitor?.preventing_sleep ? "Mac не засыпает" : "удержание сна выключено";
  const iterations = monitor?.iterations ?? 0;
  $("#monitorDetails").textContent = monitor?.active
    ? `Фоновый цикл активен, запусков: ${iterations}, интервал: ${monitor.interval_seconds} сек., ${sleep}`
    : `Фоновый цикл остановлен, запусков: ${iterations}`;
  const rawLogs = process.logs || [];
  updateLogUserFilter(rawLogs);
  logs.textContent = renderedLogLines(rawLogs).join("\n");
  if (activeTabName() === "logs" && wasPinnedToBottom) {
    scrollLogsToBottom();
  } else if (activeTabName() === "logs") {
    requestAnimationFrame(() => {
      logs.scrollTop = previousScrollTop;
    });
  }
}

function scrollLogsToBottom() {
  const logs = $("#logs");
  requestAnimationFrame(() => {
    logs.scrollTop = logs.scrollHeight;
  });
}

async function loadStatus({ keepSettings = false, reason = "manual" } = {}) {
  if (reason === "poll" && !shouldAutoRefreshTab()) {
    stopPolling();
    return null;
  }
  const range = $("#rangeHours").value || "24";
  const response = await fetch(`/api/status?range_hours=${encodeURIComponent(range)}`);
  if (response.status === 401) {
    setAppVisible(false);
    return null;
  }
  const data = await response.json();
  state.lastPayload = data;
  const tab = activeTabName();

  if (reason !== "poll") {
    renderUser(data.user, data.cityOptions);
  }

  if (reason !== "poll" || tab === "results") {
    renderMetrics(data.state);
    renderCurrentWindows(data.state);
    renderLastCheck(data.state);
  }

  if (reason !== "poll" || tab === "logs") {
    renderProcess(data.process, data.monitor);
  }

  if (reason !== "poll" || tab === "errors") {
    renderErrors(data.state);
  }

  if (reason !== "poll" || tab === "ignore") {
    renderIgnoredDates(data.state.ignoredDates);
  }

  if (tab === "debug" && canObserveRuns()) {
    await loadDebugArtifacts();
  }
  if (!keepSettings) renderSettings(data.settings);
  if ((reason !== "poll" || tab === "cities") && data.cities) renderCities(data.cities);
  return data;
}

async function loadCompactStatus() {
  if (!shouldAutoRefreshTab("results")) return null;
  const range = $("#rangeHours").value || "24";
  const response = await fetch(`/api/status/compact?range_hours=${encodeURIComponent(range)}`);
  if (response.status === 401) {
    setAppVisible(false);
    return null;
  }
  const data = await response.json();
  state.lastPayload = {
    ...(state.lastPayload || {}),
    summary: data.summary,
    state: {
      ...(state.lastPayload?.state || {}),
      ...(data.state || {}),
    },
  };
  renderMetrics(data.state || {});
  renderCurrentWindows(data.state || {});
  renderLastCheck(data.state || {});
  return data;
}

async function loadProcessStatus() {
  if (!canObserveRuns()) return null;
  const response = await fetch("/api/process-status");
  if (response.status === 401) {
    setAppVisible(false);
    return null;
  }
  if (response.status === 403) {
    return null;
  }
  const data = await response.json();
  state.lastPayload = {
    ...(state.lastPayload || {}),
    process: data.process || {},
    monitor: data.monitor || {},
  };
  renderProcess(data.process || {}, data.monitor || {});
  return data;
}

function collectSettings() {
  const settings = {};
  $$("[data-key]").forEach((input) => {
    settings[input.dataset.key] = input.type === "checkbox" ? input.checked : input.value;
  });
  return settings;
}

async function saveSettings(event) {
  event.preventDefault();
  $("#saveStatus").textContent = "Сохраняю...";
  const response = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: collectSettings() }),
  });
  if (!response.ok) {
    $("#saveStatus").textContent = "Не удалось сохранить";
    return;
  }
  $("#saveStatus").textContent = "Сохранено";
  await loadStatus();
}

async function saveIgnoredDates(
  periods = state.ignoredPeriods,
  priorityPeriods = state.priorityPeriods,
  successMessage = "Игнор-лист сохранен",
  statusTarget = "#ignoreStatus",
) {
  $(statusTarget).textContent = "Сохраняю...";
  const raw = Number($("#ignorePreparationDays")?.value ?? state.user?.profile?.preparationDays ?? 1);
  const preparationDays = Number.isFinite(raw) ? Math.max(1, raw) : 1;
  const normalizedPeriods = uniquePeriods(periods);
  const normalizedPriorityPeriods = uniquePeriods(priorityPeriods);
  const dates = expandPeriodsToDates(normalizedPeriods);
  const priorityDates = expandPeriodsToDates(normalizedPriorityPeriods);
  const response = await fetch("/api/ignored-dates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      dates,
      priorityDates,
      periods: normalizedPeriods,
      priorityPeriods: normalizedPriorityPeriods,
      preparationDays,
    }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    $(statusTarget).textContent = statusTarget === "#priorityStatus" ? "Не удалось сохранить приоритетный список" : "Не удалось сохранить игнор-лист";
    return;
  }
  if (data.profile && state.user) {
    state.user.profile = data.profile;
  }
  clearIgnoreEdit();
  clearPriorityEdit();
  $(statusTarget).textContent = successMessage;
  await loadStatus({ keepSettings: true });
}

async function startAction(path) {
  const response = await fetch(path, { method: "POST" });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    alert(data.error || "Не удалось запустить процесс");
    return;
  }
  activateTab("logs");
  await loadStatus({ keepSettings: true });
}

async function toggleMonitor() {
  const active = $("#monitorToggleBtn").classList.contains("danger-button");
  await startAction(active ? "/api/monitor/stop" : "/api/monitor/start");
}

async function stopCurrentProcess() {
  const response = await fetch("/api/stop-process", { method: "POST" });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    alert(data.error || "Не удалось остановить процесс");
    return;
  }
  await loadStatus({ keepSettings: true });
}

async function copyLogs() {
  const logsText = $("#logs").textContent || "";
  if (!logsText.trim()) {
    alert("Лог пока пустой");
    return;
  }
  try {
    await navigator.clipboard.writeText(logsText);
  } catch {
    const helper = document.createElement("textarea");
    helper.value = logsText;
    helper.setAttribute("readonly", "readonly");
    helper.style.position = "absolute";
    helper.style.left = "-9999px";
    document.body.appendChild(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
  }
}

async function resetNextCheck() {
  const response = await fetch("/api/monitor/reset-next-check", { method: "POST" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert(data.error || "Не удалось сбросить время следующей проверки");
    return;
  }
  await loadStatus({ keepSettings: true });
}

function activateTab(name) {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  $$(".panel").forEach((panel) => panel.classList.remove("active"));
  $(`#${name}Panel`).classList.add("active");
  ensurePolling();
  if (name === "results") {
    loadCompactStatus();
  } else if (name === "logs") {
    loadProcessStatus();
  } else if (!shouldAutoRefreshTab(name)) {
    loadStatus({ keepSettings: true });
  }
  if (name === "users") loadUsers();
  if (name === "cities") loadStatus({ keepSettings: true });
  if (name === "debug" && canObserveRuns()) loadDebugArtifacts();
  if (name === "logs") scrollLogsToBottom();
}

function wireEvents() {
  applyTheme(localStorage.getItem(THEME_STORAGE_KEY) || "light");
  state.showLogEventTime = localStorage.getItem(LOG_EVENT_TIME_STORAGE_KEY) !== "0";
  $("#logEventTimeToggle").checked = state.showLogEventTime;
  $("#authForm").addEventListener("submit", submitAuth);
  $("#demoAccessBtn").addEventListener("click", demoAccess);
  $("#openTermsBtn").addEventListener("click", () => $("#termsDialog").showModal());
  $("#authOpenTermsBtn").addEventListener("click", () => $("#termsDialog").showModal());
  $("#footerOpenTermsBtn").addEventListener("click", () => $("#termsDialog").showModal());
  $("#authOpenOnboardingBtn").addEventListener("click", () => $("#onboardingDialog").showModal());
  $("#openOnboardingBtn").addEventListener("click", () => startTour({ force: true, markSeenOnClose: true }));
  $("#instructionStartTourBtn").addEventListener("click", () => startTour({ force: true, markSeenOnClose: true }));
  $("#startTourBtn").addEventListener("click", () => startTour({ force: true, markSeenOnClose: true }));
  $("#closeTermsBtn").addEventListener("click", () => $("#termsDialog").close());
  $("#closeOnboardingBtn").addEventListener("click", () => {
    markOnboardingSeen();
    $("#onboardingDialog").close();
  });
  $("#tourNextBtn").addEventListener("click", nextTourStep);
  $("#tourPrevBtn").addEventListener("click", previousTourStep);
  $("#tourSkipBtn").addEventListener("click", () => closeTour({ markSeen: true }));
  $("#tourOverlay").addEventListener("click", (event) => {
    if (event.target.id === "tourOverlay" || event.target.classList.contains("tour-scrim")) {
      closeTour({ markSeen: true });
    }
  });
  window.addEventListener("resize", positionTourCard);
  window.addEventListener("scroll", positionTourCard, true);
  window.addEventListener("keydown", handleTourKeydown);
  $("#authModeBtn").addEventListener("click", () => {
    state.authMode = state.authMode === "login" ? "register" : "login";
    renderAuthMode();
  });
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));
  $("#refreshBtn").addEventListener("click", () => loadStatus({ keepSettings: true }));
  $("#logoutBtn").addEventListener("click", logout);
  $("#runCheckBtn").addEventListener("click", () => startAction("/api/run-check"));
  $("#runLoginBtn").addEventListener("click", () => startAction("/api/run-login"));
  $("#monitorToggleBtn").addEventListener("click", toggleMonitor);
  $("#themeToggleBtn").addEventListener("click", toggleTheme);
  $("#logUserFilter").addEventListener("change", () => {
    state.logUserFilter = $("#logUserFilter").value || "all";
    renderProcess(state.lastProcess || {}, state.lastMonitor || {});
  });
  $("#logEventTimeToggle").addEventListener("change", () => {
    state.showLogEventTime = $("#logEventTimeToggle").checked;
    localStorage.setItem(LOG_EVENT_TIME_STORAGE_KEY, state.showLogEventTime ? "1" : "0");
    renderProcess(state.lastProcess || {}, state.lastMonitor || {});
  });
  $("#copyLogsBtn").addEventListener("click", copyLogs);
  $("#resetNextCheckBtn").addEventListener("click", resetNextCheck);
  $("#stopProcessBtn").addEventListener("click", stopCurrentProcess);
  $("#clearErrorsBtn").addEventListener("click", clearErrors);
  $("#clearDebugArtifactsBtn").addEventListener("click", clearDebugArtifacts);
  $("#citiesForm").addEventListener("submit", saveCities);
  $("#rangeHours").addEventListener("change", () => {
    loadStatus({ keepSettings: true });
  });
  $("#ignorePeriodForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const period = periodFromInputs("#ignorePeriodStart", "#ignorePeriodEnd");
    if (!period) {
      $("#ignoreStatus").textContent = "Проверь даты периода";
      return;
    }
    const nextPeriods = uniquePeriods(state.editingIgnoreRange
      ? [...state.ignoredPeriods.filter((item) => !(item.start === state.editingIgnoreRange.start && item.end === state.editingIgnoreRange.end)), period]
      : [...state.ignoredPeriods, period]);
    saveIgnoredDates(
      nextPeriods,
      state.priorityPeriods,
      "Игнор-лист сохранен",
      "#ignoreStatus",
    );
  });
  $("#priorityPeriodForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const period = periodFromInputs("#priorityPeriodStart", "#priorityPeriodEnd");
    if (!period) {
      $("#priorityStatus").textContent = "Проверь даты приоритетного периода";
      return;
    }
    const nextPriorityPeriods = uniquePeriods(state.editingPriorityRange
      ? [...state.priorityPeriods.filter((item) => !(item.start === state.editingPriorityRange.start && item.end === state.editingPriorityRange.end)), period]
      : [...state.priorityPeriods, period]);
    saveIgnoredDates(
      state.ignoredPeriods,
      nextPriorityPeriods,
      "Приоритетный список сохранен",
      "#priorityStatus",
    );
  });
  if ($("#ignorePreparationDays")) {
    $("#ignorePreparationDays").addEventListener("change", () => {
      if (Number($("#ignorePreparationDays").value) < 1) {
        $("#ignorePreparationDays").value = "1";
      }
      renderPreparationHint();
      saveIgnoredDates(state.ignoredPeriods, state.priorityPeriods, "Игнор-лист сохранен", "#ignoreStatus");
    });
  }
  [
    "#ignorePeriodStart",
    "#ignorePeriodEnd",
    "#priorityPeriodStart",
    "#priorityPeriodEnd",
    "#userSettingsIgnoreStart",
    "#userSettingsIgnoreEnd",
    "#userSettingsPriorityStart",
    "#userSettingsPriorityEnd",
  ].forEach((selector) => {
    const input = $(selector);
    applyDateBounds(input);
  });
  $("#userSettingsBackBtn").addEventListener("click", closeUserSettings);
  $("#userSettingsAddIgnoreBtn").addEventListener("click", () => addUserSettingsPeriod("ignore"));
  $("#userSettingsAddPriorityBtn").addEventListener("click", () => addUserSettingsPeriod("priority"));
  $("#userSettingsSaveBtn").addEventListener("click", saveUserSettings);
  $("#settingsForm").addEventListener("submit", saveSettings);
  $("#profileForm").addEventListener("submit", saveProfile);
}

wireEvents();
renderAuthMode();
checkAuth().then((ok) => {
  if (ok) ensurePolling();
});
