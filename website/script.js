const landingView = document.getElementById("landing-view");
const portalChoiceView = document.getElementById("portal-choice-view");
const portalChoiceGrid = document.getElementById("portal-choice-grid");
const portalChoiceTitle = document.getElementById("portal-choice-title");
const portalChoiceCopy = document.getElementById("portal-choice-copy");
const dashboardView = document.getElementById("dashboard-view");
const welcomeMessage = document.getElementById("welcome-message");
const botNotice = document.getElementById("bot-notice");
const actionFeedback = document.getElementById("action-feedback");
const serverList = document.getElementById("server-list");
const selectorModal = document.getElementById("selector-modal");
const confirmModal = document.getElementById("confirm-modal");
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

// Music is a separate member portal, not an administrative dashboard tab.
// Remove the legacy tab as well so browsers serving an older cached HTML
// document cannot display it after the dashboard has been updated.
document.querySelectorAll('[data-management-tab="music"]').forEach((button) => button.remove());

let dashboardData = null;
let selectedGuild = null;
let pendingAction = "enable";
let pendingFeedback = "";
let pendingRequests = 0;
let pendingBlockingRequests = 0;
const REQUEST_TIMEOUT_MS = 20_000;
const GET_REQUEST_ATTEMPTS = 2;
let managementData = null;
let currentUser = null;
let ticketPageMode = "config";
let activeManagementTab = "commands";
let musicPortalMode = false;
let musicInitialOverview = null;
let ticketCountdownTimer = null;
let ticketRefreshInFlight = false;
let ticketLogsRefreshInFlight = false;
let ticketLogsQuery = "";
let ticketListSnapshot = null;
let ticketLogsSnapshot = null;
let ticketServerClockOffsetMs = 0;
// Responses fetched by the readiness gate are reused by the first render so
// navigation never performs the same Discord/Spotify request twice.
const preloadCache = new Map();

function textElement(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
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
  if (window.BirdBotMusic?.unmount) window.BirdBotMusic.unmount();
  musicPortalMode = false;
  landingView.hidden = true;
  dashboardView.hidden = true;
  managementView.hidden = true;
  portalChoiceView.hidden = false;
  backButton.hidden = true;
  portalChoiceTitle.textContent = "Where do you want to go?";
  portalChoiceCopy.textContent = notice || "Choose server management or open the member music player.";
  portalChoiceGrid.replaceChildren(
    renderPortalCard("The Dashboard", "Manage servers, tickets, commands, logs, and settings as an owner or Administrator.", "/?dashboard=1", "portal-dashboard-card"),
    renderPortalCard("Music System", "Paste a public audio link or use Spotify, then control playback as an authenticated member.", "/?music=1", "portal-music-card"),
  );
}

async function loadMusicGuildChoice() {
  if (window.BirdBotMusic?.unmount) window.BirdBotMusic.unmount();
  beginLoading("Loading your music servers...");
  try {
    const data = await requestJson("/api/music/guilds", { cache: "no-store" });
    musicPortalMode = true;
    landingView.hidden = true;
    dashboardView.hidden = true;
    managementView.hidden = true;
    portalChoiceView.hidden = false;
    backButton.hidden = false;
    backButton.href = "/?portal=1";
    backButton.textContent = "Back";
    portalChoiceTitle.textContent = "Choose a server for Music System";
    portalChoiceCopy.textContent = data.bot_online === false
      ? "BirdBot is offline right now. Try again when the global bot is online."
      : "Select a server where you are a member to open its player.";
    portalChoiceGrid.replaceChildren();
    const guilds = Array.isArray(data.guilds) ? data.guilds : [];
    if (!guilds.length) {
      portalChoiceGrid.append(textElement("p", "empty-state", data.bot_online === false ? "BirdBot is currently offline." : "No bot-connected servers are available for your account."));
      return;
    }
    guilds.forEach((guild) => {
      const card = renderPortalCard(guild.name, `${Number(guild.members || 0).toLocaleString()} members - ${guild.activated ? "Music enabled" : "Music disabled"}`, `/?guild=${encodeURIComponent(guild.id)}&music=1`, "portal-server-card");
      if (guild.icon_url) {
        const icon = document.createElement("img");
        icon.className = "portal-choice-icon";
        icon.src = guild.icon_url;
        icon.alt = "";
        card.prepend(icon);
      }
      portalChoiceGrid.append(card);
    });
  } finally {
    endLoading();
  }
}

