(function () {
  "use strict";

  const DEFAULT_GAME = {
    id: "roulette",
    name: "Roulette",
    theme: "#000000",
    bannerPath: "/assets/games/roulette_banner.png",
    minimumPlayers: 2,
    maximumPlayers: 20,
    enabled: true,
    language: "en",
    wheelMode: "multi",
    wheelColor: "#6B7280",
    wheelColors: ["#6B7280", "#9CA3AF", "#4B5563", "#374151", "#D1D5DB", "#818CF8", "#A78BFA"],
    turnTimerSeconds: 30,
  };

  let root = null;
  let api = null;
  let guildId = null;
  let channels = [];
  let catalogOptions = null;
  let loadToken = 0;

  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) {
      if (value instanceof Node) element.append(value);
      else element.textContent = value;
    }
    return element;
  }

  function image(src, className, alt) {
    const element = document.createElement("img");
    element.className = className;
    element.src = src;
    element.alt = alt || "";
    element.loading = "lazy";
    element.decoding = "async";
    element.addEventListener("error", () => element.remove());
    return element;
  }

  function wait(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  async function waitForRequest(requestId, requestApi = api) {
    const deadline = Date.now() + 25_000;
    while (Date.now() < deadline) {
      if (!requestApi) throw new Error("The Roulette dashboard is no longer available.");
      const status = await requestApi.requestJson(`/api/command-requests/${encodeURIComponent(requestId)}`, { cache: "no-store" });
      if (status.status === "complete") return status;
      if (status.status === "failed") throw new Error(status.error || "BirdBot could not create the Roulette lobby.");
      await wait(350);
    }
    throw new Error("The Roulette lobby request is taking too long. Check Discord and try again.");
  }

  function renderConfig(game) {
    const values = game || DEFAULT_GAME;
    const section = node("section", "roulette-config-card");
    const header = node("div", "spy-game-section-header");
    header.append(node("div", "spy-config-heading", node("span", "games-kicker", "ROULETTE PANEL")), node("span", "spy-config-hint", "Saved per server"));
    const copy = node("p", "spy-config-copy", "Set the lobby capacity, language, and the seven colors used by the Roulette wheel.");
    const grid = node("div", "spy-config-grid");
    const fields = [
      ["Minimum players", "minimum_players", values.minimumPlayers || 2, 2, 50],
      ["Maximum players", "maximum_players", values.maximumPlayers || 20, 2, 50],
      ["Turn timer (seconds)", "turn_timer_seconds", values.turnTimerSeconds || 30, 5, 600],
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
    const languageField = node("label", "spy-config-field", "Game language");
    const languageSelect = document.createElement("select");
    languageSelect.className = "spy-config-input";
    languageSelect.append(new Option("English", "en"), new Option("العربية", "ar"));
    languageSelect.value = values.language === "ar" ? "ar" : "en";
    languageField.append(languageSelect);
    grid.append(languageField);
    const palette = Array.isArray(values.wheelColors) && values.wheelColors.length === 7
      ? values.wheelColors
      : ["#6B7280", "#9CA3AF", "#4B5563", "#374151", "#D1D5DB", "#818CF8", "#A78BFA"];
    const paletteField = node("fieldset", "roulette-palette-field");
    const paletteLegend = node("legend", "spy-config-field", "Different colors per slice (7 colors)");
    const paletteGrid = node("div", "roulette-palette-grid");
    const colorInputs = [];
    palette.forEach((color, index) => {
      const label = node("label", "roulette-palette-item", `Slice ${index + 1}`);
      const input = document.createElement("input");
      input.type = "color";
      input.className = "spy-config-input roulette-color-input";
      input.value = /^#[0-9a-f]{6}$/i.test(String(color || "")) ? color : "#6B7280";
      input.setAttribute("aria-label", `Roulette slice ${index + 1} color`);
      label.append(input);
      paletteGrid.append(label);
      colorInputs.push(input);
    });
    paletteField.append(paletteLegend, paletteGrid);
    const paletteHint = node("p", "roulette-color-hint", "Each player slice uses one of these seven colors. Edit any swatch to customize the palette; colors repeat only if more than seven players join.");
    const status = node("span", "spy-config-status", "");
    const save = node("button", "primary-button spy-config-save", "Save Roulette Settings");
    save.type = "button";
    save.addEventListener("click", async () => {
      if (!api || save.disabled) return;
      const minimum = Number(inputs.minimum_players.value);
      const maximum = Number(inputs.maximum_players.value);
      const turnTimer = Number(inputs.turn_timer_seconds.value);
      if (!Number.isInteger(minimum) || !Number.isInteger(maximum) || !Number.isInteger(turnTimer) || minimum < 2 || maximum < minimum || maximum > 50 || turnTimer < 5 || turnTimer > 600) {
        status.className = "spy-config-status is-error";
        status.textContent = "Use 2–50 players and a 5–600 second turn timer.";
        return;
      }
      save.disabled = true;
      status.className = "spy-config-status is-loading";
      status.textContent = "Saving…";
      api.beginLoading("Saving Roulette settings...", false);
      try {
        const result = await api.requestJson(`/api/guilds/${encodeURIComponent(guildId)}/games/roulette/config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            minimum_players: minimum,
            maximum_players: maximum,
            enabled: values.enabled !== false,
            language: languageSelect.value,
            wheel_mode: "multi",
            wheel_color: colorInputs[0].value,
            wheel_colors: colorInputs.map((input) => input.value),
            turn_timer_seconds: turnTimer,
          }),
        });
        const saved = result && result.config;
        if (saved) {
          values.enabled = saved.enabled !== false;
          values.language = saved.language === "ar" ? "ar" : "en";
          values.minimumPlayers = saved.minimumPlayers || minimum;
          values.maximumPlayers = saved.maximumPlayers || maximum;
          values.turnTimerSeconds = saved.turnTimerSeconds || turnTimer;
          values.wheelMode = "multi";
          values.wheelColor = saved.wheelColor || colorInputs[0].value;
          values.wheelColors = Array.isArray(saved.wheelColors) && saved.wheelColors.length === 7 ? saved.wheelColors : values.wheelColors;
          inputs.minimum_players.value = String(saved.minimumPlayers || minimum);
          inputs.maximum_players.value = String(saved.maximumPlayers || maximum);
          inputs.turn_timer_seconds.value = String(saved.turnTimerSeconds || turnTimer);
          languageSelect.value = saved.language === "ar" ? "ar" : "en";
          colorInputs.forEach((input, index) => {
            input.value = values.wheelColors?.[index] || input.value;
          });
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
    section.append(header, copy, grid, paletteField, paletteHint, save, status);
    return section;
  }

  function renderRules(language) {
    const rules = node("section", "spy-rules-grid roulette-rules");
    const english = node("article", `spy-rule-card ${language === "en" ? "is-primary" : ""}`);
    english.append(
      node("span", "spy-rule-language", "ENGLISH"),
      node("h4", "", "How to play"),
      node("p", "", "Join the lobby, wait for the host to start, then take your turn when the bot prompts you. The wheel selects one player at a time; the match ends when the configured round or winner condition is reached."),
    );
    const arabic = node("article", `spy-rule-card spy-rule-card-ar ${language === "ar" ? "is-primary" : ""}`);
    arabic.dir = "rtl";
    arabic.append(
      node("span", "spy-rule-language", "العربية"),
      node("h4", "", "طريقة اللعب"),
      node("p", "", "انضم إلى الرَدْهة وانتظر بدء المضيف، ثم خذ دورك عندما يطلب البوت ذلك. تختار العجلة لاعباً في كل مرة، وتنتهي المباراة عند الوصول إلى الجولة أو شرط الفوز المحدد."),
    );
    rules.append(english, arabic);
    return rules;
  }

  function renderDetails(game) {
    if (!root) return;
    root.replaceChildren();
    const wrapper = node("div", "games-view roulette-game-detail");
    const top = node("div", "spy-game-detail-top");
    const back = node("button", "secondary-button games-back-button", "← Games");
    back.type = "button";
    back.addEventListener("click", () => {
      if (window.BirdBotGames?.mount && catalogOptions) window.BirdBotGames.mount(catalogOptions);
    });
    top.append(back);
    const hero = node("div", "spy-game-hero roulette-game-hero");
    hero.append(image(game.bannerPath || DEFAULT_GAME.bannerPath, "roulette-game-banner", "Roulette banner"));
    hero.append(node("div", "spy-game-hero-copy", node("h3", "", game.name || "Roulette")));
    hero.querySelector(".spy-game-hero-copy").append(node("p", "", "A quick, fair wheel spin for your community."));

    const launch = node("section", "roulette-launch-card");
    const heading = node("div", "spy-game-section-header");
    heading.append(node("span", "games-kicker", "DISCORD LOBBY"));
    const start = node("button", "primary-button roulette-start-button", "Start Roulette");
    start.type = "button";
    const status = node("span", "spy-lobby-status", "");
    heading.append(start, status);
    launch.append(heading, node("p", "", "Choose a text channel and post a ready-to-join Roulette lobby in Discord."));
    const picker = node("div", "spy-channel-picker");
    const label = node("label", "spy-channel-label", "Lobby channel");
    const select = document.createElement("select");
    select.className = "spy-channel-select";
    select.setAttribute("aria-label", "Roulette lobby channel");
    if (!channels.length) {
      select.append(new Option("No accessible text channels", ""));
      select.disabled = true;
      start.disabled = true;
    } else {
      channels.forEach((channel) => select.append(new Option(`# ${channel.name || channel.id}`, String(channel.id))));
    }
    label.append(select);
    const actions = node("div", "spy-channel-picker-actions");
    const post = node("button", "primary-button", "Post Lobby");
    const cancel = node("button", "secondary-button", "Cancel");
    post.type = "button";
    cancel.type = "button";
    actions.append(post, cancel);
    picker.append(label, actions);
    picker.hidden = true;
    launch.append(picker);
    start.addEventListener("click", () => {
      picker.hidden = false;
      start.hidden = true;
      select.focus();
    });
    if (game.enabled === false) {
      start.disabled = true;
      status.className = "spy-lobby-status is-error";
      status.textContent = "Roulette is disabled. Enable it in Games first.";
    }
    cancel.addEventListener("click", () => {
      picker.hidden = true;
      start.hidden = false;
    });
    post.addEventListener("click", async () => {
      if (!select.value || post.disabled || !api) return;
      post.disabled = true;
      cancel.disabled = true;
      status.className = "spy-lobby-status is-loading";
      status.textContent = "Posting lobby…";
      api.beginLoading("Creating Roulette lobby...", false);
      try {
        const queued = await api.requestJson(`/api/guilds/${encodeURIComponent(guildId)}/games/roulette/start`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel_id: select.value }),
        });
        await waitForRequest(queued.request_id, api);
        picker.hidden = true;
        start.hidden = false;
        status.className = "spy-lobby-status is-success";
        status.textContent = "Roulette lobby created in Discord!";
      } catch (error) {
        status.className = "spy-lobby-status is-error";
        status.textContent = error instanceof Error ? error.message : "The Roulette lobby could not be created.";
      } finally {
        post.disabled = false;
        cancel.disabled = false;
        api.endLoading(false);
      }
    });
    const config = renderConfig(game);
    const rules = renderRules(game.language === "ar" ? "ar" : "en");
    const setup = node("button", "game-setup-button", "Setup");
    setup.type = "button";
    setup.addEventListener("click", () => config.querySelector(".spy-config-save")?.click());
    wrapper.append(top, hero, launch, rules, config, setup);
    root.append(wrapper);
  }

  async function open(options) {
    unmount();
    if (!options || !options.root || !options.guildId || !options.api) return;
    root = options.root;
    api = options.api;
    guildId = options.guildId;
    channels = Array.isArray(options.channels) ? options.channels : [];
    catalogOptions = options.catalogOptions || null;
    const currentApi = api;
    const token = ++loadToken;
    root.replaceChildren(node("div", "games-loading", "Loading Roulette details…"));
    currentApi.beginLoading("Loading Roulette details...", false);
    try {
      const result = await currentApi.requestJson(`/api/guilds/${encodeURIComponent(guildId)}/games/roulette`, { cache: "no-store" });
      if (root && token === loadToken) renderDetails(result.game || DEFAULT_GAME);
    } catch (error) {
      if (root && token === loadToken) {
        const failure = node("div", "games-loading");
        failure.append(node("p", "form-error", error instanceof Error ? error.message : "Roulette details could not be loaded."));
        const retry = node("button", "secondary-button games-back-button", "Retry");
        retry.type = "button";
        retry.addEventListener("click", () => open(options));
        failure.append(retry);
        root.replaceChildren(failure);
      }
    } finally {
      currentApi.endLoading(false);
    }
  }

  function unmount() {
    loadToken += 1;
    if (root) root.replaceChildren();
    root = null;
    api = null;
    guildId = null;
    channels = [];
    catalogOptions = null;
  }

  window.BirdBotRoulette = { open, unmount };
}());
