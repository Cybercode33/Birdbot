const landingView = document.getElementById("landing-view");
const portalChoiceView = document.getElementById("portal-choice-view");
const portalChoiceGrid = document.getElementById("portal-choice-grid");
const portalChoiceTitle = document.getElementById("portal-choice-title");
const portalChoiceCopy = document.getElementById("portal-choice-copy");
const profileView = document.getElementById("profile-view");
const profilePortalContent = document.getElementById("profile-portal-content");
const dashboardView = document.getElementById("dashboard-view");
const welcomeMessage = document.getElementById("welcome-message");
const botNotice = document.getElementById("bot-notice");
const actionFeedback = document.getElementById("action-feedback");
const serverList = document.getElementById("server-list");
const selectorModal = document.getElementById("selector-modal");
const confirmModal = document.getElementById("confirm-modal");
const membersModal = document.getElementById("members-modal");
const actionModal = document.getElementById("action-modal");
const patchModal = document.getElementById("patch-modal");
const patchButton = document.getElementById("patch-button");
const actionModalTitle = document.getElementById("action-modal-title");
const actionModalCopy = document.getElementById("action-modal-copy");
const actionModalFields = document.getElementById("action-modal-fields");
const actionModalError = document.getElementById("action-modal-error");
const actionModalCancel = document.getElementById("action-modal-cancel");
const actionModalConfirm = document.getElementById("action-modal-confirm");
const membersModalContent = document.getElementById("members-modal-content");
const membersModalTitle = document.getElementById("members-title");
const membersModalCopy = document.getElementById("members-copy");
const selectorList = document.getElementById("selector-list");
const selectedServerName = document.getElementById("selected-server-name");
const selectedServerStatus = document.getElementById("selected-server-status");
const confirmationCopy = document.getElementById("confirmation-copy");
const activationError = document.getElementById("activation-error");
const confirmActivation = document.getElementById("confirm-activation");
const loadingIndicator = document.getElementById("loading-indicator");
const loadingMessage = document.getElementById("loading-message");
const authArea = document.getElementById("auth-area");
const dashboardEntry = document.getElementById("dashboard-entry");
const backButton = document.getElementById("back-button");
const managementView = document.getElementById("management-view");
const managedServerCard = document.getElementById("managed-server-card");
const managementTitle = document.getElementById("management-title");
const managementDescription = document.getElementById("management-description");
const showTicketsButton = document.getElementById("show-tickets-button") || document.createElement("button");
const ticketLogsButton = document.getElementById("ticket-logs-button") || document.createElement("button");
const commandGrid = document.getElementById("command-grid");
const commandFeedback = document.getElementById("command-feedback");

let dashboardData = null;
let selectedGuild = null;
let pendingAction = "enable";
let pendingFeedback = "";
let pendingRequests = 0;
let pendingBlockingRequests = 0;
const REQUEST_TIMEOUT_MS = 20_000;
const GET_REQUEST_ATTEMPTS = 2;
let managementData = null;
let membersPanelGuild = null;
let membersPanelRecords = [];
let membersSearchTimer = null;
let currentUser = null;
let ticketPageMode = "config";
let activeManagementTab = "commands";
let actionModalResolver = null;
let ticketCountdownTimer = null;
let ticketRefreshInFlight = false;
let ticketLogsRefreshInFlight = false;
let ticketLogsQuery = "";
let ticketListSnapshot = null;
let ticketLogsSnapshot = null;
let guildLogsRefreshInFlight = false;
let guildLogsQuery = "";
let guildLogsSnapshot = null;
let ticketServerClockOffsetMs = 0;
let tempVCRefreshTimer = null;
let tempVCRefreshInFlight = false;
let tempVCRefreshPending = false;
// Responses fetched by the readiness gate are reused by the first render so
// navigation never performs the same Discord/Spotify request twice.
const preloadCache = new Map();
// Several panels can ask for the same resource while a navigation or tab
// switch is still settling. Share that in-flight promise instead of opening
// duplicate HTTP connections and making the UI wait on redundant work.
const requestInFlight = new Map();

function textElement(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}

function transcriptLink(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  // Old ticket rows may contain the previous Render/Replit hostname. Keep
  // the generated filename but serve it from the host currently displaying
  // the dashboard, so moving hosts does not break existing transcripts.
  try {
    const parsed = new URL(raw, window.location.origin);
    const match = parsed.pathname.match(/^\/uploads\/transcripts\/([A-Za-z0-9._-]+\.html)$/i);
    if (match) return "/uploads/transcripts/" + encodeURIComponent(match[1]);
  } catch (_) {
    // Fall through to the original value for malformed legacy records.
  }
  return raw;
}

function formatTicketCountdown(totalSeconds) {
  const seconds = Math.max(0, Math.ceil(Number(totalSeconds) || 0));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function updateTicketCountdowns() {
  const countdowns = commandGrid.querySelectorAll("[data-timeout-deadline]");
  if (!countdowns.length) {
    window.clearInterval(ticketCountdownTimer);
    ticketCountdownTimer = null;
    return;
  }
  const now = Date.now() + ticketServerClockOffsetMs;
  countdowns.forEach((element) => {
    const deadline = Number(element.dataset.timeoutDeadline);
    const remaining = deadline - now;
    if (!Number.isFinite(deadline) || remaining <= 0) {
      element.textContent = "Timeout in: 00:00";
      element.classList.add("is-expired");
      return;
    }
    element.textContent = `Timeout in: ${formatTicketCountdown(remaining / 1000)}`;
    element.classList.remove("is-expired");
  });
}

function startTicketCountdowns() {
  window.clearInterval(ticketCountdownTimer);
  ticketCountdownTimer = null;
  updateTicketCountdowns();
  if (commandGrid.querySelector("[data-timeout-deadline]")) {
    ticketCountdownTimer = window.setInterval(updateTicketCountdowns, 1_000);
  }
}

function renderAccount(user) {
  currentUser = user;
  authArea.replaceChildren();
  authArea.hidden = !user;
  if (!user) return;
  if (user.avatar) {
    const avatar = document.createElement("img");
    avatar.src = user.avatar;
    avatar.alt = "";
    avatar.decoding = "async";
    authArea.append(avatar);
  }
  authArea.append(textElement("span", "account-name", user.name));
  const signOut = document.createElement("button");
  signOut.className = "signout-button";
  signOut.type = "button";
  signOut.textContent = "Sign out";
  signOut.addEventListener("click", async () => {
    signOut.disabled = true;
    beginLoading("Signing out...");
    try {
      await requestJson("/logout", { method: "POST" });
      window.location.assign("/");
    } catch (error) {
      actionFeedback.hidden = false;
      actionFeedback.textContent = errorMessage(error, "Could not sign out. Please try again.");
    } finally {
      endLoading();
      signOut.disabled = false;
    }
  });
  authArea.append(signOut);
}

function setDashboardEntry(signedIn) {
  dashboardEntry.replaceChildren(document.createTextNode(signedIn ? "Start " : "Open dashboard "));
  const arrow = document.createElement("span");
  arrow.setAttribute("aria-hidden", "true");
  arrow.textContent = "→";
  dashboardEntry.append(arrow);
  dashboardEntry.href = signedIn ? "/?portal=1" : "/dashboard";
}

function renderPortalCard(title, description, href, className) {
  const card = document.createElement("a");
  card.className = `portal-choice-card${className ? ` ${className}` : ""}`;
  card.href = href;
  card.append(textElement("strong", "portal-choice-card-title", title), textElement("span", "portal-choice-card-copy", description), textElement("span", "portal-choice-card-link", "Open portal ->"));
  return card;
}

function renderPortalChoice(notice = "") {
  landingView.hidden = true;
  dashboardView.hidden = true;
  managementView.hidden = true;
  if (profileView) profileView.hidden = true;
  portalChoiceView.hidden = false;
  backButton.hidden = true;
  portalChoiceTitle.textContent = "Where do you want to go?";
  portalChoiceCopy.textContent = notice || "Choose server management or review your BirdBot activity profile.";
  portalChoiceGrid.replaceChildren(
    renderPortalCard("The Dashboard", "Manage servers, tickets, commands, logs, and settings as an owner or Administrator.", "/?dashboard=1", "portal-dashboard-card"),
    renderPortalCard("Profile", "Review your messages, shared images, voice time, and the servers where you are most active.", "/?profile=1", "portal-profile-card"),
  );
}

function profileNumber(value) {
  return Number(value || 0).toLocaleString();
}

function profileVoiceHours(seconds) {
  const hours = Number(seconds || 0) / 3600;
  if (!Number.isFinite(hours) || hours <= 0) return "0 h";
  return `${hours < 10 ? hours.toFixed(1) : Math.round(hours).toLocaleString()} h`;
}

function renderProfileStat(label, value, hint, className = "") {
  const card = document.createElement("article");
  card.className = `member-profile-stat${className ? ` ${className}` : ""}`;
  card.append(textElement("span", "member-profile-stat-label", label), textElement("strong", "member-profile-stat-value", value), textElement("small", "member-profile-stat-hint", hint));
  return card;
}

function renderMemberProfile(data) {
  if (!profilePortalContent) return;
  const user = data?.user || currentUser || {};
  const stats = data?.stats || {};
  const servers = Array.isArray(data?.servers) ? data.servers : [];
  const mostActive = data?.most_active_server || null;
  profilePortalContent.replaceChildren();

  const shell = document.createElement("div");
  shell.className = "member-profile-shell";
  const heading = document.createElement("div");
  heading.className = "member-profile-heading";
  const profileTitle = textElement("h2", "member-profile-title", `${user.name || "Your"} activity`);
  profileTitle.id = "profile-title";
  heading.append(
    textElement("p", "eyebrow", "Member profile"),
    profileTitle,
    textElement("p", "member-profile-copy", "A clear snapshot of the activity BirdBot has recorded across your shared servers."),
  );

  const hero = document.createElement("section");
  hero.className = "member-profile-hero";
  const avatar = document.createElement("div");
  avatar.className = "member-profile-avatar";
  if (user.avatar) {
    const image = document.createElement("img");
    image.src = user.avatar;
    image.alt = "";
    image.loading = "eager";
    image.decoding = "async";
    image.addEventListener("error", () => {
      avatar.replaceChildren(textElement("span", "member-profile-avatar-fallback", String(user.name || "B").charAt(0).toUpperCase()));
    });
    avatar.append(image);
  } else {
    avatar.append(textElement("span", "member-profile-avatar-fallback", String(user.name || "B").charAt(0).toUpperCase()));
  }
  const identity = document.createElement("div");
  identity.className = "member-profile-identity";
  identity.append(
    textElement("h3", "member-profile-name", user.name || "Discord member"),
    textElement("p", "member-profile-id", user.id ? `Discord ID · ${user.id}` : "Discord member"),
  );
  const serverBadge = textElement("span", "member-profile-server-badge", `${profileNumber(stats.server_count)} server${Number(stats.server_count || 0) === 1 ? "" : "s"}`);
  hero.append(avatar, identity, serverBadge);

  const statGrid = document.createElement("div");
  statGrid.className = "member-profile-stat-grid";
  statGrid.append(
    renderProfileStat("Messages sent", profileNumber(stats.messages), "Human messages recorded"),
    renderProfileStat("Images shared", profileNumber(stats.images), "Image attachments shared"),
    renderProfileStat("Voice time", profileVoiceHours(stats.voice_seconds), "Time spent in voice calls"),
    renderProfileStat("Most active server", mostActive?.name || "No activity yet", mostActive ? `${profileNumber(mostActive.messages)} messages here` : "Activity appears as you chat", "member-profile-stat-featured"),
  );

  const serverSection = document.createElement("section");
  serverSection.className = "member-profile-servers";
  const serverHeader = document.createElement("div");
  serverHeader.className = "member-profile-section-heading";
  serverHeader.append(textElement("h3", "", "Activity by server"), textElement("span", "", `${profileNumber(servers.length)} tracked`));
  const list = document.createElement("div");
  list.className = "member-profile-server-list";
  if (!servers.length) {
    list.append(textElement("p", "member-profile-empty", "No activity has been recorded yet. Start chatting in a shared server and this profile will update automatically."));
  } else {
    const maxMessages = Math.max(1, ...servers.map((server) => Number(server.messages || 0)));
    servers.forEach((server, index) => {
      const row = document.createElement("article");
      row.className = "member-profile-server-row";
      const rank = textElement("span", "member-profile-rank", `#${index + 1}`);
      const icon = document.createElement("div");
      icon.className = "member-profile-server-icon";
      if (server.icon_url) {
        const image = document.createElement("img");
        image.src = server.icon_url;
        image.alt = "";
        image.loading = "lazy";
        image.decoding = "async";
        image.addEventListener("error", () => image.replaceWith(textElement("span", "member-profile-server-fallback", String(server.name || "?").charAt(0).toUpperCase())));
        icon.append(image);
      } else {
        icon.append(textElement("span", "member-profile-server-fallback", String(server.name || "?").charAt(0).toUpperCase()));
      }
      const copy = document.createElement("div");
      copy.className = "member-profile-server-copy";
      copy.append(textElement("strong", "", server.name || "Unknown server"), textElement("small", "", `${profileNumber(server.messages)} messages · ${profileNumber(server.images)} images · ${profileVoiceHours(server.voice_seconds)} voice`));
      const bar = document.createElement("div");
      bar.className = "member-profile-activity-bar";
      const fill = document.createElement("span");
      fill.style.width = `${Math.min(100, Math.max(4, (Number(server.messages || 0) / maxMessages) * 100))}%`;
      bar.append(fill);
      copy.append(bar);
      row.append(rank, icon, copy);
      list.append(row);
    });
  }
  serverSection.append(serverHeader, list);

  const back = textElement("a", "secondary-button member-profile-back", "Back to portal");
  back.href = "/?portal=1";
  shell.append(heading, hero, statGrid, serverSection, back);
  profilePortalContent.append(shell);
}

async function loadProfilePortal() {
  beginLoading("Loading your Profile...");
  try {
    const data = await requestJson("/api/profile", { cache: "no-store" });
    renderAccount(data.user || currentUser);
    landingView.hidden = true;
    dashboardView.hidden = true;
    managementView.hidden = true;
    portalChoiceView.hidden = true;
    if (profileView) profileView.hidden = false;
    backButton.hidden = false;
    backButton.href = "/?portal=1";
    backButton.textContent = "Back";
    renderMemberProfile(data);
  } finally {
    endLoading();
  }
}

function resolveActionModal(result = null) {
  const resolver = actionModalResolver;
  actionModalResolver = null;
  if (actionModal) actionModal.hidden = true;
  if (resolver) resolver(result);
}

function closeModals() {
  selectorModal.hidden = true;
  confirmModal.hidden = true;
  if (membersModal) membersModal.hidden = true;
  if (patchModal) patchModal.hidden = true;
  if (patchButton) patchButton.setAttribute("aria-expanded", "false");
  window.clearTimeout(membersSearchTimer);
  membersSearchTimer = null;
  resolveActionModal(null);
  activationError.textContent = "";
}

if (patchButton && patchModal) {
  patchButton.addEventListener("click", () => {
    closeModals();
    patchModal.hidden = false;
    patchButton.setAttribute("aria-expanded", "true");
    window.requestAnimationFrame(() => patchModal.querySelector(".close-button")?.focus());
  });
}

function bindDurationPicker(amountInput, unitSelect) {
  if (!amountInput || !unitSelect) return;
  const maximums = { m: 40320, h: 672, d: 28, w: 4 };
  const sync = () => {
    const maximum = maximums[unitSelect.value] || maximums.m;
    amountInput.max = String(maximum);
    amountInput.title = `Maximum ${maximum} ${unitSelect.value === "m" ? "minutes" : unitSelect.value === "h" ? "hours" : unitSelect.value === "d" ? "days" : "weeks"}`;
    if (Number(amountInput.value) > maximum) amountInput.value = String(maximum);
  };
  unitSelect.addEventListener("change", sync);
  sync();
}

function openActionModal({ title, copy, confirmLabel = "Continue", danger = false, includeDuration = false, includeReason = true } = {}) {
  if (!actionModal || !actionModalTitle || !actionModalCopy || !actionModalFields || !actionModalConfirm) return Promise.resolve(null);
  actionModalTitle.textContent = title || "Confirm action";
  actionModalCopy.textContent = copy || "Please review this action before continuing.";
  actionModalFields.replaceChildren();
  actionModalError.textContent = "";
  let durationInput = null;
  let durationUnit = null;
  let reasonInput = null;
  if (includeDuration) {
    const field = document.createElement("label");
    field.className = "action-modal-field";
    field.append(textElement("span", "action-modal-label", "Timeout duration"));
    const durationPicker = document.createElement("div");
    durationPicker.className = "action-modal-duration";
    durationInput = document.createElement("input");
    durationInput.type = "number";
    durationInput.min = "1";
    durationInput.max = "40320";
    durationInput.step = "1";
    durationInput.value = "10";
    durationInput.className = "action-modal-input";
    durationInput.dataset.actionDuration = "true";
    durationInput.setAttribute("aria-label", "Timeout duration amount");
    durationUnit = document.createElement("select");
    durationUnit.className = "action-modal-input action-modal-duration-unit";
    durationUnit.dataset.actionDurationUnit = "true";
    durationUnit.append(new Option("Minutes", "m"), new Option("Hours", "h"), new Option("Days", "d"), new Option("Weeks", "w"));
    durationUnit.setAttribute("aria-label", "Timeout duration unit");
    bindDurationPicker(durationInput, durationUnit);
    durationPicker.append(durationInput, durationUnit);
    field.append(durationPicker, textElement("small", "action-modal-hint", "Maximum 28 days. Choose minutes, hours, days, or weeks."));
    actionModalFields.append(field);
  }
  if (includeReason) {
    const field = document.createElement("label");
    field.className = "action-modal-field";
    field.append(textElement("span", "action-modal-label", "Reason (optional)"));
    reasonInput = document.createElement("textarea");
    reasonInput.className = "action-modal-input action-modal-reason";
    reasonInput.rows = 3;
    reasonInput.maxLength = 512;
    reasonInput.placeholder = "Add a short reason for the moderation log";
    reasonInput.dataset.actionReason = "true";
    field.append(reasonInput);
    actionModalFields.append(field);
  }
  actionModalConfirm.className = danger ? "danger-button" : "primary-button";
  actionModalConfirm.textContent = confirmLabel;
  actionModal.hidden = false;
  const focusTarget = durationInput || reasonInput || actionModalConfirm;
  window.requestAnimationFrame(() => focusTarget.focus());
  return new Promise((resolve) => {
    actionModalResolver = resolve;
  });
}

function submitActionModal() {
  if (!actionModal || actionModal.hidden) return;
  const durationInput = actionModalFields?.querySelector("[data-action-duration]");
  const durationUnit = actionModalFields?.querySelector("[data-action-duration-unit]");
  const reasonInput = actionModalFields?.querySelector("[data-action-reason]");
  const durationAmount = durationInput ? Number(durationInput.value) : null;
  const durationMultipliers = { m: 1, h: 60, d: 1440, w: 10080 };
  const selectedUnit = durationUnit?.value || "m";
  const duration = durationInput ? durationAmount * (durationMultipliers[selectedUnit] || 1) : null;
  if (durationInput && (!Number.isInteger(durationAmount) || durationAmount < 1 || !Number.isInteger(duration) || duration > 40320)) {
    actionModalError.textContent = "Choose a valid timeout between 1 minute and 28 days.";
    durationInput.focus();
    return;
  }
  resolveActionModal({ duration, durationAmount, durationUnit: selectedUnit, reason: reasonInput ? reasonInput.value.trim().slice(0, 512) : "" });
}

function beginLoading(message, blocking = true) {
  pendingRequests += 1;
  if (blocking) pendingBlockingRequests += 1;
  loadingMessage.textContent = message;
  // Read-only page loads render skeletons in place and stay interactive. Only
  // mutating requests need the full-screen interaction lock.
  loadingIndicator.hidden = pendingBlockingRequests === 0;
  document.body.classList.toggle("is-loading", pendingBlockingRequests > 0);
}

function endLoading(blocking = true) {
  pendingRequests = Math.max(0, pendingRequests - 1);
  if (blocking) pendingBlockingRequests = Math.max(0, pendingBlockingRequests - 1);
  if (!pendingBlockingRequests) {
    loadingIndicator.hidden = true;
    document.body.classList.remove("is-loading");
  }
}

function setConfirmButtonLoading(isLoading, label) {
  confirmActivation.disabled = isLoading;
  confirmActivation.setAttribute("aria-busy", String(isLoading));
  confirmActivation.replaceChildren();
  if (isLoading) {
    const spinner = document.createElement("span");
    spinner.className = "button-spinner";
    spinner.setAttribute("aria-hidden", "true");
    confirmActivation.append(spinner, document.createTextNode(label));
    return;
  }
  confirmActivation.textContent = label;
}

function errorMessage(error, fallback = "Something went wrong. Please try again.") {
  return error instanceof Error && error.message ? error.message : fallback;
}

function showWorkspaceError(message) {
  const text = String(message || "Something went wrong. Please try again.");
  if (!managementView.hidden) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = text;
    return;
  }
  if (!dashboardView.hidden) {
    botNotice.hidden = false;
    botNotice.textContent = text;
    return;
  }
  if (!portalChoiceView.hidden) {
    portalChoiceCopy.textContent = text;
  }
}

function waitFor(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function canRetryReadRequest(error) {
  const status = Number(error?.status || 0);
  return Boolean(error?.retryable) || error instanceof TypeError || [429, 502, 503, 504].includes(status);
}

async function requestJsonUncoalesced(url, options = {}) {
  const { skipPreload = false, ...fetchOptions } = options || {};
  const method = String(fetchOptions.method || "GET").toUpperCase();
  // Any mutation can invalidate a preloaded dashboard/profile snapshot (for
  // example a newly enabled guild or a changed playback state).
  if (method !== "GET") preloadCache.clear();
  if (!skipPreload && method === "GET" && preloadCache.has(url)) {
    return preloadCache.get(url);
  }
  const attempts = method === "GET" ? GET_REQUEST_ATTEMPTS : 1;
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(url, { ...fetchOptions, signal: controller.signal });
      const body = await response.json().catch(() => ({}));
      if (response.status === 401) {
        window.location.assign("/");
        throw new Error("Your session has expired. Please sign in again.");
      }
      if (!response.ok) {
        const failure = new Error(typeof body.detail === "string" ? body.detail : "The request could not be completed.");
        failure.status = response.status;
        failure.retryable = [429, 502, 503, 504].includes(response.status);
        throw failure;
      }
      return body;
    } catch (error) {
      const failure = error instanceof DOMException && error.name === "AbortError"
        ? new Error("The request took too long. Check your connection and try again.")
        : error;
      lastError = failure;
      // Only idempotent reads are retried.  Actions such as enabling a
      // server, moderating a user, or posting a panel are never replayed.
      if (attempt + 1 < attempts && canRetryReadRequest(failure)) {
        await waitFor(300 * (attempt + 1));
        continue;
      }
      throw failure;
    } finally {
      window.clearTimeout(timer);
    }
  }
  throw lastError || new Error("The request could not be completed.");
}

function requestJson(url, options = {}) {
  const method = String(options?.method || "GET").toUpperCase();
  if (method !== "GET") return requestJsonUncoalesced(url, options);
  const skipPreload = Boolean(options?.skipPreload);
  const key = `${url}::${skipPreload ? "fresh" : "cached"}`;
  const existing = requestInFlight.get(key);
  if (existing) return existing;
  const pending = requestJsonUncoalesced(url, options);
  requestInFlight.set(key, pending);
  pending.then(() => {
    if (requestInFlight.get(key) === pending) requestInFlight.delete(key);
  }, () => {
    if (requestInFlight.get(key) === pending) requestInFlight.delete(key);
  });
  return pending;
}

async function preloadCriticalData() {
  loadingMessage.textContent = "Preparing your BirdBot workspace...";
  const session = await requestJson("/api/session", { cache: "no-store", skipPreload: true });
  preloadCache.set("/api/session", session);
  if (!session.authenticated) return { session };

  const parameters = new URLSearchParams(window.location.search);
  const guildId = parameters.get("guild");
  const urls = [];
  const requiredUrls = new Set();
  const isProfile = parameters.get("profile") === "1" || parameters.get("music") === "1";
  const isDashboard = parameters.get("dashboard") === "1";
  if (isProfile) {
    urls.push("/api/profile");
    requiredUrls.add("/api/profile");
  } else if (isDashboard || guildId) {
    urls.push("/api/dashboard");
    requiredUrls.add("/api/dashboard");
    if (guildId) {
      const encodedGuild = encodeURIComponent(guildId);
      // Management pages need these payloads before controls become
      // interactive. They are independent and intentionally load together.
      const manageUrl = `/api/guilds/${encodedGuild}/manage`;
      // The management payload contains everything needed to render the
      // first Commands tab. Ticket and Games panels fetch their own
      // data when opened, keeping the initial navigation fast and avoiding
      // requests for panels the user may never visit.
      urls.push(manageUrl);
      requiredUrls.add(manageUrl);
    }
  }
  if (!urls.length) return { session };
  loadingMessage.textContent = `Loading ${urls.length} workspace resources...`;
  const results = await Promise.allSettled(urls.map((url) => requestJson(url, { cache: "no-store", skipPreload: true })));
  const failures = [];
  results.forEach((result, index) => {
    const url = urls[index];
    if (result.status === "fulfilled") {
      preloadCache.set(url, result.value);
    } else if (requiredUrls.has(url)) {
      failures.push(result.reason);
    }
  });
  if (failures.length) throw failures[0];
  loadingMessage.textContent = "Workspace ready.";
  return { session };
}