function closeModals() {
  selectorModal.hidden = true;
  confirmModal.hidden = true;
  activationError.textContent = "";
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

async function requestJson(url, options = {}) {
  const { skipPreload = false, ...fetchOptions } = options || {};
  const method = String(fetchOptions.method || "GET").toUpperCase();
  // Any mutation can invalidate a preloaded dashboard/music snapshot (for
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

async function preloadCriticalData() {
  loadingMessage.textContent = "Preparing your BirdBot workspace...";
  const session = await requestJson("/api/session", { cache: "no-store", skipPreload: true });
  preloadCache.set("/api/session", session);
  if (!session.authenticated) return { session };

  const parameters = new URLSearchParams(window.location.search);
  const guildId = parameters.get("guild");
  const urls = [];
  const requiredUrls = new Set();
  const isMusic = parameters.get("music") === "1";
  const isDashboard = parameters.get("dashboard") === "1";
  if (isMusic) {
    const musicOverviewUrl = guildId
      ? `/api/guilds/${encodeURIComponent(guildId)}/music`
      : "/api/music/guilds";
    urls.push(musicOverviewUrl);
    requiredUrls.add(musicOverviewUrl);
    if (guildId) urls.push(`/api/guilds/${encodeURIComponent(guildId)}/music/state`);
  } else if (isDashboard || guildId) {
    urls.push("/api/dashboard");
    requiredUrls.add("/api/dashboard");
    if (guildId) {
      const encodedGuild = encodeURIComponent(guildId);
      // Management pages need these payloads before controls become
      // interactive. They are independent and intentionally load together.
      const manageUrl = `/api/guilds/${encodedGuild}/manage`;
      urls.push(
        manageUrl,
        `/api/guilds/${encodedGuild}/tickets/config`,
        `/api/guilds/${encodedGuild}/music/state`,
        `/api/guilds/${encodedGuild}/games`,
      );
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

function renderDashboard(data) {
  dashboardData = data;
  renderAccount(data.user);
  backButton.hidden = false;
  backButton.href = "/";
  backButton.textContent = "Back";
  landingView.hidden = true;
  portalChoiceView.hidden = true;
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
    icon.addEventListener("error", () => icon.replaceWith(fallback));
    managedServerCard.append(icon);
  } else {
    managedServerCard.append(fallback);
  }
  const text = document.createElement("div");
  text.append(textElement("strong", "", guild.name), textElement("span", "", musicPortalMode ? "BirdBot online" : "BirdBot active"));
  managedServerCard.append(text);
}

function renderControlPanel() {
  commandGrid.replaceChildren();
  commandFeedback.hidden = true;
  const grid = document.createElement("section");
  grid.className = "control-panel-grid";
  grid.setAttribute("aria-label", "Control Panel tools");

  const channels = Array.isArray(managementData?.channels) ? managementData.channels.length : 0;
  const roles = Array.isArray(managementData?.roles) ? managementData.roles.length : 0;
  const cards = [
    ["Server message", "Configure automated server announcements."],
    ["Roles", `${roles.toLocaleString()} roles available to manage.`],
    ["Channels", `${channels.toLocaleString()} channels available to manage.`],
    ["VC", "Manage voice-channel and connection settings."],
    ["DM's Messages", "Configure private messages sent by BirdBot."],
    ["Bot profile", "Customize BirdBot's profile and presence."],
  ];

  cards.forEach(([title, description], index) => {
    const card = document.createElement("article");
    card.className = "control-panel-card";
    const isReady = [0, 1, 4, 5].includes(index);
    if (isReady) {
      card.classList.add("control-panel-card-action");
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-label", `Open ${title} manager`);
      const open = () => {
        if (index === 0) return renderServerMessagePanel();
        if (index === 1) return renderRolesPanel();
        if (index === 4) return renderDMMessagePanel();
        return renderBotProfilePanel();
      };
      card.addEventListener("click", open);
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
    }
    card.append(
      textElement("h3", "control-panel-card-title", title),
      textElement("p", "control-panel-card-copy", description),
      textElement("span", "control-panel-card-status", index === 0 ? "Open composer" : index === 1 ? "Manage roles" : index === 4 ? "Open composer" : "Edit profile"),
    );
    grid.append(card);
  });
  commandGrid.append(grid);
}

async function waitForDashboardCommand(requestId) {
  const id = String(requestId || "");
  if (!id) throw new Error("BirdBot did not return a command request ID.");
  // The bot worker checks its queue continuously. A short poll keeps the
  // dashboard responsive without making the browser wait for a fixed delay.
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await waitFor(250);
    const state = await requestJson(`/api/command-requests/${encodeURIComponent(id)}`, { cache: "no-store" });
    if (state.status === "complete") return state;
    if (state.status === "failed") throw new Error(state.error || "BirdBot could not send that message.");
  }
  return { status: "pending" };
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
      const state = await waitForDashboardCommand(queued.request_id);
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
    textElement("p", "server-message-copy", "Send one private message to a server member. Mentions and one image or video attachment are supported."),
  );
  const form = document.createElement("form");
  form.className = "server-message-form dm-message-form";
  form.noValidate = true;
  const targetPicker = createMemberSelect();
  wireMemberPickerSearch(targetPicker);
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
  actions.append(back, send);
  form.append(
    labeledControl("Send to server member", targetPicker.element),
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
    const memberId = targetPicker.select.value;
    const messageType = typeSelect.value === "embed" ? "embed" : "normal";
    const content = normalInput.value.trim();
    const title = titleInput.value.trim();
    const description = descriptionInput.value.trim();
    if (!memberId) {
      commandFeedback.hidden = false;
      commandFeedback.textContent = "Choose a server member first.";
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
      const payload = { member_id: memberId, message_type: messageType, mention_user_ids: [...mentionIds] };
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
      commandFeedback.textContent = state.status === "pending"
        ? "The private message is still queued. Check again shortly."
        : "Private message sent successfully.";
      if (state.status === "complete") {
        normalInput.value = "";
        titleInput.value = "";
        descriptionInput.value = "";
        attachmentInput.value = "";
        attachmentHint.textContent = "Optional image or MP4/WebM/MOV video · max 8 MB";
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
}

function normalizedRoleColor(value) {
  const color = String(value || "").trim().toUpperCase();
  return /^#[0-9A-F]{6}$/.test(color) ? color : "#000000";
}

async function reloadManagementRoles() {
  const fresh = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/manage`, { cache: "no-store" });
  managementData = { ...managementData, roles: Array.isArray(fresh.roles) ? fresh.roles : [] };
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
    textElement("p", "server-message-copy", editingRole ? "Update this role’s name and color." : "Create and manage the roles in your server."),
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
  const save = textElement("button", "primary-button", editingRole ? "Save changes" : "Create role");
  save.type = "submit";
  const cancel = textElement("button", "secondary-button", "Cancel");
  cancel.type = "button";
  cancel.hidden = !editingRole;
  cancel.addEventListener("click", () => renderRolesPanel());
  const actions = document.createElement("div");
  actions.className = "server-message-actions";
  actions.append(cancel, save);
  form.append(labeledControl("Role name", nameInput), labeledControl("Role color", colorRow), actions);
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
      const remove = textElement("button", "danger-button", "Delete");
      remove.type = "button";
      remove.addEventListener("click", () => {
        if (remove.disabled || !window.confirm(`Delete the “${role.name}” role?`)) return;
        void runRoleAction("delete", { role_id: String(role.id) }, remove, role.name);
      });
      rowActions.append(edit, remove);
    }
    row.append(swatch, details, rowActions);
    list.append(row);
  });
  panel.append(heading, form, list);
  commandGrid.append(panel);
}

function setManagementTab(tab) {
  if (tab !== "music" && window.BirdBotMusic?.unmount) window.BirdBotMusic.unmount();
  if (tab !== "games" && window.BirdBotGames?.unmount) window.BirdBotGames.unmount();
  const canConfigure = managementData?.guild?.can_configure !== false;
  document.querySelectorAll("[data-management-tab]").forEach((button) => {
    button.hidden = musicPortalMode || ((button.dataset.managementTab === "commands" || button.dataset.managementTab === "control") && !canConfigure);
  });
  if (musicPortalMode) tab = "music";
  else if (!canConfigure && (tab === "commands" || tab === "control" || tab === "music")) tab = "tickets";
  activeManagementTab = tab;
  document.querySelectorAll("[data-management-tab]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.managementTab === tab);
  });
  commandFeedback.hidden = true;
  showTicketsButton.hidden = musicPortalMode || tab !== "tickets";
  ticketLogsButton.hidden = musicPortalMode || tab !== "tickets";
  if (tab !== "commands") {
    managementTitle.textContent = tab === "tickets"
      ? "Ticket system"
      : tab === "control"
        ? "Control Panel"
      : tab === "music"
          ? "Music system"
          : tab === "games"
            ? "Games"
          : "Ticket Logs";
    managementDescription.textContent = tab === "tickets"
      ? "Build the panel your members will use to open a ticket."
      : tab === "control"
        ? "Manage your server messages, roles, channels, voice, DMs, and bot profile."
      : tab === "music"
        ? "Link Spotify, join your current voice channel, and control BirdBot playback."
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
      void loadTicketLogs();
      return;
    }
    if (tab === "music" && window.BirdBotMusic && managementData?.guild?.id) {
      window.BirdBotMusic.mount({
        root: commandGrid,
        guildId: managementData.guild.id,
        requestJson,
        beginLoading,
        endLoading,
        initialOverview: musicInitialOverview,
      });
      musicInitialOverview = null;
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
  managementDescription.textContent = "Choose a command, then choose the text channel where BirdBot should run it.";
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
      transcript.href = ticket.transcript_url;
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
      link.href = log.transcript_url;
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

function renderCommands() {
  commandGrid.replaceChildren();
  managementData.commands.forEach((command) => {
    const card = document.createElement("article");
    card.className = `command-card${command.name === "ban" ? " ban-command-card" : ""}`;
    const button = document.createElement("button");
    button.className = "command-button";
    button.type = "button";
    button.textContent = command.label;
    const description = textElement("p", "command-description", command.description);
    const config = document.createElement("div");
    config.className = "command-config";
    config.hidden = true;
    const select = createChannelSelect();
    let memberSelect = null;
    if (["profile", "kick", "ban"].includes(command.name)) {
      const memberControl = createMemberSelect();
      memberSelect = memberControl.select;
      let searchTimer = null;
      memberControl.search.addEventListener("input", () => {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(() => searchMembers(memberControl.search.value, memberSelect), 180);
      });
      config.append(labeledControl("Target member", memberControl.element));
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
    if (["kick", "ban"].includes(command.name)) {
      reason = document.createElement("input");
      reason.className = "channel-select";
      reason.maxLength = 512;
      reason.placeholder = "Reason (optional)";
      config.append(labeledControl("Reason (optional)", reason));
      const announcement = command.name === "kick"
        ? "Announcement: @user has been Kicked from the server"
        : "Announcement: @user has been Banned from the server";
      config.append(textElement("p", "command-description command-announcement", announcement));
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
    runButton.textContent = command.name === "server" ? "Send Server Info" : `Run ${command.label}`;
    runButton.disabled = !managementData.channels.length;
    button.addEventListener("click", () => { config.hidden = !config.hidden; });
    runButton.addEventListener("click", () => runWebsiteCommand(command.name, select, runButton, {
      member_id: memberSelect?.value,
      reason: reason?.value,
      delete_message_days: deleteDays ? Number(deleteDays.value) : 0,
    }));
    config.append(labeledControl("Target text channel", select), runButton);
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
    if (!managementData.channels.length) config.append(textElement("p", "form-error", "BirdBot cannot access any text channels in this server."));
    card.append(button, description, config);
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
  if (button.disabled) return;
  if (!select.value) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = "Choose a text channel first.";
    return;
  }
  if (["profile", "kick", "ban"].includes(commandName) && !payload.member_id) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = "Choose a target member first.";
    return;
  }
  button.disabled = true;
  button.textContent = "Sending...";
  beginLoading(`Sending /${commandName} to BirdBot...`);
  try {
    const queued = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/commands/${commandName}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_id: select.value, ...payload }),
    });
    commandFeedback.hidden = false;
    commandFeedback.textContent = `/${commandName} is being sent to the selected channel...`;
    for (let attempt = 0; attempt < 30; attempt += 1) {
      // The bot worker checks its queue every two seconds; a shorter poll
      // interval makes completed commands feel immediate without waiting for
      // a long fixed sleep.
      await delay(700);
      const status = await requestJson(`/api/command-requests/${encodeURIComponent(queued.request_id)}`);
      if (status.status === "complete") {
        const announcement = commandName === "kick"
          ? "@user has been Kicked from the server"
          : commandName === "ban" ? "@user has been Banned from the server" : "";
        commandFeedback.textContent = announcement
          ? `/${commandName} completed. ${announcement}`
          : `/${commandName} was sent successfully.`;
        if (commandName === "ban" || commandName === "unban") {
          const bans = await requestJson(`/api/guilds/${encodeURIComponent(managementData.guild.id)}/bans`);
          managementData.bans = bans.bans;
          renderCommands();
        }
        return;
      }
      if (status.status === "failed") throw new Error(status.error || "BirdBot could not run that command.");
    }
    commandFeedback.textContent = `/${commandName} is still queued. Check the selected channel shortly.`;
  } catch (error) {
    commandFeedback.hidden = false;
    commandFeedback.textContent = errorMessage(error, "BirdBot could not run that command.");
  } finally {
    endLoading();
    button.disabled = false;
    button.textContent = commandName === "server" ? "Send Server Info" : `Run /${commandName}`;
  }
}

async function loadManagement(guildId) {
  musicPortalMode = false;
  musicInitialOverview = null;
  beginLoading("Loading server management...");
  try {
    managementData = await requestJson(`/api/guilds/${encodeURIComponent(guildId)}/manage`);
    renderAccount(currentUser);
    renderManagedServer(managementData.guild);
    landingView.hidden = true;
    portalChoiceView.hidden = true;
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

async function loadMusicManagement(guildId) {
  beginLoading("Loading Music system...");
  try {
    const overview = await requestJson(`/api/guilds/${encodeURIComponent(guildId)}/music`, { cache: "no-store" });
    if (!overview || !overview.guild) throw new Error("The Music server could not be resolved.");
    musicPortalMode = true;
    managementData = { guild: { ...overview.guild, can_configure: false } };
    renderAccount(currentUser);
    renderManagedServer(managementData.guild);
    landingView.hidden = true;
    dashboardView.hidden = true;
    portalChoiceView.hidden = true;
    managementView.hidden = false;
    backButton.hidden = false;
    backButton.href = "/?portal=1";
    backButton.textContent = "Back";
    // Pass the already-fetched overview so the player does not perform a
    // second Spotify request during the same navigation.
    musicInitialOverview = overview;
    setManagementTab("music");
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
    // Hold the initial interface behind one readiness gate.  The gate loads
    // independent guild/music/config resources in parallel and populates the
    // short-lived preload cache consumed by the route renderers below.
    const preload = await preloadCriticalData();
    const session = preload.session;
    renderAccount(session.user);
    setDashboardEntry(session.authenticated);
    if (!session.authenticated) return;
    const parameters = new URLSearchParams(window.location.search);
    const guildId = parameters.get("guild");
    if (parameters.get("music") === "1") {
      if (guildId) {
        try {
          await loadMusicManagement(guildId);
        } catch (error) {
          const message = errorMessage(error, "The Music system could not be loaded.");
          window.history.replaceState({ portal: true }, "", "/?portal=1");
          renderPortalChoice(message);
        }
      } else {
        try {
          await loadMusicGuildChoice();
        } catch (error) {
          renderPortalChoice(errorMessage(error, "Music servers could not be loaded."));
        }
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
    if (parameters.get("music") === "1" || parameters.get("portal") === "1") {
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
document.querySelectorAll("[data-close-modal]").forEach((button) => button.addEventListener("click", closeModals));
document.querySelectorAll(".modal").forEach((modal) => modal.addEventListener("click", (event) => {
  if (event.target === modal) closeModals();
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
  setManagementTab("logs");
});
function refreshActiveTicketView() {
  if (document.hidden || managementView.hidden || pendingRequests > 0) return;
  if (activeManagementTab === "tickets" && ticketPageMode === "list" && !ticketRefreshInFlight) {
    ticketRefreshInFlight = true;
    void loadTicketsPage(false).finally(() => { ticketRefreshInFlight = false; });
    return;
  }
  if (activeManagementTab === "logs" && !ticketLogsRefreshInFlight) {
    ticketLogsRefreshInFlight = true;
    void loadTicketLogs(false).finally(() => { ticketLogsRefreshInFlight = false; });
  }
}
// Discord-side claims and timeout deletions are reflected without requiring a
// full page refresh.  The API's server_time keeps the local countdown aligned
// even when the browser clock is skewed.
window.setInterval(refreshActiveTicketView, 5_000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshActiveTicketView();
});
window.addEventListener("keydown", (event) => { if (event.key === "Escape") closeModals(); });
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
