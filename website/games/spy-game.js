(function () {
  "use strict";

  const DEFAULT_GAME = {
    id: "spy",
    name: "Spy Game",
    theme: "#000000",
    bannerPath: "/assets/games/spy_banner.svg",
    iconPath: "/assets/games/spy_icon.svg",
    minimumPlayers: 3,
    maximumPlayers: 20,
    questionTimerSeconds: 30,
    endMode: "manual",
    autoEndRounds: 20,
    enabled: true,
    language: "en",
  };
  const DEFAULT_ROULETTE_GAME = {
    id: "roulette",
    name: "Roulette",
    theme: "#000000",
    bannerPath: "/assets/games/roulette_banner.png",
    iconPath: "/assets/games/roulette_icon.svg",
    minimumPlayers: 2,
    maximumPlayers: 20,
    enabled: true,
    language: "en",
    wheelMode: "multi",
    wheelColor: "#6B7280",
    wheelColors: ["#6B7280", "#9CA3AF", "#4B5563", "#374151", "#D1D5DB", "#818CF8", "#A78BFA"],
    turnTimerSeconds: 30,
  };
  // Icons8 CDN assets keep the controls lightweight while matching the rest
  // of the dashboard icon treatment.
  const ICONS8 = {
    start: "https://img.icons8.com/ios/24/ffffff/play--v1.png",
    join: "https://img.icons8.com/ios/24/ffffff/login-rounded-right.png",
    leave: "https://img.icons8.com/ios/24/ffffff/logout-rounded-left.png",
    players: "https://img.icons8.com/ios/24/ffffff/conference-call.png",
  };

  let mountedRoot = null;
  let mountedGuildId = null;
  let api = null;
  let availableChannels = [];
  let mountOptions = null;
  let catalogGames = [];

  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) {
      if (value instanceof Node) element.append(value);
      else element.textContent = value;
    }
    return element;
  }

  function renderImage(src, className, alt) {
    const image = document.createElement("img");
    image.className = className;
    image.src = src;
    image.alt = alt || "";
    image.loading = "lazy";
    image.addEventListener("error", () => image.remove());
    return image;
  }

  function iconButton(label, icon, className) {
    const button = node("button", className || "primary-button");
    button.type = "button";
    button.append(renderImage(icon, "spy-action-icon", ""), node("span", "spy-action-label", label));
    return button;
  }

  function wait(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  function gameConfigPayload(game, enabled) {
    const payload = {
      enabled: Boolean(enabled),
      language: game?.language === "ar" ? "ar" : "en",
      minimum_players: Number(game?.minimumPlayers) || (game?.id === "roulette" ? 2 : 3),
      maximum_players: Number(game?.maximumPlayers) || 20,
    };
    if (game?.id === "spy") {
      payload.question_timer_seconds = Number(game?.questionTimerSeconds) || 30;
      payload.end_mode = game?.endMode === "auto" ? "auto" : "manual";
      payload.auto_end_rounds = Number(game?.autoEndRounds) || 20;
    }
    if (game?.id === "roulette") {
      payload.wheel_mode = "multi";
      const colors = Array.isArray(game?.wheelColors) ? game.wheelColors.slice(0, 7) : [];
      payload.wheel_colors = colors.length === 7 && colors.every((value) => /^#[0-9a-f]{6}$/i.test(String(value)))
        ? colors
        : ["#6B7280", "#9CA3AF", "#4B5563", "#374151", "#D1D5DB", "#818CF8", "#A78BFA"];
      payload.wheel_color = payload.wheel_colors[0];
      payload.turn_timer_seconds = Number(game?.turnTimerSeconds) || 30;
    }
    return payload;
  }

  async function toggleGame(game, button) {
    if (!api || !game || button.disabled) return;
    const gameId = String(game.id || "").toLowerCase();
    if (!["spy", "roulette"].includes(gameId)) return;
    const previous = game.enabled !== false;
    const next = !previous;
    button.disabled = true;
    button.textContent = next ? "Enabling…" : "Disabling…";
    api.beginLoading(`${next ? "Enabling" : "Disabling"} ${game.name || "game"}...`, false);
    try {
      const result = await api.requestJson(`/api/guilds/${encodeURIComponent(mountedGuildId)}/games/${gameId}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(gameConfigPayload(game, next)),
      });
      const saved = result && result.config;
      if (!saved || typeof saved.enabled !== "boolean") throw new Error("The game setting could not be confirmed.");
      game.enabled = saved.enabled;
      game.language = saved.language || game.language || "en";
      game.minimumPlayers = saved.minimumPlayers || game.minimumPlayers;
      game.maximumPlayers = saved.maximumPlayers || game.maximumPlayers;
      if (gameId === "spy") game.questionTimerSeconds = saved.questionTimerSeconds || game.questionTimerSeconds;
      if (gameId === "spy") {
        game.endMode = saved.endMode === "auto" ? "auto" : "manual";
        game.autoEndRounds = saved.autoEndRounds || game.autoEndRounds || 20;
      }
      if (gameId === "roulette") {
        game.wheelMode = saved.wheelMode || game.wheelMode;
        game.wheelColor = saved.wheelColor || game.wheelColor;
        game.wheelColors = Array.isArray(saved.wheelColors) ? saved.wheelColors : game.wheelColors;
      }
      renderCatalog(catalogGames);
    } catch (error) {
      button.disabled = false;
      button.textContent = previous ? "Game Enabled" : "Enable Game";
      renderCatalog(catalogGames, error instanceof Error ? error.message : "The game setting could not be saved.");
    } finally {
      api.endLoading(false);
    }
  }

  async function waitForRequest(requestApi, requestId) {
    const deadline = Date.now() + 25_000;
    while (Date.now() < deadline) {
      const status = await requestApi.requestJson(`/api/command-requests/${encodeURIComponent(requestId)}`, { cache: "no-store" });
      if (status.status === "complete") return status;
      if (status.status === "failed") throw new Error(status.error || "BirdBot could not create the Spy Game lobby.");
      await wait(350);
    }
    throw new Error("The lobby request is taking too long. Check Discord and try again.");
  }

  function renderCatalog(games, notice = "") {
    if (!mountedRoot) return;
    catalogGames = Array.isArray(games) ? games.slice() : [];
    mountedRoot.replaceChildren();
    const wrapper = node("div", "games-view");
    const heading = node("div", "games-view-heading");
    heading.append(
      node("p", "games-kicker", "MINI-GAMES"),
      node("h3", "games-view-title", "Choose a game"),
      node("p", "games-view-copy", "Start a lobby in Discord and bring your community into the action."),
    );
    if (notice) heading.append(node("p", "form-error", notice));
    const grid = node("div", "games-catalog-grid");
    (games.length ? games : [DEFAULT_GAME, DEFAULT_ROULETTE_GAME]).forEach((game) => {
      const card = node("article", "game-catalog-card");
      const gameId = String(game?.id || "").toLowerCase();
      card.append(renderImage(game.iconPath || DEFAULT_GAME.iconPath, "game-catalog-icon", ""));
      const copy = node("span", "game-catalog-copy");
      copy.append(
        node("strong", "game-catalog-name", game.name || "Spy Game"),
        node("span", "game-catalog-description", gameId === "roulette"
          ? "A quick, fair wheel spin for your community."
          : "A hidden-role game of questions, clues, and careful deception."),
        node("span", "game-catalog-action", "View game details →"),
      );
      card.append(copy);
      const enableButton = node("button", "game-catalog-enable", game.enabled === false ? "Enable Game" : "Game Enabled");
      enableButton.type = "button";
      enableButton.classList.toggle("is-enabled", game.enabled !== false);
      enableButton.setAttribute("aria-pressed", String(game.enabled !== false));
      enableButton.title = game.enabled === false ? "Enable this game for the server" : "Disable this game for the server";
      enableButton.addEventListener("click", (event) => {
        event.stopPropagation();
        void toggleGame(game, enableButton);
      });
      card.append(enableButton);
      card.addEventListener("click", () => {
        if (gameId === "roulette") {
          if (!window.BirdBotRoulette?.open) {
            renderCatalog(catalogGames, "Roulette details could not be loaded. Refresh the page and try again.");
            return;
          }
          window.BirdBotRoulette.open({
            root: mountedRoot,
            guildId: mountedGuildId,
            channels: availableChannels,
            api,
            catalogOptions: mountOptions,
          });
          return;
        }
        loadDetails(game);
      });
      grid.append(card);
    });
    wrapper.append(heading, grid);
    mountedRoot.append(wrapper);
  }

  function renderRules(language) {
    const rules = node("div", "spy-rules-grid");
    if (language === "ar") {
      const arabic = node("article", "spy-rule-card spy-rule-card-ar");
      arabic.dir = "rtl";
      arabic.append(
        node("span", "spy-rule-language", "العربية"),
        node("h4", "", "طريقة اللعب"),
        node("p", "", "يحصل المواطنون على كلمة أو مكان سري. يتم اختيار لاعب واحد سراً ليكون الجاسوس. يطرح المواطنون أسئلة ذكية لاكتشاف الجاسوس، بينما يحاول الجاسوس الاندماج ومعرفة السر دون أن ينكشف."),
      );
      rules.append(arabic);
    } else {
      const english = node("article", "spy-rule-card");
      english.append(
        node("span", "spy-rule-language", "ENGLISH"),
        node("h4", "", "How it works"),
        node("p", "", "Citizens receive a secret word or location. One player is secretly the Spy. Citizens ask careful questions to find the Spy, while the Spy blends in and tries to discover the secret without getting caught."),
      );
      rules.append(english);
    }
    return rules;
  }

  function formatDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
  }

  function renderLogs(logs) {
    const section = node("section", "spy-game-logs");
    const header = node("div", "spy-game-section-header");
    header.append(node("h3", "", "Game Logs"), node("span", "spy-game-log-count", `${logs.length} match${logs.length === 1 ? "" : "es"}`));
    if (!logs.length) {
      section.append(header, node("p", "empty-state", "No completed Spy Game matches have been logged yet."));
      return section;
    }
    const table = node("div", "spy-game-log-table");
    logs.forEach((log) => {
      const row = node("article", "spy-game-log-row");
      const identities = Array.isArray(log.citizens) ? log.citizens.map((citizen) => citizen && citizen.name ? citizen.name : citizen.id || "Unknown").join(", ") : "None";
      row.append(
        node("div", "spy-log-main", node("strong", "", log.outcome || "Match finished")),
        node("div", "spy-log-field", `${formatDate(log.match_at)} · Server ${log.guild_id || mountedGuildId}`),
        node("div", "spy-log-field", `Secret: ${log.secret || "Unknown"}`),
        node("div", "spy-log-field", `Spy: ${log.spy_name || "Unknown"} (${log.spy_id || "—"})`),
        node("div", "spy-log-field", `Citizens: ${identities || "None"}`),
      );
      table.append(row);
    });
    section.append(header, table);
    return section;
  }

  function renderSpyConfig(config) {
    const values = config || DEFAULT_GAME;
    const section = node("section", "spy-config-card");
    const header = node("div", "spy-game-section-header");
    header.append(
      node("div", "spy-config-heading", node("span", "games-kicker", "SPY PANEL")),
      node("span", "spy-config-hint", "Saved per server"),
    );
    const copy = node("p", "spy-config-copy", "Set the lobby capacity, question timer, and how the discussion ends.");
    const grid = node("div", "spy-config-grid");
    const fields = [
      ["Minimum players", "minimum_players", values.minimumPlayers || 3, 3, 50],
      ["Maximum players", "maximum_players", values.maximumPlayers || 20, 3, 50],
      ["Question timer (seconds)", "question_timer_seconds", values.questionTimerSeconds || 30, 5, 600],
    ];
    const inputs = {};
    fields.forEach(([label, name, value, min, max]) => {
      const wrapper = node("label", "spy-config-field", label);
      const input = document.createElement("input");
      input.type = "number";
      input.name = name;
      input.min = String(min);
      input.max = String(max);
      input.step = "1";
      input.value = String(value);
      input.className = "spy-config-input";
      input.inputMode = "numeric";
      wrapper.append(input);
      grid.append(wrapper);
      inputs[name] = input;
    });
    const endModeField = node("label", "spy-config-field", "Game end mode");
    const endModeSelect = document.createElement("select");
    endModeSelect.className = "spy-config-input";
    endModeSelect.append(new Option("Manual End", "manual"), new Option("Auto End", "auto"));
    endModeSelect.value = values.endMode === "auto" ? "auto" : "manual";
    endModeField.append(endModeSelect);
    grid.append(endModeField);
    const autoRoundsField = node("label", "spy-config-field", "Auto End after rounds");
    const autoRoundsInput = document.createElement("input");
    autoRoundsInput.type = "number";
    autoRoundsInput.name = "auto_end_rounds";
    autoRoundsInput.min = "1";
    autoRoundsInput.max = "1000";
    autoRoundsInput.step = "1";
    autoRoundsInput.value = String(values.autoEndRounds || 20);
    autoRoundsInput.className = "spy-config-input";
    autoRoundsInput.inputMode = "numeric";
    autoRoundsField.append(autoRoundsInput);
    grid.append(autoRoundsField);
    const syncEndMode = () => {
      autoRoundsField.hidden = endModeSelect.value !== "auto";
      autoRoundsInput.disabled = endModeSelect.value !== "auto";
    };
    endModeSelect.addEventListener("change", syncEndMode);
    syncEndMode();
    const languageField = node("label", "spy-config-field", "Game language");
    const languageSelect = document.createElement("select");
    languageSelect.className = "spy-config-input";
    languageSelect.append(new Option("English", "en"), new Option("العربية", "ar"));
    languageSelect.value = values.language === "ar" ? "ar" : "en";
    languageField.append(languageSelect);
    grid.append(languageField);
    const actions = node("div", "spy-config-actions");
    const save = node("button", "primary-button spy-config-save", "Save Spy Settings");
    save.type = "button";
    const status = node("span", "spy-config-status", "");
    actions.append(save, status);
    save.addEventListener("click", async () => {
      if (!api || save.disabled) return;
      const minimum = Number(inputs.minimum_players.value);
      const maximum = Number(inputs.maximum_players.value);
      const timer = Number(inputs.question_timer_seconds.value);
      const autoRounds = Number(autoRoundsInput.value);
      if (!Number.isInteger(minimum) || !Number.isInteger(maximum) || !Number.isInteger(timer) || !Number.isInteger(autoRounds) || minimum < 3 || maximum < minimum || maximum > 50 || timer < 5 || timer > 600 || autoRounds < 1 || autoRounds > 1000) {
        status.className = "spy-config-status is-error";
        status.textContent = "Use 3–50 players, a 5–600 second timer, and 1–1000 auto-end rounds.";
        return;
      }
      save.disabled = true;
      status.className = "spy-config-status is-loading";
      status.textContent = "Saving…";
      api.beginLoading("Saving Spy Game settings...", false);
      try {
        const result = await api.requestJson(`/api/guilds/${encodeURIComponent(mountedGuildId)}/games/spy/config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ minimum_players: minimum, maximum_players: maximum, question_timer_seconds: timer, end_mode: endModeSelect.value, auto_end_rounds: autoRounds, language: languageSelect.value, enabled: values.enabled !== false }),
        });
        const saved = result && result.config ? result.config : null;
        if (saved) {
          values.enabled = saved.enabled !== false;
          values.language = saved.language === "ar" ? "ar" : "en";
          values.minimumPlayers = saved.minimumPlayers || minimum;
          values.maximumPlayers = saved.maximumPlayers || maximum;
          values.questionTimerSeconds = saved.questionTimerSeconds || timer;
          values.endMode = saved.endMode === "auto" ? "auto" : "manual";
          values.autoEndRounds = saved.autoEndRounds || autoRounds;
          inputs.minimum_players.value = String(saved.minimumPlayers || minimum);
          inputs.maximum_players.value = String(saved.maximumPlayers || maximum);
          inputs.question_timer_seconds.value = String(saved.questionTimerSeconds || timer);
          endModeSelect.value = values.endMode;
          autoRoundsInput.value = String(values.autoEndRounds);
          syncEndMode();
          languageSelect.value = saved.language === "ar" ? "ar" : "en";
        }
        status.className = "spy-config-status is-success";
        status.textContent = "Settings saved.";
      } catch (error) {
        status.className = "spy-config-status is-error";
        status.textContent = error instanceof Error ? error.message : "Settings could not be saved.";
      } finally {
        save.disabled = false;
        api.endLoading(false);
      }
    });
    section.append(header, copy, grid, actions);
    return section;
  }

  function renderDetails(game, logs) {
    if (!mountedRoot) return;
    mountedRoot.replaceChildren();
    const wrapper = node("div", "games-view spy-game-detail");
    const top = node("div", "spy-game-detail-top");
    const back = node("button", "secondary-button games-back-button", "← Games");
    back.type = "button";
    back.addEventListener("click", () => renderCatalog(catalogGames.length ? catalogGames : [game]));
    top.append(back);
    const hero = node("div", "spy-game-hero");
    hero.style.setProperty("--spy-banner", `url("${game.bannerPath || DEFAULT_GAME.bannerPath}")`);
    hero.append(renderImage(game.iconPath || DEFAULT_GAME.iconPath, "spy-game-icon", ""), node("div", "spy-game-hero-copy", node("h3", "", game.name || "Spy Game")));
    hero.querySelector(".spy-game-hero-copy").append(node("p", "", "A social deduction game for curious minds and clever spies."));

    const launch = node("aside", "spy-game-launch-card");
    const launchHeader = node("div", "spy-game-launch-header");
    launchHeader.append(node("span", "games-kicker", "DISCORD SETUP"));
    const start = iconButton("Start", ICONS8.start, "primary-button spy-start-button");
    const status = node("span", "spy-lobby-status", "");
    launchHeader.append(start, status);
    launch.append(
      launchHeader,
      node("strong", "", "Create a Spy Game lobby"),
      node("p", "", "Choose a channel and post a ready-to-join lobby in Discord. Players can then join from the lobby controls."),
    );
    launch.append(node("p", "spy-config-hint", `Language: ${game.language === "ar" ? "العربية" : "English"} (managed in Setup below)`));
    const rules = renderRules(game.language === "ar" ? "ar" : "en");

    const channelPicker = node("div", "spy-channel-picker");
    const pickerLabel = node("label", "spy-channel-label", "Lobby channel");
    const channelSelect = document.createElement("select");
    channelSelect.className = "spy-channel-select";
    channelSelect.setAttribute("aria-label", "Spy Game lobby channel");
    const channels = Array.isArray(availableChannels) ? availableChannels.filter((channel) => channel && channel.id) : [];
    if (!channels.length) {
      channelSelect.append(new Option("No accessible text channels", ""));
      channelSelect.disabled = true;
    } else {
      channels.forEach((channel) => channelSelect.append(new Option(`# ${channel.name || channel.id}`, String(channel.id))));
    }
    pickerLabel.append(channelSelect);
    const pickerActions = node("div", "spy-channel-picker-actions");
    const confirm = node("button", "primary-button spy-confirm-lobby", "Post Lobby");
    const cancel = node("button", "secondary-button spy-cancel-lobby", "Cancel");
    confirm.type = "button";
    cancel.type = "button";
    pickerActions.append(confirm, cancel);
    channelPicker.append(pickerLabel, pickerActions);
    channelPicker.hidden = true;
    launch.append(channelPicker, node("code", "spy-game-command", "/start"));
    start.disabled = !channels.length;
    if (game.enabled === false) {
      start.disabled = true;
      status.className = "spy-lobby-status is-error";
      status.textContent = "Spy Game is disabled. Enable it in Games first.";
    }
    start.addEventListener("click", () => {
      channelPicker.hidden = false;
      start.hidden = true;
      channelSelect.focus();
    });
    cancel.addEventListener("click", () => {
      channelPicker.hidden = true;
      start.hidden = false;
    });
    confirm.addEventListener("click", async () => {
      if (!channelSelect.value || confirm.disabled) return;
      const actionApi = api;
      if (!actionApi) return;
      confirm.disabled = true;
      cancel.disabled = true;
      status.className = "spy-lobby-status is-loading";
      status.textContent = "Posting lobby…";
      actionApi.beginLoading("Creating Spy Game lobby...", false);
      try {
        const queued = await actionApi.requestJson(`/api/guilds/${encodeURIComponent(mountedGuildId)}/games/spy/start`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel_id: channelSelect.value }),
        });
        await waitForRequest(actionApi, queued.request_id);
        channelPicker.hidden = true;
        start.hidden = false;
        status.className = "spy-lobby-status is-success";
        status.textContent = "Lobby Created in Discord!";
      } catch (error) {
        status.className = "spy-lobby-status is-error";
        status.textContent = error instanceof Error ? error.message : "The Spy Game lobby could not be created.";
      } finally {
        confirm.disabled = false;
        cancel.disabled = false;
        actionApi.endLoading(false);
      }
    });

    const config = renderSpyConfig(game);
    const setup = node("button", "game-setup-button", "Setup");
    setup.type = "button";
    setup.addEventListener("click", () => config.querySelector(".spy-config-save")?.click());
    wrapper.append(top, hero, launch, rules, config, renderLogs(logs), setup);
    mountedRoot.append(wrapper);
  }

  async function loadDetails(game) {
    if (!mountedRoot || !api || !mountedGuildId) return;
    const currentApi = api;
    const guildId = mountedGuildId;
    mountedRoot.replaceChildren(node("div", "games-loading", "Loading Spy Game details…"));
    currentApi.beginLoading("Loading Spy Game details...", false);
    try {
      const result = await currentApi.requestJson(`/api/guilds/${encodeURIComponent(guildId)}/games/spy`, { cache: "no-store" });
      if (mountedRoot && mountedGuildId === guildId) renderDetails(result.game || game, Array.isArray(result.logs) ? result.logs : []);
    } catch (error) {
      if (mountedRoot && mountedGuildId === guildId) {
        const failure = node("div", "games-loading");
        failure.append(node("p", "form-error", error instanceof Error ? error.message : "Spy Game details could not be loaded."));
        const retry = node("button", "secondary-button games-back-button", "Retry");
        retry.type = "button";
        retry.addEventListener("click", () => loadDetails(game));
        failure.append(retry);
        mountedRoot.replaceChildren(failure);
      }
    } finally {
      currentApi.endLoading(false);
    }
  }

  async function mount(options) {
    unmount();
    if (!options || !options.root || !options.guildId) return;
    mountedRoot = options.root;
    mountedGuildId = options.guildId;
    api = options;
    mountOptions = options;
    availableChannels = Array.isArray(options.channels) ? options.channels : [];
    const currentApi = api;
    const guildId = mountedGuildId;
    renderCatalog([DEFAULT_GAME, DEFAULT_ROULETTE_GAME]);
    currentApi.beginLoading("Loading games...", false);
    try {
      const result = await currentApi.requestJson(`/api/guilds/${encodeURIComponent(mountedGuildId)}/games`, { cache: "no-store" });
      if (mountedRoot && mountedGuildId === guildId && Array.isArray(result.games) && result.games.length) renderCatalog(result.games);
    } catch (error) {
      if (mountedRoot && mountedGuildId === guildId) {
        renderCatalog([DEFAULT_GAME, DEFAULT_ROULETTE_GAME], error instanceof Error ? error.message : "Games could not be loaded. Try again.");
      }
    } finally {
      currentApi.endLoading(false);
    }
  }

  function unmount() {
    if (window.BirdBotRoulette?.unmount) window.BirdBotRoulette.unmount();
    if (mountedRoot) mountedRoot.replaceChildren();
    mountedRoot = null;
    mountedGuildId = null;
    api = null;
    availableChannels = [];
    mountOptions = null;
    catalogGames = [];
  }

  window.BirdBotGames = { mount, unmount };
}());