function renderGuilds(guilds) {
  serverList.replaceChildren();
  if (!guilds.length) {
    serverList.append(textElement("p", "empty-state", "No shared servers you can manage were found."));
    return;
  }
  guilds.forEach((guild) => {
    const card = document.createElement("article");
    card.className = "server-card";
    const identity = document.createElement("div");
    identity.className = "server-identity";
    const fallback = textElement("span", "server-icon-fallback", guild.name.trim().charAt(0).toUpperCase() || "?");
    if (guild.icon_url) {
      const icon = document.createElement("img");
      icon.className = "server-icon";
      icon.src = guild.icon_url;
      icon.alt = "";
      icon.addEventListener("error", () => icon.replaceWith(fallback));
      identity.append(icon);
    } else {
      identity.append(fallback);
    }
    const title = textElement("h3", "server-name", guild.name);
    const meta = textElement("p", "server-meta", `${guild.members.toLocaleString()} members · BirdBot online`);
    const serverText = document.createElement("div");
    serverText.append(title, meta);
    identity.append(serverText);
    const active = Boolean(guild.activated);
    const canConfigure = guild.can_configure !== false;
    const status = textElement("p", active ? "status active" : "status", active ? "● Bot Active" : "○ Bot Disabled");
    if (!active && !canConfigure) status.textContent = "◌ Awaiting owner activation";
    const actions = document.createElement("div");
    actions.className = "server-actions";
    if (canConfigure) {
      const stateButton = document.createElement("button");
      stateButton.className = active ? "danger-button" : "primary-button";
      stateButton.type = "button";
      stateButton.textContent = active ? "Stop Bot" : "Enable Bot";
      stateButton.addEventListener("click", () => openConfirmation(guild, active ? "disable" : "enable"));
      actions.append(stateButton);
    }
    // Keep the member action in the same far-right action area for every
    // server.  An inactive server cannot be queried or moderated until the
    // owner enables BirdBot, so show the affordance but make that state clear.
    const membersButton = document.createElement("button");
    membersButton.className = "secondary-button members-button";
    membersButton.type = "button";
    membersButton.textContent = "Show Members";
    membersButton.disabled = !active;
    if (active) {
      membersButton.addEventListener("click", () => { void showMembers(guild); });
    } else {
      membersButton.title = "Enable BirdBot first";
    }
    actions.append(membersButton);
    if (active) {
      const manageButton = document.createElement("button");
      manageButton.className = "secondary-button";
      manageButton.type = "button";
      manageButton.textContent = "Manage";
      manageButton.addEventListener("click", async () => {
        if (pendingBlockingRequests) return;
        manageButton.disabled = true;
        try {
          await loadManagement(guild.id);
          const url = `/?dashboard=1&guild=${encodeURIComponent(guild.id)}`;
          window.history.pushState({ dashboard: true, guild: guild.id }, "", url);
        } catch (error) {
          actionFeedback.hidden = false;
          actionFeedback.textContent = errorMessage(error, "Server management could not be loaded.");
        } finally {
          manageButton.disabled = false;
        }
      });
      actions.append(manageButton);
    }
    card.append(identity, status, actions);
    serverList.append(card);
  });
}

function memberDisplayName(member) {
  return String(member?.display_name || member?.global_name || member?.username || member?.member_id || "Unknown member");
}

function memberJoinedLabel(member) {
  if (!member?.joined_at) return "Joined date unavailable";
  const parsed = new Date(member.joined_at);
  return Number.isNaN(parsed.getTime()) ? "Joined date unavailable" : "Joined " + parsed.toLocaleString();
}

function renderMembersPanel(query = "") {
  if (!membersModalContent) return;
  const normalized = String(query || "").trim().toLowerCase();
  const visible = membersPanelRecords.filter((member) => !normalized || [
    memberDisplayName(member), member.username, member.global_name, member.member_id,
    ...(Array.isArray(member.roles) ? member.roles : []),
  ].filter(Boolean).join(" ").toLowerCase().includes(normalized));
  membersModalContent.replaceChildren();

  const toolbar = document.createElement("div");
  toolbar.className = "members-toolbar";
  const search = document.createElement("input");
  search.type = "search";
  search.className = "channel-select members-search";
  search.placeholder = "Search members by name or ID";
  search.value = query;
   search.addEventListener("input", () => {
     window.clearTimeout(membersSearchTimer);
     const nextQuery = search.value;
     membersSearchTimer = window.setTimeout(() => {
       membersSearchTimer = null;
       renderMembersPanel(nextQuery);
     }, 120);
   });
  const count = textElement("span", "members-count", visible.length + " of " + membersPanelRecords.length + " members");
  toolbar.append(search, count);

  const bulk = document.createElement("div");
  bulk.className = "members-bulk-actions";
  [["kick", "Kick All", "danger-button"], ["ban", "Ban All", "danger-button"], ["timeout", "Timeout All", "secondary-button"]].forEach(([action, label, className]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", () => { void runMembersAction(action, null, true); });
    bulk.append(button);
  });
  toolbar.append(bulk);

  const list = document.createElement("div");
  list.className = "members-list";
  if (!visible.length) list.append(textElement("p", "empty-state", normalized ? "No matching members." : "No members were returned."));
   const rows = document.createDocumentFragment();
   visible.forEach((member) => {
    const row = document.createElement("article");
    row.className = "member-row";
    const identity = document.createElement("div");
    identity.className = "member-row-identity";
    if (member.avatar_url) {
      const avatar = document.createElement("img");
      avatar.className = "member-row-avatar";
      avatar.src = member.avatar_url;
      avatar.alt = "";
      avatar.loading = "lazy";
      avatar.decoding = "async";
      avatar.addEventListener("error", () => avatar.remove(), { once: true });
      identity.append(avatar);
    } else {
      identity.append(textElement("span", "member-row-avatar member-row-fallback", memberDisplayName(member).charAt(0).toUpperCase() || "?"));
    }
    const details = document.createElement("div");
    details.className = "member-row-details";
    details.append(
      textElement("strong", "", memberDisplayName(member)),
      textElement("small", "", "@" + (member.username || member.global_name || "unknown") + " · ID: " + member.member_id),
      textElement("small", "", memberJoinedLabel(member)),
      textElement("small", "", "Roles: " + (Array.isArray(member.roles) && member.roles.length ? member.roles.join(", ") : "None") + (member.is_bot ? " · Bot account" : "")),
    );
    identity.append(details);
    const actions = document.createElement("div");
    actions.className = "member-row-actions";
    [["kick", "Kick", "danger-button"], ["ban", "Ban", "danger-button"], ["timeout", "Timeout", "secondary-button"]].forEach(([action, label, className]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = className;
      button.textContent = label;
      button.disabled = Boolean(member.is_bot);
      button.title = member.is_bot ? "Bot accounts are protected" : "";
      button.addEventListener("click", () => { void runMembersAction(action, member, false); });
      actions.append(button);
    });
    row.append(identity, actions);
     rows.append(row);
   });
   list.append(rows);
   membersModalContent.append(toolbar, list);
}

async function runMembersAction(action, member = null, bulk = false) {
  if (!membersPanelGuild || !membersPanelContext?.channels?.length) {
    actionFeedback.hidden = false;
    actionFeedback.textContent = "BirdBot cannot access a text channel for this action.";
    return;
  }
  const candidates = (bulk ? membersPanelRecords : [member]).filter((item) => item && !item.is_bot);
  if (!candidates.length) return;
  const actionName = String(action || "action").charAt(0).toUpperCase() + String(action || "action").slice(1);
  const label = bulk ? actionName + " all eligible members" : actionName + " " + memberDisplayName(candidates[0]);
  const confirmation = await openActionModal({
    title: bulk ? actionName + " all eligible members" : "Confirm " + actionName,
    copy: "You are about to " + label.toLowerCase() + ". Discord permissions and role hierarchy still apply.",
    confirmLabel: bulk ? "Queue actions" : actionName,
    danger: action === "kick" || action === "ban",
    includeDuration: action === "timeout",
    includeReason: true,
  });
  if (!confirmation) return;
  let reason = "Dashboard member action";
  membersPanelActionDuration = 10;
  membersPanelActionDurationAmount = 10;
  membersPanelActionDurationUnit = "m";
  if (action === "timeout") {
    membersPanelActionDuration = confirmation.duration || 10;
    membersPanelActionDurationAmount = confirmation.durationAmount || 10;
    membersPanelActionDurationUnit = confirmation.durationUnit || "m";
  }
  if (confirmation.reason) reason = confirmation.reason;
  if (candidates.length > 500) {
    const limited = await openActionModal({
      title: "Large action queue",
      copy: "Only the first 500 eligible members will be queued to stay within Discord rate limits.",
      confirmLabel: "Queue first 500",
      danger: action === "kick" || action === "ban",
      includeReason: false,
    });
    if (!limited) return;
  }
  const targets = candidates.slice(0, 500);
  beginLoading("Queuing " + action + " action" + (bulk ? "s" : "") + "...");
  try {
    const channelId = String(membersPanelContext.channels[0].id);
    const queued = await Promise.all(targets.map((target) => requestJson(
      "/api/guilds/" + encodeURIComponent(membersPanelGuild.id) + "/commands/" + action,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          channel_id: channelId,
          member_id: target.member_id,
          reason,
          silent: true,
          duration_minutes: membersPanelActionDuration,
          duration_amount: action === "timeout" ? membersPanelActionDurationAmount : undefined,
          duration_unit: action === "timeout" ? membersPanelActionDurationUnit : undefined,
        }),
      },
    )));
    if (bulk) {
      const queuedMessage = action + " queued for " + queued.length + " eligible member" + (queued.length === 1 ? "" : "s") + ". Discord will process the actions in order and the moderation log will record each result.";
      if (membersModalCopy) membersModalCopy.textContent = queuedMessage;
      actionFeedback.hidden = false;
      actionFeedback.textContent = queuedMessage;
      return;
    }
    const results = await Promise.all(queued.map((request) => waitForDashboardCommand(request.request_id, 80).catch((error) => ({ status: "failed", error: errorMessage(error, "Action failed.") }))));
    const succeeded = results.filter((result) => result.status === "complete").length;
    const failed = results.length - succeeded;
    if (membersModalCopy) membersModalCopy.textContent = action + " completed for " + succeeded + " member" + (succeeded === 1 ? "" : "s") + (failed ? "; " + failed + " could not be processed." : ".");
    actionFeedback.hidden = false;
    actionFeedback.textContent = action + " completed for " + succeeded + " member" + (succeeded === 1 ? "" : "s") + (failed ? "; " + failed + " could not be processed." : ".");
    const refreshed = await requestJson("/api/guilds/" + encodeURIComponent(membersPanelGuild.id) + "/members", { cache: "no-store" });
    membersPanelRecords = refreshed.members || [];
    renderMembersPanel("");
  } catch (error) {
    if (membersModalCopy) membersModalCopy.textContent = errorMessage(error, "The member action could not be queued.");
    actionFeedback.hidden = false;
    actionFeedback.textContent = errorMessage(error, "The member action could not be queued.");
  } finally {
    endLoading();
  }
}

let membersPanelContext = null;
let membersPanelActionDuration = 10;
let membersPanelActionDurationAmount = 10;
let membersPanelActionDurationUnit = "m";

async function showMembers(guild) {
  if (!guild?.id || !membersModal) return;
  beginLoading("Loading server members...");
  try {
    const [management, roster] = await Promise.all([
      requestJson("/api/guilds/" + encodeURIComponent(guild.id) + "/manage", { cache: "no-store" }),
      requestJson("/api/guilds/" + encodeURIComponent(guild.id) + "/members", { cache: "no-store" }),
    ]);
    membersPanelGuild = guild;
    membersPanelContext = management;
    membersPanelRecords = roster.members || [];
    // Keep the English section label separate from RTL server names so mixed
    // Arabic/English titles remain readable in the members dialog.
    membersModalTitle.textContent = "Members — " + guild.name;
    membersModalCopy.textContent = "Review member details or run a moderation action. Bot accounts are protected; Discord permissions and role hierarchy are enforced.";
    renderMembersPanel("");
    membersModal.hidden = false;
  } catch (error) {
    actionFeedback.hidden = false;
    actionFeedback.textContent = errorMessage(error, "Server members could not be loaded.");
  } finally {
    endLoading();
  }
}

function renderDashboard(data) {
  dashboardData = data;
  renderAccount(data.user);
  backButton.hidden = false;
  backButton.href = "/";
  backButton.textContent = "Back";
  landingView.hidden = true;
  portalChoiceView.hidden = true;
  if (profileView) profileView.hidden = true;
  dashboardView.hidden = false;
  managementView.hidden = true;
  welcomeMessage.textContent = `Welcome, ${data.user.name}`;
  botNotice.hidden = data.bot_online;
  botNotice.textContent = "BirdBot is not currently connected. Server changes will be available when the global bot is online.";
  actionFeedback.hidden = !pendingFeedback;
  actionFeedback.textContent = pendingFeedback;
  pendingFeedback = "";
  renderGuilds(data.guilds);
}

function renderManagedServer(guild) {
  managedServerCard.replaceChildren();
  const fallback = textElement("span", "managed-server-icon server-icon-fallback", guild.name.trim().charAt(0).toUpperCase() || "?");
  if (guild.icon_url) {
    const icon = document.createElement("img");
    icon.className = "managed-server-icon server-icon";
    icon.src = guild.icon_url;
    icon.alt = "";
    icon.decoding = "async";
    icon.addEventListener("error", () => icon.replaceWith(fallback));
    managedServerCard.append(icon);
  } else {
    managedServerCard.append(fallback);
  }
  const text = document.createElement("div");
  text.append(textElement("strong", "", guild.name), textElement("span", "", "BirdBot active"));
  managedServerCard.append(text);
}

function renderControlPanel() {
  stopTempVCRefresh();
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const grid = document.createElement("section");
  grid.className = "control-panel-grid";
  grid.setAttribute("aria-label", "Control Panel tools");

  const channels = Array.isArray(managementData?.channels) ? managementData.channels.length : 0;
  const roles = Array.isArray(managementData?.roles) ? managementData.roles.length : 0;
  const autoReactRules = Array.isArray(managementData?.auto_reacts) ? managementData.auto_reacts.length : 0;
  const tempVCEnabled = Boolean(managementData?.temp_vc?.enabled);
  const vcPremium = managementData?.vc_premium !== false;
  const automodEnabled = Boolean(managementData?.automod?.enabled);
  const automodFeatureCount = ["anti_link", "anti_spam", "banned_words", "raid_protection", "auto_warning", "auto_timeout"]
    .filter((key) => Boolean(managementData?.automod?.[key])).length;
  const aiEnabled = Boolean(managementData?.ai?.enabled);
  const levelEnabled = Boolean(managementData?.level?.enabled);
  const levelStyle = String(managementData?.level?.style || "classic");
  const streakEnabled = Boolean(managementData?.streak?.enabled);
  const cards = [
    ["Server message", "Configure automated server announcements."],
    ["Roles", `${roles.toLocaleString()} roles available to manage.`],
    ["Channels", `${channels.toLocaleString()} channels available to manage.`],
    ["Temp VC", tempVCEnabled ? "Temporary voice rooms are enabled." : "Enable and manage temporary voice rooms."],
    ["DM's Messages", "Configure private messages sent by BirdBot."],
    ["Bot Settings", `${automodEnabled ? `${automodFeatureCount} Automod rule${automodFeatureCount === 1 ? "" : "s"} enabled` : "Automod disabled"} · ${autoReactRules.toLocaleString()} auto-react rule${autoReactRules === 1 ? "" : "s"} · AI ${aiEnabled ? "enabled" : "disabled"}.`],
    ["Level", levelEnabled ? `Leveling enabled (${levelStyle} progression).` : "Reward active members with a customizable leveling system."],
    ["Bot profile", "Customize BirdBot's profile and presence."],
    ["VC", vcPremium ? "Premium feature · place secure, host-configured bots in voice channels." : "Premium feature · unlock voice presence controls."],
    ["Streak", streakEnabled ? "Number streak is active in its configured channel." : "Build an alternating 1, 2, 3… number streak."],
  ];

  cards.forEach(([title, description], index) => {
    const card = document.createElement("article");
    card.className = "control-panel-card";
    if (index === 8) card.classList.add("control-panel-card-premium");
    const isReady = [0, 1, 3, 4, 5, 6, 7, 8, 9].includes(index);
    if (isReady) {
      card.classList.add("control-panel-card-action");
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-label", `Open ${title} manager`);
      const open = () => {
        if (index === 0) return renderServerMessagePanel();
        if (index === 1) return renderRolesPanel();
        if (index === 3) return renderTempVCPanel();
        if (index === 4) return renderDMMessagePanel();
        if (index === 5) return renderBotSettingsPanel();
        if (index === 6) return renderLevelPanel();
        if (index === 7) return renderBotProfilePanel();
        if (index === 9) return renderStreakPanel();
        return vcPremium ? renderVCPresencePanel() : renderVCPremiumPanel();
      };
      card.addEventListener("click", open);
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
    }
    if (index === 8) card.append(textElement("span", "control-panel-card-premium-badge", "Premium feature"));
    card.append(
      textElement("h3", "control-panel-card-title", title),
      textElement("p", "control-panel-card-copy", description),
      textElement("span", "control-panel-card-status", index === 0 ? "Open composer" : index === 1 ? "Manage roles" : index === 3 ? "Configure rooms" : index === 4 ? "Open composer" : index === 5 ? "Configure rules" : index === 6 ? "View leaderboards" : index === 7 ? "Edit profile" : index === 9 ? "Configure game" : vcPremium ? "Premium · Manage voice bots" : "Premium required"),
    );
    const statusLabel = card.querySelector(".control-panel-card-status");
    if (statusLabel) {
      if (index === 6) statusLabel.textContent = "View leaderboards";
      else if (index === 7) statusLabel.textContent = "Edit profile";
      else if (index === 9) statusLabel.textContent = "Configure game";
    }
    grid.append(card);
  });
  commandGrid.append(grid);
}

async function waitForDashboardCommand(requestId, attempts = 40) {
  const id = String(requestId || "");
  if (!id) throw new Error("BirdBot did not return a command request ID.");
  // The bot worker checks its queue continuously. A short poll keeps the
  // dashboard responsive without making the browser wait for a fixed delay.
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    await waitFor(250);
    const state = await requestJson(`/api/command-requests/${encodeURIComponent(id)}`, { cache: "no-store" });
    if (state.status === "complete") return state;
    if (state.status === "failed") throw new Error(state.error || "BirdBot could not send that message.");
  }
  return { status: "pending" };
}

function stopTempVCRefresh() {
  if (tempVCRefreshTimer !== null) {
    window.clearInterval(tempVCRefreshTimer);
    tempVCRefreshTimer = null;
  }
  tempVCRefreshPending = false;
}

