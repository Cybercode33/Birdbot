(function () {
  "use strict";

  const DEFAULT_GAME = {
    id: "guess-number",
    name: "Guess the Number",
    bannerPath: "/assets/games/spy_banner.svg",
    iconPath: "/assets/games/spy_icon.svg",
    minimumPlayers: 2,
    maximumPlayers: 20,
    numberMinimum: 1,
    numberMaximum: 100,
    enabled: true,
    language: "en",
  };
  let root = null;
  let api = null;
  let guildId = null;
  let channels = [];
  let catalogOptions = null;

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
    element.src = src || DEFAULT_GAME.iconPath;
    element.alt = alt || "";
    element.loading = "lazy";
    element.addEventListener("error", () => element.remove());
    return element;
  }

  function wait(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  async function waitForRequest(requestId) {
    const deadline = Date.now() + 25_000;
    while (Date.now() < deadline) {
      const status = await api.requestJson(`/api/command-requests/${encodeURIComponent(requestId)}`, { cache: "no-store" });
      if (status.status === "complete") return status;
      if (status.status === "failed") throw new Error(status.error || "BirdBot could not create the game lobby.");
      await wait(350);
    }
    throw new Error("The lobby request is taking too long. Check Discord and try again.");
  }

  function renderConfig(game) {
    const values = game || DEFAULT_GAME;
    const section = node("section", "spy-config-card");
    const header = node("div", "spy-game-section-header");
    header.append(node("div", "spy-config-heading", node("span", "games-kicker", "GUESS THE NUMBER PANEL")), node("span", "spy-config-hint", "Saved per server"));
    const copy = node("p", "spy-config-copy", "Choose the lobby size, hidden-number range, and language used by the Discord game.");
    const grid = node("div", "spy-config-grid");
    const fields = [
      ["Minimum players", "minimum_players", values.minimumPlayers || 2, 2, 50],
      ["Maximum players", "maximum_players", values.maximumPlayers || 20, 2, 50],
      ["Smallest hidden number", "number_minimum", values.numberMinimum ?? 1, -1000000, 1000000],
      ["Largest hidden number", "number_maximum", values.numberMaximum ?? 100, -1000000, 1000000],
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
    const language = document.createElement("select");
    language.className = "spy-config-input";
    language.append(new Option("English", "en"), new Option("العربية", "ar"));
    language.value = values.language === "ar" ? "ar" : "en";
    languageField.append(language);
    grid.append(languageField);
    const actions = node("div", "spy-config-actions");
    const save = node("button", "primary-button spy-config-save", "Save Guess the Number settings");
    save.type = "button";
    const status = node("span", "spy-config-status", "");
    actions.append(save, status);
    save.addEventListener("click", async () => {
      if (!api || save.disabled) return;
      const valuesToSave = Object.fromEntries(Object.keys(inputs).map((key) => [key, Number(inputs[key].value)]));
      if (!Number.isInteger(valuesToSave.minimum_players) || !Number.isInteger(valuesToSave.maximum_players) || valuesToSave.minimum_players < 2 || valuesToSave.maximum_players < valuesToSave.minimum_players || valuesToSave.maximum_players > 50 || !Number.isInteger(valuesToSave.number_minimum) || !Number.isInteger(valuesToSave.number_maximum) || valuesToSave.number_minimum >= valuesToSave.number_maximum) {
        status.className = "spy-config-status is-error";
        status.textContent = "Use 2–50 players and a valid number range.";
        return;
      }
      save.disabled = true;
      status.className = "spy-config-status is-loading";
      status.textContent = "Saving…";
      api.beginLoading("Saving Guess the Number settings...", false);
      try {
        const result = await api.requestJson(`/api/guilds/${encodeURIComponent(guildId)}/games/guess-number/config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...valuesToSave, language: language.value, enabled: values.enabled !== false }),
        });
        const saved = result && result.config;
        if (saved) {
          values.minimumPlayers = saved.minimumPlayers ?? valuesToSave.minimum_players;
          values.maximumPlayers = saved.maximumPlayers ?? valuesToSave.maximum_players;
          values.numberMinimum = saved.numberMinimum ?? valuesToSave.number_minimum;
          values.numberMaximum = saved.numberMaximum ?? valuesToSave.number_maximum;
          values.language = saved.language === "ar" ? "ar" : "en";
          Object.entries({ minimum_players: values.minimumPlayers, maximum_players: values.maximumPlayers, number_minimum: values.numberMinimum, number_maximum: values.numberMaximum }).forEach(([key, value]) => { inputs[key].value = String(value); });
          language.value = values.language;
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

  function renderDetails(game) {
    if (!root) return;
    root.replaceChildren();
    const wrapper = node("div", "games-view spy-game-detail");
    const top = node("div", "spy-game-detail-top");
    const back = node("button", "secondary-button games-back-button", "← Games");
    back.type = "button";
    back.addEventListener("click", () => {
      if (window.BirdBotGames?.mount && catalogOptions) window.BirdBotGames.mount(catalogOptions);
    });
    top.append(back);
    const hero = node("div", "spy-game-hero");
    hero.style.setProperty("--spy-banner", `url("${game.bannerPath || DEFAULT_GAME.bannerPath}")`);
    hero.append(image(game.iconPath || DEFAULT_GAME.iconPath, "spy-game-icon", "Guess the Number icon"));
    const heroCopy = node("div", "spy-game-hero-copy");
    heroCopy.append(node("h3", "", game.name || DEFAULT_GAME.name), node("p", "", "A calm, turn-based number hunt with clear clues and a sudden-death tie round."));
    hero.append(heroCopy);

    const launch = node("aside", "spy-game-launch-card");
    const launchHeader = node("div", "spy-game-launch-header");
    launchHeader.append(node("span", "games-kicker", "DISCORD LOBBY"));
    const start = node("button", "primary-button spy-start-button", "Start Guess the Number");
    start.type = "button";
    const status = node("span", "spy-lobby-status", "");
    launchHeader.append(start, status);
    launch.append(launchHeader, node("strong", "", "Create a Guess the Number lobby"), node("p", "", "Pick a text channel and post a ready-to-join lobby. Players use the Guess number button for every round—no command needed."));
    const picker = node("div", "spy-channel-picker");
    const label = node("label", "spy-channel-label", "Lobby channel");
    const select = document.createElement("select");
    select.className = "spy-channel-select";
    select.setAttribute("aria-label", "Guess the Number lobby channel");
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
    if (game.enabled === false) {
      start.disabled = true;
      status.className = "spy-lobby-status is-error";
      status.textContent = "Guess the Number is disabled. Enable it in Games first.";
    }
    start.addEventListener("click", () => { picker.hidden = false; start.hidden = true; select.focus(); });
    cancel.addEventListener("click", () => { picker.hidden = true; start.hidden = false; });
    post.addEventListener("click", async () => {
      if (!select.value || post.disabled || !api) return;
      post.disabled = true;
      cancel.disabled = true;
      status.className = "spy-lobby-status is-loading";
      status.textContent = "Posting lobby…";
      api.beginLoading("Creating Guess the Number lobby...", false);
      try {
        const queued = await api.requestJson(`/api/guilds/${encodeURIComponent(guildId)}/games/guess-number/start`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel_id: select.value }),
        });
        await waitForRequest(queued.request_id);
        picker.hidden = true;
        start.hidden = false;
        status.className = "spy-lobby-status is-success";
        status.textContent = "Lobby created in Discord!";
      } catch (error) {
        status.className = "spy-lobby-status is-error";
        status.textContent = error instanceof Error ? error.message : "The lobby could not be created.";
      } finally {
        post.disabled = false;
        cancel.disabled = false;
        api.endLoading(false);
      }
    });

    const rules = node("section", "spy-rules-grid");
    rules.append(
      node("article", "spy-rule-card", node("span", "spy-rule-language", "ENGLISH")),
      node("article", "spy-rule-card spy-rule-card-ar", node("span", "spy-rule-language", "العربية")),
    );
    rules.children[0].append(node("h4", "", "How it works"), node("p", "", "Everyone joins the lobby. Each active player submits one guess per round. If nobody is correct, the bot reveals a higher, lower, or between hint. A single correct guess wins; a tie removes everyone else and starts a fresh Round 0 with a new number."));
    rules.children[1].dir = "rtl";
    rules.children[1].append(node("h4", "", "طريقة اللعب"), node("p", "", "ينضم الجميع إلى الرَدْهة. يرسل كل لاعب تخمينًا واحدًا في كل جولة. إذا لم يجب أحد بشكل صحيح، يعرض البوت تلميحًا: أكبر أو أصغر أو بين رقمين. يفوز صاحب التخمين الصحيح الوحيد؛ وعند التعادل يستمر المتعادلون فقط وتبدأ جولة 0 برقم جديد."));
    wrapper.append(top, hero, launch, rules, renderConfig(game));
    root.append(wrapper);
  }

  async function open(options) {
    unmount();
    if (!options || !options.root || !options.guildId || !options.api) return;
    root = options.root;
    guildId = options.guildId;
    api = options.api;
    channels = Array.isArray(options.channels) ? options.channels : [];
    catalogOptions = options.catalogOptions || null;
    const currentApi = api;
    const currentRoot = root;
    root.replaceChildren(node("div", "games-loading", "Loading Guess the Number details…"));
    currentApi.beginLoading("Loading Guess the Number details...", false);
    try {
      const result = await currentApi.requestJson(`/api/guilds/${encodeURIComponent(guildId)}/games/guess-number`, { cache: "no-store" });
      if (root === currentRoot) renderDetails(result.game || DEFAULT_GAME);
    } catch (error) {
      if (root === currentRoot) currentRoot.replaceChildren(node("p", "form-error", error instanceof Error ? error.message : "Game details could not be loaded."));
    } finally {
      currentApi.endLoading(false);
    }
  }

  function unmount() {
    if (root) root.replaceChildren();
    root = null;
    api = null;
    guildId = null;
    channels = [];
    catalogOptions = null;
  }

  window.BirdBotGuessNumber = { open, unmount };
}());
