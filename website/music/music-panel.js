/* BirdBot Music system dashboard.  Spotify tokens stay on the server; this
 * module only receives public playlist/track metadata and lightweight player
 * state. */
(function () {
  let cleanupPrevious = null;

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = value;
    return node;
  }

  // Icons8 artwork keeps the controls crisp without relying on platform
  // emoji fonts (which render differently on Windows, macOS and mobile).
  // The shuffle artwork is sourced from the Icons8 shuffle collection.
  const ICON_URLS = {
    play: "https://img.icons8.com/ios/50/ffffff/play--v1.png",
    pause: "https://img.icons8.com/ios/50/ffffff/pause--v1.png",
    previous: "https://img.icons8.com/ios/50/ffffff/skip-to-start.png",
    next: "https://img.icons8.com/ios/50/ffffff/skip-to-end.png",
    stop: "https://img.icons8.com/ios/50/ffffff/stop.png",
    rewind: "https://img.icons8.com/ios/50/ffffff/rewind.png",
    fastForward: "https://img.icons8.com/ios/50/ffffff/fast-forward.png",
    volumeDown: "https://img.icons8.com/ios/50/ffffff/low-volume.png",
    volumeUp: "https://img.icons8.com/ios/50/ffffff/high-volume.png",
    shuffle: "https://img.icons8.com/ios/24/ffffff/shuffle.png",
    repeat: "https://img.icons8.com/ios/50/ffffff/repeat.png",
  };

  function trackKey(track) {
    return String(track && (track.id || track.source_url || `${track.artist || ""}\u0000${track.name || ""}`));
  }

  function imageOrFallback(item, className, label) {
    if (item && item.image_url) {
      const image = document.createElement("img");
      image.className = className;
      image.src = item.image_url;
      image.alt = "";
      image.loading = "lazy";
      image.addEventListener("error", () => image.replaceWith(element("span", `${className} music-art-fallback`, (label || "?").trim().charAt(0).toUpperCase() || "?")));
      return image;
    }
    return element("span", `${className} music-art-fallback`, (label || "?").trim().charAt(0).toUpperCase() || "?");
  }

  function formatTime(value) {
    const seconds = Math.max(0, Math.floor(Number(value) || 0));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function mountMusicPanel({ root, guildId, requestJson, beginLoading, endLoading, initialOverview = null }) {
    if (cleanupPrevious) cleanupPrevious();
    let disposed = false;
    let overview = null;
    let state = null;
    let pollTimer = null;
    let searchTimer = null;
    let requestBusy = false;
    let statusNode = null;
    let playerNode = null;
    let startButton = null;
    let leaveButton = null;
    let musicDisabled = false;
    let queueNode = null;
    let queueSignature = "";
    let activePlaylistCard = null;
    let trackResultsNode = null;
    let playlistTracksNode = null;
    let stateRequestBusy = false;
    let lastErrorShown = null;
    let stateFailureCount = 0;
    let searchSequence = 0;
    let stateSocket = null;
    let socketRetryTimer = null;
    let socketGeneration = 0;
    const base = `/api/guilds/${encodeURIComponent(guildId)}/music`;

    function setStatus(message, type) {
      if (!statusNode) return;
      statusNode.hidden = !message;
      statusNode.textContent = message || "";
      statusNode.className = `music-status${type ? ` music-status-${type}` : ""}`;
    }

    async function waitForRequest(requestId) {
      if (!requestId) throw new Error("BirdBot did not return a valid music request. Please try again.");
      for (let attempt = 0; attempt < 160; attempt += 1) {
        if (disposed) throw new Error("Music panel was closed.");
        const response = await requestJson(`/api/command-requests/${encodeURIComponent(requestId)}`, { cache: "no-store" });
        if (response.status === "complete") return response;
        if (response.status === "failed") throw new Error(response.error || "BirdBot could not complete that music action.");
        await new Promise((resolve) => window.setTimeout(resolve, 150));
      }
      throw new Error("That music action is taking too long. Check the player shortly.");
    }

    async function runAction(action, payload, button, label) {
      if (requestBusy || disposed) return;
      requestBusy = true;
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
      }
      // Fast controls (seek, volume, shuffle, loop) only disable the player
      // action itself. Longer operations still use the global loading lock.
      const blocking = ["start", "play", "queue", "playlist", "skip", "previous", "pause", "resume", "stop"].includes(action);
      beginLoading(label || "Updating music player...", blocking);
      setStatus(label || "Updating music player...", "pending");
      try {
        const queued = await requestJson(`${base}/${action}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload || {}),
        });
        if (!queued || typeof queued.request_id !== "string" || !queued.request_id) {
          throw new Error("BirdBot did not accept that music action. Please try again.");
        }
        await waitForRequest(queued.request_id);
        if (action === "start") {
          // The bot request completed successfully, so prevent a duplicate
          // Start click immediately even if the next websocket frame has not
          // arrived yet.
          state = { ...(state || {}), connected: true, connection_state: "ready", last_error: null };
          syncStartButton();
        }
        setStatus("Music player updated.", "success");
        await refreshState();
      } catch (error) {
        setStatus(error instanceof Error ? error.message : "The music action failed.", "error");
      } finally {
        endLoading(blocking);
        requestBusy = false;
        if (button) {
          button.removeAttribute("aria-busy");
          if (action === "start") {
            // State (HTTP fallback or websocket) decides whether Start stays
            // disabled as Connected / Ready or becomes available after an
            // explicit stop/idle disconnect.
            syncStartButton();
          } else {
            button.disabled = false;
          }
        }
      }
    }

    function makeActionButton(label, action, payload, className, iconName) {
      const button = element("button", className || "secondary-button");
      button.type = "button";
      if (iconName && ICON_URLS[iconName]) {
        const icon = document.createElement("img");
        icon.className = "music-button-icon";
        icon.src = ICON_URLS[iconName];
        icon.alt = "";
        icon.loading = "lazy";
        icon.addEventListener("error", () => icon.remove());
        button.append(icon, element("span", "music-button-label", label));
      } else {
        button.textContent = label;
      }
      button.title = label;
      button.addEventListener("click", () => runAction(action, payload, button, `${label}...`));
      return button;
    }

    function renderTrackRow(track, compact) {
      const row = element("article", compact ? "music-track-row music-track-row-compact" : "music-track-row");
      row.dataset.musicTrackRow = "true";
      row.dataset.trackId = trackKey(track);
      row.append(imageOrFallback(track, "music-track-art", track.name));
      const details = element("div", "music-track-details");
      details.append(element("strong", "music-track-name", track.name), element("span", "music-track-meta", `${track.artist}${track.album ? ` · ${track.album}` : ""}`));
      row.append(details);
      const actions = element("div", "music-track-actions");
      const play = makeActionButton("Play", "play", { track_id: track.id }, "music-small-button music-track-play-button", "play");
      const queue = makeActionButton("Queue", "queue", { track_id: track.id }, "music-small-button music-queue-button music-track-queue-button", "next");
      play.dataset.queueTrackAction = "play";
      queue.dataset.queueTrackAction = "queue";
      play.dataset.trackId = row.dataset.trackId;
      queue.dataset.trackId = row.dataset.trackId;
      const queuedNote = element("span", "music-queued-note", "Already in Queue");
      queuedNote.hidden = true;
      actions.append(play, queue, queuedNote);
      row.append(actions);
      return row;
    }

    function syncQueueControls() {
      const queued = new Set((state && Array.isArray(state.queue) ? state.queue : []).map(trackKey));
      root.querySelectorAll("[data-music-track-row]").forEach((row) => {
        const alreadyQueued = queued.has(row.dataset.trackId);
        row.querySelectorAll("[data-queue-track-action]").forEach((button) => {
          button.disabled = alreadyQueued;
          button.setAttribute("aria-disabled", String(alreadyQueued));
        });
        const note = row.querySelector(".music-queued-note");
        if (note) note.hidden = !alreadyQueued;
      });
    }

    function renderTrackList(target, tracks, emptyText) {
      target.replaceChildren();
      if (!tracks || !tracks.length) {
        target.append(element("p", "music-empty", emptyText || "No tracks found."));
        return;
      }
      tracks.forEach((track) => target.append(renderTrackRow(track, false)));
      syncQueueControls();
    }

    function renderQueue() {
      if (!queueNode) return;
      const queue = state && Array.isArray(state.queue) ? state.queue : [];
      const signature = `${queue.map(trackKey).join("|")}::${state && state.queue_finished ? "finished" : "active"}`;
      if (queueSignature === signature) return;
      queueSignature = signature;
      queueNode.replaceChildren();
      const heading = element("div", "music-queue-heading");
      heading.append(element("h4", "music-subheading", "Up next"), element("span", "music-queue-count", `${queue.length} ${queue.length === 1 ? "track" : "tracks"}`));
      queueNode.append(heading);
      if (!queue.length) {
        queueNode.append(state && state.queue_finished
          ? element("span", "music-queue-finished-badge", "Queue Finished")
          : element("p", "music-empty", "Your queue is empty. Add a track to get started."));
        return;
      }
      const list = element("ol", "music-queue-list");
      queue.forEach((track, index) => {
        const item = element("li", "music-queue-item");
        item.append(element("span", "music-queue-index", String(index + 1)), imageOrFallback(track, "music-queue-art", track.name));
        const details = element("div", "music-queue-details");
        details.append(element("strong", "music-track-name", track.name || "Unknown track"), element("span", "music-track-meta", track.artist || "Unknown artist"));
        item.append(details, element("span", "music-queue-duration", formatTime((Number(track.duration_ms) || 0) / 1000)));
        list.append(item);
      });
      queueNode.append(list);
    }

    function syncStartButton() {
      if (!startButton) return;
      const connected = Boolean(state && state.connected);
      const recovering = Boolean(state && (
        state.connection_state === "reconnecting" ||
        (!connected && (state.current || (Array.isArray(state.queue) && state.queue.length)) && /disconnect|reconnect|interrupted/i.test(String(state.last_error || "")))
      ));
      const ready = connected && !recovering;
      if (musicDisabled) {
        startButton.disabled = true;
        startButton.className = "secondary-button music-start-button";
        startButton.textContent = "Start";
        startButton.title = "Music is disabled for this server";
        if (leaveButton) leaveButton.hidden = true;
        return;
      }
      startButton.disabled = ready || recovering;
      startButton.className = ready
        ? "secondary-button music-start-button music-start-ready"
        : "primary-button music-start-button";
      startButton.textContent = ready ? "Connected / Ready" : recovering ? "Reconnecting..." : "Start";
      startButton.title = ready
        ? "BirdBot is connected and ready"
        : recovering
          ? "BirdBot is recovering the voice connection"
          : "Join your current voice channel";
      startButton.setAttribute("aria-busy", String(recovering));
      if (leaveButton) {
        const hasSession = connected || recovering || Boolean(state && (state.current || (Array.isArray(state.queue) && state.queue.length)));
        leaveButton.hidden = !hasSession;
      }
    }

    function updatePlayer() {
      syncStartButton();
      if (!playerNode) return;
      const current = state && state.current;
      const currentKey = current ? trackKey(current) : "__empty__";
      if (playerNode.dataset.trackKey === currentKey && current) {
        // State polling happens frequently; update only the changing values
        // instead of rebuilding the whole player and interrupting slider
        // interaction/focus on every poll.
        const progress = playerNode.querySelector(".music-progress");
        const duration = Math.max(1, Number(state.duration) || Number(current.duration_ms || 0) / 1000 || 1);
        if (progress) {
          progress.max = String(duration);
          if (document.activeElement !== progress) progress.value = String(Math.min(duration, Number(state.position) || 0));
        }
        const times = playerNode.querySelectorAll(".music-progress-times > span");
        if (times.length >= 2) {
          times[0].textContent = formatTime(state.position);
          times[1].textContent = formatTime(state.duration || Number(current.duration_ms || 0) / 1000);
        }
        const connection = playerNode.querySelector(".music-player-connection");
        if (connection) connection.textContent = state.connection_state === "reconnecting"
          ? "Reconnecting..."
          : state.connected
            ? `Connected${state.voice_channel_name ? ` · ${state.voice_channel_name}` : ""}`
            : "Not connected";
        const toggle = playerNode.querySelector("[data-music-toggle]");
        const toggleAction = state.paused ? "resume" : "pause";
        if (toggle && toggle.dataset.musicAction !== toggleAction) {
          const replacement = makeActionButton(state.paused ? "Play" : "Pause", toggleAction, {}, "primary-button", state.paused ? "play" : "pause");
          replacement.dataset.musicToggle = "true";
          replacement.dataset.musicAction = toggleAction;
          toggle.replaceWith(replacement);
        }
        const shuffle = playerNode.querySelector(".music-shuffle-button");
        if (shuffle) {
          shuffle.className = state.shuffle_enabled ? "primary-button music-shuffle-button music-toggle-active" : "secondary-button music-shuffle-button";
          shuffle.setAttribute("aria-pressed", String(Boolean(state.shuffle_enabled)));
        }
        const loop = playerNode.querySelector(".music-loop-button");
        if (loop) {
          loop.className = state.loop_enabled ? "primary-button music-loop-button music-toggle-active" : "secondary-button music-loop-button";
          loop.setAttribute("aria-pressed", String(Boolean(state.loop_enabled)));
        }
        const volumeLabel = playerNode.querySelector(".music-volume-label");
        if (volumeLabel) volumeLabel.textContent = `Volume ${Math.round((Number(state.volume) || 0) * 100)}%`;
        const volumeSlider = playerNode.querySelector(".music-volume-slider");
        if (volumeSlider && document.activeElement !== volumeSlider) volumeSlider.value = String(Math.round((Number(state.volume) || 0) * 100));
        renderQueue();
        syncQueueControls();
        return;
      }
      playerNode.dataset.trackKey = currentKey;
      playerNode.replaceChildren();
      if (!current) {
        playerNode.append(state && state.queue_finished
          ? element("span", "music-queue-finished-badge", "Queue Finished")
          : element("p", "music-empty", "Nothing is playing yet. Start the bot, then choose a track."));
        renderQueue();
        syncQueueControls();
        return;
      }
      const card = element("div", "music-player-card");
      const art = imageOrFallback(current, "music-player-art", current.name);
      const info = element("div", "music-player-info");
      info.append(element("strong", "music-now-playing", current.name), element("span", "music-track-meta", current.artist));
      const connected = state && state.connection_state === "reconnecting"
        ? "Reconnecting..."
        : state && state.connected
          ? `Connected${state.voice_channel_name ? ` · ${state.voice_channel_name}` : ""}`
          : "Not connected";
      info.append(element("span", "music-player-connection", connected));
      card.append(art, info);
      const progress = element("input", "music-progress");
      progress.type = "range";
      progress.min = "0";
      progress.max = String(Math.max(1, Number(state.duration) || Number(current.duration_ms || 0) / 1000 || 1));
      progress.value = String(Math.min(Number(progress.max), Number(state.position) || 0));
      progress.setAttribute("aria-label", "Track progress");
      progress.addEventListener("change", () => {
        const desired = Number(progress.value) || 0;
        const currentPosition = Number(state.position) || 0;
        runAction("seek", { seconds: desired - currentPosition }, null, "Seeking...");
      });
      const times = element("div", "music-progress-times");
      times.append(element("span", "", formatTime(state.position)), element("span", "", formatTime(state.duration || Number(current.duration_ms || 0) / 1000)));
      const controls = element("div", "music-player-controls");
      controls.append(makeActionButton("Previous", "previous", {}, "secondary-button", "previous"));
      const toggle = makeActionButton(state.paused ? "Play" : "Pause", state.paused ? "resume" : "pause", {}, "primary-button", state.paused ? "play" : "pause");
      toggle.dataset.musicToggle = "true";
      toggle.dataset.musicAction = state.paused ? "resume" : "pause";
      controls.append(toggle);
      controls.append(makeActionButton("Skip", "skip", {}, "secondary-button", "next"));
      controls.append(makeActionButton("Stop", "stop", {}, "danger-button", "stop"));
      const shuffle = makeActionButton("Shuffle", "shuffle", {}, state.shuffle_enabled ? "primary-button music-shuffle-button music-toggle-active" : "secondary-button music-shuffle-button", "shuffle");
      shuffle.setAttribute("aria-pressed", String(Boolean(state.shuffle_enabled)));
      controls.append(shuffle);
      const loop = makeActionButton("Loop", "loop", {}, state.loop_enabled ? "primary-button music-loop-button music-toggle-active" : "secondary-button music-loop-button", "repeat");
      loop.setAttribute("aria-pressed", String(Boolean(state.loop_enabled)));
      controls.append(loop);
      controls.append(makeActionButton("-10s", "seek", { seconds: -10 }, "music-small-button", "rewind"));
      controls.append(makeActionButton("+10s", "seek", { seconds: 10 }, "music-small-button", "fastForward"));
      const volume = element("div", "music-volume");
      volume.append(element("span", "music-volume-label", `Volume ${Math.round((Number(state.volume) || 0) * 100)}%`));
      const volumeSlider = element("input", "music-volume-slider");
      volumeSlider.type = "range"; volumeSlider.min = "0"; volumeSlider.max = "100"; volumeSlider.value = String(Math.round((Number(state.volume) || 0) * 100));
      volumeSlider.setAttribute("aria-label", "Volume");
      volumeSlider.addEventListener("change", () => runAction("volume", { volume: Number(volumeSlider.value) }, null, "Changing volume..."));
      volume.append(
        volumeSlider,
        makeActionButton("Volume down", "volume_down", {}, "music-small-button", "volumeDown"),
        makeActionButton("Volume up", "volume_up", {}, "music-small-button", "volumeUp"),
      );
      card.append(progress, times, controls, volume);
      playerNode.append(card);
      renderQueue();
      syncQueueControls();
    }

    async function refreshState() {
      // A connected websocket is authoritative and already carries the same
      // state published by the bot worker. Keep HTTP polling only as a
      // fallback for older deployments/proxies that do not support upgrades.
      if (disposed || document.hidden || stateRequestBusy || (stateSocket && stateSocket.readyState === window.WebSocket.OPEN)) return;
      stateRequestBusy = true;
      try {
        const response = await requestJson(`${base}/state`, { cache: "no-store" });
        if (!response || typeof response !== "object" || (response.state != null && typeof response.state !== "object")) {
          throw new Error("The music player returned an invalid state.");
        }
        const recovered = stateFailureCount >= 3;
        stateFailureCount = 0;
        state = response.state || state || {};
        updatePlayer();
        if (state.last_error && state.last_error !== lastErrorShown) {
          lastErrorShown = state.last_error;
          setStatus(state.last_error, "error");
        } else if (!state.last_error) {
          lastErrorShown = null;
          if (recovered) setStatus("Live player state restored.", "success");
        }
      } catch (_) {
        // Do not replace the whole player for one failed poll. After a few
        // consecutive failures, show a recoverable warning to the user.
        stateFailureCount += 1;
        if (stateFailureCount >= 3) setStatus("Live player state is temporarily unavailable. Retrying...", "error");
      } finally {
        stateRequestBusy = false;
      }
    }

    function applySocketState(message) {
      if (!message || message.type !== "music_state" || !message.state || typeof message.state !== "object") return;
      stateFailureCount = 0;
      state = message.state;
      updatePlayer();
      if (state.last_error && state.last_error !== lastErrorShown) {
        lastErrorShown = state.last_error;
        setStatus(state.last_error, "error");
      } else if (!state.last_error) {
        lastErrorShown = null;
      }
    }

    function startFallbackPolling() {
      if (disposed || pollTimer != null) return;
      // WebSocket is the normal live path.  Use a slower fallback poll when a
      // reverse proxy blocks upgrades so the player stays responsive without
      // generating a request every second.
      pollTimer = window.setInterval(refreshState, 2_000);
    }

    function connectStateSocket() {
      if (disposed || !window.WebSocket) {
        startFallbackPolling();
        return;
      }
      const generation = ++socketGeneration;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${protocol}//${window.location.host}/ws/guilds/${encodeURIComponent(guildId)}/music`;
      let socket;
      try {
        socket = new WebSocket(url);
      } catch (_) {
        startFallbackPolling();
        return;
      }
      stateSocket = socket;
      socket.addEventListener("open", () => {
        if (disposed || generation !== socketGeneration) return;
        window.clearInterval(pollTimer);
        pollTimer = null;
        setStatus("Live player connected.", "success");
      });
      socket.addEventListener("message", (event) => {
        if (disposed || generation !== socketGeneration) return;
        try { applySocketState(JSON.parse(event.data)); } catch (_) { /* ignore malformed frames */ }
      });
      socket.addEventListener("error", () => {
        if (!disposed && generation === socketGeneration) startFallbackPolling();
      });
      socket.addEventListener("close", (event) => {
        if (disposed || generation !== socketGeneration) return;
        stateSocket = null;
        startFallbackPolling();
        if ([4401, 4403].includes(event.code)) return;
        window.clearTimeout(socketRetryTimer);
        socketRetryTimer = window.setTimeout(() => connectStateSocket(), 2_000);
      });
    }

    async function searchTracks(query) {
      if (!trackResultsNode || disposed) return;
      const sequence = ++searchSequence;
      if (query.trim().length < 2) {
        trackResultsNode.replaceChildren();
        return;
      }
      trackResultsNode.replaceChildren(element("p", "music-loading", "Searching Spotify..."));
      try {
        const result = await requestJson(`${base}/search?q=${encodeURIComponent(query.trim())}`, { cache: "no-store" });
        if (sequence !== searchSequence || disposed) return;
        renderTrackList(trackResultsNode, result.tracks || [], "No matching Spotify tracks found.");
      } catch (error) {
        if (sequence !== searchSequence || disposed) return;
        trackResultsNode.replaceChildren(element("p", "music-empty", error instanceof Error ? error.message : "Spotify search failed."));
      }
    }

    async function openPlaylist(playlist) {
      if (!playlistTracksNode) return;
      playlistTracksNode.replaceChildren(element("p", "music-loading", "Loading playlist tracks..."));
      try {
        const result = await requestJson(`${base}/playlists/${encodeURIComponent(playlist.id)}/tracks`, { cache: "no-store" });
        playlistTracksNode.replaceChildren(element("h4", "music-subheading", playlist.name));
        const list = element("div", "music-track-list");
        renderTrackList(list, result.tracks || [], "This playlist has no playable tracks.");
        playlistTracksNode.append(list);
      } catch (error) {
        playlistTracksNode.replaceChildren(element("p", "music-empty", error instanceof Error ? error.message : "Playlist could not be loaded."));
      }
    }

    function makeExternalSourceSection() {
      const section = element("section", "music-section music-source-section");
      section.append(
        element("h4", "music-subheading", "Play from a link"),
        element("p", "music-copy", "Paste a YouTube or another public audio link. It will use the same BirdBot player controls as Spotify."),
      );
      const form = element("form", "music-source-form");
      const input = element("input", "music-source-input");
      input.type = "url";
      input.placeholder = "https://youtube.com/... or a direct audio URL";
      input.maxLength = 2048;
      input.autocomplete = "off";
      input.required = true;
      input.setAttribute("aria-label", "Audio URL");
      const actions = element("div", "music-source-actions");
      const play = element("button", "primary-button", "Play link");
      play.type = "button";
      const queue = element("button", "secondary-button", "Add to queue");
      queue.type = "button";
      const submit = (action, button, label) => {
        const url = input.value.trim();
        if (!url) {
          setStatus("Paste a YouTube or audio URL first.", "error");
          input.focus();
          return;
        }
        runAction(action, { url }, button, label);
      };
      play.addEventListener("click", () => submit("play", play, "Playing link..."));
      queue.addEventListener("click", () => submit("queue", queue, "Adding link..."));
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        submit("play", play, "Playing link...");
      });
      actions.append(play, queue);
      form.append(input, actions);
      section.append(form);
      return section;
    }

    function renderPlayerControls(panel) {
      const start = makeActionButton("Start", "start", {}, "primary-button music-start-button");
      startButton = start;
      if (overview.guild && overview.guild.activated === false) {
        musicDisabled = true;
        panel.append(element("p", "music-status-error", "Music is disabled for this server. Ask an owner or Administrator to enable BirdBot."));
      }
      const sessionActions = element("div", "music-session-actions");
      sessionActions.append(start);
      const leave = makeActionButton("Leave voice", "stop", {}, "danger-button music-leave-button", "stop");
      leaveButton = leave;
      leave.hidden = true;
      sessionActions.append(leave);
      panel.append(sessionActions);
      playerNode = element("div", "music-player");
      queueNode = element("section", "music-queue-section");
      queueSignature = "";
      panel.append(playerNode, queueNode);
      state = overview.state || {};
      updatePlayer();
    }

    function renderOverview() {
      root.replaceChildren();
      startButton = null;
      leaveButton = null;
      musicDisabled = false;
      const panel = element("section", "music-panel");
      const header = element("div", "music-header");
      const heading = element("div");
      heading.append(element("p", "music-kicker", "MUSIC SYSTEM"), element("h3", "music-title", "Your music, in voice"), element("p", "music-copy", "Paste a public audio link or link Spotify, then control BirdBot's player without leaving your server dashboard."));
      const headerActions = element("div", "music-header-actions");
      const spotifyLink = document.createElement("a");
      spotifyLink.className = "spotify-button";
      const spotifyPremiumBlocked = overview.spotify_premium_required === true && overview.spotify_premium === false;
      if (overview.spotify_configured !== false && !spotifyPremiumBlocked) spotifyLink.href = `/music/spotify/login?guild=${encodeURIComponent(guildId)}`;
      else spotifyLink.classList.add("is-disabled");
      spotifyLink.textContent = spotifyPremiumBlocked
        ? "Spotify Premium required"
        : overview.spotify_configured === false
          ? "Spotify setup required"
          : (overview.linked ? "Relink Spotify" : "Link Your Spotify Account");
      spotifyLink.target = "_self";
      if (overview.spotify_configured === false || spotifyPremiumBlocked) spotifyLink.addEventListener("click", (event) => {
        event.preventDefault();
        setStatus(
          spotifyPremiumBlocked
            ? "Spotify linking is a premium feature. Paste a YouTube or audio URL to play without Spotify."
            : "Spotify integration is not configured on this server yet.",
          "error",
        );
      });
      headerActions.append(spotifyLink);
      if (overview.linked) {
        const unlink = element("button", "secondary-button music-unlink-button", "Unlink");
        unlink.type = "button";
        unlink.addEventListener("click", async () => {
          if (requestBusy || disposed) return;
          requestBusy = true;
          unlink.disabled = true;
          beginLoading("Unlinking Spotify...");
          setStatus("Unlinking Spotify...", "pending");
          try {
            await requestJson("/api/spotify/unlink", { method: "POST" });
            overview.linked = false;
            overview.playlists = []; overview.albums = []; overview.top_tracks = [];
            window.clearInterval(pollTimer);
            pollTimer = null;
            ++socketGeneration;
            window.clearTimeout(socketRetryTimer);
            if (stateSocket) {
              try { stateSocket.close(1000, "Spotify unlinked"); } catch (_) { /* ignore */ }
            }
            stateSocket = null;
            renderOverview();
          } catch (error) {
            setStatus(error instanceof Error ? error.message : "Spotify could not be unlinked.", "error");
            unlink.disabled = false;
          } finally {
            endLoading();
            requestBusy = false;
          }
        });
        headerActions.append(unlink);
      }
      header.append(heading, headerActions);
      panel.append(header);
      panel.append(element("p", "music-warning", "Warning: The Music system is not working properly right now and is being fixed.\nتحذير: نظام الموسيقى لا يعمل بشكل صحيح حالياً ويجري إصلاحه."));
      statusNode = element("p", "music-status"); statusNode.hidden = true; panel.append(statusNode);
      if (!overview.linked) {
        const empty = element("div", "music-link-card");
        empty.append(
          element("strong", "", spotifyPremiumBlocked ? "Spotify Premium" : "Connect your Spotify account"),
          element("p", "music-copy", spotifyPremiumBlocked
            ? "Spotify linking is reserved for premium members. You can still paste a YouTube or public audio link below."
            : "Your access is stored securely by BirdBot. Playlists and track metadata are only shown after you approve the Spotify link."),
        );
        empty.append(spotifyLink.cloneNode(true));
        panel.append(empty);
        renderPlayerControls(panel);
        panel.append(makeExternalSourceSection());
        root.append(panel);
        return;
      }
      const account = element("p", "music-account", `Linked as ${overview.spotify?.name || "Spotify user"}`);
      panel.append(account);
      renderPlayerControls(panel);
      panel.append(makeExternalSourceSection());

      const search = element("input", "music-search");
      search.type = "search"; search.placeholder = "Search your Spotify tracks..."; search.autocomplete = "off";
      search.setAttribute("aria-label", "Search Spotify tracks");
      trackResultsNode = element("div", "music-track-list");
      search.addEventListener("input", () => { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(() => searchTracks(search.value), 300); });
      const searchSection = element("section", "music-section");
      searchSection.append(element("h4", "music-subheading", "Search"), search, trackResultsNode); panel.append(searchSection);

      const playlistSection = element("section", "music-section");
      playlistSection.append(element("h4", "music-subheading", "Your playlists"));
      const playlistGrid = element("div", "music-playlist-rail");
      (overview.playlists || []).forEach((playlist) => {
        const card = element("article", "music-card music-playlist-card");
        card.append(imageOrFallback(playlist, "music-card-art", playlist.name));
        const details = element("div", "music-playlist-details");
        details.append(element("strong", "", playlist.name), element("span", "music-card-meta", `${playlist.track_count || 0} tracks`));
        card.append(details);
        const actions = element("div", "music-playlist-actions");
        const view = element("button", "music-small-button", "View tracks");
        view.type = "button";
        view.addEventListener("click", (event) => { event.stopPropagation(); openPlaylist(playlist); });
        const playAll = makeActionButton("Play all", "playlist", { playlist_id: playlist.id }, "music-playlist-play-button", "play");
        playAll.addEventListener("click", (event) => { event.stopPropagation(); });
        actions.append(view, playAll);
        card.append(actions);
        card.addEventListener("click", () => {
          if (activePlaylistCard) activePlaylistCard.classList.remove("is-selected");
          activePlaylistCard = card;
          card.classList.add("is-selected");
          openPlaylist(playlist);
        });
        playlistGrid.append(card);
      });
      if (!playlistGrid.children.length) playlistGrid.append(element("p", "music-empty", "No Spotify playlists were found."));
      playlistSection.append(playlistGrid); panel.append(playlistSection);
      playlistTracksNode = element("div", "music-playlist-tracks"); panel.append(playlistTracksNode);

      const albumSection = element("section", "music-section"); albumSection.append(element("h4", "music-subheading", "Saved albums"));
      const albumGrid = element("div", "music-grid");
      (overview.albums || []).forEach((album) => { const card = element("div", "music-card music-card-static"); card.append(imageOrFallback(album, "music-card-art", album.name), element("strong", "", album.name), element("span", "music-card-meta", album.artist || "Spotify album")); albumGrid.append(card); });
      if (!albumGrid.children.length) albumGrid.append(element("p", "music-empty", "No saved albums were found.")); albumSection.append(albumGrid); panel.append(albumSection);

      const topSection = element("section", "music-section"); topSection.append(element("h4", "music-subheading", "Top tracks"));
      const topList = element("div", "music-track-list"); renderTrackList(topList, overview.top_tracks || [], "No top tracks were found yet."); topSection.append(topList); panel.append(topSection);
      root.append(panel);
    }

    async function load() {
      beginLoading("Loading Music system...");
      try {
        overview = initialOverview || await requestJson(base, { cache: "no-store" });
        if (!overview || typeof overview !== "object") throw new Error("Music data could not be loaded.");
        renderOverview();
        if (Array.isArray(overview.warnings) && overview.warnings.length) {
          setStatus(overview.warnings.join(" "), "error");
        }
        // Player state is independent of Spotify linking: URL playback must
        // stay live for members who do not use Spotify.
        await refreshState();
        connectStateSocket();
        startFallbackPolling();
      } catch (error) {
        root.replaceChildren(element("p", "music-empty", error instanceof Error ? error.message : "Music system could not be loaded."));
      } finally { endLoading(); }
    }

    cleanupPrevious = () => {
      disposed = true;
      ++socketGeneration;
      window.clearInterval(pollTimer);
      window.clearTimeout(searchTimer);
      window.clearTimeout(socketRetryTimer);
      if (stateSocket) {
        try { stateSocket.close(1000, "panel closed"); } catch (_) { /* ignore */ }
      }
      stateSocket = null;
      cleanupPrevious = null;
    };
    if (initialOverview) {
      // The application readiness gate has already fetched this overview.
      // Render it synchronously so the gate never reveals an empty/half-built
      // music panel while a second request is in flight.
      overview = initialOverview;
      renderOverview();
      if (Array.isArray(overview.warnings) && overview.warnings.length) {
        setStatus(overview.warnings.join(" "), "error");
      }
      void refreshState().finally(() => {
        connectStateSocket();
        startFallbackPolling();
      });
    } else {
      void load();
    }
  }

  window.BirdBotMusic = {
    mount: mountMusicPanel,
    unmount: () => { if (cleanupPrevious) cleanupPrevious(); },
  };
}());