async function refreshTempVCPanel() {
  if (document.hidden || managementView.hidden || activeManagementTab !== "control" || !commandGrid.querySelector(".temp-vc-panel") || tempVCRefreshInFlight || pendingRequests > 0) return;
  tempVCRefreshInFlight = true;
  try {
    const fresh = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/temp-vc`, { cache: "no-store", skipPreload: true });
    if (activeManagementTab !== "control" || !commandGrid.querySelector(".temp-vc-panel")) return;
    const previous = JSON.stringify({ config: managementData.temp_vc || {}, channels: managementData.temp_vc_channels || [], voice: managementData.voice_channels || [] });
    const next = JSON.stringify({ config: fresh.config || {}, channels: fresh.channels || [], voice: fresh.voice_channels || [] });
    if (previous === next) return;
    managementData = {
      ...managementData,
      temp_vc: fresh.config || managementData.temp_vc,
      temp_vc_channels: fresh.channels || [],
      voice_channels: fresh.voice_channels || managementData.voice_channels,
      categories: fresh.categories || managementData.categories,
    };
    const activeElement = document.activeElement;
    const panel = commandGrid.querySelector(".temp-vc-panel");
    if (panel && activeElement && panel.contains(activeElement)) {
      tempVCRefreshPending = true;
      return;
    }
    tempVCRefreshPending = false;
    renderTempVCPanel();
  } catch (_) {
    // A transient poll failure should not interrupt an open form. The next
    // interval will retry automatically.
  } finally {
    tempVCRefreshInFlight = false;
  }
}

function startTempVCRefresh() {
  stopTempVCRefresh();
  tempVCRefreshTimer = window.setInterval(() => { void refreshTempVCPanel(); }, 4_000);
}

function renderTempVCPanel() {
  stopTempVCRefresh();
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const panel = document.createElement("section");
  panel.className = "temp-vc-panel";
  panel.setAttribute("aria-labelledby", "temp-vc-title");
  const heading = document.createElement("div");
  heading.className = "temp-vc-heading";
  heading.append(
    textElement("h3", "temp-vc-title", "Temporary voice channels"),
    textElement("p", "temp-vc-copy", "Choose a lobby. BirdBot creates a temporary room when someone joins and removes it when everyone leaves. Active rooms update automatically."),
  );
  const config = managementData?.temp_vc || {};
  const form = document.createElement("form");
  form.className = "temp-vc-config-form";
  form.noValidate = true;
  const enabledField = document.createElement("label");
  enabledField.className = "temp-vc-enable-field";
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = Boolean(config.enabled);
  enabledField.append(enabled, textElement("span", "", "Enable Temp VC"));
  const lobbySelect = document.createElement("select");
  lobbySelect.className = "channel-select";
  lobbySelect.append(new Option("Choose a voice lobby", ""));
  (managementData?.voice_channels || []).forEach((channel) => lobbySelect.append(new Option(`🔊 ${channel.name}`, channel.id)));
  lobbySelect.value = String(config.lobby_channel_id || "");
  const categorySelect = document.createElement("select");
  categorySelect.className = "channel-select";
  categorySelect.append(new Option("Use the lobby category", ""));
  (managementData?.categories || []).forEach((category) => categorySelect.append(new Option(category.name, category.id)));
  categorySelect.value = String(config.category_id || "");
  const templateInput = document.createElement("input");
  templateInput.className = "channel-select";
  templateInput.type = "text";
  templateInput.maxLength = 100;
  templateInput.value = String(config.channel_name_template || "{owner}'s room");
  templateInput.placeholder = "{owner}'s room";
  const limitInput = document.createElement("input");
  limitInput.className = "channel-select";
  limitInput.type = "number";
  limitInput.min = "0";
  limitInput.max = "99";
  limitInput.step = "1";
  limitInput.value = String(config.user_limit ?? 0);
  const save = textElement("button", "primary-button temp-vc-save", "Save Temp VC settings");
  save.type = "submit";
  const configStatus = textElement("span", "temp-vc-status", "");
  form.append(
    enabledField,
    labeledControl("Lobby voice channel", lobbySelect),
    labeledControl("Category (optional)", categorySelect),
    labeledControl("Channel name ({owner} or {username})", templateInput),
    labeledControl("User limit (0 = unlimited)", limitInput),
    save,
    configStatus,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (save.disabled) return;
    save.disabled = true;
    configStatus.textContent = "Saving...";
    configStatus.className = "temp-vc-status is-loading";
    try {
      const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/temp-vc`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: enabled.checked,
          lobby_channel_id: lobbySelect.value || null,
          category_id: categorySelect.value || null,
          channel_name_template: templateInput.value,
          user_limit: limitInput.value,
        }),
      });
      managementData.temp_vc = result.config || managementData.temp_vc;
      managementData.temp_vc_channels = result.channels || managementData.temp_vc_channels || [];
      configStatus.textContent = "Saved. New rooms will use these settings.";
      configStatus.className = "temp-vc-status is-success";
    } catch (error) {
      configStatus.textContent = errorMessage(error, "Temp VC settings could not be saved.");
      configStatus.className = "temp-vc-status is-error";
    } finally {
      save.disabled = false;
    }
  });

  const activeChannels = Array.isArray(managementData?.temp_vc_channels) ? managementData.temp_vc_channels : [];
  const activeHeading = document.createElement("div");
  activeHeading.className = "temp-vc-section-heading";
  const activeMeta = document.createElement("div");
  activeMeta.className = "temp-vc-section-meta";
  activeMeta.append(
    textElement("span", "temp-vc-live-indicator", "Live"),
    textElement("span", "temp-vc-count", `${activeChannels.length} room${activeChannels.length === 1 ? "" : "s"}`),
  );
  activeHeading.append(
    textElement("h4", "", "Active temporary rooms"),
    activeMeta,
  );
  const list = document.createElement("div");
  list.className = "temp-vc-channel-list";
  const queueAction = async (payload, button) => {
    if (button?.disabled) return;
    if (button) {
      button.disabled = true;
      button.dataset.originalLabel = button.textContent || "Working...";
      button.textContent = "Working...";
    }
    beginLoading("Updating temporary voice channel...", true);
    try {
      const queued = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/temp-vc/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await waitForDashboardCommand(queued.request_id);
      if (result.status === "pending") throw new Error("BirdBot is still processing that action. Refresh the panel in a moment.");
      const fresh = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/manage`, { cache: "no-store" });
      managementData = { ...managementData, ...fresh };
      renderTempVCPanel();
    } catch (error) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = errorMessage(error, "Temp VC action failed.");
      if (button) {
        button.disabled = false;
        button.textContent = button.dataset.originalLabel || "Try again";
      }
    } finally {
      endLoading(true);
    }
  };
  if (!activeChannels.length) {
    list.append(textElement("p", "temp-vc-empty", "No temporary rooms are active right now."));
  } else {
    activeChannels.forEach((room) => {
      const card = document.createElement("article");
      card.className = "temp-vc-channel-card";
      const info = document.createElement("div");
      info.className = "temp-vc-channel-info";
      const marker = textElement("span", "temp-vc-channel-marker", "VC");
      marker.setAttribute("aria-hidden", "true");
      const infoCopy = document.createElement("div");
      infoCopy.className = "temp-vc-channel-copy";
      const state = textElement("span", `temp-vc-state ${room.locked ? "is-locked" : "is-open"}`, room.locked ? "Locked" : "Open");
      info.append(
        marker,
        infoCopy,
        state,
      );
      infoCopy.append(
        textElement("strong", "", room.channel_name || "Temporary room"),
        textElement("span", "", `Owner: ${room.owner_name || room.owner_id || "Unknown"} · ${(room.blocked_user_ids || []).length} blocked`),
      );
      const targetPicker = createMemberSelect();
      targetPicker.element.classList.add("temp-vc-target-picker");
      let targetSearchTimer = null;
      targetPicker.search.addEventListener("input", () => {
        window.clearTimeout(targetSearchTimer);
        targetSearchTimer = window.setTimeout(() => searchMembers(targetPicker.search.value, targetPicker.select), 180);
      });
      const actionBar = document.createElement("div");
      actionBar.className = "temp-vc-action-bar";
      const roomControls = document.createElement("div");
      roomControls.className = "temp-vc-room-controls";
      const nameTool = document.createElement("div");
      nameTool.className = "temp-vc-tool";
      nameTool.append(textElement("span", "temp-vc-tool-label", "Room name"));
      const renameInput = document.createElement("input");
      renameInput.className = "channel-select";
      renameInput.maxLength = 100;
      renameInput.placeholder = "New room name";
      const rename = textElement("button", "secondary-button", "Rename");
      rename.type = "button";
      rename.addEventListener("click", () => queueAction({ action: "rename", channel_id: room.channel_id, name: renameInput.value }, rename));
      const nameInline = document.createElement("div");
      nameInline.className = "temp-vc-tool-inline temp-vc-name-inline";
      nameInline.append(renameInput, rename);
      nameTool.append(nameInline);
      const limitTool = document.createElement("div");
      limitTool.className = "temp-vc-tool";
      limitTool.append(textElement("span", "temp-vc-tool-label", "Member limit"));
      const roomLimit = document.createElement("input");
      roomLimit.className = "channel-select temp-vc-limit-input";
      roomLimit.type = "number";
      roomLimit.min = "0";
      roomLimit.max = "99";
      const liveVoiceChannel = (managementData?.voice_channels || []).find((voiceChannel) => String(voiceChannel.id) === String(room.channel_id));
      roomLimit.value = String(liveVoiceChannel?.user_limit ?? 0);
      const setLimit = textElement("button", "secondary-button", "Set limit");
      setLimit.type = "button";
      setLimit.addEventListener("click", () => queueAction({ action: "limit", channel_id: room.channel_id, user_limit: roomLimit.value }, setLimit));
      const limitInline = document.createElement("div");
      limitInline.className = "temp-vc-tool-inline";
      limitInline.append(roomLimit, setLimit);
      limitTool.append(limitInline);
      const lock = textElement("button", "secondary-button", room.locked ? "Unlock" : "Lock");
      lock.type = "button";
      lock.addEventListener("click", () => queueAction({ action: room.locked ? "unlock" : "lock", channel_id: room.channel_id }, lock));
      const lockTool = document.createElement("div");
      lockTool.className = "temp-vc-tool temp-vc-lock-tool";
      lockTool.append(textElement("span", "temp-vc-tool-label", "Access"), lock);
      roomControls.append(nameTool, limitTool, lockTool);
      const memberControls = document.createElement("div");
      memberControls.className = "temp-vc-member-controls";
      const memberHeading = textElement("span", "temp-vc-tool-label", "Member actions");
      memberHeading.classList.add("temp-vc-member-heading");
      memberControls.append(memberHeading, targetPicker.element);
      const block = textElement("button", "secondary-button", "Block");
      block.type = "button";
      block.addEventListener("click", () => queueAction({ action: "block", channel_id: room.channel_id, member_id: targetPicker.select.value }, block));
      const unblock = textElement("button", "secondary-button", "Unblock");
      unblock.type = "button";
      unblock.addEventListener("click", () => queueAction({ action: "unblock", channel_id: room.channel_id, member_id: targetPicker.select.value }, unblock));
      const transfer = textElement("button", "secondary-button", "Transfer owner");
      transfer.type = "button";
      transfer.addEventListener("click", () => queueAction({ action: "transfer", channel_id: room.channel_id, member_id: targetPicker.select.value }, transfer));
      const kick = textElement("button", "secondary-button", "Kick");
      kick.type = "button";
      kick.addEventListener("click", () => queueAction({ action: "kick", channel_id: room.channel_id, member_id: targetPicker.select.value }, kick));
      memberControls.append(block, unblock, transfer, kick);
      actionBar.append(roomControls, memberControls);
      card.append(info, actionBar);
      list.append(card);
    });
  }
  const actions = document.createElement("div");
  actions.className = "temp-vc-footer-actions";
  const back = textElement("button", "secondary-button", "Back to Control Panel");
  back.type = "button";
  back.addEventListener("click", () => renderControlPanel());
  actions.append(back);
  panel.append(heading, form, activeHeading, list, actions);
  panel.addEventListener("focusout", () => {
    window.queueMicrotask(() => {
      if (tempVCRefreshPending && !panel.contains(document.activeElement)) {
        tempVCRefreshPending = false;
        renderTempVCPanel();
      }
    });
  });
  commandGrid.append(panel);
  startTempVCRefresh();
}

function renderVCPremiumPanel() {
  stopTempVCRefresh();
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const panel = document.createElement("section");
  panel.className = "vc-presence-panel vc-premium-panel";
  panel.setAttribute("aria-labelledby", "vc-premium-title");
  const heading = document.createElement("div");
  heading.className = "vc-presence-heading";
  heading.append(
    textElement("span", "vc-presence-slot-label", "Premium feature"),
    textElement("h3", "vc-premium-title", "VC voice presence"),
    textElement("p", "vc-presence-copy", "The VC control panel lets you place secure presence bots in your server's voice channels. It is available on premium accounts."),
  );
  const note = textElement("p", "vc-presence-security", "Ask the BirdBot owner or support team to activate premium access for your Discord account. Bot tokens remain private and are never entered here.");
  const back = textElement("button", "secondary-button", "Back to Control Panel");
  back.type = "button";
  back.addEventListener("click", () => renderControlPanel());
  panel.append(heading, note, back);
  commandGrid.append(panel);
}

function renderVCPresencePanel() {
  stopTempVCRefresh();
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const panel = document.createElement("section");
  panel.className = "vc-presence-panel";
  panel.setAttribute("aria-labelledby", "vc-presence-title");
  const heading = document.createElement("div");
  heading.className = "vc-presence-heading";
  heading.append(
    textElement("h3", "vc-presence-title", "VC"),
    textElement("p", "vc-presence-copy", "Keep up to five dedicated presence bots in voice channels. Each slot is configured by the host and can be placed separately in this server."),
  );
  const security = textElement("p", "vc-presence-security", "Security: bot tokens are never entered, stored, or shown on this website. The host must add VC_BOT_1_TOKEN through VC_BOT_5_TOKEN as private secrets, and invite each bot with Connect and Speak permissions." );
  const status = textElement("p", "vc-presence-load-status", "Loading voice bot status...");
  const list = document.createElement("div");
  list.className = "vc-presence-list";

  const renderSlots = (data) => {
    const slots = Array.isArray(data?.slots) ? data.slots : [];
    const configs = Array.isArray(data?.configs) ? data.configs : [];
    const channels = Array.isArray(data?.voice_channels) ? data.voice_channels : [];
    list.replaceChildren();
    for (let number = 1; number <= 5; number += 1) {
      const slot = slots.find((item) => Number(item.slot) === number) || {};
      const config = configs.find((item) => Number(item.slot) === number) || {};
      const configured = Boolean(slot.configured);
      const card = document.createElement("article");
      card.className = "vc-presence-card";
      const cardHeader = document.createElement("div");
      cardHeader.className = "vc-presence-card-header";
      const botCopy = document.createElement("div");
      botCopy.className = "vc-presence-bot-copy";
      botCopy.append(
        textElement("span", "vc-presence-slot-label", `Slot ${number}`),
        textElement("strong", "", configured ? (slot.bot_name || `Presence bot ${number}`) : "Not configured on host"),
        textElement("small", "", configured
          ? `${slot.online ? "Online" : "Connecting"} · ${Number(slot.guild_count || 0).toLocaleString()} server${Number(slot.guild_count || 0) === 1 ? "" : "s"}`
          : `Add VC_BOT_${number}_TOKEN as a private secret.`),
      );
      const state = textElement("span", `vc-presence-state ${slot.online ? "is-online" : ""}`, configured ? (slot.connected ? "In call" : slot.online ? "Online" : "Offline") : "Setup needed");
      cardHeader.append(botCopy, state);
      const form = document.createElement("form");
      form.className = "vc-presence-form";
      form.noValidate = true;
      const enableLabel = document.createElement("label");
      enableLabel.className = "vc-presence-enable";
      const enable = document.createElement("input");
      enable.type = "checkbox";
      enable.checked = Boolean(slot.enabled || config.enabled);
      enable.disabled = !configured;
      enableLabel.append(enable, textElement("span", "", "Place this bot in a voice channel"));
      const channelSelect = document.createElement("select");
      channelSelect.className = "channel-select";
      channelSelect.append(new Option("Choose a voice channel", ""));
      channels.forEach((channel) => channelSelect.append(new Option(`🔊 ${channel.name}`, channel.id)));
      channelSelect.value = String(slot.channel_id || config.channel_id || "");
      channelSelect.disabled = !configured;
      const save = textElement("button", "primary-button vc-presence-save", enable.checked ? "Save placement" : "Leave voice");
      save.type = "submit";
      save.disabled = !configured;
      const feedback = textElement("span", "vc-presence-feedback", slot.error || (configured ? "" : "This slot is waiting for a private host secret."));
      feedback.classList.toggle("is-error", Boolean(slot.error));
      enable.addEventListener("change", () => { save.textContent = enable.checked ? "Save & join" : "Leave voice"; });
      form.append(enableLabel, labeledControl("Voice channel", channelSelect), save, feedback);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (save.disabled) return;
        save.disabled = true;
        enable.disabled = true;
        channelSelect.disabled = true;
        feedback.className = "vc-presence-feedback is-loading";
        feedback.textContent = "Saving placement...";
        beginLoading("Updating VC presence bot...", true);
        try {
          const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/vc`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ slot: number, enabled: enable.checked, channel_id: channelSelect.value || null }),
          });
          const command = await waitForDashboardCommand(result.request_id);
          if (command.status === "pending") throw new Error("BirdBot is still processing that placement. Refresh status in a moment.");
          const fresh = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/vc`, { cache: "no-store" });
          managementData.vc_presence = fresh.configs || managementData.vc_presence;
          managementData.voice_channels = fresh.voice_channels || managementData.voice_channels;
          status.textContent = "Status updated.";
          renderSlots(fresh);
        } catch (error) {
          feedback.className = "vc-presence-feedback is-error";
          feedback.textContent = errorMessage(error, "VC bot placement could not be saved.");
          save.disabled = false;
          enable.disabled = !configured;
          channelSelect.disabled = !configured;
        } finally {
          endLoading(true);
        }
      });
      card.append(cardHeader, form);
      list.append(card);
    }
  };

  const load = async () => {
    status.className = "vc-presence-load-status is-loading";
    status.textContent = "Loading voice bot status...";
    try {
      const data = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/vc`, { cache: "no-store", skipPreload: true });
      if (!commandGrid.contains(panel)) return;
      managementData.vc_presence = data.configs || managementData.vc_presence;
      managementData.voice_channels = data.voice_channels || managementData.voice_channels;
      renderSlots(data);
      status.className = "vc-presence-load-status";
      status.textContent = "Live status · refresh whenever a host bot reconnects.";
    } catch (error) {
      status.className = "vc-presence-load-status is-error";
      status.textContent = errorMessage(error, "VC bot status could not be loaded.");
      renderSlots({ configs: managementData.vc_presence, voice_channels: managementData.voice_channels });
    }
  };
  const actions = document.createElement("div");
  actions.className = "vc-presence-actions";
  const refresh = textElement("button", "secondary-button", "Refresh status");
  refresh.type = "button";
  refresh.addEventListener("click", () => { void load(); });
  const back = textElement("button", "secondary-button", "Back to Control Panel");
  back.type = "button";
  back.addEventListener("click", () => renderControlPanel());
  actions.append(refresh, back);
  panel.append(heading, security, status, list, actions);
  commandGrid.append(panel);
  void load();
}

function renderServerMessagePanel() {
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const panel = document.createElement("section");
  panel.className = "server-message-panel";
  panel.setAttribute("aria-labelledby", "server-message-title");

  const heading = document.createElement("div");
  heading.className = "server-message-heading";
  heading.append(
    textElement("h3", "server-message-title", "Server message"),
    textElement("p", "server-message-copy", "Send a quick announcement or a polished embed through BirdBot."),
  );

  const form = document.createElement("form");
  form.className = "server-message-form";
  form.noValidate = true;
  const channelSelect = createChannelSelect();
  channelSelect.id = "server-message-channel";
  const typeSelect = document.createElement("select");
  typeSelect.className = "channel-select";
  typeSelect.id = "server-message-type";
  typeSelect.append(new Option("Normal message", "normal"), new Option("Embed message", "embed"));

  const normalField = document.createElement("label");
  normalField.className = "command-field server-message-field";
  normalField.append(textElement("span", "command-field-label", "Message"));
  const normalInput = document.createElement("textarea");
  normalInput.className = "server-message-textarea";
  normalInput.rows = 5;
  normalInput.maxLength = 2000;
  normalInput.placeholder = "Write your message...";
  normalInput.required = true;
  normalField.append(normalInput);

  const embedFields = document.createElement("div");
  embedFields.className = "server-message-embed-fields";
  const titleField = document.createElement("label");
  titleField.className = "command-field";
  titleField.append(textElement("span", "command-field-label", "Embed title (optional)"));
  const titleInput = document.createElement("input");
  titleInput.className = "channel-select";
  titleInput.maxLength = 256;
  titleInput.placeholder = "Announcement";
  titleField.append(titleInput);
  const descriptionField = document.createElement("label");
  descriptionField.className = "command-field";
  descriptionField.append(textElement("span", "command-field-label", "Embed description"));
  const descriptionInput = document.createElement("textarea");
  descriptionInput.className = "server-message-textarea";
  descriptionInput.rows = 6;
  descriptionInput.maxLength = 4096;
  descriptionInput.placeholder = "Write the content of your embed...";
  descriptionField.append(descriptionInput);
  embedFields.append(titleField, descriptionField);

  const mentionIds = new Set();
  const mentionPicker = createMemberSelect();
  let mentionSearchTimer = null;
  mentionPicker.search.addEventListener("input", () => {
    window.clearTimeout(mentionSearchTimer);
    mentionSearchTimer = window.setTimeout(() => searchMembers(mentionPicker.search.value, mentionPicker.select), 180);
  });
  const mentionField = document.createElement("div");
  mentionField.className = "server-message-mention-field";
  mentionField.append(textElement("span", "command-field-label", "Mention a member (optional)"), mentionPicker.element);
  const insertMention = textElement("button", "secondary-button", "Insert mention");
  insertMention.type = "button";
  insertMention.disabled = true;
  const mentionHint = textElement("span", "server-message-hint", "Choose a member, then insert their mention into the message.");
  mentionField.append(insertMention, mentionHint);

  const replyField = document.createElement("label");
  replyField.className = "command-field";
  replyField.append(textElement("span", "command-field-label", "Reply to a message (optional)"));
  const replyInput = document.createElement("input");
  replyInput.className = "channel-select";
  replyInput.maxLength = 200;
  replyInput.placeholder = "Paste a Discord message link or message ID";
  replyField.append(replyInput);

  mentionPicker.select.addEventListener("change", () => {
    insertMention.disabled = !mentionPicker.select.value;
  });
  insertMention.addEventListener("click", () => {
    const memberId = mentionPicker.select.value;
    const member = (mentionPicker.select._memberRecords || managementData.members).find((item) => item.member_id === memberId);
    if (!memberId || !member) return;
    const targetInput = typeSelect.value === "embed" ? descriptionInput : normalInput;
    const token = `<@${memberId}>`;
    if (!targetInput.value.includes(token)) {
      const start = Number.isInteger(targetInput.selectionStart) ? targetInput.selectionStart : targetInput.value.length;
      const end = Number.isInteger(targetInput.selectionEnd) ? targetInput.selectionEnd : start;
      const separatorBefore = start > 0 && !/\s$/.test(targetInput.value.slice(0, start)) ? " " : "";
      const separatorAfter = end < targetInput.value.length && !/^\s/.test(targetInput.value.slice(end)) ? " " : "";
      targetInput.value = `${targetInput.value.slice(0, start)}${separatorBefore}${token}${separatorAfter}${targetInput.value.slice(end)}`;
      const cursor = start + separatorBefore.length + token.length + separatorAfter.length;
      targetInput.focus();
      targetInput.setSelectionRange(cursor, cursor);
    }
    mentionIds.add(memberId);
    mentionHint.textContent = `Mention ready: @${member.display_name || member.username || memberId}`;
    syncPreview();
  });

  const preview = document.createElement("aside");
  preview.className = "server-message-preview";
  preview.setAttribute("aria-live", "polite");
  const previewLabel = textElement("span", "server-message-preview-label", "Live preview");
  const normalPreview = textElement("p", "server-message-preview-normal", "Your message will appear here.");
  const embedPreview = document.createElement("div");
  embedPreview.className = "server-message-preview-embed";
  const embedPreviewTitle = textElement("strong", "", "Announcement");
  const embedPreviewDescription = textElement("p", "", "Your embed description will appear here.");
  embedPreview.append(embedPreviewTitle, embedPreviewDescription);
  preview.append(previewLabel, normalPreview, embedPreview);

  const actions = document.createElement("div");
  actions.className = "server-message-actions";
  const back = textElement("button", "secondary-button", "Back to Control Panel");
  back.type = "button";
  back.addEventListener("click", () => renderControlPanel());
  const send = textElement("button", "primary-button", "Send message");
  send.type = "submit";
  actions.append(back, send);

  const syncPreview = () => {
    const embed = typeSelect.value === "embed";
    normalField.hidden = embed;
    embedFields.hidden = !embed;
    // Keep accessibility metadata aligned with the visible editor so keyboard
    // and assistive-technology users get the same mode-specific expectations.
    normalInput.required = !embed;
    descriptionInput.required = embed;
    normalPreview.hidden = embed;
    embedPreview.hidden = !embed;
    normalPreview.textContent = normalInput.value.trim() || "Your message will appear here.";
    embedPreviewTitle.textContent = titleInput.value.trim() || "Announcement";
    embedPreviewDescription.textContent = descriptionInput.value.trim() || "Your embed description will appear here.";
  };
  typeSelect.addEventListener("change", syncPreview);
  [normalInput, titleInput, descriptionInput].forEach((input) => input.addEventListener("input", syncPreview));

  form.append(
    labeledControl("Target text channel", channelSelect),
    labeledControl("Message style", typeSelect),
    normalField,
    embedFields,
    mentionField,
    replyField,
    preview,
    actions,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (send.disabled) return;
    const messageType = typeSelect.value === "embed" ? "embed" : "normal";
    const channelId = channelSelect.value;
    const normalContent = normalInput.value.trim();
    const embedTitle = titleInput.value.trim();
    const embedDescription = descriptionInput.value.trim();
    const replyTo = replyInput.value.trim();
    if (!channelId) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Choose a text channel first.";
      channelSelect.focus();
      return;
    }
    if (messageType === "normal" && !normalContent) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Write a message before sending it.";
      normalInput.focus();
      return;
    }
    if (messageType === "embed" && !embedDescription) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Write an embed description before sending it.";
      descriptionInput.focus();
      return;
    }
    send.disabled = true;
    back.disabled = true;
    beginLoading("Sending your message through BirdBot...");
    try {
      const payload = {
        channel_id: channelId,
        message_type: messageType,
        mention_user_ids: [...mentionIds],
      };
      if (replyTo) payload.reply_to = replyTo;
      if (messageType === "normal") payload.content = normalContent;
      else {
        payload.title = embedTitle;
        payload.description = embedDescription;
      }
      const queued = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/server-message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      commandFeedback.hidden = false;
      commandFeedback.textContent = "BirdBot is sending your message...";
      const state = await waitForDashboardCommand(queued.request_id, recipientMode === "everyone" ? 240 : 40);
      commandFeedback.textContent = state.status === "pending"
        ? "The message is queued and will appear in the selected channel shortly."
        : "Message sent successfully.";
      if (state.status === "complete") {
        if (messageType === "normal") normalInput.value = "";
        else { titleInput.value = ""; descriptionInput.value = ""; }
        syncPreview();
      }
    } catch (error) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = errorMessage(error, "BirdBot could not send that message.");
    } finally {
      endLoading();
      send.disabled = false;
      back.disabled = false;
    }
  });
  panel.append(heading, form);
  commandGrid.append(panel);
  syncPreview();
}

function wireMemberPickerSearch(picker) {
  let timer = null;
  picker.search.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => searchMembers(picker.search.value, picker.select), 180);
  });
}

function addMentionToInput(input, memberId) {
  const token = `<@${memberId}>`;
  if (input.value.includes(token)) return;
  const start = Number.isInteger(input.selectionStart) ? input.selectionStart : input.value.length;
  const end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : start;
  const before = start > 0 && !/\s$/.test(input.value.slice(0, start)) ? " " : "";
  const after = end < input.value.length && !/^\s/.test(input.value.slice(end)) ? " " : "";
  input.value = `${input.value.slice(0, start)}${before}${token}${after}${input.value.slice(end)}`;
  const cursor = start + before.length + token.length + after.length;
  input.focus();
  input.setSelectionRange(cursor, cursor);
}

function renderBotProfilePanel() {
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const profile = managementData?.profile || {};
  const panel = document.createElement("section");
  panel.className = "bot-profile-panel server-message-panel";
  panel.setAttribute("aria-labelledby", "bot-profile-title");
  const heading = document.createElement("div");
  heading.className = "server-message-heading";
  heading.append(
    textElement("h3", "server-message-title", "Bot profile"),
    textElement("p", "server-message-copy", "Change BirdBot’s nickname and avatar in this server only. The global bot account stays unchanged."),
  );
  const form = document.createElement("form");
  form.className = "server-message-form profile-form";
  form.noValidate = true;
  const avatarWrap = document.createElement("div");
  avatarWrap.className = "profile-avatar-editor";
  const avatarPreview = document.createElement("div");
  avatarPreview.className = "profile-avatar-preview";
  const setAvatarPreview = (src) => {
    avatarPreview.replaceChildren();
    if (src) {
      const image = document.createElement("img");
      image.src = src;
      image.alt = "Current server avatar";
      image.loading = "lazy";
      image.decoding = "async";
      image.addEventListener("error", () => setAvatarPreview(""));
      avatarPreview.append(image);
    } else {
      avatarPreview.append(textElement("span", "profile-avatar-fallback", "B"));
    }
  };
  setAvatarPreview(profile.avatar_url || "");
  const avatarInput = document.createElement("input");
  avatarInput.type = "file";
  avatarInput.accept = "image/png,image/jpeg,image/webp,image/gif";
  avatarInput.className = "channel-select profile-file-input";
  const avatarHelp = textElement("span", "server-message-hint", "PNG, JPEG, WebP or GIF · max 8 MB");
  const removeAvatarLabel = document.createElement("label");
  removeAvatarLabel.className = "profile-remove-avatar";
  const removeAvatar = document.createElement("input");
  removeAvatar.type = "checkbox";
  removeAvatar.checked = false;
  removeAvatarLabel.append(removeAvatar, textElement("span", "", "Remove the current avatar"));
  avatarInput.addEventListener("change", () => {
    const file = avatarInput.files?.[0];
    if (!file) return;
    removeAvatar.checked = false;
    if (file.size > 8 * 1024 * 1024) {
      avatarInput.value = "";
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Avatar must be 8 MB or smaller.";
      return;
    }
    setAvatarPreview(URL.createObjectURL(file));
  });
  removeAvatar.addEventListener("change", () => {
    if (removeAvatar.checked) {
      avatarInput.value = "";
      setAvatarPreview("");
    } else setAvatarPreview(profile.avatar_url || "");
  });
  avatarWrap.append(avatarPreview, labeledControl("Server avatar", avatarInput), avatarHelp, removeAvatarLabel);
  const nicknameInput = document.createElement("input");
  nicknameInput.className = "channel-select";
  nicknameInput.maxLength = 32;
  nicknameInput.placeholder = "BirdBot (leave blank to reset)";
  nicknameInput.value = profile.nickname || "";
  const actions = document.createElement("div");
  actions.className = "server-message-actions";
  const back = textElement("button", "secondary-button", "Back to Control Panel");
  back.type = "button";
  back.addEventListener("click", () => renderControlPanel());
  const save = textElement("button", "primary-button", "Save profile");
  save.type = "submit";
  actions.append(back, save);
  form.append(avatarWrap, labeledControl("Server nickname", nicknameInput), actions);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (save.disabled) return;
    const file = avatarInput.files?.[0];
    if (file && file.size > 8 * 1024 * 1024) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Avatar must be 8 MB or smaller.";
      return;
    }
    save.disabled = true;
    back.disabled = true;
    beginLoading("Updating BirdBot’s server profile...");
    try {
      const formData = new FormData();
      formData.append("payload", JSON.stringify({ nickname: nicknameInput.value.trim(), remove_avatar: removeAvatar.checked }));
      if (file) formData.append("avatar", file);
      const queued = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/profile`, {
        method: "POST",
        body: formData,
      });
      commandFeedback.hidden = false;
      commandFeedback.textContent = "BirdBot is applying the server profile...";
      const state = await waitForDashboardCommand(queued.request_id);
      if (state.status === "pending") {
        commandFeedback.textContent = "The profile update is still queued. Check again shortly.";
      } else {
        const fresh = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/manage`, { cache: "no-store" });
        managementData = { ...managementData, profile: fresh.profile || queued.profile || {} };
        renderBotProfilePanel();
        commandFeedback.hidden = false;
        commandFeedback.textContent = "BirdBot’s server profile was updated.";
      }
    } catch (error) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = errorMessage(error, "The bot profile could not be updated.");
    } finally {
      endLoading();
      save.disabled = false;
      back.disabled = false;
    }
  });
  panel.append(heading, form);
  commandGrid.append(panel);
}

