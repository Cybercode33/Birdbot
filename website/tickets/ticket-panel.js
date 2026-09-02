(function () {
  "use strict";

  const MAX_OPTIONS = 25;
  const BUTTON_STYLES = [
    { value: "primary", label: "Blue (Primary)" },
    { value: "success", label: "Green (Success)" },
    { value: "danger", label: "Red (Danger)" },
    { value: "secondary", label: "Gray (Secondary)" },
  ];

  function defaultButtonStyle(index) {
    return BUTTON_STYLES[index % BUTTON_STYLES.length].value;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function field(labelText, control) {
    const wrapper = element("label", "ticket-field");
    wrapper.append(element("span", "ticket-field-label", labelText), control);
    return wrapper;
  }

  function configSection(kicker, title, copy) {
    const section = element("section", "ticket-config-section");
    const heading = element("div", "ticket-section-heading");
    heading.append(
      element("span", "ticket-section-kicker", kicker),
      element("h4", "ticket-section-title", title),
      element("p", "ticket-section-copy", copy),
    );
    section.append(heading);
    return section;
  }

  function optionTemplate(index = 0) {
    return { label: "", value: "", description: "", emoji: "", button_style: defaultButtonStyle(index) };
  }

  function renderOptions(container, options, onRemove, layout = "select_menu", onChange = () => {}) {
    const buttonsMode = layout === "buttons";
    container.replaceChildren();
    options.forEach((option, index) => {
      const row = element("article", "ticket-option-row");
      const heading = element("div", "ticket-option-heading");
      heading.append(element("strong", "", `Option ${index + 1}`));
      if (options.length > 1) {
        const remove = element("button", "ticket-remove-option", "Remove");
        remove.type = "button";
        remove.addEventListener("click", () => onRemove(index));
        heading.append(remove);
      }
      const controls = element("div", "ticket-option-fields");
      const label = element("input", "ticket-input");
      label.type = "text";
      label.maxLength = 80;
      label.placeholder = buttonsMode ? "Button label" : "General Support";
      label.value = option.label || "";
      label.addEventListener("input", () => { option.label = label.value; onChange(); });
      const value = element("input", "ticket-input");
      value.type = "text";
      value.maxLength = 100;
      value.placeholder = "support_topic";
      value.value = option.value || "";
      value.addEventListener("input", () => { option.value = value.value; onChange(); });
      const description = element("textarea", "ticket-input ticket-description-input");
      description.rows = 2;
      description.maxLength = 100;
      description.placeholder = buttonsMode ? "Optional button help text" : "How can we help? (optional)";
      description.value = option.description || "";
      description.addEventListener("input", () => { option.description = description.value; onChange(); });
      const emoji = element("input", "ticket-input ticket-emoji-input");
      emoji.type = "text";
      emoji.maxLength = 64;
      emoji.placeholder = "Emoji (optional)";
      emoji.value = option.emoji || "";
      emoji.addEventListener("input", () => { option.emoji = emoji.value; onChange(); });
      let buttonColor = null;
      if (buttonsMode) {
        buttonColor = element("select", "ticket-input ticket-button-color");
        BUTTON_STYLES.forEach((style) => buttonColor.append(new Option(style.label, style.value)));
        buttonColor.value = BUTTON_STYLES.some((style) => style.value === option.button_style)
          ? option.button_style
          : defaultButtonStyle(index);
        option.button_style = buttonColor.value;
        buttonColor.setAttribute("aria-label", "Discord button color");
        buttonColor.addEventListener("change", () => {
          option.button_style = buttonColor.value;
          onChange();
        });
      }
      controls.append(
        field(buttonsMode ? "Button label" : "Option label", label),
        field("Value / ID", value),
        field("Description", description),
        field("Emoji", emoji),
      );
      if (buttonColor) controls.append(field("Button color", buttonColor));
      row.append(heading, controls);
      container.append(row);
    });
  }

  async function mount({ root, guildId, requestJson, beginLoading, endLoading }) {
    root.replaceChildren();
    const panel = element("section", "ticket-config-panel");
    const panelHeader = element("div", "ticket-panel-header");
    const panelHeaderCopy = element("div", "ticket-panel-header-copy");
    panelHeaderCopy.append(
      element("div", "ticket-panel-kicker", "Ticket setup"),
      element("h3", "ticket-panel-title", "Configure your ticket panel"),
      element("p", "ticket-panel-copy", "Build a clear support flow, choose where tickets live, and preview the member experience before posting."),
    );
    panelHeader.append(panelHeaderCopy, element("span", "ticket-config-state", "Ready to configure"));
    panel.append(panelHeader);
    const status = element("p", "ticket-status");
    status.setAttribute("role", "status");
    const form = element("form", "ticket-form ticket-config-form");
    form.noValidate = true;
    const setupChannel = element("select", "ticket-input");
    const category = element("select", "ticket-input");
    const priority = element("select", "ticket-input");
    const maxOpenTickets = element("input", "ticket-input");
    maxOpenTickets.type = "number";
    maxOpenTickets.min = "1";
    maxOpenTickets.max = "25";
    maxOpenTickets.step = "1";
    maxOpenTickets.inputMode = "numeric";
    maxOpenTickets.placeholder = "1";
    const logChannel = element("select", "ticket-input");
    const layoutField = element("fieldset", "ticket-layout-field");
    const layoutLegend = element("legend", "ticket-field-label", "Ticket Panel Layout");
    const layoutChoices = element("div", "ticket-layout-choices");
    const buttonsLayoutId = `ticket-layout-buttons-${guildId}`;
    const selectLayoutId = `ticket-layout-select-${guildId}`;
    const buttonsLayout = element("input", "ticket-layout-radio");
    buttonsLayout.type = "radio";
    buttonsLayout.name = `ticket-panel-layout-${guildId}`;
    buttonsLayout.value = "buttons";
    buttonsLayout.id = buttonsLayoutId;
    const buttonsLayoutLabel = element("label", "ticket-layout-choice");
    buttonsLayoutLabel.htmlFor = buttonsLayoutId;
    buttonsLayoutLabel.append(buttonsLayout, element("span", "ticket-layout-choice-copy", "Buttons Mode"));
    const selectLayout = element("input", "ticket-layout-radio");
    selectLayout.type = "radio";
    selectLayout.name = `ticket-panel-layout-${guildId}`;
    selectLayout.value = "select_menu";
    selectLayout.id = selectLayoutId;
    const selectLayoutLabel = element("label", "ticket-layout-choice");
    selectLayoutLabel.htmlFor = selectLayoutId;
    selectLayoutLabel.append(selectLayout, element("span", "ticket-layout-choice-copy", "Select Menu (Dropdown) Mode"));
    layoutChoices.append(buttonsLayoutLabel, selectLayoutLabel);
    layoutField.append(layoutLegend, layoutChoices);
    const layoutHelp = element("p", "ticket-help ticket-layout-help");
    const preview = element("div", "ticket-panel-preview");
    [
      ["low", "Low"],
      ["medium", "Medium"],
      ["high", "High"],
    ].forEach(([value, label]) => priority.append(new Option(label, value)));
    const supportRoles = element("select", "ticket-input ticket-role-select ticket-native-role-select");
    supportRoles.multiple = true;
    supportRoles.hidden = true;
    const rolePicker = element("div", "ticket-role-picker");
    const roleTrigger = element("button", "ticket-role-trigger", "Select support role(s)");
    roleTrigger.type = "button";
    roleTrigger.setAttribute("aria-expanded", "false");
    const roleMenu = element("div", "ticket-role-menu");
    roleMenu.hidden = true;
    rolePicker.append(roleTrigger, roleMenu, supportRoles);
    const requireDescription = element("input", "ticket-checkbox-input");
    requireDescription.type = "checkbox";
    const descriptionPrompt = element("input", "ticket-input");
    descriptionPrompt.type = "text";
    descriptionPrompt.maxLength = 200;
    descriptionPrompt.placeholder = "Please describe your request.";
    const customIcon = element("input", "ticket-input ticket-icon-input");
    customIcon.type = "file";
    customIcon.accept = "image/png,image/jpeg,image/webp,image/gif";
    const removeIcon = element("button", "secondary-button ticket-remove-icon", "Delete Picture");
    removeIcon.type = "button";
    removeIcon.disabled = true;
    const iconControl = element("div", "ticket-icon-control");
    iconControl.append(customIcon, removeIcon);
    const customIconStatus = element("span", "ticket-icon-status", "No custom icon selected. The server icon will be used.");
    const descriptionToggle = element("label", "ticket-checkbox");
    descriptionToggle.append(requireDescription, element("span", "ticket-checkbox-label", "Ask users for a description when opening a ticket"));
    const optionsContainer = element("div", "ticket-options");
    const addOption = element("button", "secondary-button ticket-add-option", "Add ticket option");
    addOption.type = "button";
    const save = element("button", "primary-button ticket-save", "Save ticket settings");
    save.type = "submit";
    const post = element("button", "primary-button ticket-post", "Post");
    post.type = "button";
    const actions = element("div", "ticket-actions");
    actions.append(save, post);
    const basicSection = configSection("01 · Basics", "Where should tickets start?", "Choose the setup channel, ticket category, and the layout members will use to open a ticket.");
    basicSection.append(
      field("Ticket panel channel", setupChannel),
      field("Ticket channel category", category),
      layoutField,
      layoutHelp,
    );
    const behaviorSection = configSection("02 · Behavior", "How should tickets behave?", "Set priority defaults, per-member limits, and the information members provide when opening a ticket.");
    behaviorSection.append(
      field("Priority level", priority),
      field("Maximum open tickets per user", maxOpenTickets),
      element("p", "ticket-help", "Closed tickets do not count toward this limit."),
      descriptionToggle,
      field("Description prompt", descriptionPrompt),
    );
    const supportSection = configSection("03 · Support", "Who should receive ticket alerts?", "Send ticket activity to a dedicated log channel and choose the roles that can help members.");
    supportSection.append(
      field("Ticket logs channel", logChannel),
      field("Support role(s)", rolePicker),
      element("p", "ticket-help ticket-role-help", "Click a role to add or remove it from the support team."),
    );
    const appearanceSection = configSection("04 · Appearance", "Make the panel feel like your server", "Add an optional custom icon. If you leave it empty, the server icon will be used.");
    appearanceSection.append(field("Custom panel icon", iconControl), customIconStatus);
    const optionsSection = configSection("05 · Options", "What can members ask for?", "Add up to 25 support topics. Each option needs a unique value/ID and can include help text and an emoji.");
    const optionsHeading = element("div", "ticket-options-heading");
    optionsHeading.append(element("strong", "", "Support topics"), element("span", "ticket-options-count", "1–25 options"));
    optionsSection.append(optionsHeading, optionsContainer, addOption);
    const previewSection = configSection("Preview", "Member view", "This preview updates as you edit the panel and shows how the controls will appear in Discord.");
    previewSection.append(preview);
    form.append(
      basicSection,
      behaviorSection,
      supportSection,
      appearanceSection,
      optionsSection,
      previewSection,
      actions,
    );
    panel.append(status, form);
    root.append(panel);

    let state;
    const showError = (message) => {
      status.className = "ticket-status ticket-status-error";
      status.textContent = message;
    };
    const showSuccess = (message) => {
      status.className = "ticket-status ticket-status-success";
      status.textContent = message;
    };
    const renderPreview = () => {
      if (!state) return;
      const buttonsMode = state.panel_layout === "buttons";
      layoutHelp.textContent = buttonsMode
        ? "Each ticket option will appear as a button below the embed. Choose an individual Discord button color: Blue, Green, Red, or Gray. Discord does not support arbitrary hex colors for buttons."
        : "All ticket options will appear in one dropdown below the embed. Descriptions are shown to members when they open the menu.";
      preview.replaceChildren();
      preview.append(element("div", "ticket-panel-preview-label", buttonsMode ? "Buttons preview" : "Select menu preview"));
      if (buttonsMode) {
        const buttonGrid = element("div", "ticket-preview-buttons");
        state.options.slice(0, MAX_OPTIONS).forEach((option, index) => {
          const style = BUTTON_STYLES.some((item) => item.value === option.button_style)
            ? option.button_style
            : defaultButtonStyle(index);
          const button = element("span", `ticket-preview-button ticket-preview-button-${style}`);
          button.textContent = `${option.emoji ? `${option.emoji} ` : ""}${option.label || `Option ${index + 1}`}`;
          buttonGrid.append(button);
        });
        preview.append(buttonGrid);
      } else {
        const selectPreview = element("select", "ticket-input ticket-preview-select");
        selectPreview.disabled = true;
        selectPreview.append(new Option("Choose a support topic", ""));
        state.options.slice(0, MAX_OPTIONS).forEach((option, index) => {
          const label = option.label || `Option ${index + 1}`;
          const description = option.description ? ` — ${option.description}` : "";
          selectPreview.append(new Option(`${label}${description}`, option.value || ""));
        });
        preview.append(selectPreview);
      }
    };
    const renderOptionRows = () => {
      renderOptions(optionsContainer, state.options, (index) => {
        state.options.splice(index, 1);
        renderOptionRows();
      }, state.panel_layout, renderPreview);
      addOption.disabled = state.options.length >= MAX_OPTIONS;
      renderPreview();
    };
    const populateSelect = (select, placeholder, items, selected) => {
      select.replaceChildren(new Option(placeholder, ""));
      items.forEach((item) => select.append(new Option(item.name, item.id)));
      if (selected && items.some((item) => item.id === selected)) select.value = selected;
    };
    const populateRoleSelect = (items, selected) => {
      supportRoles.replaceChildren();
      roleMenu.replaceChildren();
      (items || []).forEach((role) => {
        const option = new Option(`@${role.name}`, role.id);
        option.disabled = Boolean(role.managed);
        option.title = role.managed ? "Managed roles cannot be assigned to tickets" : "";
        option.selected = Array.isArray(selected) && selected.includes(role.id);
        supportRoles.append(option);
        const roleButton = element("button", "ticket-role-option");
        roleButton.type = "button";
        roleButton.disabled = option.disabled;
        roleButton.dataset.roleId = role.id;
        roleButton.append(
          element("span", "ticket-role-check", option.selected ? "✓" : ""),
          element("span", "ticket-role-name", `@${role.name}`),
        );
        roleButton.addEventListener("click", () => {
          option.selected = !option.selected;
          supportRoles.dispatchEvent(new Event("change", { bubbles: true }));
        });
        roleMenu.append(roleButton);
      });
      updateRolePickerSummary();
    };
    const updateRolePickerSummary = () => {
      const selected = Array.from(supportRoles.selectedOptions);
      roleTrigger.textContent = selected.length
        ? `${selected.length} support role${selected.length === 1 ? "" : "s"} selected`
        : "Select support role(s)";
      roleMenu.querySelectorAll(".ticket-role-option").forEach((button) => {
        const option = Array.from(supportRoles.options).find((item) => item.value === button.dataset.roleId);
        const check = button.querySelector(".ticket-role-check");
        if (check) check.textContent = option?.selected ? "✓" : "";
        button.classList.toggle("is-selected", Boolean(option?.selected));
      });
    };
    const collectPayload = () => {
      const payload = new FormData();
      // Keep the last loaded values as a fallback.  This prevents a browser
      // select from submitting an empty value while its options are being
      // refreshed or after a cached page is restored from bfcache.
      payload.append("setup_channel_id", String(setupChannel.value || state.setup_channel_id || ""));
      payload.append("category_id", String(category.value || state.category_id || ""));
      payload.append("panel_layout", state.panel_layout === "buttons" ? "buttons" : "select_menu");
      payload.append("priority", priority.value);
      payload.append("max_open_tickets", String(maxOpenTickets.value || state.max_open_tickets || 1));
      payload.append("log_channel_id", String(logChannel.value || state.log_channel_id || ""));
      payload.append("support_role_ids", JSON.stringify(Array.from(supportRoles.selectedOptions).map((option) => option.value)));
      payload.append("require_description", String(requireDescription.checked));
      payload.append("description_prompt", descriptionPrompt.value);
      payload.append("remove_custom_icon", String(Boolean(state.remove_custom_icon)));
      payload.append("options", JSON.stringify(state.options.map((option) => ({
        label: option.label,
        value: option.value,
        description: option.description,
        emoji: option.emoji,
        button_style: option.button_style,
      }))));
      if (customIcon.files && customIcon.files[0]) payload.append("custom_icon", customIcon.files[0]);
      return payload;
    };
    const waitForPost = async (requestId) => {
      const deadline = Date.now() + 25_000;
      while (Date.now() < deadline) {
        const result = await requestJson(`/api/command-requests/${encodeURIComponent(requestId)}`);
        if (result.status === "complete") return;
        if (result.status === "failed") throw new Error(result.error || "BirdBot could not post the ticket panel.");
        await new Promise((resolve) => window.setTimeout(resolve, 500));
      }
      throw new Error("Posting the ticket panel is taking too long. Check the selected channel shortly.");
    };

    beginLoading("Loading ticket settings...", false);
    try {
      const data = await requestJson(`/api/guilds/${encodeURIComponent(guildId)}/tickets/config`);
      const configuredMaxTickets = Number.parseInt(String(data.config?.max_open_tickets ?? "1"), 10);
      state = {
        setup_channel_id: data.config?.setup_channel_id || "",
        category_id: data.config?.category_id || "",
        panel_layout: data.config?.panel_layout === "buttons" ? "buttons" : "select_menu",
        priority: data.config?.priority || "medium",
        max_open_tickets: Number.isFinite(configuredMaxTickets)
          ? Math.min(25, Math.max(1, configuredMaxTickets))
          : 1,
        log_channel_id: data.config?.log_channel_id || "",
        support_role_ids: Array.isArray(data.config?.support_role_ids) ? data.config.support_role_ids : [],
        require_description: Boolean(data.config?.require_description),
        description_prompt: data.config?.description_prompt || "Please describe your request.",
        remove_custom_icon: false,
        options: Array.isArray(data.config?.options) && data.config.options.length
          ? data.config.options.map((option, index) => ({
            label: option.label || "",
            value: option.value || "",
            description: option.description || "",
            emoji: option.emoji || "",
            button_style: BUTTON_STYLES.some((style) => style.value === option.button_style)
              ? option.button_style
              : defaultButtonStyle(index),
          }))
          : [{ label: "General Support", value: "support", description: "Get help from the team", emoji: "", button_style: defaultButtonStyle(0) }],
      };
      populateSelect(setupChannel, "Choose a text channel", data.channels || [], state.setup_channel_id);
      populateSelect(category, "Choose a channel category", data.categories || [], state.category_id);
      populateSelect(logChannel, "No ticket logs channel", data.channels || [], state.log_channel_id);
      populateRoleSelect(data.roles || [], state.support_role_ids);
      buttonsLayout.checked = state.panel_layout === "buttons";
      selectLayout.checked = state.panel_layout !== "buttons";
      priority.value = state.priority;
      maxOpenTickets.value = String(state.max_open_tickets);
      requireDescription.checked = state.require_description;
      descriptionPrompt.value = state.description_prompt;
      if (data.config?.custom_icon_url) {
        customIconStatus.textContent = "A custom icon is currently saved. Upload a new file to replace it, or use Delete Picture to revert to the server icon.";
        customIconStatus.className = "ticket-icon-status ticket-icon-status-saved";
        removeIcon.disabled = false;
      }
      setupChannel.addEventListener("change", () => { state.setup_channel_id = setupChannel.value; });
      category.addEventListener("change", () => { state.category_id = category.value; });
      buttonsLayout.addEventListener("change", () => {
        if (!buttonsLayout.checked) return;
        state.panel_layout = "buttons";
        renderOptionRows();
      });
      selectLayout.addEventListener("change", () => {
        if (!selectLayout.checked) return;
        state.panel_layout = "select_menu";
        renderOptionRows();
      });
      priority.addEventListener("change", () => { state.priority = priority.value; });
      maxOpenTickets.addEventListener("change", () => {
        const parsed = Number.parseInt(maxOpenTickets.value, 10);
        state.max_open_tickets = Number.isFinite(parsed) ? Math.min(25, Math.max(1, parsed)) : 1;
        maxOpenTickets.value = String(state.max_open_tickets);
      });
      logChannel.addEventListener("change", () => { state.log_channel_id = logChannel.value; });
      supportRoles.addEventListener("change", () => {
        state.support_role_ids = Array.from(supportRoles.selectedOptions).map((option) => option.value);
        updateRolePickerSummary();
      });
      requireDescription.addEventListener("change", () => { state.require_description = requireDescription.checked; });
      descriptionPrompt.addEventListener("input", () => { state.description_prompt = descriptionPrompt.value; });
      customIcon.addEventListener("change", () => {
        const file = customIcon.files && customIcon.files[0];
        state.remove_custom_icon = false;
        customIconStatus.textContent = file
          ? `${file.name} selected. It will replace the current panel icon when saved.`
          : "No custom icon selected. The server icon will be used.";
        customIconStatus.className = "ticket-icon-status";
        removeIcon.disabled = !file && !data.config?.custom_icon_url;
      });
      removeIcon.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        customIcon.value = "";
        state.remove_custom_icon = true;
        removeIcon.disabled = true;
        customIconStatus.textContent = "Removing custom icon...";
        customIconStatus.className = "ticket-icon-status";
        // Deleting the picture is an immediate configuration action. The
        // existing Save/Post actions remain available for other settings.
        void saveSettings(null, false);
      });
      renderOptionRows();
      if (!data.channels?.length) showError("No accessible text channels were found for the ticket panel.");
      else if (!data.categories?.length) showError("No channel categories were found. Create a category before saving.");
    } catch (error) {
      showError(error instanceof Error ? error.message : "Ticket settings could not be loaded.");
      form.querySelectorAll("input, select, button").forEach((control) => { control.disabled = true; });
    } finally {
      endLoading(false);
    }

    addOption.addEventListener("click", () => {
      if (state.options.length >= MAX_OPTIONS) return;
      state.options.push(optionTemplate(state.options.length));
      renderOptionRows();
    });
    roleTrigger.addEventListener("click", () => {
      roleMenu.hidden = !roleMenu.hidden;
      roleTrigger.setAttribute("aria-expanded", String(!roleMenu.hidden));
    });

    const saveSettings = async (event, shouldPost = false) => {
      if (event) event.preventDefault();
      if (save.disabled || post.disabled) return;
      const setupChannelId = String(setupChannel.value || state?.setup_channel_id || "");
      const categoryId = String(category.value || state?.category_id || "");
      if (!setupChannelId || !categoryId) {
        showError("Choose both a ticket panel channel and a ticket category.");
        return;
      }
      if (!state || !["buttons", "select_menu"].includes(state.panel_layout)) {
        showError("Choose a ticket panel layout.");
        return;
      }
      const parsedMaxTickets = Number(maxOpenTickets.value);
      if (!Number.isInteger(parsedMaxTickets) || parsedMaxTickets < 1 || parsedMaxTickets > 25) {
        showError("Maximum open tickets must be a whole number from 1 to 25.");
        return;
      }
      state.max_open_tickets = parsedMaxTickets;
      const invalidOptionIndex = state.options.findIndex((option) => (
        !String(option.label || "").trim() || !String(option.value || "").trim()
      ));
      if (invalidOptionIndex !== -1) {
        showError(`${state.panel_layout === "buttons" ? "Button" : "Ticket option"} ${invalidOptionIndex + 1} needs a label and value.`);
        return;
      }
      save.disabled = true;
      post.disabled = true;
      addOption.disabled = true;
      beginLoading(shouldPost ? "Saving and posting ticket panel..." : "Saving ticket settings...");
      try {
        const response = await requestJson(
          `/api/guilds/${encodeURIComponent(guildId)}/tickets/${shouldPost ? "post" : "config"}`,
          {
            method: "POST",
            // Do not set Content-Type manually: the browser adds the
            // multipart boundary required by FastAPI for FormData uploads.
            body: collectPayload(),
          },
        );
        if (shouldPost) {
          await waitForPost(response.request_id);
          showSuccess("Ticket panel posted successfully.");
        } else {
          showSuccess(response.message || "Ticket system configuration saved.");
        }
        if (response.config) {
          state.remove_custom_icon = false;
          state.panel_layout = response.config.panel_layout === "buttons" ? "buttons" : "select_menu";
          const responseMaxTickets = Number.parseInt(String(response.config.max_open_tickets ?? state.max_open_tickets ?? "1"), 10);
          state.max_open_tickets = Number.isFinite(responseMaxTickets)
            ? Math.min(25, Math.max(1, responseMaxTickets))
            : 1;
          buttonsLayout.checked = state.panel_layout === "buttons";
          selectLayout.checked = state.panel_layout !== "buttons";
          maxOpenTickets.value = String(state.max_open_tickets);
          renderOptionRows();
          if (response.config.custom_icon_url) {
            customIconStatus.textContent = "A custom icon is saved. Upload a replacement or use Delete Picture to revert to the server icon.";
            customIconStatus.className = "ticket-icon-status ticket-icon-status-saved";
            removeIcon.disabled = false;
          } else {
            customIconStatus.textContent = "No custom icon is saved. The server icon will be used.";
            customIconStatus.className = "ticket-icon-status";
            removeIcon.disabled = true;
          }
        }
      } catch (error) {
        if (state?.remove_custom_icon) removeIcon.disabled = false;
        showError(error instanceof Error ? error.message : "Ticket settings could not be saved.");
      } finally {
        save.disabled = false;
        post.disabled = false;
        addOption.disabled = state.options.length >= MAX_OPTIONS;
        endLoading();
      }
    };
    form.addEventListener("submit", (event) => saveSettings(event, false));
    post.addEventListener("click", () => saveSettings(null, true));
  }

  window.BirdBotTickets = { mount };
}());