function renderDMMessagePanel() {
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const panel = document.createElement("section");
  panel.className = "dm-message-panel server-message-panel";
  panel.setAttribute("aria-labelledby", "dm-message-title");
  const heading = document.createElement("div");
  heading.className = "server-message-heading";
  heading.append(
    textElement("h3", "server-message-title", "DM’s messages"),
    textElement("p", "server-message-copy", "Send a private message to one member or every human member in the server. Mentions and one image or video attachment are supported."),
  );
  const form = document.createElement("form");
  form.className = "server-message-form dm-message-form";
  form.noValidate = true;
  const recipientModeSelect = document.createElement("select");
  recipientModeSelect.className = "channel-select";
  recipientModeSelect.append(new Option("One server member", "member"), new Option("Everyone in the server", "everyone"));
  const targetPicker = createMemberSelect();
  wireMemberPickerSearch(targetPicker);
  const targetField = labeledControl("Send to", targetPicker.element);
  const mentionPicker = createMemberSelect();
  wireMemberPickerSearch(mentionPicker);
  const mentionIds = new Set();
  const mentionField = document.createElement("div");
  mentionField.className = "server-message-mention-field";
  mentionField.append(textElement("span", "command-field-label", "Mention in the message (optional)"), mentionPicker.element);
  const insertMention = textElement("button", "secondary-button", "Insert mention");
  insertMention.type = "button";
  insertMention.disabled = true;
  const mentionHint = textElement("span", "server-message-hint", "Select a member, then insert their mention.");
  mentionField.append(insertMention, mentionHint);
  const typeSelect = document.createElement("select");
  typeSelect.className = "channel-select";
  typeSelect.append(new Option("Normal message", "normal"), new Option("Embed message", "embed"));
  const normalField = document.createElement("label");
  normalField.className = "command-field";
  normalField.append(textElement("span", "command-field-label", "Private message"));
  const normalInput = document.createElement("textarea");
  normalInput.className = "server-message-textarea";
  normalInput.rows = 6;
  normalInput.maxLength = 2000;
  normalInput.placeholder = "Write a private message...";
  normalField.append(normalInput);
  const embedFields = document.createElement("div");
  embedFields.className = "server-message-embed-fields";
  const titleInput = document.createElement("input");
  titleInput.className = "channel-select";
  titleInput.maxLength = 256;
  titleInput.placeholder = "Embed title (optional)";
  const descriptionInput = document.createElement("textarea");
  descriptionInput.className = "server-message-textarea";
  descriptionInput.rows = 6;
  descriptionInput.maxLength = 4096;
  descriptionInput.placeholder = "Write the embed description...";
  embedFields.append(labeledControl("Embed title (optional)", titleInput), labeledControl("Embed description", descriptionInput));
  const attachmentInput = document.createElement("input");
  attachmentInput.type = "file";
  attachmentInput.accept = "image/*,video/mp4,video/webm,video/quicktime";
  attachmentInput.className = "channel-select";
  const attachmentHint = textElement("span", "server-message-hint", "Optional image or MP4/WebM/MOV video · max 8 MB");
  attachmentInput.addEventListener("change", () => {
    const file = attachmentInput.files?.[0];
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) {
      attachmentInput.value = "";
      attachmentHint.textContent = "Attachment must be 8 MB or smaller.";
      return;
    }
    attachmentHint.textContent = `${file.name} ready to attach.`;
  });
  mentionPicker.select.addEventListener("change", () => { insertMention.disabled = !mentionPicker.select.value; });
  insertMention.addEventListener("click", () => {
    const memberId = mentionPicker.select.value;
    const member = (mentionPicker.select._memberRecords || managementData.members).find((item) => item.member_id === memberId);
    if (!memberId || !member) return;
    addMentionToInput(typeSelect.value === "embed" ? descriptionInput : normalInput, memberId);
    mentionIds.add(memberId);
    mentionHint.textContent = `Mention ready: @${member.display_name || member.username || memberId}`;
  });
  const preview = document.createElement("aside");
  preview.className = "server-message-preview";
  preview.append(textElement("span", "server-message-preview-label", "Live preview"));
  const normalPreview = textElement("p", "server-message-preview-normal", "Your private message will appear here.");
  const embedPreview = document.createElement("div");
  embedPreview.className = "server-message-preview-embed";
  const embedPreviewTitle = textElement("strong", "", "Announcement");
  const embedPreviewDescription = textElement("p", "", "Your embed description will appear here.");
  embedPreview.append(embedPreviewTitle, embedPreviewDescription);
  preview.append(normalPreview, embedPreview);
  const syncPreview = () => {
    const embed = typeSelect.value === "embed";
    normalField.hidden = embed;
    embedFields.hidden = !embed;
    normalInput.required = !embed;
    descriptionInput.required = embed;
    normalPreview.hidden = embed;
    embedPreview.hidden = !embed;
    normalPreview.textContent = normalInput.value.trim() || "Your private message will appear here.";
    embedPreviewTitle.textContent = titleInput.value.trim() || "Announcement";
    embedPreviewDescription.textContent = descriptionInput.value.trim() || "Your embed description will appear here.";
  };
  typeSelect.addEventListener("change", syncPreview);
  [normalInput, titleInput, descriptionInput].forEach((input) => input.addEventListener("input", syncPreview));
  const actions = document.createElement("div");
  actions.className = "server-message-actions";
  const back = textElement("button", "secondary-button", "Back to Control Panel");
  back.type = "button";
  back.addEventListener("click", () => renderControlPanel());
  const send = textElement("button", "primary-button", "Send private message");
  send.type = "submit";
  const syncRecipientMode = () => {
    const everyone = recipientModeSelect.value === "everyone";
    targetField.hidden = everyone;
    send.textContent = everyone ? "Send to everyone" : "Send private message";
    attachmentHint.textContent = everyone
      ? "Optional image or MP4/WebM/MOV video Â· max 8 MB Â· broadcast delivery is rate-limited by Discord"
      : "Optional image or MP4/WebM/MOV video Â· max 8 MB";
  };
  recipientModeSelect.addEventListener("change", syncRecipientMode);
  actions.append(back, send);
  form.append(
    labeledControl("Recipients", recipientModeSelect),
    targetField,
    labeledControl("Message style", typeSelect),
    normalField,
    embedFields,
    mentionField,
    labeledControl("Attachment (optional)", attachmentInput),
    attachmentHint,
    preview,
    actions,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (send.disabled) return;
    const recipientMode = recipientModeSelect.value === "everyone" ? "everyone" : "member";
    const memberId = targetPicker.select.value;
    const messageType = typeSelect.value === "embed" ? "embed" : "normal";
    const content = normalInput.value.trim();
    const title = titleInput.value.trim();
    const description = descriptionInput.value.trim();
    if (recipientMode === "member" && !memberId) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Choose a server member first.";
      return;
    }
    if (recipientMode === "everyone" && !window.confirm("Send this private message to every human member in the server? Discord may rate-limit a large broadcast.")) {
      return;
    }
    if (messageType === "normal" && !content) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Write a private message before sending it.";
      normalInput.focus();
      return;
    }
    if (messageType === "embed" && !description) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Write an embed description before sending it.";
      descriptionInput.focus();
      return;
    }
    const file = attachmentInput.files?.[0];
    if (file && file.size > 8 * 1024 * 1024) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Attachment must be 8 MB or smaller.";
      return;
    }
    send.disabled = true;
    back.disabled = true;
    beginLoading("Sending the private message through BirdBot...");
    try {
      const payload = { recipient_mode: recipientMode, member_id: recipientMode === "member" ? memberId : null, message_type: messageType, mention_user_ids: [...mentionIds] };
      if (messageType === "normal") payload.content = content;
      else { payload.title = title; payload.description = description; }
      const formData = new FormData();
      formData.append("payload", JSON.stringify(payload));
      if (file) formData.append("media", file);
      const queued = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/dm-message`, {
        method: "POST",
        body: formData,
      });
      commandFeedback.hidden = false;
      commandFeedback.textContent = "BirdBot is sending the private message...";
      const state = await waitForDashboardCommand(queued.request_id);
      const result = state.result && typeof state.result === "object" ? state.result : {};
      commandFeedback.textContent = recipientMode === "everyone" && state.status === "complete"
        ? `Broadcast complete: ${Number(result.delivered || 0)} delivered, ${Number(result.failed || 0)} failed${Number(result.skipped_bots || 0) ? `, ${Number(result.skipped_bots)} bot accounts skipped` : ""}.`
        : state.status === "pending"
          ? "The private message is still queued. Check again shortly."
          : "Private message sent successfully.";
      if (state.status === "complete") {
        normalInput.value = "";
        titleInput.value = "";
        descriptionInput.value = "";
        attachmentInput.value = "";
        attachmentHint.textContent = recipientMode === "everyone"
          ? "Optional image or MP4/WebM/MOV video · max 8 MB · broadcast delivery is rate-limited by Discord"
          : "Optional image or MP4/WebM/MOV video · max 8 MB";
        syncPreview();
      }
    } catch (error) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = errorMessage(error, "BirdBot could not send that private message.");
    } finally {
      endLoading();
      send.disabled = false;
      back.disabled = false;
    }
  });
  panel.append(heading, form);
  commandGrid.append(panel);
  syncPreview();
  syncRecipientMode();
}

function renderBotSettingsPanel() {
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const panel = document.createElement("section");
  panel.className = "server-message-panel bot-settings-panel";
  panel.setAttribute("aria-labelledby", "bot-settings-title");
  const heading = document.createElement("div");
  heading.className = "server-message-heading";
  heading.append(
    textElement("h3", "server-message-title", "Bot Settings"),
    textElement("p", "server-message-copy", "Configure Automod, automatic reactions, and the AI assistant. Settings apply only to this server."),
  );
  const automod = managementData.automod || {};
  const automodSection = document.createElement("section");
  automodSection.className = "automod-settings-card";
  automodSection.setAttribute("aria-labelledby", "automod-settings-title");
  const automodHeader = document.createElement("div");
  automodHeader.className = "automod-settings-header";
  const automodCopy = document.createElement("div");
  automodCopy.className = "automod-settings-copy";
  const automodTitle = textElement("strong", "", "Automod");
  automodTitle.id = "automod-settings-title";
  automodCopy.append(
    automodTitle,
    textElement("p", "", "Keep your server calm with per-server moderation rules. Moderators and administrators are always ignored."),
    textElement("p", "automod-permission-hint", "BirdBot needs Manage Messages to remove content and Moderate Members for warnings or timeouts."),
  );
  const automodStatus = textElement("span", "automod-status", automod.enabled ? "Enabled" : "Disabled");
  automodStatus.classList.toggle("is-enabled", Boolean(automod.enabled));
  automodHeader.append(automodCopy, automodStatus);
  const automodForm = document.createElement("form");
  automodForm.className = "automod-form";
  automodForm.noValidate = true;
  const automodStep = (number, title, description) => {
    const section = document.createElement("section");
    section.className = "automod-step";
    const header = document.createElement("div");
    header.className = "automod-step-header";
    header.append(
      textElement("span", "automod-step-number", String(number).padStart(2, "0")),
      textElement("strong", "", title),
    );
    const copy = textElement("p", "automod-step-description", description);
    const content = document.createElement("div");
    content.className = "automod-step-content";
    section.append(header, copy, content);
    return { section, content };
  };
  const masterLabel = document.createElement("label");
  masterLabel.className = "automod-master-toggle";
  const masterInput = document.createElement("input");
  masterInput.type = "checkbox";
  masterInput.checked = Boolean(automod.enabled);
  masterLabel.append(masterInput, textElement("span", "", "Enable Automod"));
  const featureGrid = document.createElement("div");
  featureGrid.className = "automod-feature-grid";
  const featureInputs = {};
  const featureOptions = {};
  const featureStatuses = {};
  const featureDefinitions = [
    ["anti_link", "Anti-link", "Remove messages containing external links."],
    ["anti_spam", "Anti-spam", "Detect repeated messages in a short window."],
    ["banned_words", "Banned words filter", "Remove messages matching your word list."],
    ["raid_protection", "Raid protection", "Detect a burst of new member joins."],
    ["auto_warning", "Auto-warning", "Issue a numbered warning for a violation."],
    ["auto_timeout", "Auto-timeout", "Timeout a violating member automatically."],
  ];
  featureDefinitions.forEach(([key, label, description]) => {
    const option = document.createElement("label");
    option.className = "automod-feature-option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(automod[key]);
    featureInputs[key] = input;
    featureOptions[key] = option;
    const copy = document.createElement("span");
    copy.className = "automod-feature-copy";
    const status = textElement("small", "automod-feature-status", input.checked ? "On" : "Off");
    featureStatuses[key] = status;
    copy.append(textElement("strong", "", label), textElement("small", "", description), status);
    option.append(input, copy);
    featureGrid.append(option);
  });
  const wordsInput = document.createElement("textarea");
  wordsInput.className = "automod-words-input";
  wordsInput.rows = 3;
  wordsInput.maxLength = 6_500;
  wordsInput.placeholder = "One word or phrase per line";
  wordsInput.value = Array.isArray(automod.banned_words_list) ? automod.banned_words_list.join("\n") : "";
  const automodOptions = document.createElement("div");
  automodOptions.className = "automod-options-grid";
  const numberInput = (value, min, max) => {
    const input = document.createElement("input");
    input.className = "channel-select automod-number-input";
    input.type = "number";
    input.min = String(min);
    input.max = String(max);
    input.step = "1";
    input.value = String(value ?? min);
    return input;
  };
  const spamLimitInput = numberInput(automod.spam_message_limit || 5, 3, 30);
  const spamWindowInput = numberInput(automod.spam_window_seconds || 8, 3, 60);
  const raidLimitInput = numberInput(automod.raid_join_limit || 8, 3, 50);
  const raidWindowInput = numberInput(automod.raid_window_seconds || 10, 5, 120);
  const storedTimeoutMinutes = Number(automod.auto_timeout_minutes || 10);
  const storedTimeoutUnit = storedTimeoutMinutes % 10080 === 0 ? "w" : storedTimeoutMinutes % 1440 === 0 ? "d" : storedTimeoutMinutes % 60 === 0 ? "h" : "m";
  const storedTimeoutAmount = storedTimeoutMinutes / ({ w: 10080, d: 1440, h: 60, m: 1 }[storedTimeoutUnit] || 1);
  const timeoutAmountInput = numberInput(storedTimeoutAmount, 1, 40_320);
  timeoutAmountInput.classList.add("automod-timeout-amount");
  const timeoutUnitSelect = document.createElement("select");
  timeoutUnitSelect.className = "channel-select automod-timeout-unit";
  timeoutUnitSelect.append(new Option("Minutes", "m"), new Option("Hours", "h"), new Option("Days", "d"), new Option("Weeks", "w"));
  timeoutUnitSelect.value = storedTimeoutUnit;
  bindDurationPicker(timeoutAmountInput, timeoutUnitSelect);
  const bannedWordsField = labeledControl("Words or phrases (one per line)", wordsInput);
  bannedWordsField.classList.add("automod-setting-field", "automod-setting-banned-words");
  const spamLimitField = labeledControl("Messages in window", spamLimitInput);
  const spamWindowField = labeledControl("Window (seconds)", spamWindowInput);
  const spamFields = document.createElement("div");
  spamFields.className = "automod-rule-fields";
  spamFields.append(spamLimitField, spamWindowField);
  const spamGroup = document.createElement("div");
  spamGroup.className = "automod-rule-group";
  spamGroup.dataset.automodSetting = "anti_spam";
  spamGroup.append(textElement("strong", "", "Anti-spam threshold"), spamFields);
  const raidLimitField = labeledControl("Joins in window", raidLimitInput);
  const raidWindowField = labeledControl("Window (seconds)", raidWindowInput);
  const raidFields = document.createElement("div");
  raidFields.className = "automod-rule-fields";
  raidFields.append(raidLimitField, raidWindowField);
  const raidGroup = document.createElement("div");
  raidGroup.className = "automod-rule-group";
  raidGroup.dataset.automodSetting = "raid_protection";
  raidGroup.append(textElement("strong", "", "Raid protection threshold"), raidFields);
  const timeoutPicker = document.createElement("div");
  timeoutPicker.className = "command-duration-picker";
  timeoutPicker.append(timeoutAmountInput, timeoutUnitSelect);
  const timeoutField = labeledControl("Auto-timeout length", timeoutPicker);
  timeoutField.classList.add("automod-setting-field");
  timeoutField.dataset.automodSetting = "auto_timeout";
  timeoutField.append(textElement("small", "command-field-hint", "Choose minutes, hours, days, or weeks. Maximum 28 days."));
  automodOptions.append(
    bannedWordsField,
    spamGroup,
    raidGroup,
    timeoutField,
  );
  const automodActions = document.createElement("div");
  automodActions.className = "automod-actions";
  const automodSave = textElement("button", "primary-button", "Save Automod");
  automodSave.type = "submit";
  const automodFormStatus = textElement("span", "automod-form-status", "");
  automodActions.append(automodSave, automodFormStatus);
  const enableStep = automodStep(1, "Turn Automod on", "Enable the master switch before any protection rule can take effect.");
  enableStep.content.append(masterLabel);
  const rulesStep = automodStep(2, "Choose protections", "Turn on only what you need. Auto-warning and Auto-timeout are response actions used when another rule detects a violation.");
  rulesStep.content.append(featureGrid);
  const tuningStep = automodStep(3, "Fine-tune selected rules", "Optional thresholds are shown only after their related rule is enabled.");
  const tuningEmpty = textElement("p", "automod-tuning-empty", "Enable Anti-spam, Banned words, Raid protection, or Auto-timeout above to see its settings here.");
  tuningStep.content.append(automodOptions);
  tuningStep.content.append(tuningEmpty);
  const saveStep = automodStep(4, "Save and apply", "Changes are applied to this server immediately after saving. Existing messages are not scanned retroactively.");
  saveStep.content.append(automodActions);
  const syncAutomodEnabled = () => {
    const active = masterInput.checked;
    automodStatus.textContent = active ? "Enabled" : "Disabled";
    automodStatus.classList.toggle("is-enabled", active);
    Object.entries(featureInputs).forEach(([key, input]) => {
      input.disabled = !active;
      featureOptions[key].classList.toggle("is-selected", active && input.checked);
      featureStatuses[key].textContent = !active ? "Paused" : (input.checked ? "On" : "Off");
      featureStatuses[key].classList.toggle("is-on", active && input.checked);
    });
    automodOptions.querySelectorAll("input, textarea, select").forEach((input) => { input.disabled = !active; });
    const settingVisibility = {
      banned_words: Boolean(featureInputs.banned_words?.checked),
      anti_spam: Boolean(featureInputs.anti_spam?.checked),
      raid_protection: Boolean(featureInputs.raid_protection?.checked),
      auto_timeout: Boolean(featureInputs.auto_timeout?.checked),
    };
    bannedWordsField.hidden = !active || !settingVisibility.banned_words;
    automodOptions.querySelectorAll("[data-automod-setting]").forEach((element) => {
      const key = element.dataset.automodSetting;
      element.hidden = !active || !settingVisibility[key];
    });
    tuningEmpty.hidden = [...automodOptions.children].some((element) => !element.hidden);
  };
  masterInput.addEventListener("change", syncAutomodEnabled);
  Object.values(featureInputs).forEach((input) => input.addEventListener("change", syncAutomodEnabled));
  automodForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (automodSave.disabled) return;
    automodSave.disabled = true;
    automodFormStatus.textContent = "Saving...";
    automodFormStatus.className = "automod-form-status is-loading";
    try {
      const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/bot-settings/automod`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: masterInput.checked,
          anti_link: featureInputs.anti_link.checked,
          anti_spam: featureInputs.anti_spam.checked,
          banned_words: featureInputs.banned_words.checked,
          raid_protection: featureInputs.raid_protection.checked,
          auto_warning: featureInputs.auto_warning.checked,
          auto_timeout: featureInputs.auto_timeout.checked,
          banned_words_list: wordsInput.value.split(/\r?\n/),
          spam_message_limit: spamLimitInput.value,
          spam_window_seconds: spamWindowInput.value,
          raid_join_limit: raidLimitInput.value,
          raid_window_seconds: raidWindowInput.value,
          auto_timeout_amount: timeoutAmountInput.value,
          auto_timeout_unit: timeoutUnitSelect.value,
        }),
      });
      managementData.automod = result.automod || managementData.automod;
      automodFormStatus.textContent = "Saved and applied.";
      automodFormStatus.className = "automod-form-status is-success";
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Automod settings saved and applied to this server.";
      syncAutomodEnabled();
    } catch (error) {
      automodFormStatus.textContent = errorMessage(error, "Automod settings could not be saved.");
      automodFormStatus.className = "automod-form-status is-error";
    } finally {
      automodSave.disabled = false;
    }
  });
  automodForm.append(enableStep.section, rulesStep.section, tuningStep.section, saveStep.section);
  automodSection.append(automodHeader, automodForm);
  syncAutomodEnabled();
  const ai = managementData.ai || {};
  const aiAvailable = managementData.ai_available !== false;
  const aiSection = document.createElement("section");
  aiSection.className = "automod-settings-card ai-settings-card";
  aiSection.setAttribute("aria-labelledby", "ai-settings-title");
  const aiHeader = document.createElement("div");
  aiHeader.className = "automod-settings-header";
  const aiCopy = document.createElement("div");
  aiCopy.className = "automod-settings-copy";
  const aiTitle = textElement("strong", "", "AI assistant");
  aiTitle.id = "ai-settings-title";
  aiCopy.append(
    aiTitle,
    textElement("p", "", "When enabled, BirdBot replies to every human message in one selected text channel."),
    textElement("p", "automod-permission-hint", aiAvailable
      ? "Groq is ready. The provider key stays on the host and is never sent to the browser."
      : "Provider unavailable. Add GROQ_API_KEY as a private host secret, install the groq package, and restart BirdBot."),
  );
  const aiStatus = textElement("span", "automod-status", ai.enabled ? "Enabled" : "Disabled");
  aiStatus.classList.toggle("is-enabled", Boolean(ai.enabled));
  aiHeader.append(aiCopy, aiStatus);
  const aiForm = document.createElement("form");
  aiForm.className = "automod-form ai-settings-form";
  aiForm.noValidate = true;
  const aiStep = (number, title, description) => {
    const section = document.createElement("section");
    section.className = "automod-step";
    const stepHeader = document.createElement("div");
    stepHeader.className = "automod-step-header";
    stepHeader.append(
      textElement("span", "automod-step-number", String(number).padStart(2, "0")),
      textElement("strong", "", title),
    );
    const copy = textElement("p", "automod-step-description", description);
    const content = document.createElement("div");
    content.className = "automod-step-content";
    section.append(stepHeader, copy, content);
    return { section, content };
  };
  const aiEnabledLabel = document.createElement("label");
  aiEnabledLabel.className = "automod-master-toggle";
  const aiEnabledInput = document.createElement("input");
  aiEnabledInput.type = "checkbox";
  aiEnabledInput.checked = Boolean(ai.enabled);
  aiEnabledLabel.append(aiEnabledInput, textElement("span", "", "Enable AI assistant"));
  const aiChannelSelect = createChannelSelect();
  aiChannelSelect.value = String(ai.channel_id || "");
  const aiChannelField = labeledControl("AI response channel", aiChannelSelect);
  const aiHint = textElement("p", "automod-tuning-empty", "Every non-bot message in this channel receives a reply. Choose a channel where members expect the assistant.");
  const aiSave = textElement("button", "primary-button", "Save AI settings");
  aiSave.type = "submit";
  const aiFormStatus = textElement("span", "automod-form-status", "");
  const aiActions = document.createElement("div");
  aiActions.className = "automod-actions";
  aiActions.append(aiSave, aiFormStatus);
  const aiEnableStep = aiStep(1, "Turn the assistant on", "Enable the switch before BirdBot responds in Discord.");
  aiEnableStep.content.append(aiEnabledLabel);
  const aiChannelStep = aiStep(2, "Choose one channel", "Select where the assistant should listen and reply. Messages in other channels are ignored.");
  aiChannelStep.content.append(aiChannelField, aiHint);
  const aiSaveStep = aiStep(3, "Save and apply", "The setting applies to this server immediately after saving.");
  aiSaveStep.content.append(aiActions);
  const syncAi = () => {
    const enabled = aiEnabledInput.checked;
    aiStatus.textContent = enabled ? "Enabled" : "Disabled";
    aiStatus.classList.toggle("is-enabled", enabled);
    aiChannelSelect.disabled = !enabled;
  };
  aiEnabledInput.addEventListener("change", syncAi);
  aiForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (aiSave.disabled) return;
    if (aiEnabledInput.checked && !aiChannelSelect.value) {
      aiFormStatus.textContent = "Choose a response channel first.";
      aiFormStatus.className = "automod-form-status is-error";
      aiChannelSelect.focus();
      return;
    }
    aiSave.disabled = true;
    aiFormStatus.textContent = "Saving...";
    aiFormStatus.className = "automod-form-status is-loading";
    try {
      const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/bot-settings/ai`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: aiEnabledInput.checked, channel_id: aiChannelSelect.value || null }),
      });
      managementData.ai = result.ai || managementData.ai;
      if (typeof result.available === "boolean") managementData.ai_available = result.available;
      aiFormStatus.textContent = result.available === false ? "Saved, but the provider key is missing." : "Saved and applied.";
      aiFormStatus.className = result.available === false ? "automod-form-status is-error" : "automod-form-status is-success";
      commandFeedback.hidden = false;
      commandFeedback.textContent = result.available === false
        ? "AI settings saved. Add GROQ_API_KEY to the host before enabling replies."
        : "AI assistant settings saved and applied to this server.";
      syncAi();
    } catch (error) {
      aiFormStatus.textContent = errorMessage(error, "AI settings could not be saved.");
      aiFormStatus.className = "automod-form-status is-error";
    } finally {
      aiSave.disabled = false;
    }
  });
  aiForm.append(aiEnableStep.section, aiChannelStep.section, aiSaveStep.section);
  aiSection.append(aiHeader, aiForm);
  syncAi();
  const form = document.createElement("form");
  form.className = "server-message-form auto-react-form";
  form.noValidate = true;
  const channelSelect = createChannelSelect();
  const emojiInput = document.createElement("input");
  emojiInput.className = "channel-select auto-react-emoji";
  emojiInput.type = "text";
  emojiInput.maxLength = 100;
  emojiInput.placeholder = "e.g. \u{1F44D} or <:custom:123456789012345678>";
  const enabledLabel = document.createElement("label");
  enabledLabel.className = "command-inline-toggle auto-react-enabled";
  const enabledInput = document.createElement("input");
  enabledInput.type = "checkbox";
  enabledInput.checked = true;
  enabledLabel.append(enabledInput, textElement("span", "", "Enable this rule"));
  const editing = { ruleId: null };
  const save = textElement("button", "primary-button", "Add auto-react");
  save.type = "submit";
  const cancel = textElement("button", "secondary-button", "Cancel edit");
  cancel.type = "button";
  cancel.hidden = true;
  const formStatus = textElement("span", "command-settings-status", "");
  const resetForm = () => {
    editing.ruleId = null;
    channelSelect.value = "";
    emojiInput.value = "";
    enabledInput.checked = true;
    save.textContent = "Add auto-react";
    cancel.hidden = true;
    formStatus.textContent = "";
  };
  cancel.addEventListener("click", resetForm);
  form.append(
    labeledControl("Room / channel", channelSelect),
    labeledControl("Reaction emoji", emojiInput),
    enabledLabel,
    textElement("span", "server-message-hint", "When a member posts in the selected channel, BirdBot adds this reaction. Multiple rules can use different channels."),
    document.createElement("div"),
  );
  const formActions = form.lastElementChild;
  formActions.className = "server-message-actions";
  formActions.append(cancel, save, formStatus);

  const rulesSection = document.createElement("section");
  rulesSection.className = "auto-react-rules";
  const rulesHeader = document.createElement("div");
  rulesHeader.className = "auto-react-rules-header";
  const rulesTitle = textElement("strong", "", "Auto-react rules");
  const rulesCount = textElement("span", "auto-react-count", "0 rules");
  rulesHeader.append(rulesTitle, rulesCount);
  const rulesList = document.createElement("div");
  rulesList.className = "auto-react-list";
  rulesSection.append(rulesHeader, rulesList);
  const back = textElement("button", "secondary-button", "Back to Control Panel");
  back.type = "button";
  back.addEventListener("click", () => renderControlPanel());

  const channelName = (channelId) => {
    const channel = (managementData.channels || []).find((item) => String(item.id) === String(channelId));
    return channel ? `#${channel.name}` : `#${channelId}`;
  };
  const renderRules = () => {
    const rules = Array.isArray(managementData.auto_reacts) ? managementData.auto_reacts : [];
    rulesCount.textContent = `${rules.length} rule${rules.length === 1 ? "" : "s"}`;
    rulesList.replaceChildren();
    if (!rules.length) {
      rulesList.append(textElement("p", "empty-state", "No auto-react rules have been created yet."));
      return;
    }
    rules.forEach((rule) => {
      const row = document.createElement("article");
      row.className = "auto-react-row";
      const details = document.createElement("div");
      details.className = "auto-react-row-details";
      details.append(
        textElement("strong", "", `${channelName(rule.channel_id)}  ${rule.emoji || ""}`),
        textElement("small", "", rule.enabled === false ? "Disabled" : "Enabled"),
      );
      const actions = document.createElement("div");
      actions.className = "auto-react-row-actions";
      const edit = textElement("button", "secondary-button", "Edit");
      edit.type = "button";
      edit.addEventListener("click", () => {
        editing.ruleId = String(rule.rule_id);
        channelSelect.value = String(rule.channel_id || "");
        emojiInput.value = String(rule.emoji || "");
        enabledInput.checked = rule.enabled !== false;
        save.textContent = "Save auto-react";
        cancel.hidden = false;
        emojiInput.focus();
      });
      const remove = textElement("button", "danger-button", "Delete");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        if (remove.disabled || !window.confirm(`Delete the auto-react rule for ${channelName(rule.channel_id)}?`)) return;
        remove.disabled = true;
        try {
          const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/bot-settings/auto-react/${encodeURIComponent(rule.rule_id)}`, { method: "DELETE" });
          managementData.auto_reacts = result.auto_reacts || [];
          if (editing.ruleId === String(rule.rule_id)) resetForm();
          renderRules();
          commandFeedback.hidden = false;
          commandFeedback.textContent = "Auto-react rule deleted.";
        } catch (error) {
          commandFeedback.hidden = false;
          commandFeedback.textContent = errorMessage(error, "The auto-react rule could not be deleted.");
          remove.disabled = false;
        }
      });
      actions.append(edit, remove);
      row.append(details, actions);
      rulesList.append(row);
    });
  };
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (save.disabled) return;
    if (!channelSelect.value) {
      formStatus.textContent = "Choose a channel first.";
      formStatus.className = "command-settings-status is-error";
      return;
    }
    if (!emojiInput.value.trim()) {
      formStatus.textContent = "Enter an emoji first.";
      formStatus.className = "command-settings-status is-error";
      emojiInput.focus();
      return;
    }
    save.disabled = true;
    formStatus.textContent = "Saving...";
    formStatus.className = "command-settings-status is-loading";
    try {
      const payload = { channel_id: channelSelect.value, emoji: emojiInput.value.trim(), enabled: enabledInput.checked };
      if (editing.ruleId) payload.rule_id = editing.ruleId;
      const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/bot-settings/auto-react`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      managementData.auto_reacts = result.auto_reacts || [];
      formStatus.textContent = "Saved.";
      formStatus.className = "command-settings-status is-success";
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Auto-react settings saved and applied.";
      resetForm();
      renderRules();
    } catch (error) {
      formStatus.textContent = errorMessage(error, "The auto-react rule could not be saved.");
      formStatus.className = "command-settings-status is-error";
    } finally {
      save.disabled = false;
    }
  });
  panel.append(heading, automodSection, aiSection, form, rulesSection, back);
  commandGrid.append(panel);
  renderRules();
}

function normalizedRoleColor(value) {
  const color = String(value || "").trim().toUpperCase();
  return /^#[0-9A-F]{6}$/.test(color) ? color : "#000000";
}

async function reloadManagementRoles() {
  const fresh = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/manage`, { cache: "no-store" });
  managementData = {
    ...managementData,
    roles: Array.isArray(fresh.roles) ? fresh.roles : [],
    role_permission_fields: Array.isArray(fresh.role_permission_fields)
      ? fresh.role_permission_fields
      : managementData.role_permission_fields,
  };
}

const DEFAULT_ROLE_PERMISSION_FIELDS = [
  ["view_channel", "View Channels", "See text, voice, and stage channels."],
  ["send_messages", "Send Messages", "Send messages in text channels."],
  ["embed_links", "Embed Links", "Show rich previews for links."],
  ["attach_files", "Attach Files", "Upload images and other files."],
  ["read_message_history", "Read Message History", "Read earlier messages."],
  ["add_reactions", "Add Reactions", "Add reactions to messages."],
  ["use_external_emojis", "Use External Emojis", "Use emojis from other servers."],
  ["send_messages_in_threads", "Send Messages in Threads", "Reply in threads."],
  ["create_public_threads", "Create Public Threads", "Start public threads."],
  ["create_private_threads", "Create Private Threads", "Start private threads."],
  ["manage_threads", "Manage Threads", "Rename, archive, or delete threads."],
  ["mention_everyone", "Mention Everyone", "Use @everyone and @here mentions."],
  ["manage_messages", "Manage Messages", "Delete or pin other members' messages."],
  ["manage_channels", "Manage Channels", "Create and edit channels."],
  ["manage_roles", "Manage Roles", "Create and edit roles below the bot."],
  ["manage_webhooks", "Manage Webhooks", "Create and manage webhooks."],
  ["view_audit_log", "View Audit Log", "See the server audit log."],
  ["kick_members", "Kick Members", "Remove members from the server."],
  ["ban_members", "Ban Members", "Ban members from the server."],
  ["moderate_members", "Moderate Members", "Timeout or otherwise moderate members."],
  ["change_nickname", "Change Nickname", "Change your own nickname."],
  ["manage_nicknames", "Manage Nicknames", "Change other members' nicknames."],
  ["connect", "Connect", "Join voice channels."],
  ["speak", "Speak", "Transmit audio in voice channels."],
  ["stream", "Video", "Share video or stream in voice channels."],
  ["use_voice_activation", "Use Voice Activity", "Use voice activation instead of push-to-talk."],
  ["mute_members", "Mute Members", "Mute members in voice channels."],
  ["deafen_members", "Deafen Members", "Deafen members in voice channels."],
  ["move_members", "Move Members", "Move members between voice channels."],
  ["use_application_commands", "Use App Commands", "Use slash commands and other app commands."],
  ["manage_events", "Manage Events", "Create and manage server events."],
  ["administrator", "Administrator", "Grant every permission. Use with care."],
];

function rolePermissionFields() {
  const fields = Array.isArray(managementData?.role_permission_fields)
    ? managementData.role_permission_fields
    : DEFAULT_ROLE_PERMISSION_FIELDS;
  return fields.filter((field) => Array.isArray(field) ? field.length >= 2 : field && field.key)
    .map((field) => Array.isArray(field)
      ? { key: String(field[0]), label: String(field[1]), description: String(field[2] || "") }
      : { key: String(field.key), label: String(field.label || field.key), description: String(field.description || "") });
}

async function runRoleAction(action, payload, button, roleName) {
  if (button.disabled) return;
  button.disabled = true;
  const method = action === "create" ? "POST" : action === "edit" ? "PATCH" : "DELETE";
  const url = action === "create"
    ? `/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/roles`
    : `/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/roles/${encodeURIComponent(payload.role_id)}`;
  beginLoading(`${action === "create" ? "Creating" : action === "edit" ? "Updating" : "Deleting"} role...`);
  try {
    const options = { method, headers: { "Content-Type": "application/json" } };
    if (action !== "delete") options.body = JSON.stringify(payload);
    const queued = await requestJson(url, options);
    const status = await waitForDashboardCommand(queued.request_id);
    if (status.status === "pending") {
      commandFeedback.hidden = false;
      commandFeedback.textContent = `The ${roleName || "role"} action is still queued. Check the role list shortly.`;
      return;
    }
    await reloadManagementRoles();
    renderRolesPanel();
    commandFeedback.hidden = false;
    commandFeedback.textContent = action === "create"
      ? `Role “${roleName}” created successfully.`
      : action === "edit" ? `Role “${roleName}” updated successfully.` : "Role deleted successfully.";
  } catch (error) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "The role action could not be completed.");
  } finally {
    endLoading();
    button.disabled = false;
  }
}

function renderRolesPanel(editingRoleId = "") {
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const roles = Array.isArray(managementData?.roles) ? managementData.roles : [];
  const editingRole = roles.find((role) => String(role.id) === String(editingRoleId)) || null;
  const panel = document.createElement("section");
  panel.className = "roles-panel";
  panel.setAttribute("aria-labelledby", "roles-panel-title");
  const heading = document.createElement("div");
  heading.className = "roles-panel-heading";
  heading.append(
    textElement("h3", "server-message-title", editingRole ? "Edit role" : "Roles"),
    textElement("p", "server-message-copy", editingRole ? "Update this role’s name, color, and permissions." : "Create and manage the roles in your server."),
  );
  const back = textElement("button", "secondary-button", "Back to Control Panel");
  back.type = "button";
  back.addEventListener("click", () => renderControlPanel());
  heading.append(back);

  const form = document.createElement("form");
  form.className = "role-editor-form";
  form.noValidate = true;
  const nameInput = document.createElement("input");
  nameInput.className = "channel-select";
  nameInput.maxLength = 100;
  nameInput.placeholder = "Role name";
  nameInput.value = editingRole?.name || "";
  const colorRow = document.createElement("div");
  colorRow.className = "role-color-row";
  const colorPicker = document.createElement("input");
  colorPicker.type = "color";
  colorPicker.className = "role-color-picker";
  colorPicker.value = normalizedRoleColor(editingRole?.color);
  const colorHex = document.createElement("input");
  colorHex.className = "channel-select role-color-hex";
  colorHex.maxLength = 7;
  colorHex.placeholder = "#000000";
  colorHex.value = colorPicker.value.toUpperCase();
  colorRow.append(colorPicker, colorHex);
  colorPicker.addEventListener("input", () => { colorHex.value = colorPicker.value.toUpperCase(); });
  colorHex.addEventListener("input", () => {
    const value = colorHex.value.trim().toUpperCase();
    if (/^#[0-9A-F]{6}$/.test(value)) colorPicker.value = value;
  });
  let permissionDraft = { ...(editingRole?.permissions || {}) };
  let permissionSection = null;
  if (editingRole) {
    permissionSection = document.createElement("fieldset");
    permissionSection.className = "role-permissions-panel";
    const permissionHeading = document.createElement("div");
    permissionHeading.className = "role-permissions-heading";
    permissionHeading.append(
      textElement("strong", "", "Role permissions"),
      textElement("p", "", "Choose what members with this role can do. Changes apply after saving."),
    );
    const groups = [
      ["Messages & channels", ["view_channel", "send_messages", "embed_links", "attach_files", "read_message_history", "add_reactions", "use_external_emojis", "send_messages_in_threads", "create_public_threads", "create_private_threads", "manage_threads", "mention_everyone", "manage_messages", "manage_channels"]],
      ["Server management", ["manage_roles", "manage_webhooks", "view_audit_log", "kick_members", "ban_members", "moderate_members", "manage_events", "administrator"]],
      ["Members & profile", ["change_nickname", "manage_nicknames"]],
      ["Voice & apps", ["connect", "speak", "stream", "use_voice_activation", "mute_members", "deafen_members", "move_members", "use_application_commands"]],
    ];
    const fieldMap = new Map(rolePermissionFields().map((field) => [field.key, field]));
    const groupList = document.createElement("div");
    groupList.className = "role-permission-groups";
    groups.forEach(([groupName, keys]) => {
      const available = keys.map((key) => fieldMap.get(key)).filter(Boolean);
      if (!available.length) return;
      const group = document.createElement("section");
      group.className = "role-permission-group";
      group.append(textElement("h4", "", groupName));
      const options = document.createElement("div");
      options.className = "role-permission-grid";
      available.forEach((field) => {
        const option = document.createElement("label");
        option.className = "role-permission-option";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = Boolean(permissionDraft[field.key]);
        checkbox.addEventListener("change", () => { permissionDraft[field.key] = checkbox.checked; });
        const copy = document.createElement("span");
        copy.append(textElement("strong", "", field.label), textElement("small", "", field.description));
        option.append(checkbox, copy);
        options.append(option);
      });
      group.append(options);
      groupList.append(group);
    });
    permissionSection.append(permissionHeading, groupList);
  }
  const save = textElement("button", "primary-button", editingRole ? "Save changes" : "Create role");
  save.type = "submit";
  const cancel = textElement("button", "secondary-button", "Cancel");
  cancel.type = "button";
  cancel.hidden = !editingRole;
  cancel.addEventListener("click", () => renderRolesPanel());
  const actions = document.createElement("div");
  actions.className = "server-message-actions";
  actions.append(cancel, save);
  form.append(labeledControl("Role name", nameInput), labeledControl("Role color", colorRow));
  if (permissionSection) form.append(permissionSection);
  form.append(actions);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = nameInput.value.trim();
    const color = colorHex.value.trim().toUpperCase();
    if (!name) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Enter a role name first.";
      nameInput.focus();
      return;
    }
    if (name.length > 100 || name.toLowerCase() === "@everyone") {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Role names must be 1–100 characters and cannot be @everyone.";
      nameInput.focus();
      return;
    }
    if (!/^#[0-9A-F]{6}$/.test(color)) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Choose a valid six-digit hexadecimal color.";
      colorHex.focus();
      return;
    }
    const payload = { name, color };
    if (editingRole) payload.permissions = permissionDraft;
    if (editingRole) payload.role_id = String(editingRole.id);
    void runRoleAction(editingRole ? "edit" : "create", payload, save, name);
  });

  const list = document.createElement("div");
  list.className = "roles-list";
  const listHeader = document.createElement("div");
  listHeader.className = "roles-list-header";
  listHeader.append(textElement("strong", "", "Server roles"), textElement("span", "roles-count", `${roles.length} role${roles.length === 1 ? "" : "s"}`));
  list.append(listHeader);
  if (!roles.length) list.append(textElement("p", "empty-state", "No custom roles found. Create the first one above."));
  roles.forEach((role) => {
    const row = document.createElement("article");
    row.className = "role-row";
    const swatch = document.createElement("span");
    swatch.className = "role-color-swatch";
    swatch.style.backgroundColor = normalizedRoleColor(role.color);
    swatch.title = normalizedRoleColor(role.color);
    const details = document.createElement("div");
    details.className = "role-row-details";
    details.append(textElement("strong", "", role.name), textElement("small", "", `${normalizedRoleColor(role.color)} · Position ${role.position}`));
    const rowActions = document.createElement("div");
    rowActions.className = "role-row-actions";
    if (role.managed) {
      rowActions.append(textElement("span", "control-panel-card-status", "Managed"));
    } else {
      const edit = textElement("button", "secondary-button", "Edit");
      edit.type = "button";
      edit.addEventListener("click", () => renderRolesPanel(String(role.id)));
      const permissions = textElement("button", "secondary-button", "Permissions");
      permissions.type = "button";
      permissions.addEventListener("click", () => renderRolesPanel(String(role.id)));
      const remove = textElement("button", "danger-button", "Delete");
      remove.type = "button";
      remove.addEventListener("click", () => {
        if (remove.disabled || !window.confirm(`Delete the “${role.name}” role?`)) return;
        void runRoleAction("delete", { role_id: String(role.id) }, remove, role.name);
      });
      rowActions.append(edit, permissions, remove);
    }
    row.append(swatch, details, rowActions);
    list.append(row);
  });
  panel.append(heading, form, list);
  commandGrid.append(panel);
}

function renderLevelPanel() {
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const panel = document.createElement("section");
  panel.className = "level-panel";
  const heading = document.createElement("div");
  heading.className = "level-heading";
  heading.append(
    textElement("h3", "level-title", "Leveling & activity"),
    textElement("p", "level-copy", "Reward members for participating, then celebrate milestones with a clear level-up announcement."),
  );
  const config = managementData?.level || {};
  const form = document.createElement("form");
  form.className = "level-config-form";
  form.noValidate = true;
  const enabledLabel = document.createElement("label");
  enabledLabel.className = "level-enable-field";
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = Boolean(config.enabled);
  enabledLabel.append(enabled, textElement("span", "", "Enable leveling"));

  const styles = [
    ["classic", "Classic XP", "Steady progression with a configurable XP reward per message."],
    ["milestone", "Message milestones", "Simple and predictable: one level every 10 messages."],
    ["activity", "Activity ladder", "Longer-term goals: one level every 25 messages."],
    ["streak", "Daily momentum", "Reward consistent participation with daily and weekly activity."],
  ];
  const selectedConfigStyle = styles.some(([value]) => value === String(config.style || ""))
    ? String(config.style)
    : "classic";
  const styleGrid = document.createElement("div");
  styleGrid.className = "level-style-grid";
  const styleInputs = {};
  styles.forEach(([value, label, description]) => {
    const option = document.createElement("label");
    option.className = "level-style-option";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "level-style";
    input.value = value;
    input.checked = selectedConfigStyle === value;
    styleInputs[value] = input;
    option.append(input, textElement("span", "level-style-copy", label), textElement("small", "level-style-description", description));
    styleGrid.append(option);
  });
  const channel = createChannelSelect();
  channel.value = String(config.channel_id || "");
  const channelField = labeledControl("Level-up announcement channel", channel);
  const language = document.createElement("select");
  language.className = "channel-select";
  language.append(new Option("English", "en"), new Option("Arabic", "ar"));
  language.value = String(config.language || "en");
  const languageField = labeledControl("Announcement language", language);
  const xp = document.createElement("input");
  xp.className = "channel-select";
  xp.type = "number";
  xp.min = "1";
  xp.max = "100";
  xp.step = "1";
  xp.value = String(config.xp_per_message || 10);
  const xpField = labeledControl("XP per message (Classic XP)", xp);
  const save = textElement("button", "primary-button", "Save Level settings");
  save.type = "submit";
  const status = textElement("span", "level-form-status", "");
  const hint = textElement("p", "level-form-hint", "Level-up messages mention the member directly and are sent only to the channel you choose.");
  form.append(
    enabledLabel,
    labeledControl("Choose a progression style", styleGrid),
    channelField,
    languageField,
    xpField,
    hint,
    document.createElement("div"),
  );
  const actions = form.lastElementChild;
  actions.className = "level-form-actions";
  actions.append(save, status);
  const syncEnabled = () => {
    const active = enabled.checked;
    channel.disabled = !active;
    language.disabled = !active;
    Object.values(styleInputs).forEach((input) => { input.disabled = !active; });
    const classicSelected = Boolean(styleInputs.classic?.checked);
    xp.disabled = !active || !classicSelected;
    xpField.hidden = !classicSelected;
  };
  enabled.addEventListener("change", syncEnabled);
  Object.values(styleInputs).forEach((input) => input.addEventListener("change", syncEnabled));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (save.disabled) return;
    if (enabled.checked && !channel.value) {
      status.textContent = "Choose a channel for level-up announcements.";
      status.className = "level-form-status is-error";
      channel.focus();
      return;
    }
    const selectedStyle = Object.entries(styleInputs).find(([, input]) => input.checked)?.[0] || "classic";
    save.disabled = true;
    status.textContent = "Saving...";
    status.className = "level-form-status is-loading";
    try {
      const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/level`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: enabled.checked, style: selectedStyle, channel_id: channel.value || null, language: language.value, xp_per_message: xp.value }),
      });
      managementData.level = result.level || managementData.level;
      managementData.level_stats = result.level_stats || managementData.level_stats;
      status.textContent = "Saved and applied.";
      status.className = "level-form-status is-success";
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Leveling settings saved and applied to this server.";
      renderLevelStats(stats, managementData.level_stats || {});
      syncEnabled();
    } catch (error) {
      status.textContent = errorMessage(error, "Level settings could not be saved.");
      status.className = "level-form-status is-error";
    } finally {
      save.disabled = false;
    }
  });
  const stats = document.createElement("section");
  stats.className = "level-stats";
  const renderRows = (rows, valueLabel) => {
    const list = document.createElement("div");
    list.className = "level-stat-list";
    if (!Array.isArray(rows) || !rows.length) {
      list.append(textElement("p", "empty-state", "No activity recorded yet."));
      return list;
    }
    rows.slice(0, 10).forEach((row, index) => {
      const item = document.createElement("article");
      item.className = "level-stat-row";
      const rank = textElement("span", "level-stat-rank", `#${index + 1}`);
      const avatar = document.createElement("img");
      avatar.className = "level-stat-avatar";
      avatar.src = String(row.avatar_url || "");
      avatar.alt = "";
      avatar.loading = "lazy";
      avatar.decoding = "async";
      avatar.addEventListener("error", () => { avatar.hidden = true; }, { once: true });
      const copy = document.createElement("div");
      copy.className = "level-stat-copy";
      const metric = valueLabel === "XP" ? row.xp : row.messages;
      copy.append(textElement("strong", "", String(row.display_name || row.username || row.member_id || "Member")), textElement("small", "", `${valueLabel}: ${Number(metric || 0).toLocaleString()} Â· Level ${Number(row.level || 1).toLocaleString()}`));
      item.append(rank, avatar, copy);
      list.append(item);
    });
    return list;
  };
  function renderLevelStats(root, source) {
    root.replaceChildren();
    const statDefinitions = [
      ["Top levels", "leaderboard", "XP"],
      ["Most active today", "daily", "messages"],
      ["Most active this week", "weekly", "messages"],
    ];
    statDefinitions.forEach(([title, key, valueLabel]) => {
      const card = document.createElement("section");
      card.className = "level-stat-card";
      card.append(textElement("h4", "", title), renderRows(source?.[key], valueLabel));
      root.append(card);
    });
  }
  renderLevelStats(stats, managementData.level_stats || {});
  const back = textElement("button", "secondary-button", "Back to Control Panel");
  back.type = "button";
  back.addEventListener("click", () => renderControlPanel());
  panel.append(heading, form, stats, back);
  commandGrid.append(panel);
  syncEnabled();
  // Refresh the card when opened so leaderboards remain useful after a long
  // dashboard session without reloading the complete management payload.
  void requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/level`, { cache: "no-store" }).then((fresh) => {
    if (!fresh) return;
    managementData.level = fresh.level || managementData.level;
    managementData.level_stats = fresh.level_stats || managementData.level_stats;
    renderLevelStats(stats, managementData.level_stats || {});
  }).catch(() => {});
}

function renderStreakPanel() {
  stopTempVCRefresh();
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const panel = document.createElement("section");
  panel.className = "streak-panel";
  panel.setAttribute("aria-labelledby", "streak-title");
  const config = managementData?.streak || {};
  const heading = document.createElement("div");
  heading.className = "streak-heading";
  const status = textElement("span", `streak-status ${config.enabled ? "is-enabled" : ""}`, config.enabled ? "Enabled" : "Disabled");
  heading.append(
    textElement("h3", "streak-title", "Streak"),
    textElement("p", "streak-copy", "Build an endless number sequence together. Players take turns sending 1, 2, 3… and every correct number earns a check mark."),
    status,
  );

  const form = document.createElement("form");
  form.className = "streak-config-form";
  form.noValidate = true;
  const enabledField = document.createElement("label");
  enabledField.className = "streak-enable-field";
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = Boolean(config.enabled);
  enabledField.append(enabled, textElement("span", "", "Enable Streak game"));
  const channel = createChannelSelect();
  channel.value = String(config.channel_id || "");
  const channelField = labeledControl("Streak channel", channel);
  const language = document.createElement("select");
  language.className = "channel-select";
  language.append(new Option("English", "en"), new Option("Arabic", "ar"));
  language.value = String(config.language || "en");
  const languageField = labeledControl("Streak message language", language);
  const hint = textElement("p", "streak-form-hint", "Choose English or Arabic for streak messages. Only whole positive numbers are accepted; players must alternate turns, and a wrong number or repeated turn resets the streak to 1." );
  const save = textElement("button", "primary-button", "Save Streak settings");
  save.type = "submit";
  const formStatus = textElement("span", "streak-form-status", "");
  const actions = document.createElement("div");
  actions.className = "streak-actions";
  actions.append(save, formStatus);
  form.append(enabledField, channelField, languageField, hint, actions);

  const state = document.createElement("section");
  state.className = "streak-state";
  const nextNumber = textElement("strong", "streak-next-number", `Next number: ${Math.max(1, Number(config.next_number) || 1)}`);
  const stateCopy = textElement("p", "streak-state-copy", config.enabled
    ? "The game is listening in the selected channel."
    : "Enable the game and save a channel to start the sequence.");
  state.append(textElement("h4", "", "Live sequence"), nextNumber, stateCopy);

  const sync = () => {
    const active = enabled.checked;
    channel.disabled = !active;
    language.disabled = !active;
    status.textContent = active ? "Enabled" : "Disabled";
    status.classList.toggle("is-enabled", active);
    stateCopy.textContent = active
      ? "The game is listening in the selected channel."
      : "Enable the game and save a channel to start the sequence.";
  };
  enabled.addEventListener("change", sync);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (save.disabled) return;
    if (enabled.checked && !channel.value) {
      formStatus.textContent = "Choose a channel for the Streak game.";
      formStatus.className = "streak-form-status is-error";
      channel.focus();
      return;
    }
    save.disabled = true;
    formStatus.textContent = "Saving...";
    formStatus.className = "streak-form-status is-loading";
    try {
      const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/streak`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: enabled.checked, channel_id: channel.value || null, language: language.value }),
      });
      managementData.streak = result.streak || managementData.streak;
      const currentNext = Math.max(1, Number(managementData.streak?.next_number) || 1);
      nextNumber.textContent = `Next number: ${currentNext}`;
      formStatus.textContent = "Saved and reset to 1.";
      formStatus.className = "streak-form-status is-success";
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Streak settings saved and applied to this server.";
      sync();
    } catch (error) {
      formStatus.textContent = errorMessage(error, "Streak settings could not be saved.");
      formStatus.className = "streak-form-status is-error";
    } finally {
      save.disabled = false;
    }
  });

  const back = textElement("button", "secondary-button", "Back to Control Panel");
  back.type = "button";
  back.addEventListener("click", () => renderControlPanel());
  panel.append(heading, form, state, back);
  commandGrid.append(panel);
  sync();
}

function setManagementTab(tab) {
  if (tab !== "control") stopTempVCRefresh();
  if (tab !== "games" && window.BirdBotGames?.unmount) window.BirdBotGames.unmount();
  const canConfigure = managementData?.guild?.can_configure !== false;
  document.querySelectorAll("[data-management-tab]").forEach((button) => {
    button.hidden = (["commands", "control", "logs"].includes(button.dataset.managementTab) && !canConfigure);
  });
  if (!canConfigure && (["commands", "control", "logs"].includes(tab))) tab = "tickets";
  activeManagementTab = tab;
  document.querySelectorAll("[data-management-tab]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.managementTab === tab);
  });
  commandFeedback.hidden = true;
  showTicketsButton.hidden = tab !== "tickets";
  ticketLogsButton.hidden = tab !== "tickets";
  if (tab !== "commands") {
    managementTitle.textContent = tab === "tickets"
      ? "Ticket system"
      : tab === "control"
        ? "Control Panel"
      : tab === "logs"
        ? "Logs"
      : tab === "ticket_logs"
        ? "Ticket Logs"
      : tab === "games"
            ? "Games"
          : "Ticket Logs";
    managementDescription.textContent = tab === "tickets"
      ? "Build the panel your members will use to open a ticket."
      : tab === "control"
        ? "Manage messages, roles, channels, voice, DMs, leveling, streak games, and your bot profile."
      : tab === "logs"
        ? "Choose where BirdBot sends activity logs and enable the events you want to track."
      : tab === "games"
           ? "Choose a mini-game, review its rules, and browse completed match logs."
        : "This section is ready for the next BirdBot feature.";
    if (tab === "tickets" && window.BirdBotTickets && managementData?.guild?.id) {
      // Support-role staff can work tickets and logs, but only an owner or
      // Administrator may edit the panel configuration.
      if (managementData.guild.can_configure === false) {
        ticketPageMode = "list";
        showTicketsButton.textContent = "Ticket settings";
        void loadTicketsPage();
        return;
      }
      ticketPageMode = "config";
      showTicketsButton.textContent = "Show Tickets";
      window.BirdBotTickets.mount({
        root: commandGrid,
        guildId: managementData.guild.id,
        requestJson,
        beginLoading,
        endLoading,
      });
      return;
    }
    if (tab === "logs" && managementData?.guild?.id) {
      void loadGuildLogs();
      return;
    }
    if (tab === "ticket_logs" && managementData?.guild?.id) {
      void loadTicketLogs();
      return;
    }
    if (tab === "games" && window.BirdBotGames && managementData?.guild?.id) {
      window.BirdBotGames.mount({
        root: commandGrid,
        guildId: managementData.guild.id,
        channels: managementData.channels || [],
        requestJson,
        beginLoading,
        endLoading,
      });
      return;
    }
    if (tab === "control") {
      renderControlPanel();
      return;
    }
    commandGrid.replaceChildren(textElement("p", "empty-state", "Coming soon."));
    return;
  }
  managementTitle.textContent = "Commands";
  managementDescription.textContent = "Browse organized command cards for General and Moderation tools. Open a card to configure shortcuts, language, and run options.";
  renderCommands();
}

function formatTicketDate(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}

function ticketLogIdControl(label, value) {
  if (value == null || value === "") return null;
  const wrapper = document.createElement("span");
  wrapper.className = "ticket-log-id-control";
  wrapper.append(textElement("span", "ticket-log-id-label", `${label}:`));
  wrapper.append(textElement("code", "ticket-log-id", String(value)));
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "ticket-log-copy";
  // Keep this source ASCII-safe; the runtime still renders the clipboard
  // emoji even when a legacy charset header is used by a deployment.
  copy.textContent = "\u{1F4CB} Copy";
  copy.title = `Copy ${label}`;
  copy.setAttribute("aria-label", `Copy ${label}`);
  copy.addEventListener("click", () => copyTicketLogId(String(value), copy));
  wrapper.append(copy);
  return wrapper;
}

async function copyTicketLogId(value, button) {
  let copied = false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      copied = true;
    }
  } catch (_) {
    copied = false;
  }
  if (!copied) {
    const input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "true");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.append(input);
    input.select();
    try { copied = document.execCommand("copy"); } catch (_) { copied = false; }
    input.remove();
  }
  if (!copied) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = "Could not copy the ID. Please copy it manually.";
    return;
  }
  const original = button.textContent;
  button.textContent = "\u2705 Copied";
  button.title = "ID Copied!";
  commandFeedback.hidden = false;
  commandFeedback.textContent = "ID Copied!";
  window.setTimeout(() => {
    button.textContent = original;
    button.title = "Copy ID";
    if (commandFeedback.textContent === "ID Copied!") commandFeedback.hidden = true;
  }, 1_200);
}

async function waitForCommandRequest(requestId) {
  const deadline = Date.now() + 25_000;
  while (Date.now() < deadline) {
    const result = await requestJson(`/api/command-requests/${encodeURIComponent(requestId)}`);
    if (result.status === "complete") return result;
    if (result.status === "failed") throw new Error(result.error || "BirdBot could not complete that ticket action.");
    await delay(450);
  }
  throw new Error("That ticket action is taking too long. Refresh the list shortly.");
}

function renderTicketList(tickets) {
  window.clearInterval(ticketCountdownTimer);
  ticketCountdownTimer = null;
  commandGrid.replaceChildren();
  const closedTickets = tickets.filter((ticket) => ticket.status === "closed");
  const listToolbar = document.createElement("div");
  listToolbar.className = "ticket-list-toolbar";
  const listSummary = textElement("span", "ticket-list-summary", `${tickets.length} ticket${tickets.length === 1 ? "" : "s"} · ${closedTickets.length} closed`);
  const deleteAll = document.createElement("button");
  deleteAll.className = "danger-button";
  deleteAll.type = "button";
  deleteAll.textContent = "Delete All Closed Tickets";
  deleteAll.disabled = !closedTickets.length;
  deleteAll.addEventListener("click", () => {
    if (!closedTickets.length || deleteAll.disabled) return;
    if (window.confirm(`Permanently delete ${closedTickets.length} closed ticket record${closedTickets.length === 1 ? "" : "s"}? Ticket logs and transcripts will be preserved.`)) {
      runDeleteAllClosedTickets(deleteAll);
    }
  });
  listToolbar.append(listSummary, deleteAll);
  commandGrid.append(listToolbar);
  const backToSettings = document.createElement("button");
  backToSettings.className = "secondary-button ticket-settings-back";
  backToSettings.type = "button";
  backToSettings.textContent = "Ticket settings";
  backToSettings.addEventListener("click", () => {
    ticketPageMode = "config";
    showTicketsButton.textContent = "Show Tickets";
    setManagementTab("tickets");
  });
  commandGrid.append(backToSettings);
  if (!tickets.length) {
    commandGrid.append(textElement("p", "empty-state ticket-list-empty", "No tickets have been created in this server yet."));
    return;
  }
  const grid = document.createElement("div");
  grid.className = "ticket-list-grid";
  tickets.forEach((ticket) => {
    const card = document.createElement("article");
    card.className = `ticket-record-card ticket-status-${ticket.status || "open"}`;
    card.dataset.ticketId = ticket.ticket_id || "";
    const header = document.createElement("div");
    header.className = "ticket-record-header";
    const statusArea = document.createElement("div");
    statusArea.className = "ticket-record-status-area";
    const status = textElement("span", "ticket-record-status", ticket.status === "claimed" ? "Claimed" : ticket.status === "closed" ? "Closed" : "Active");
    statusArea.append(status);
    if (ticket.status === "open" && !ticket.claimed_by) {
      const deadline = Date.parse(String(ticket.unclaimed_until || ""));
      if (Number.isFinite(deadline)) {
        const timeout = textElement("span", "ticket-record-timeout", "Timeout in: --:--");
        timeout.dataset.timeoutDeadline = String(deadline);
        statusArea.append(timeout);
      }
    }
    header.append(
      textElement("strong", "ticket-record-channel", `#${ticket.channel_name || ticket.channel_id}`),
      statusArea,
    );
    const details = document.createElement("dl");
    details.className = "ticket-record-details";
    const addDetail = (label, value) => {
      const row = document.createElement("div");
      row.append(textElement("dt", "", label), textElement("dd", "", value || "Unknown"));
      details.append(row);
    };
    addDetail("Ticket ID", ticket.ticket_id);
    addDetail("Creator", ticket.creator_name);
    addDetail("Creator ID", ticket.creator_id);
    addDetail("Topic", ticket.option_label);
    addDetail("Priority", String(ticket.priority || "medium").replace(/^./, (character) => character.toUpperCase()));
    addDetail("Category", ticket.category_name || "No category");
    addDetail("Opened", formatTicketDate(ticket.created_at));
    if (ticket.claimed_by_name) addDetail("Claimed by", ticket.claimed_by_name);
    if (ticket.closed_at) addDetail("Closed", formatTicketDate(ticket.closed_at));
    if (ticket.transcript_url) {
      const transcript = document.createElement("a");
      transcript.href = transcriptLink(ticket.transcript_url);
      transcript.target = "_blank";
      transcript.rel = "noopener";
      transcript.className = "ticket-log-transcript";
      transcript.textContent = "View transcript";
      details.append(transcript);
    }
    const manage = document.createElement("button");
    manage.className = "secondary-button ticket-manage-button";
    manage.type = "button";
    manage.textContent = "Manage";
    const controls = document.createElement("div");
    controls.className = "ticket-record-actions";
    controls.hidden = true;
    let quickDelete = null;
    if (ticket.status !== "closed" && ticket.channel_available) {
      const claim = document.createElement("button");
      claim.className = "secondary-button";
      claim.type = "button";
      claim.textContent = ticket.status === "claimed" ? `Claimed by ${ticket.claimed_by_name || "staff"}` : "Claim ticket";
      claim.disabled = ticket.status === "claimed";
      if (ticket.status !== "claimed") claim.addEventListener("click", () => runTicketAction(ticket, "claim", claim));
      controls.append(claim);
      const close = document.createElement("button");
      close.className = "danger-button";
      close.type = "button";
      close.textContent = "Close ticket";
      close.addEventListener("click", () => {
        if (window.confirm("Close this ticket and generate its transcript in the configured logs channel?")) {
          runTicketAction(ticket, "close", close);
        }
      });
      controls.append(close);
      const memberControl = createMemberSelect();
      let searchTimer = null;
      memberControl.search.addEventListener("input", () => {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(() => searchMembers(memberControl.search.value, memberControl.select), 180);
      });
      const memberActions = document.createElement("div");
      memberActions.className = "ticket-member-actions";
      const addMember = document.createElement("button");
      addMember.className = "secondary-button";
      addMember.type = "button";
      addMember.textContent = "Add User";
      addMember.addEventListener("click", () => runTicketMemberAction(ticket, "add", memberControl.select, addMember));
      const removeMember = document.createElement("button");
      removeMember.className = "danger-button";
      removeMember.type = "button";
      removeMember.textContent = "Remove User";
      removeMember.addEventListener("click", () => runTicketMemberAction(ticket, "remove", memberControl.select, removeMember));
      memberActions.append(addMember, removeMember);
      controls.append(labeledControl("Ticket member", memberControl.element), memberActions);
    } else if (ticket.status !== "closed") {
      controls.append(textElement("span", "ticket-record-unavailable", "The Discord channel is no longer available."));
    } else {
      const deleteButton = document.createElement("button");
      deleteButton.className = "danger-button";
      deleteButton.type = "button";
      deleteButton.textContent = "Delete";
      deleteButton.addEventListener("click", () => {
        if (window.confirm("Permanently delete this closed ticket record? Ticket logs and transcript files will be preserved.")) {
          runTicketDeletion(ticket, deleteButton);
        }
      });
      quickDelete = deleteButton;
      controls.append(textElement("span", "ticket-record-unavailable", "This ticket is closed."));
    }
    manage.addEventListener("click", () => {
      controls.hidden = !controls.hidden;
      manage.textContent = controls.hidden ? "Manage" : "Hide actions";
    });
    card.append(header, details);
    if (quickDelete) card.append(quickDelete);
    card.append(manage, controls);
    grid.append(card);
  });
  commandGrid.append(grid);
  startTicketCountdowns();
}

async function runTicketDeletion(ticket, button) {
  if (button.disabled) return;
  if (ticket.status !== "closed") {
    commandFeedback.hidden = false;
    commandFeedback.textContent = "Error: You must close the ticket first before deleting it.";
    return;
  }
  button.disabled = true;
  beginLoading("Deleting closed ticket...");
  try {
    await requestJson(
      `/api/guilds/${encodeURIComponent(managementData.guild.id)}/tickets/${encodeURIComponent(ticket.ticket_id)}`,
      { method: "DELETE" },
    );
    await loadTicketsPage(false);
    commandFeedback.hidden = false;
    commandFeedback.textContent = "Closed ticket deleted. Ticket logs and transcripts were preserved.";
  } catch (error) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "The closed ticket could not be deleted.");
  } finally {
    button.disabled = false;
    endLoading();
  }
}

async function runDeleteAllClosedTickets(button) {
  if (button.disabled) return;
  button.disabled = true;
  beginLoading("Deleting closed tickets...");
  try {
    const result = await requestJson(
      `/api/guilds/${encodeURIComponent(managementData.guild.id)}/tickets/closed/delete`,
      { method: "POST" },
    );
    await loadTicketsPage(false);
    commandFeedback.hidden = false;
    commandFeedback.textContent = `${result.deleted || 0} closed ticket record${result.deleted === 1 ? "" : "s"} deleted. Ticket logs and transcripts were preserved.`;
  } catch (error) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "Closed tickets could not be deleted.");
  } finally {
    button.disabled = false;
    endLoading();
  }
}

async function runTicketMemberAction(ticket, action, memberSelect, button) {
  if (button.disabled) return;
  if (!memberSelect.value) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = "Choose a member first.";
    return;
  }
  button.disabled = true;
  beginLoading(action === "add" ? "Adding member to ticket..." : "Removing member from ticket...");
  try {
    const queued = await requestJson(
      `/api/guilds/${encodeURIComponent(managementData.guild.id)}/tickets/${encodeURIComponent(ticket.ticket_id)}/members/${action}`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ member_id: memberSelect.value }) },
    );
    await waitForCommandRequest(queued.request_id);
    await loadTicketsPage(false);
    commandFeedback.hidden = false;
    commandFeedback.textContent = action === "add" ? "Member added to the ticket." : "Member removed from the ticket.";
  } catch (error) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "The member action could not be completed.");
  } finally {
    button.disabled = false;
    endLoading();
  }
}

async function runTicketAction(ticket, action, button) {
  if (button.disabled) return;
  const ticketKey = String(ticket.ticket_id);
  const card = Array.from(commandGrid.querySelectorAll("[data-ticket-id]"))
    .find((candidate) => candidate.dataset.ticketId === ticketKey);
  const status = card?.querySelector(".ticket-record-status");
  const previousStatus = status?.textContent || "";
  button.disabled = true;
  if (status) {
    status.textContent = action === "claim" ? "Claiming…" : "Closing…";
    card.classList.add("ticket-action-pending");
  }
  beginLoading(action === "claim" ? "Claiming ticket..." : "Closing ticket and generating transcript...");
  try {
    const queued = await requestJson(
      `/api/guilds/${encodeURIComponent(managementData.guild.id)}/tickets/${encodeURIComponent(ticket.ticket_id)}/${action}`,
      { method: "POST" },
    );
    await waitForCommandRequest(queued.request_id);
    const loaded = await loadTicketsPage(false);
    if (loaded) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = action === "claim" ? "Ticket claimed successfully." : "Ticket closed and logged successfully.";
    }
  } catch (error) {
    if (status) status.textContent = previousStatus;
    card?.classList.remove("ticket-action-pending");
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "The ticket action could not be completed.");
  } finally {
    button.disabled = false;
    endLoading();
  }
}

async function loadTicketsPage(showLoading = true) {
  if (!managementData?.guild?.id) return;
  if (showLoading) {
    renderTicketListSkeleton();
    beginLoading("Loading tickets...", false);
  }
  try {
    const result = await requestJson(
      `/api/guilds/${encodeURIComponent(managementData.guild.id)}/tickets`,
      { cache: "no-store" },
    );
    const serverTime = Date.parse(String(result.server_time || ""));
    if (Number.isFinite(serverTime)) {
      ticketServerClockOffsetMs = serverTime - Date.now();
    } else {
      ticketServerClockOffsetMs = 0;
    }
    ticketPageMode = "list";
    showTicketsButton.textContent = "Ticket settings";
    managementTitle.textContent = "Ticket system";
    managementDescription.textContent = "Review active and historical tickets, claim ownership, or close a ticket with a transcript log.";
    commandFeedback.hidden = true;
    const tickets = result.tickets || [];
    const snapshot = JSON.stringify(tickets);
    // Keep open management controls and search state intact during background
    // polling.  A redraw is only needed when the server data changes.
    if (showLoading || snapshot !== ticketListSnapshot) {
      ticketListSnapshot = snapshot;
      renderTicketList(tickets);
    }
    return true;
  } catch (error) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "Tickets could not be loaded.");
    return false;
  } finally {
    if (showLoading) endLoading(false);
  }
}

function renderTicketListSkeleton() {
  commandGrid.replaceChildren();
  const grid = document.createElement("div");
  grid.className = "ticket-list-grid ticket-list-skeleton";
  for (let index = 0; index < 3; index += 1) {
    const card = document.createElement("article");
    card.className = "ticket-record-card";
    card.append(textElement("span", "skeleton-line skeleton-line-wide", ""));
    card.append(textElement("span", "skeleton-line", ""));
    card.append(textElement("span", "skeleton-line skeleton-line-short", ""));
    grid.append(card);
  }
  commandGrid.append(grid);
}

function renderTicketLogs(logs, query = "") {
  ticketLogsQuery = query;
  commandGrid.replaceChildren();
  const wrapper = document.createElement("section");
  wrapper.className = "ticket-logs-view";
  const toolbar = document.createElement("div");
  toolbar.className = "ticket-logs-toolbar";
  const search = document.createElement("input");
  search.type = "search";
  search.className = "channel-select ticket-logs-search";
  search.placeholder = "Search ticket logs...";
  search.value = query;
  const count = textElement("span", "ticket-logs-count", `${logs.length} event${logs.length === 1 ? "" : "s"}`);
  toolbar.append(search, count);
  if (managementData?.guild?.can_configure !== false) {
    const deleteAll = document.createElement("button");
    deleteAll.type = "button";
    deleteAll.className = "danger-button ticket-logs-delete-all";
    deleteAll.textContent = "Delete All Logs";
    deleteAll.addEventListener("click", () => {
      if (deleteAll.disabled) return;
      if (window.confirm("Permanently delete every ticket log for this server? Ticket records and transcripts will be preserved.")) {
        void runDeleteAllTicketLogs(deleteAll);
      }
    });
    toolbar.append(deleteAll);
  }
  const table = document.createElement("div");
  table.className = "ticket-logs-table";
  if (!logs.length) {
    table.append(textElement("p", "empty-state", "No ticket events have been logged yet."));
  }
  logs.forEach((log) => {
    const row = document.createElement("article");
    row.className = `ticket-log-row ticket-log-${log.event_type || "event"}`;
    const rawEventType = String(log.event_type || "event").toLowerCase();
    const eventName = rawEventType === "auto_deleted"
      ? "Ticket Auto-Deleted (Unclaimed Timeout - 5 min)"
      : rawEventType.replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase());
    const actor = log.actor_name || "System";
    const channel = log.channel_name ? `#${log.channel_name}` : "Unknown channel";
    const rowHeader = document.createElement("div");
    rowHeader.className = "ticket-log-header";
    const summary = document.createElement("div");
    summary.className = "ticket-log-summary";
    rowHeader.append(summary);
    if (managementData?.guild?.can_configure !== false) {
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "danger-button ticket-log-delete";
      deleteButton.textContent = "Delete";
      deleteButton.addEventListener("click", () => {
        if (window.confirm("Permanently delete this ticket log? The ticket record and transcript will be preserved.")) {
          void runTicketLogDeletion(log, deleteButton);
        }
      });
      rowHeader.append(deleteButton);
    }
    // Keep the log row neutral and color only the lifecycle event label.
    const eventLabel = textElement("span", "ticket-log-event-name", eventName);
    summary.replaceChildren(eventLabel, document.createTextNode(` · ${channel}`));
    const dmStatus = String(log.dm_status || "").trim().toLowerCase();
    const dmDelivered = ["delivered", "sent", "true"].includes(dmStatus);
    const isClosureEvent = ["closed", "auto_deleted"].includes(String(log.event_type || "").toLowerCase());
    if (isClosureEvent && dmStatus) {
      summary.append(
        document.createTextNode(" "),
        textElement(
          "span",
          `ticket-log-dm ticket-log-dm-${dmDelivered ? "delivered" : "failed"}`,
          dmDelivered ? "DM Sent" : "DM Failed / DMs Closed",
        ),
      );
    }
    const meta = textElement("div", "ticket-log-meta", `${formatTicketDate(log.created_at)} · Actor: ${actor}`);
    if (log.creator_name) meta.textContent += ` · Creator: ${log.creator_name}`;
    if (log.priority) meta.textContent += ` · Priority: ${String(log.priority).replace(/^./, (character) => character.toUpperCase())}`;
    if (log.duration_seconds != null) {
      const totalMinutes = Math.floor(Number(log.duration_seconds) / 60);
      meta.textContent += ` · Open ${Math.floor(totalMinutes / 60)}h ${totalMinutes % 60}m`;
    }
    row.append(rowHeader, meta);
    const identifiers = document.createElement("div");
    identifiers.className = "ticket-log-identifiers";
    [
      ["Log ID", log.log_id],
      ["Ticket ID", log.ticket_id],
      ["User ID", log.creator_id],
      [log.event_type === "opened" ? "Actor ID" : "Staff ID", log.actor_id],
      ["Ticket Channel ID", log.channel_id],
    ].forEach(([label, value]) => {
      const control = ticketLogIdControl(label, value);
      if (control) identifiers.append(control);
    });
    if (identifiers.children.length) row.append(identifiers);
    if (log.details) row.append(textElement("p", "ticket-log-details", log.details));
    if (log.transcript_url) {
      const link = document.createElement("a");
      link.href = transcriptLink(log.transcript_url);
      link.target = "_blank";
      link.rel = "noopener";
      link.className = "ticket-log-transcript";
      link.textContent = "View transcript";
      row.append(link);
    }
    table.append(row);
  });
  search.addEventListener("input", () => {
    const needle = search.value.trim().toLowerCase();
    const filtered = logs.filter((log) => JSON.stringify(log).toLowerCase().includes(needle));
    renderTicketLogs(filtered, search.value);
    const replacement = commandGrid.querySelector(".ticket-logs-search");
    if (replacement) {
      replacement.focus();
      replacement.setSelectionRange(replacement.value.length, replacement.value.length);
    }
  });
  wrapper.append(toolbar, table);
  commandGrid.append(wrapper);
}

async function loadTicketLogs(showLoading = true) {
  if (!managementData?.guild?.id) return;
  if (showLoading) {
    renderTicketLogsSkeleton();
    beginLoading("Loading ticket logs...", false);
  }
  try {
    const query = ticketLogsQuery.trim();
    const queryString = query ? `?q=${encodeURIComponent(query)}` : "";
    const result = await requestJson(
      `/api/guilds/${encodeURIComponent(managementData.guild.id)}/tickets/logs${queryString}`,
      { cache: "no-store" },
    );
    managementTitle.textContent = "Ticket logs";
    managementDescription.textContent = "Search ticket activity, staff actions, and transcript links.";
    const logs = result.logs || [];
    const snapshot = JSON.stringify(logs);
    if (showLoading || snapshot !== ticketLogsSnapshot) {
      ticketLogsSnapshot = snapshot;
      renderTicketLogs(logs, ticketLogsQuery);
    }
  } catch (error) {
    commandGrid.replaceChildren(textElement("p", "form-error", errorMessage(error, "Ticket logs could not be loaded.")));
  } finally {
    if (showLoading) endLoading(false);
  }
}

function renderTicketLogsSkeleton() {
  commandGrid.replaceChildren();
  const list = document.createElement("section");
  list.className = "ticket-logs-view ticket-logs-skeleton";
  for (let index = 0; index < 5; index += 1) {
    const row = document.createElement("article");
    row.className = "ticket-log-row";
    row.append(textElement("span", "skeleton-line skeleton-line-wide", ""), textElement("span", "skeleton-line", ""));
    list.append(row);
  }
  commandGrid.append(list);
}

async function runTicketLogDeletion(log, button) {
  if (button.disabled) return;
  button.disabled = true;
  const row = button.closest(".ticket-log-row");
  row?.classList.add("ticket-log-row-pending");
  beginLoading("Deleting ticket log...");
  try {
    await requestJson(
      `/api/guilds/${encodeURIComponent(managementData.guild.id)}/tickets/logs/${encodeURIComponent(log.log_id)}`,
      { method: "DELETE" },
    );
    await loadTicketLogs(false);
    commandFeedback.hidden = false;
    commandFeedback.textContent = "Ticket log deleted.";
  } catch (error) {
    row?.classList.remove("ticket-log-row-pending");
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "The ticket log could not be deleted.");
    button.disabled = false;
  } finally {
    endLoading();
  }
}

async function runDeleteAllTicketLogs(button) {
  if (button.disabled) return;
  button.disabled = true;
  beginLoading("Deleting ticket logs...");
  try {
    const result = await requestJson(
      `/api/guilds/${encodeURIComponent(managementData.guild.id)}/tickets/logs/delete-all`,
      { method: "POST" },
    );
    await loadTicketLogs(false);
    commandFeedback.hidden = false;
    commandFeedback.textContent = `${result.deleted || 0} ticket log${result.deleted === 1 ? "" : "s"} deleted.`;
  } catch (error) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "Ticket logs could not be deleted.");
    button.disabled = false;
  } finally {
    endLoading();
  }
}

const guildLogCategories = [
  ["voice", "Voice activity", "Joins, leaves, moves, disconnects, server mute and deaf changes."],
  ["messages", "Message activity", "Messages sent, edited, or deleted in server channels."],
  ["server", "Server changes", "Server settings, channel, and role changes."],
  ["members", "Member activity", "Members joining, leaving, or changing roles/profile details."],
  ["moderation", "Moderation actions", "Bans, kicks, warnings, warning removals, timeouts, timeout removals, mutes, un-mutes, unbans, and Automod actions."],
];

async function createLogChannel(category, button) {
  if (!managementData?.guild?.id || button.disabled) return;
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = "Creating...";
  beginLoading("Creating log channel...");
  try {
    const queued = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/logs/create-channel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category }),
    });
    const result = await waitForDashboardCommand(queued.request_id, 80);
    if (result.status !== "complete") throw new Error("Channel creation is still queued. Check Discord shortly.");
    const created = result.result || {};
    if (created.channel_id) {
      const id = String(created.channel_id);
      const name = String(created.name || `${category}-logs`);
      const existing = Array.isArray(managementData.channels) ? managementData.channels : [];
      if (!existing.some((channel) => String(channel.id) === id)) managementData.channels = [...existing, { id, name }].sort((a, b) => String(a.name).localeCompare(String(b.name)));
      const current = managementData.log_config || {};
      const destinations = { ...(current.category_channels || {}), [category]: id };
      managementData.log_config = { ...current, categories: { ...(current.categories || {}), [category]: true }, category_channels: destinations };
    }
    commandFeedback.hidden = false;
    commandFeedback.textContent = `${created.name ? `#${created.name}` : "The log channel"} is ready and assigned to ${category} logs.`;
    renderGuildLogs(managementData.__guildLogs || [], guildLogsQuery);
  } catch (error) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "The log channel could not be created.");
  } finally {
    endLoading();
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function guildLogEventName(value) {
  return String(value || "event").replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase());
}

function guildLogDetails(log) {
  let details = String(log.details || "");
  const replacements = [
    [log.actor_id, log.actor_name, "user"],
    [log.target_id, log.target_name, String(log.event_type || "").startsWith("role_") ? "role" : "user"],
    [log.channel_id, log.channel_name, "channel"],
  ];
  replacements.forEach(([id, name, kind]) => {
    if (!id || !name) return;
    const mention = kind === "channel" ? `<#${id}>` : kind === "role" ? `<@&${id}>` : `<@${id}>`;
    details = details.split(mention).join(kind === "channel" ? `#${name}` : `@${name}`);
  });
  return details;
}

function renderGuildLogs(logs, query = "") {
  guildLogsQuery = query;
  commandGrid.replaceChildren();
  const wrapper = document.createElement("section");
  wrapper.className = "guild-logs-view";
  const settingsPanel = document.createElement("section");
  settingsPanel.className = "guild-logs-settings";
  const settingsHeading = document.createElement("div");
  settingsHeading.className = "guild-logs-heading";
  settingsHeading.append(
    textElement("strong", "", "Activity log settings"),
    textElement("p", "command-settings-hint", "Choose a separate channel for each activity stream. Create Channel makes a private administrator-only destination and assigns it automatically."),
  );
  const config = managementData?.log_config || {};
  const categoryValues = config.categories || {};
  const categoryDestinations = config.category_channels || {};
  const enabledLabel = document.createElement("label");
  enabledLabel.className = "command-inline-toggle guild-logs-master";
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = config.enabled === true;
  enabledLabel.append(enabled, textElement("span", "", "Enable activity logs"));
  const categoryGrid = document.createElement("div");
  categoryGrid.className = "guild-logs-category-grid";
  const categoryInputs = {};
  const categoryChannelInputs = {};
  guildLogCategories.forEach(([key, title, description]) => {
    const label = document.createElement("div");
    label.className = "guild-log-category";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.setAttribute("aria-label", `Enable ${title}`);
    checkbox.checked = categoryValues[key] !== false;
    categoryInputs[key] = checkbox;
    const copy = document.createElement("span");
    copy.className = "guild-log-category-copy";
    copy.append(textElement("strong", "guild-log-category-title", title), textElement("small", "guild-log-category-description", description));
    const channel = createChannelSelect();
    channel.classList.add("guild-log-category-channel");
    channel.setAttribute("aria-label", `${title} log channel`);
    channel.value = categoryDestinations[key] || "";
    categoryChannelInputs[key] = channel;
    const create = textElement("button", "secondary-button guild-log-create-channel", "Create Channel");
    create.type = "button";
    create.disabled = managementData?.guild?.can_configure === false;
    create.title = create.disabled ? "Only server managers can create channels." : `Create a private administrator-only ${title.toLowerCase()} channel`;
    create.addEventListener("click", (event) => {
      event.preventDefault();
      void createLogChannel(key, create);
    });
    label.append(checkbox, copy, channel, create);
    categoryGrid.append(label);
  });
  const save = document.createElement("button");
  save.type = "button";
  save.className = "primary-button guild-logs-save";
  save.textContent = "Save log settings";
  const status = textElement("span", "command-settings-status", "");
  save.addEventListener("click", async () => {
    save.disabled = true;
    status.textContent = "Saving...";
    status.className = "command-settings-status is-loading";
    try {
      const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/logs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: enabled.checked,
          log_channel_id: null,
          categories: Object.fromEntries(Object.entries(categoryInputs).map(([key, input]) => [key, input.checked])),
          category_channels: Object.fromEntries(Object.entries(categoryChannelInputs).map(([key, input]) => [key, input.value || null])),
        }),
      });
      managementData.log_config = result.log_config;
      status.textContent = "Saved and applied to Discord.";
      status.className = "command-settings-status is-success";
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Activity log settings saved.";
    } catch (error) {
      status.textContent = errorMessage(error, "Log settings could not be saved.");
      status.className = "command-settings-status is-error";
    } finally {
      save.disabled = false;
    }
  });
  settingsPanel.append(
    settingsHeading,
    enabledLabel,
    categoryGrid,
    document.createElement("div"),
  );
  const actions = settingsPanel.lastElementChild;
  actions.className = "guild-logs-actions";
  actions.append(save, status);
  const toolbar = document.createElement("div");
  toolbar.className = "ticket-logs-toolbar guild-logs-toolbar";
  const search = document.createElement("input");
  search.type = "search";
  search.className = "channel-select ticket-logs-search";
  search.placeholder = "Search activity logs...";
  search.value = query;
  const count = textElement("span", "ticket-logs-count", `${logs.length} event${logs.length === 1 ? "" : "s"}`);
  toolbar.append(search, count);
  const table = document.createElement("div");
  table.className = "ticket-logs-table guild-logs-table";
  if (!logs.length) table.append(textElement("p", "empty-state", "No activity events have been logged yet."));
  logs.forEach((log) => {
    const row = document.createElement("article");
    row.className = "ticket-log-row guild-log-row";
    const header = document.createElement("div");
    header.className = "ticket-log-header guild-log-header";
    const actorIdentity = document.createElement("span");
    actorIdentity.className = "guild-log-identity";
    if (typeof log.actor_avatar_url === "string" && /^https:\/\//i.test(log.actor_avatar_url)) {
      const avatar = document.createElement("img");
      avatar.className = "guild-log-avatar";
      avatar.src = log.actor_avatar_url;
      avatar.alt = "";
      avatar.loading = "lazy";
      avatar.decoding = "async";
      avatar.addEventListener("error", () => avatar.remove(), { once: true });
      actorIdentity.append(avatar);
    }
    const actorName = String(log.actor_name || "System");
    actorIdentity.append(textElement("span", "guild-log-mention", log.actor_id ? `@${actorName}` : actorName));
    header.append(actorIdentity, textElement("span", "ticket-log-event-name guild-log-event", guildLogEventName(log.event_type)));
    const channel = log.channel_name ? `#${log.channel_name}` : "No channel";
    header.append(textElement("span", "guild-log-channel", channel));
    const meta = textElement("div", "ticket-log-meta", formatTicketDate(log.created_at));
    if (log.target_name) meta.textContent += ` · Target: ${log.target_id ? `@${log.target_name}` : log.target_name}`;
    row.append(header, meta);
    if (log.details) row.append(textElement("p", "ticket-log-details guild-log-details", guildLogDetails(log)));
    table.append(row);
  });
  search.addEventListener("input", () => {
    const needle = search.value.trim().toLowerCase();
    const filtered = logs.filter((log) => JSON.stringify(log).toLowerCase().includes(needle));
    renderGuildLogs(filtered, search.value);
    const replacement = commandGrid.querySelector(".ticket-logs-search");
    if (replacement) {
      replacement.focus();
      replacement.setSelectionRange(replacement.value.length, replacement.value.length);
    }
  });
  wrapper.append(settingsPanel, toolbar, table);
  commandGrid.append(wrapper);
}

async function loadGuildLogs(showLoading = true) {
  if (!managementData?.guild?.id) return;
  if (showLoading) {
    renderTicketLogsSkeleton();
    beginLoading("Loading activity logs...", false);
  }
  try {
    const query = guildLogsQuery.trim();
    const queryString = query ? `?q=${encodeURIComponent(query)}` : "";
    const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/logs${queryString}`, { cache: "no-store" });
    managementData.log_config = result.log_config || managementData.log_config || {};
    managementTitle.textContent = "Logs";
    managementDescription.textContent = "Choose where BirdBot sends activity logs and enable the events you want to track.";
  const logs = result.logs || [];
    managementData.__guildLogs = logs;
    const snapshot = JSON.stringify({ logs, config: managementData.log_config });
    if (showLoading || snapshot !== guildLogsSnapshot) {
      guildLogsSnapshot = snapshot;
      renderGuildLogs(logs, guildLogsQuery);
    }
  } catch (error) {
    commandGrid.replaceChildren(textElement("p", "form-error", errorMessage(error, "Activity logs could not be loaded.")));
  } finally {
    if (showLoading) endLoading(false);
  }
}

function renderCommands() {
  commandGrid.replaceChildren();
  const saved = managementData.command_settings || {};
  const draft = {
    prefix: typeof saved.prefix === "string" && saved.prefix ? saved.prefix : "!",
    prefix_enabled: saved.prefix_enabled !== false,
    commands: {},
  };
  managementData.commands.forEach((command) => {
    const setting = saved.commands?.[command.name] || {};
    draft.commands[command.name] = {
      enabled: setting.enabled !== false,
      language: setting.language === "ar" ? "ar" : "en",
      shortcuts: Array.isArray(setting.shortcuts) ? [...setting.shortcuts] : [],
    };
  });

  const toolbar = document.createElement("section");
  toolbar.className = "commands-settings-toolbar";
  const toolbarHeading = document.createElement("div");
  toolbarHeading.className = "commands-settings-heading";
  const commandSummary = textElement("span", "command-settings-summary", "");
  const updateCommandSummary = () => {
    const enabledCount = Object.values(draft.commands).filter((setting) => setting.enabled).length;
    commandSummary.textContent = `${enabledCount} of ${managementData.commands.length} commands enabled`;
  };
  toolbarHeading.append(
    textElement("strong", "", "Prefix commands"),
    textElement("p", "command-settings-hint", "Configure how text commands work in this server. Shortcuts work with or without the prefix."),
    commandSummary,
  );
  const toolbarControls = document.createElement("div");
  toolbarControls.className = "commands-settings-controls";
  const prefixField = document.createElement("label");
  prefixField.className = "commands-prefix-field";
  prefixField.append(textElement("span", "command-field-label", "Prefix:"));
  const prefixInput = document.createElement("input");
  prefixInput.className = "channel-select commands-prefix-input";
  prefixInput.type = "text";
  prefixInput.value = draft.prefix;
  prefixInput.placeholder = "!";
  prefixInput.setAttribute("aria-label", "Command prefix");
  prefixInput.addEventListener("input", () => {
    draft.prefix = prefixInput.value;
    commandGrid.querySelectorAll("[data-command-preview]").forEach((element) => {
      element.textContent = `${draft.prefix || "!"}${element.dataset.commandPreview}`;
    });
    commandGrid.querySelectorAll("[data-command-run-label]").forEach((element) => {
      element.textContent = `Run ${draft.prefix || "!"}${element.dataset.commandRunLabel}`;
    });
  });
  prefixField.append(prefixInput);
  const prefixToggleLabel = document.createElement("label");
  prefixToggleLabel.className = "command-inline-toggle";
  const prefixToggle = document.createElement("input");
  prefixToggle.type = "checkbox";
  prefixToggle.checked = draft.prefix_enabled;
  prefixToggle.addEventListener("change", () => { draft.prefix_enabled = prefixToggle.checked; });
  prefixToggleLabel.append(prefixToggle, textElement("span", "", "Enable prefix commands"));
  const saveButton = document.createElement("button");
  saveButton.className = "primary-button command-settings-save";
  saveButton.type = "button";
  saveButton.textContent = "Save settings";
  const saveStatus = textElement("span", "command-settings-status", "");
  saveButton.addEventListener("click", async () => {
    saveButton.disabled = true;
    saveStatus.textContent = "Saving...";
    saveStatus.className = "command-settings-status is-loading";
    try {
      const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/control/commands/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
      managementData.command_settings = result.command_settings;
      saveStatus.textContent = "Saved and applied to Discord.";
      saveStatus.className = "command-settings-status is-success";
      renderCommands();
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Command settings saved. Prefix commands now use the new configuration.";
    } catch (error) {
      saveStatus.textContent = errorMessage(error, "Command settings could not be saved.");
      saveStatus.className = "command-settings-status is-error";
    } finally {
      saveButton.disabled = false;
    }
  });
  toolbarControls.append(prefixField, prefixToggleLabel, saveButton, saveStatus);
  toolbar.append(toolbarHeading, toolbarControls);
  commandGrid.append(toolbar);
  updateCommandSummary();

  let lastCommandCategory = "";
  managementData.commands.forEach((command) => {
    const commandCategory = command.category || "General";
    if (commandCategory !== lastCommandCategory) {
      const groupHeading = document.createElement("div");
      groupHeading.className = "command-group-heading";
      const groupLine = document.createElement("span");
      groupLine.className = "command-group-line";
      const groupTitle = textElement("strong", "", `${commandCategory} commands`);
      const groupCount = managementData.commands.filter((item) => (item.category || "General") === commandCategory).length;
      groupHeading.append(groupLine, groupTitle, textElement("span", "command-group-count", `${groupCount} available`));
      commandGrid.append(groupHeading);
      lastCommandCategory = commandCategory;
    }
    const setting = draft.commands[command.name];
    const isMusicCommand = ["play", "q", "pause", "skip", "stop"].includes(command.name);
    const needsMusicQuery = ["play", "q"].includes(command.name);
    const hasRunTarget = isMusicCommand
      ? Boolean((managementData.voice_channels || []).length)
      : Boolean((managementData.channels || []).length);
    const commandInvocation = String(command.label || `/${command.name}`).replace(/^\/+/, "");
    const card = document.createElement("article");
    const categoryClass = String(commandCategory).toLowerCase().replace(/[^a-z0-9]+/g, "-");
    card.className = `command-card command-card-${categoryClass}${command.name === "ban" ? " ban-command-card" : ""}${setting.enabled ? "" : " command-card-disabled"}`;
    card.dataset.commandName = command.name;
    card.dataset.commandCategory = commandCategory;
    const cardHeader = document.createElement("div");
    cardHeader.className = "command-card-header";
    const cardIdentity = document.createElement("div");
    cardIdentity.className = "command-card-identity";
    const categoryBadge = textElement("span", "command-category", commandCategory);
    const cardTitle = textElement("strong", "command-card-title", command.label || `/${command.name}`);
    cardIdentity.append(categoryBadge, cardTitle);
    const status = textElement("span", `command-status${setting.enabled ? "" : " is-disabled"}`, setting.enabled ? "Enabled" : "Disabled");
    cardHeader.append(cardIdentity, status);
    const button = document.createElement("button");
    button.className = "command-button";
    button.type = "button";
    button.dataset.commandPreview = commandInvocation;
    button.textContent = `${draft.prefix}${commandInvocation}`;
    const configId = `command-config-${command.name.replace(/[^a-z0-9]+/gi, "-")}`;
    button.title = "Open command settings";
    button.setAttribute("aria-label", `Open settings for ${command.label || `/${command.name}`}`);
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", configId);
    const description = textElement("p", "command-description", command.description);
    const commandDetails = document.createElement("div");
    commandDetails.className = "command-details";
    const usageRow = document.createElement("div");
    usageRow.className = "command-detail-row command-usage-row";
    usageRow.append(textElement("span", "command-detail-label", "Usage"), textElement("code", "command-usage-code", command.usage || `${command.label || `/${command.name}`}`));
    commandDetails.append(usageRow);
    if (command.details) {
      const detailsRow = document.createElement("div");
      detailsRow.className = "command-detail-row command-explanation-row";
      detailsRow.append(textElement("span", "command-detail-label", "What it does"), textElement("span", "command-detail-copy", command.details));
      commandDetails.append(detailsRow);
    }
    if (Array.isArray(command.options) && command.options.length) {
      const optionsRow = document.createElement("div");
      optionsRow.className = "command-detail-row command-options-row";
      const optionsList = document.createElement("ul");
      optionsList.className = "command-options";
      command.options.forEach((option) => optionsList.append(textElement("li", "", option)));
      optionsRow.append(textElement("span", "command-detail-label", "Options"), optionsList);
      commandDetails.append(optionsRow);
    }
    if (command.requirements) {
      const requirement = textElement("p", "command-requirement", command.requirements);
      requirement.prepend(textElement("span", "command-requirement-label", "Requirement · "));
      commandDetails.append(requirement);
    }
    const config = document.createElement("div");
    config.className = "command-config";
    config.id = configId;
    config.hidden = true;
    const configHeader = document.createElement("div");
    configHeader.className = "command-config-header";
    configHeader.append(textElement("strong", "", "Command settings"), textElement("span", "", "Customize, then run a test from this panel."));
    config.append(configHeader);
    const enabledLabel = document.createElement("label");
    enabledLabel.className = "command-inline-toggle command-setting-toggle";
    const enabledInput = document.createElement("input");
    enabledInput.type = "checkbox";
    enabledInput.checked = setting.enabled;
    enabledInput.addEventListener("change", () => {
      setting.enabled = enabledInput.checked;
      card.classList.toggle("command-card-disabled", !setting.enabled);
      status.textContent = setting.enabled ? "Enabled" : "Disabled";
      status.classList.toggle("is-disabled", !setting.enabled);
      runButton.disabled = !hasRunTarget || !setting.enabled;
      updateCommandSummary();
    });
    enabledLabel.append(enabledInput, textElement("span", "", "Enable this command"));
    const shortcutsField = document.createElement("div");
    shortcutsField.className = "command-shortcuts-field";
    const shortcutList = document.createElement("div");
    shortcutList.className = "command-shortcuts-list";
    const shortcutEntries = [];
    const syncShortcuts = () => {
      setting.shortcuts = shortcutEntries.map((entry) => entry.value).filter((value) => value.trim());
    };
    const addShortcutField = (value = "") => {
      const entry = { value };
      shortcutEntries.push(entry);
      const row = document.createElement("div");
      row.className = "command-shortcut-row";
      const shortcutInput = document.createElement("input");
      shortcutInput.className = "channel-select command-shortcut-input";
      shortcutInput.type = "text";
      shortcutInput.maxLength = 32;
      shortcutInput.value = value;
      shortcutInput.placeholder = "Shortcut, for example pong";
      shortcutInput.addEventListener("input", () => {
        entry.value = shortcutInput.value;
        syncShortcuts();
      });
      const removeShortcut = document.createElement("button");
      removeShortcut.className = "secondary-button command-shortcut-remove";
      removeShortcut.type = "button";
      removeShortcut.textContent = "Remove";
      removeShortcut.addEventListener("click", () => {
        const index = shortcutEntries.indexOf(entry);
        if (index >= 0) shortcutEntries.splice(index, 1);
        row.remove();
        syncShortcuts();
      });
      row.append(shortcutInput, removeShortcut);
      shortcutList.append(row);
      syncShortcuts();
    };
    setting.shortcuts.forEach((shortcut) => addShortcutField(String(shortcut)));
    if (!setting.shortcuts.length) addShortcutField();
    const addShortcut = document.createElement("button");
    addShortcut.className = "secondary-button command-shortcut-add";
    addShortcut.type = "button";
    addShortcut.textContent = "+ Add shortcut";
    addShortcut.addEventListener("click", () => addShortcutField());
    shortcutsField.append(shortcutList, addShortcut);
    const languageSelect = document.createElement("select");
    languageSelect.className = "channel-select command-language-select";
    languageSelect.append(new Option("English replies", "en"), new Option("Arabic replies", "ar"));
    languageSelect.value = setting.language;
    languageSelect.addEventListener("change", () => { setting.language = languageSelect.value; });
    config.append(enabledLabel, labeledControl("Shortcuts (without the prefix)", shortcutsField), labeledControl("Reply language", languageSelect));
    const select = isMusicCommand ? null : createChannelSelect();
    let musicQuery = null;
    if (needsMusicQuery) {
      musicQuery = document.createElement("input");
      musicQuery.className = "channel-select music-command-query";
      musicQuery.type = "text";
      musicQuery.maxLength = 500;
      musicQuery.placeholder = "YouTube link or music name";
      config.append(labeledControl("YouTube link or music name", musicQuery));
    }
    if (isMusicCommand) {
      config.append(textElement("p", "command-description command-announcement", "Music uses the requesting member's current voice channel. Join the voice channel before running this command."));
    }
    let memberSelect = null;
    if (["profile", "show_level", "kick", "ban", "warning", "show_warning", "timeout", "untimeout", "mute", "unmute"].includes(command.name)) {
      const memberControl = createMemberSelect();
      memberSelect = memberControl.select;
      let searchTimer = null;
      memberControl.search.addEventListener("input", () => {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(() => searchMembers(memberControl.search.value, memberSelect), 180);
      });
      config.append(labeledControl(command.name === "show_level" ? "Target member (optional)" : "Target member", memberControl.element));
      if (command.name === "show_level") {
        config.append(textElement("p", "command-description", "Leave this blank to generate your own level card."));
      }
      if (command.name === "profile") {
        const profileStats = document.createElement("div");
        profileStats.className = "profile-preview command-description";
        profileStats.textContent = "Choose a member to view their profile details.";
        memberSelect.addEventListener("change", () => {
          const member = (memberSelect._memberRecords || managementData.members).find((item) => item.member_id === memberSelect.value);
          profileStats.textContent = member
            ? `ID: ${member.member_id} · Joined: ${member.joined_at ? new Date(member.joined_at).toLocaleDateString() : "Unavailable"} · Roles: ${member.roles.length ? member.roles.join(", ") : "No roles"}`
            : "Choose a member to view their profile details.";
        });
        config.append(profileStats);
      }
    }
    let reason = null;
    let deleteDays = null;
    if (["kick", "ban", "warning", "timeout", "untimeout", "mute", "unmute"].includes(command.name)) {
      reason = document.createElement("input");
      reason.className = "channel-select";
      reason.maxLength = 512;
      reason.placeholder = "Reason (optional)";
      config.append(labeledControl("Reason (optional)", reason));
      const announcement = command.name === "kick"
        ? "Announcement: @user has been Kicked from the server"
        : command.name === "ban"
          ? "Announcement: @user has been Banned from the server"
          : command.name === "warning"
            ? "The member will receive a numbered warning and a private notification when possible."
          : command.name === "timeout"
            ? "The member will be timed out for the selected duration."
          : command.name === "untimeout"
            ? "The member's active timeout will be removed."
          : command.name === "mute"
            ? "The member will be prevented from sending messages in the selected text channel; their voice state is unchanged."
            : "The member's chat restriction will be removed from the selected text channel; their voice state is unchanged.";
      config.append(textElement("p", "command-description command-announcement", announcement));
    }
    let warningNumber = null;
    if (command.name === "unwarning") {
      warningNumber = document.createElement("input");
      warningNumber.className = "channel-select";
      warningNumber.type = "number";
      warningNumber.min = "1";
      warningNumber.step = "1";
      warningNumber.placeholder = "Warning number, for example 12";
      config.append(labeledControl("Warning number", warningNumber));
    }
    let durationAmount = null;
    let durationUnit = null;
    if (command.name === "timeout") {
      durationAmount = document.createElement("input");
      durationAmount.className = "channel-select timeout-duration-amount";
      durationAmount.type = "number";
      durationAmount.min = "1";
      durationAmount.step = "1";
      durationAmount.value = "10";
      durationAmount.placeholder = "Amount";
      durationUnit = document.createElement("select");
      durationUnit.className = "channel-select timeout-duration-unit";
      durationUnit.append(new Option("Minutes", "m"), new Option("Hours", "h"), new Option("Days", "d"), new Option("Weeks", "w"));
      bindDurationPicker(durationAmount, durationUnit);
      const durationPicker = document.createElement("div");
      durationPicker.className = "command-duration-picker";
      durationPicker.append(durationAmount, durationUnit);
      const durationField = labeledControl("Timeout duration", durationPicker);
      durationField.append(textElement("small", "command-field-hint", "Choose minutes, hours, days, or weeks. Maximum 28 days."));
      config.append(durationField);
    }
    let deleteAmount = null;
    if (command.name === "delete") {
      deleteAmount = document.createElement("input");
      deleteAmount.className = "channel-select";
      deleteAmount.type = "number";
      deleteAmount.min = "1";
      deleteAmount.max = "100";
      deleteAmount.step = "1";
      deleteAmount.value = "10";
      deleteAmount.placeholder = "Messages (1-100)";
      config.append(labeledControl("Number of messages", deleteAmount));
    }
    if (command.name === "ban") {
      deleteDays = document.createElement("select");
      deleteDays.className = "channel-select";
      for (let day = 0; day <= 7; day += 1) deleteDays.append(new Option(`Delete messages from the last ${day} day(s)`, String(day)));
      config.append(labeledControl("Message history cleanup", deleteDays));
    }
    if (command.name === "server") {
      const overview = textElement("p", "command-description", `Owner: ${managementData.guild.owner_name || "Unavailable"} · ${managementData.guild.members.toLocaleString()} members · Boost level ${managementData.guild.boost_level}`);
      config.append(overview);
    }
    const runButton = document.createElement("button");
    runButton.className = "primary-button";
    runButton.type = "button";
    runButton.disabled = !hasRunTarget || !setting.enabled;
    if (command.name !== "server") runButton.dataset.commandRunLabel = commandInvocation;
    runButton.textContent = command.name === "server" ? "Send Server Info" : `Run ${draft.prefix}${commandInvocation}`;
    button.addEventListener("click", () => {
      config.hidden = !config.hidden;
      button.setAttribute("aria-expanded", String(!config.hidden));
    });
    runButton.addEventListener("click", () => runWebsiteCommand(command.name, select, runButton, {
      query: musicQuery?.value,
      member_id: memberSelect?.value,
      reason: reason?.value,
      warning_id: warningNumber?.value,
      duration_amount: durationAmount ? Number(durationAmount.value) : undefined,
      duration_unit: durationUnit ? durationUnit.value : undefined,
      amount: deleteAmount ? Number(deleteAmount.value) : undefined,
      delete_message_days: deleteDays ? Number(deleteDays.value) : 0,
    }));
    if (select) config.append(labeledControl("Target text channel", select));
    config.append(runButton);
    if (command.name === "ban" && managementData.bans.length) {
      const bans = document.createElement("div");
      bans.className = "ban-list";
      const listHeader = document.createElement("div");
      listHeader.className = "ban-list-header";
      listHeader.append(textElement("strong", "", "Active bans"), textElement("span", "ban-count", `${managementData.bans.length} total`));
      bans.append(listHeader);
      managementData.bans.forEach((ban) => {
        const row = document.createElement("div");
        row.className = "ban-row";
        const avatar = textElement("span", "ban-avatar", (ban.user_name || "?").trim().charAt(0).toUpperCase() || "?");
        row.append(avatar);
        const label = textElement("span", "", `${ban.user_name}${ban.reason ? ` — ${ban.reason}` : ""}`);
        const unban = document.createElement("button");
        const identity = textElement("small", "ban-user-id", `ID: ${ban.user_id}`);
        unban.className = "secondary-button";
        unban.type = "button";
        unban.textContent = "Unban";
        unban.addEventListener("click", () => runWebsiteCommand("unban", select, unban, { member_id: ban.user_id }));
        row.append(label, identity, unban);
        bans.append(row);
      });
      config.append(bans);
    }
    if (command.name === "ban" && !managementData.bans.length) {
      const bans = document.createElement("div");
      bans.className = "ban-list ban-list-empty";
      const listHeader = document.createElement("div");
      listHeader.className = "ban-list-header";
      listHeader.append(textElement("strong", "", "Active bans"), textElement("span", "ban-count", "0 total"));
      bans.append(listHeader, textElement("p", "empty-state", "No active bans in this server."));
      config.append(bans);
    }
    if (!hasRunTarget) {
      config.append(textElement(
        "p",
        "form-error",
        isMusicCommand
          ? "BirdBot cannot access any voice channels in this server."
          : "BirdBot cannot access any text channels in this server.",
      ));
    }
    card.append(cardHeader, button, description, commandDetails, config);
    commandGrid.append(card);
  });
}

function labeledControl(labelText, control) {
  const wrapper = document.createElement("label");
  wrapper.className = "command-field";
  const label = document.createElement("span");
  label.className = "command-field-label";
  label.textContent = labelText;
  wrapper.append(label, control);
  return wrapper;
}

function createChannelSelect() {
  const select = document.createElement("select");
  select.className = "channel-select";
  select.append(new Option("Choose a text channel", ""));
  managementData.channels.forEach((channel) => select.append(new Option(`#${channel.name}`, channel.id)));
  return select;
}

function createMemberSelect() {
  const select = document.createElement("select");
  select.className = "member-value-select";
  select.append(new Option("Choose a server member", ""));
  select.hidden = true;
  const element = document.createElement("div");
  element.className = "member-picker";
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "member-select-trigger";
  trigger.textContent = "Choose a server member";
  trigger.setAttribute("aria-expanded", "false");
  const menu = document.createElement("div");
  menu.className = "member-select-menu";
  menu.hidden = true;
  const search = document.createElement("input");
  search.className = "channel-select member-search";
  search.type = "search";
  search.placeholder = "Search all members by name";
  const searchStatus = textElement("span", "member-search-status", "");
  const options = document.createElement("div");
  options.className = "member-options";
  menu.append(search, searchStatus, options);
  trigger.addEventListener("click", () => {
    menu.hidden = !menu.hidden;
    trigger.setAttribute("aria-expanded", String(!menu.hidden));
    if (!menu.hidden) search.focus();
  });
  select._renderMembers = (members) => {
    select._memberRecords = members;
    const selected = select.value;
    options.replaceChildren();
    select.replaceChildren(new Option("Choose a server member", ""));
    if (!members.length) {
      options.append(textElement("p", "member-options-empty", "No matching members"));
      if (selected) select.value = "";
      trigger.textContent = "Choose a server member";
      return;
    }
    let selectedMember = null;
    members.forEach((member) => {
      const displayName = member.display_name || member.username || member.member_id;
      const option = document.createElement("button");
      option.type = "button";
      option.className = "member-option";
      const avatar = member.avatar_url
        ? document.createElement("img")
        : textElement("span", "member-option-avatar", displayName.charAt(0).toUpperCase() || "?");
      if (member.avatar_url) {
        avatar.className = "member-option-avatar";
        avatar.src = member.avatar_url;
        avatar.alt = "";
        avatar.addEventListener("error", () => avatar.replaceWith(textElement("span", "member-option-avatar", displayName.charAt(0).toUpperCase() || "?")));
      }
      const details = document.createElement("span");
      details.className = "member-option-details";
      details.append(textElement("strong", "", displayName), textElement("small", "", `@${member.username || member.global_name || "unknown"} · ${member.member_id}`));
      option.append(avatar, details);
      select.append(new Option(displayName, member.member_id));
      option.addEventListener("click", () => {
        select.value = member.member_id;
        trigger.textContent = displayName;
        menu.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      options.append(option);
      if (member.member_id === selected) selectedMember = displayName;
    });
    if (selectedMember) trigger.textContent = selectedMember;
    else if (selected) select.value = "";
  };
  select._searchStatus = searchStatus;
  element.append(trigger, menu, select);
  // Keep the initial menu light for large servers; the embedded search queries
  // the complete member database when a name is entered.
  populateMemberSelect(select, managementData.members.slice(0, 100));
  return { element, select, search, searchStatus };
}

function populateMemberSelect(select, members) {
  if (typeof select._renderMembers === "function") {
    select._renderMembers(members);
    return;
  }
  const selected = select.value;
  select.replaceChildren(new Option("Choose a server member", ""));
  if (!members.length) {
    const empty = new Option("No matching members", "");
    empty.disabled = true;
    select.append(empty);
    return;
  }
  members.forEach((member) => {
    const displayName = member.display_name || member.username || member.member_id;
    select.append(new Option(displayName, member.member_id));
  });
  if (members.some((member) => member.member_id === selected)) select.value = selected;
}

async function searchMembers(query, select) {
  const normalized = query.trim();
  const version = Number(select.dataset.searchVersion || "0") + 1;
  select.dataset.searchVersion = String(version);
  const status = select._searchStatus;
  if (!normalized) {
    if (status) {
      status.textContent = "";
      status.removeAttribute("aria-busy");
      status.classList.remove("is-loading");
    }
    populateMemberSelect(select, managementData.members.slice(0, 100));
    return;
  }
  if (status) {
    status.textContent = "Searching members...";
    status.setAttribute("aria-busy", "true");
    status.classList.add("is-loading");
  }
  try {
    const result = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/members/search?q=${encodeURIComponent(normalized)}`);
    if (select.dataset.searchVersion !== String(version)) return;
    populateMemberSelect(select, result.members || []);
    if (status) {
      status.textContent = result.members?.length ? `${result.members.length} match${result.members.length === 1 ? "" : "es"}` : "No matching users found";
      status.removeAttribute("aria-busy");
      status.classList.remove("is-loading");
    }
  } catch (error) {
    // Keep the picker usable if a search request fails: filter the already loaded snapshot.
    if (select.dataset.searchVersion !== String(version)) return;
    const local = managementData.members.filter((member) => [member.display_name, member.username, member.global_name, member.member_id]
      .filter(Boolean).join(" ").toLowerCase().includes(normalized.toLowerCase()));
    if (local.length) populateMemberSelect(select, local);
    if (status) {
      status.textContent = local.length ? `${local.length} local match${local.length === 1 ? "" : "es"}` : "Search unavailable. Try again.";
      status.removeAttribute("aria-busy");
      status.classList.remove("is-loading");
    }
  }
}

const delay = waitFor;

async function runWebsiteCommand(commandName, select, button, payload = {}) {
  const musicCommand = ["play", "q", "pause", "skip", "stop"].includes(commandName);
  if (button.disabled) return;
  const commandSettings = managementData.command_settings?.commands?.[commandName];
  if (commandSettings && commandSettings.enabled === false) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = "That command is disabled. Enable it from the Commands settings first.";
    return;
  }
  const commandPrefix = managementData.command_settings?.prefix || "!";
  if (!musicCommand && !select?.value) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = "Choose a text channel first.";
    return;
  }
  if (["profile", "kick", "ban", "warning", "show_warning", "timeout", "untimeout", "mute", "unmute"].includes(commandName) && !payload.member_id) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = "Choose a target member first.";
    return;
  }
  button.disabled = true;
  button.textContent = "Sending...";
  const commandLabel = commandName === "show_warning" || commandName === "show_level"
    ? `show ${commandName === "show_level" ? "level" : "warning"}`
    : commandName;
  beginLoading(`Sending ${commandPrefix}${commandLabel} to BirdBot...`);
  try {
    const requestPayload = { ...payload };
    if (!musicCommand && select?.value) requestPayload.channel_id = select.value;
    const queued = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/commands/${commandName}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload),
    });
    commandFeedback.hidden = false;
    commandFeedback.textContent = musicCommand
      ? `${commandPrefix}${commandLabel} is being queued for your current voice channel...`
      : `${commandPrefix}${commandLabel} is being sent to the selected channel...`;
    for (let attempt = 0; attempt < 30; attempt += 1) {
      // The bot worker checks its queue every two seconds; a shorter poll
      // interval makes completed commands feel immediate without waiting for
      // a long fixed sleep.
      await delay(700);
      const status = await requestJson(`/api/command-requests/${encodeURIComponent(queued.request_id)}`);
      if (status.status === "complete") {
        const announcement = commandName === "play"
          ? "Music request queued."
          : commandName === "q"
            ? "Track added to the music queue."
            : commandName === "pause"
              ? "Playback paused."
              : commandName === "skip"
                ? "Track skipped."
                : commandName === "stop"
                  ? "Music stopped and the queue was cleared."
                  : commandName === "kick"
          ? "@user has been Kicked from the server"
          : commandName === "ban" ? "@user has been Banned from the server" : "";
        commandFeedback.textContent = announcement
          ? `${commandPrefix}${commandLabel} completed. ${announcement}`
          : `${commandPrefix}${commandLabel} was sent successfully.`;
        if (commandName === "ban" || commandName === "unban") {
          const bans = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/bans`);
          managementData.bans = bans.bans;
          renderCommands();
        }
        return;
      }
      if (status.status === "failed") throw new Error(status.error || "BirdBot could not run that command.");
    }
    commandFeedback.textContent = musicCommand
      ? `${commandPrefix}${commandLabel} is still queued. Check the voice channel shortly.`
      : `${commandPrefix}${commandLabel} is still queued. Check the selected channel shortly.`;
  } catch (error) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "BirdBot could not run that command.");
  } finally {
    endLoading();
    button.disabled = false;
    button.textContent = commandName === "server" ? "Send Server Info" : `Run ${commandPrefix}${commandLabel}`;
  }
}

async function loadManagement(guildId) {
  beginLoading("Loading server management...");
  try {
    managementData = await requestJson(`/api/guilds/${encodeURIComponent(guildId)}/manage`);
    renderAccount(currentUser);
    renderManagedServer(managementData.guild);
    landingView.hidden = true;
    portalChoiceView.hidden = true;
    if (profileView) profileView.hidden = true;
    dashboardView.hidden = true;
    managementView.hidden = false;
    backButton.hidden = false;
    backButton.href = "/?dashboard=1";
    backButton.textContent = "Back";
    setManagementTab(managementData.guild.can_configure === false ? "tickets" : "commands");
  } finally {
    endLoading();
  }
}

function openSelector() {
  if (!dashboardData?.bot_online) return;
  selectorList.replaceChildren();
  const disabledGuilds = dashboardData.guilds.filter((guild) => !guild.activated && guild.can_configure !== false);
  if (!disabledGuilds.length) {
    selectorList.append(textElement("p", "empty-state", "All of your eligible servers already have BirdBot enabled."));
  }
  disabledGuilds.forEach((guild) => {
    const button = document.createElement("button");
    button.className = "selector-row";
    button.type = "button";
    button.append(textElement("strong", "", guild.name), textElement("span", "", "Bot Disabled"));
    button.addEventListener("click", () => openConfirmation(guild, "enable"));
    selectorList.append(button);
  });
  selectorModal.hidden = false;
}

function openConfirmation(guild, action) {
  selectedGuild = guild;
  pendingAction = action;
  const disabling = action === "disable";
  selectedServerName.textContent = guild.name;
  selectedServerStatus.textContent = disabling ? "Bot Active" : "Bot Disabled";
  document.getElementById("confirm-title").textContent = disabling ? "Stop Bot for Server" : "Enable Bot for Server";
  confirmationCopy.textContent = disabling
    ? "BirdBot will stop operating in this server. This does not disconnect the global bot or affect any other server."
    : "This enables BirdBot’s features only in this server. It does not start, reconnect, or restart the global Discord bot.";
  confirmActivation.className = disabling ? "danger-button" : "primary-button";
  confirmActivation.textContent = disabling ? "Stop Bot for This Server" : "Enable Bot for This Server";
  selectorModal.hidden = true;
  confirmModal.hidden = false;
  activationError.textContent = "";
}

async function changeSelectedGuildState() {
  if (!selectedGuild || confirmActivation.disabled) return;
  const disabling = pendingAction === "disable";
  const actionLabel = disabling ? "Stopping…" : "Enabling…";
  confirmActivation.disabled = true;
  confirmActivation.textContent = actionLabel;
  setConfirmButtonLoading(true, actionLabel);
  beginLoading(disabling ? "Disabling BirdBot for this server..." : "Enabling BirdBot for this server...");
  activationError.textContent = "";
  try {
    await requestJson(
      `/api/guilds/${encodeURIComponent(selectedGuild.id)}/${disabling ? "disable" : "activate"}`,
      { method: "POST" },
    );
    pendingFeedback = disabling
      ? `BirdBot is now disabled for ${selectedGuild.name}.`
      : `BirdBot is now enabled for ${selectedGuild.name}.`;
    closeModals();
    await loadDashboard();
  } catch (error) {
    activationError.textContent = errorMessage(error, "The server setting could not be changed.");
  } finally {
    confirmActivation.disabled = false;
    confirmActivation.textContent = disabling ? "Stop Bot for This Server" : "Enable Bot for This Server";
    endLoading();
    setConfirmButtonLoading(false, disabling ? "Stop Bot for This Server" : "Enable Bot for This Server");
  }
}

async function loadDashboard() {
  const data = await requestJson("/api/dashboard");
  renderDashboard(data);
}

async function initialize() {
  beginLoading("Preparing your BirdBot workspace...");
  try {
    // Hold the initial interface behind one readiness gate. The gate loads
    // independent profile/dashboard resources in parallel and populates the
    // short-lived preload cache consumed by the route renderers below.
    const preload = await preloadCriticalData();
    const session = preload.session;
    renderAccount(session.user);
    setDashboardEntry(session.authenticated);
    if (!session.authenticated) return;
    const parameters = new URLSearchParams(window.location.search);
    const guildId = parameters.get("guild");
    if (parameters.get("profile") === "1" || parameters.get("music") === "1") {
      // Old Music portal links are redirected to the replacement Profile
      // portal instead of opening a removed website feature.
      if (parameters.get("music") === "1") window.history.replaceState({ profile: true }, "", "/?profile=1");
      try {
        await loadProfilePortal();
      } catch (error) {
        renderPortalChoice(errorMessage(error, "Your Profile could not be loaded."));
      }
      return;
    }
    if (parameters.get("portal") === "1") {
      renderPortalChoice();
      return;
    }
    if (guildId) {
      try {
        await loadManagement(guildId);
      } catch (error) {
        const message = errorMessage(error, "Server management could not be loaded.");
        const denied = /owner|administrator|permission|access denied/i.test(message);
        window.history.replaceState({ portal: true }, "", "/?portal=1");
        renderPortalChoice(denied ? "Access Denied: Administrator permissions required" : message);
      }
      return;
    }
    if (!parameters.has("dashboard")) {
      renderPortalChoice();
      return;
    }
    try {
      loadingMessage.textContent = "Loading your servers...";
      await loadDashboard();
    } catch (error) {
      const message = errorMessage(error, "Dashboard data could not be loaded.");
      if (/access denied|administrator|owner|permission/i.test(message)) {
        window.history.replaceState({ portal: true }, "", "/?portal=1");
        renderPortalChoice("Access Denied: Administrator permissions required");
      } else {
        botNotice.hidden = false;
        botNotice.textContent = message;
      }
    }
  } catch (error) {
    const message = errorMessage(error, "The website could not prepare your workspace.");
    const parameters = new URLSearchParams(window.location.search);
    const cachedSession = preloadCache.get("/api/session");
    if (cachedSession?.authenticated) renderAccount(cachedSession.user);
    // A failed protected preload should resolve to a useful portal/error
    // state instead of leaving every view hidden behind a blank page.
    if (parameters.get("profile") === "1" || parameters.get("music") === "1" || parameters.get("portal") === "1") {
      window.history.replaceState({ portal: true }, "", "/?portal=1");
      renderPortalChoice(message);
    } else if (parameters.get("guild") || parameters.get("dashboard") === "1") {
      // A temporary member/cache lookup failure is not an authorization
      // failure. Keep its real retry guidance instead of showing Access
      // Denied and sending the user to the wrong portal.
      const denied = /owner|administrator|permission|access denied/i.test(message);
      window.history.replaceState({ portal: true }, "", "/?portal=1");
      renderPortalChoice(denied ? "Access Denied: Administrator permissions required" : message);
    } else {
      botNotice.hidden = false;
      botNotice.textContent = message;
    }
  } finally {
    loadingIndicator.dataset.phase = "ready";
    document.body.classList.remove("is-preloading");
    endLoading();
  }
}

document.getElementById("open-selector").addEventListener("click", openSelector);
confirmActivation.addEventListener("click", changeSelectedGuildState);
if (actionModalConfirm) actionModalConfirm.addEventListener("click", submitActionModal);
if (actionModalCancel) actionModalCancel.addEventListener("click", () => resolveActionModal(null));
document.querySelectorAll("[data-close-action-modal]").forEach((button) => button.addEventListener("click", () => resolveActionModal(null)));
document.querySelectorAll("[data-close-modal]").forEach((button) => button.addEventListener("click", closeModals));
document.querySelectorAll(".modal").forEach((modal) => modal.addEventListener("click", (event) => {
  if (event.target !== modal) return;
  if (modal === actionModal) resolveActionModal(null);
  else closeModals();
}));
document.querySelectorAll("[data-management-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    try {
      setManagementTab(button.dataset.managementTab);
    } catch (error) {
      showWorkspaceError(errorMessage(error, "That dashboard section could not be opened. Please try again."));
    }
  });
});
showTicketsButton.addEventListener("click", () => {
  if (ticketPageMode === "list") {
    ticketPageMode = "config";
    showTicketsButton.textContent = "Show Tickets";
    setManagementTab("tickets");
  } else {
    loadTicketsPage();
  }
});
ticketLogsButton.addEventListener("click", () => {
  setManagementTab("ticket_logs");
});
function refreshActiveTicketView() {
  if (document.hidden || managementView.hidden || pendingRequests > 0) return;
  if (activeManagementTab === "tickets" && ticketPageMode === "list" && !ticketRefreshInFlight) {
    ticketRefreshInFlight = true;
    void loadTicketsPage(false).finally(() => { ticketRefreshInFlight = false; });
    return;
  }
  if (activeManagementTab === "ticket_logs" && !ticketLogsRefreshInFlight) {
    ticketLogsRefreshInFlight = true;
    void loadTicketLogs(false).finally(() => { ticketLogsRefreshInFlight = false; });
    return;
  }
  if (activeManagementTab === "logs" && !guildLogsRefreshInFlight) {
    guildLogsRefreshInFlight = true;
    void loadGuildLogs(false).finally(() => { guildLogsRefreshInFlight = false; });
  }
}
// Discord-side claims and timeout deletions are reflected without requiring a
// full page refresh.  The API's server_time keeps the local countdown aligned
// even when the browser clock is skewed.
window.setInterval(refreshActiveTicketView, 8_000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshActiveTicketView();
});
window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (actionModal && !actionModal.hidden) resolveActionModal(null);
  else closeModals();
});
window.addEventListener("popstate", () => initialize());
window.addEventListener("unhandledrejection", (event) => {
  event.preventDefault();
  showWorkspaceError(errorMessage(event.reason, "An unexpected request failed. Please try again."));
});
window.addEventListener("error", (event) => {
  if (event.error) {
    showWorkspaceError(errorMessage(event.error, "The page encountered an unexpected error. Please try again."));
  }
});
initialize();
