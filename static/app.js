const TIMELEFT_UPDATE_MS = 30000;
const STALE_THRESHOLD_HOURS = 8;

const stateNode = document.getElementById("app-state");
const form = document.getElementById("search-form");
const queryInput = document.getElementById("q");
const sortSelect = document.getElementById("sort");
const endingWithinSelect = document.getElementById("ending_within");
const homePostalCodeInput = document.getElementById("home_postal_code");
const radiusKmInput = document.getElementById("radius_km");
const sourceInputs = Array.from(document.querySelectorAll('input[name="source"]'));
const clearButton = document.getElementById("clear-search");
const submitButton = document.getElementById("search-submit");
const resultsList = document.getElementById("results-list");
const resultsTitle = document.getElementById("results-title");
const resultsSubtitle = document.getElementById("results-subtitle");
const resultStatus = document.getElementById("result-status");
const indexStatus = document.getElementById("index-status");
const paginationShell = document.getElementById("pagination-shell");
const paginationPrevious = document.getElementById("pagination-previous");
const paginationNext = document.getElementById("pagination-next");
const paginationNote = document.getElementById("pagination-note");
const reindexButtons = Array.from(document.querySelectorAll("[data-reindex-trigger]"));
const reindexButton = document.getElementById("reindex-button");
const reindexStatus = document.getElementById("reindex-status");
const progressShell = document.getElementById("index-progress-shell");
const progressFill = document.getElementById("index-progress-fill");
const progressLabel = document.getElementById("index-progress-label");
const progressPercent = document.getElementById("index-progress-percent");
const historyBody = document.getElementById("history-body");
const historyStatus = document.getElementById("history-status");
const configForm = document.getElementById("index-config-form");
const configStatus = document.getElementById("config-status");
const themeToggle = document.getElementById("theme-toggle");

const initialState = {
  apiUrl: stateNode?.dataset.apiUrl || "/api/search",
  query: stateNode?.dataset.query || window.__INITIAL_QUERY__ || "",
  sort: stateNode?.dataset.sort || window.__INITIAL_SORT__ || "ending_soonest",
  limit: Number(stateNode?.dataset.limit || window.__INITIAL_LIMIT__ || 50),
  offset: Number(stateNode?.dataset.offset || 0),
  total: Number(stateNode?.dataset.total || 0),
  sources: stateNode?.dataset.sources ? stateNode.dataset.sources.split(",").filter(Boolean) : Array.isArray(window.__INITIAL_SOURCES__) ? window.__INITIAL_SOURCES__ : [],
  endingWithin: stateNode?.dataset.endingWithin || window.__INITIAL_ENDING_WITHIN__ || "",
  homePostalCode: new URL(window.location.href).searchParams.get("home_postal_code") || "",
  radiusKm: new URL(window.location.href).searchParams.get("radius_km") || "",
  indexedAt: stateNode?.dataset.indexedAt || "",
  deployCommit: stateNode?.dataset.deployCommit || "",
  lastRunStatus: stateNode?.dataset.lastRunStatus || "",
  lastRunFinishedAt: stateNode?.dataset.lastRunFinishedAt || "",
  lastRunSummary: stateNode?.dataset.lastRunSummary || "",
  lastRunDurationSeconds: Number(stateNode?.dataset.lastRunDurationSeconds || 0),
  progressTotal: Number(stateNode?.dataset.progressTotal || 0),
  progressDone: Number(stateNode?.dataset.progressDone || 0),
  progressPercent: Number(stateNode?.dataset.progressPercent || 0),
  progressMessage: stateNode?.dataset.progressMessage || "",
  indexing: stateNode?.dataset.indexing === "true",
  currentRunStartedAt: stateNode?.dataset.currentRunStartedAt || "",
  currentRunScope: stateNode?.dataset.currentRunScope || "",
  results: Array.isArray(window.__INITIAL_RESULTS__) ? window.__INITIAL_RESULTS__ : [],
  indexingHistory: Array.isArray(window.__INITIAL_INDEXING_HISTORY__) ? window.__INITIAL_INDEXING_HISTORY__ : [],
};

function money(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "Price unavailable";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(num);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatEndTime(endTime) {
  if (!endTime) return "Time unavailable";
  const parsed = Date.parse(endTime);
  if (Number.isNaN(parsed)) return "Time unavailable";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function formatTimeLeft(endTime) {
  if (!endTime) {
    return { text: "Time unavailable", badge: null, ended: false };
  }
  const end = Date.parse(endTime);
  if (Number.isNaN(end)) {
    return { text: "Time unavailable", badge: null, ended: false };
  }

  const deltaMs = end - Date.now();
  if (deltaMs <= 0) {
    return { text: "Ended", badge: "Ended", ended: true };
  }

  const totalMinutes = Math.floor(deltaMs / 60000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;

  const parts = [];
  if (days) parts.push(`${days}d`);
  if (days || hours) parts.push(`${hours}h`);
  if (!days && !hours) parts.push(`${minutes}m`);
  else if (minutes) parts.push(`${minutes}m`);

  const endDate = new Date(end);
  const now = new Date();
  const sameDay =
    endDate.getFullYear() === now.getFullYear() &&
    endDate.getMonth() === now.getMonth() &&
    endDate.getDate() === now.getDate();

  let badge = null;
  if (deltaMs < 60 * 60 * 1000) {
    badge = "Ending soon";
  } else if (sameDay) {
    badge = "Today";
  }

  return { text: `Ends in ${parts.join(" ")}`, badge, ended: false };
}

function buildChip(text) {
  return `<span class="chip">${escapeHtml(text)}</span>`;
}

function timeBadgeClass(label) {
  if (label === "Ended") return "time-badge danger";
  if (label === "Ending soon") return "time-badge warning";
  if (label === "Today") return "time-badge success";
  return "time-badge";
}

function productResultCard(result) {
  const image = result.imageUrl ? `<img src="${escapeHtml(result.imageUrl)}" alt="" loading="lazy">` : '<div class="thumb-placeholder"><span aria-hidden="true">□</span><span>No image</span></div>';
  const chips = [];
  if (result.lot_number) chips.push(buildChip(`Lot ${result.lot_number}`));
  if (result.condition) chips.push(buildChip(result.condition));
  if (result.auctionAddress) chips.push(buildChip(result.auctionAddress));
  if (result.distance_km !== null && result.distance_km !== undefined) chips.push(buildChip(`${Number(result.distance_km).toFixed(1)} km away`));
  if (result.shipping_available !== null && result.shipping_available !== undefined) {
    chips.push(buildChip(result.shipping_available ? "Shipping" : "Pickup"));
  }
  const time = formatTimeLeft(result.endTime || result.end_time);
  return `
    <article class="result-card" data-result-card data-end-time="${escapeHtml(result.endTime || result.end_time || "")}" data-source="${escapeHtml(result.source || "")}" data-price="${escapeHtml(result.currentPrice ?? result.current_bid ?? "")}">
      <div class="thumb">${image}</div>
      <div class="result-body">
        <div class="result-top">
          <div class="result-title-wrap">
            <p class="result-source">${escapeHtml(result.source || "")}${result.sourceAuction ? ` · ${escapeHtml(result.sourceAuction)}` : ""}</p>
            <h2 class="result-title"><a href="${escapeHtml(result.productUrl || result.url || "#")}" target="_blank" rel="noreferrer">${escapeHtml(result.lot_title || "")}</a></h2>
          </div>
          <div class="result-price">${result.currentPrice !== null && result.currentPrice !== undefined ? escapeHtml(money(result.currentPrice)) : "Price unavailable"}</div>
        </div>
        <div class="result-meta">${chips.join("")}</div>
        <div class="time-row">
          <span class="time-left" data-time-left>${escapeHtml(time.text)}</span>
          <span class="${timeBadgeClass(time.badge)}" data-time-badge>${escapeHtml(time.badge || "")}</span>
        </div>
        ${result.description || result.details ? `<p class="result-copy">${escapeHtml(result.description || result.details)}</p>` : ""}
        <div class="result-links">
          <a class="link-button" href="${escapeHtml(result.productUrl || result.url || "#")}" target="_blank" rel="noreferrer">View lot <span aria-hidden="true">↗</span></a>
        </div>
      </div>
    </article>
  `;
}

function resultSkeleton() {
  return `
    <article class="result-card skeleton">
      <div class="thumb skeleton-box"></div>
      <div class="result-body">
        <div class="skeleton-line short"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line medium"></div>
        <div class="skeleton-line long"></div>
      </div>
    </article>
  `;
}

function emptyState(title, detail) {
  return `
    <section class="empty-state" role="status">
      <h2>${escapeHtml(title)}</h2>
      <p>${escapeHtml(detail)}</p>
    </section>
  `;
}

function updateIndexStatus() {
  if (!indexStatus) return;
  const indexedAt = indexStatus.dataset.indexedAt || initialState.indexedAt;
  if (!indexedAt) {
    indexStatus.textContent = "Last indexed: Not indexed yet";
    indexStatus.classList.remove("warning", "success");
    return;
  }

  const indexed = Date.parse(indexedAt);
  if (Number.isNaN(indexed)) {
    indexStatus.textContent = `Last indexed: ${indexedAt}`;
    return;
  }

  const ageHours = (Date.now() - indexed) / (1000 * 60 * 60);
  indexStatus.textContent = `Last indexed: ${new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(indexed)}`;
  indexStatus.classList.toggle("warning", ageHours > STALE_THRESHOLD_HOURS);
  if (ageHours > STALE_THRESHOLD_HOURS) {
    indexStatus.classList.add("warning");
  }
}

function updateReindexStatus(payload) {
  if (!reindexStatus || !reindexButtons.length) return;
  const indexing = payload?.indexing ?? initialState.indexing;
  const scope = payload?.current_run_scope || initialState.currentRunScope;
  if (indexing) {
    reindexStatus.textContent = `Reindexing${scope ? ` (${scope})` : ""}...`;
    reindexButtons.forEach((button) => {
      button.disabled = true;
    });
    return;
  }
  const finishedAt = payload?.last_run_finished_at || initialState.lastRunFinishedAt;
  const duration = payload?.last_run_duration_seconds || initialState.lastRunDurationSeconds;
  reindexStatus.textContent = finishedAt
    ? `Last reindex: ${finishedAt}${duration ? ` (${duration.toFixed(1)}s)` : ""}`
    : "Ready to reindex";
  reindexButtons.forEach((button) => {
    button.disabled = false;
  });
}

function historyBadgeClass(status) {
  if (status === "running") return "history-badge running";
  if (status === "failed") return "history-badge failed";
  return "history-badge success";
}

function renderHistory(history) {
  if (!historyBody) return;
  if (!Array.isArray(history) || !history.length) {
    historyBody.innerHTML = '<tr><td colspan="4" class="muted">No index runs yet.</td></tr>';
    return;
  }
  historyBody.innerHTML = history.map((run) => {
    const summary = run.error_text || run.success_summary || run.progress_message || "";
    const items = Number.isFinite(Number(run.item_count)) ? Number(run.item_count) : 0;
    return `
      <tr data-history-run>
        <td>
          <div class="history-run">
            <strong>${escapeHtml((run.scope || "manual").replace(/^./, (m) => m.toUpperCase()))}</strong>
            <span class="muted">${escapeHtml(run.started_at || "")}</span>
          </div>
        </td>
        <td>
          <span class="${historyBadgeClass(run.status)}">${escapeHtml(run.status || "success")}</span>
          ${summary ? `<div class="muted history-note">${escapeHtml(summary)}</div>` : ""}
        </td>
        <td>${items}</td>
        <td>${escapeHtml(run.finished_at || "Running")}</td>
      </tr>
    `;
  }).join("");
}

function updateProgress(payload) {
  if (!progressShell || !progressFill || !progressLabel || !progressPercent) return;

  const indexing = payload?.indexing ?? initialState.indexing;
  const total = Number(payload?.progress_total ?? initialState.progressTotal ?? 0);
  const done = Number(payload?.progress_done ?? initialState.progressDone ?? 0);
  const rawPercent = payload?.progress_percent ?? initialState.progressPercent;
  const computedPercent = total > 0 ? (done / total) * 100 : 0;
  const percent = Number.isFinite(Number(rawPercent))
    ? Math.max(0, Math.min(100, Number(rawPercent)))
    : computedPercent;
  const message = payload?.progress_message || initialState.progressMessage || "";
  const indeterminate = indexing && (rawPercent == null || (done <= 0 && percent <= 0));
  const label = message || (indeterminate ? "Indexing..." : indexing ? "Indexing..." : "Idle");

  progressShell.hidden = !indexing && percent <= 0 && !message;
  progressShell.classList.toggle("indeterminate", indeterminate);
  progressFill.style.width = indeterminate ? "100%" : `${percent}%`;
  progressLabel.textContent = total > 0 ? `${label} (${done}/${total})` : label;
  progressPercent.textContent = indeterminate ? "..." : `${percent.toFixed(0)}%`;
  progressFill.parentElement?.setAttribute("aria-valuenow", indeterminate ? "0" : String(Math.round(percent)));
  if (historyStatus) {
    historyStatus.textContent = indexing ? "Index running" : "History loaded";
  }
}

function readConfig() {
  const sources = {};
  configForm?.querySelectorAll("[data-source-name]").forEach((card) => {
    const sourceName = card.dataset.sourceName;
    const config = {};
    card.querySelectorAll("input[name]").forEach((input) => {
      const [_, key] = input.name.split(".");
      config[key] = input.type === "number" ? Number(input.value) : input.value;
    });
    sources[sourceName] = config;
  });
  return sources;
}

async function saveConfig(event) {
  event.preventDefault();
  if (!configForm || !configStatus) return;
  configStatus.textContent = "Saving...";
  try {
    const response = await fetch("/api/index-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sources: readConfig() }),
    });
    if (!response.ok) throw new Error("Save failed");
    configStatus.textContent = "Saved. Reindex to apply.";
  } catch (error) {
    configStatus.textContent = "Could not save settings.";
  }
}

function updateTimeLeft() {
  document.querySelectorAll("[data-result-card]").forEach((card) => {
    const endTime = card.dataset.endTime;
    const timeLeftNode = card.querySelector("[data-time-left]");
    const badgeNode = card.querySelector("[data-time-badge]");
    if (!timeLeftNode || !badgeNode) return;
    const time = formatTimeLeft(endTime);
    timeLeftNode.textContent = time.text;
    badgeNode.textContent = time.badge || "";
    badgeNode.hidden = !time.badge;
    badgeNode.className = timeBadgeClass(time.badge);
    card.classList.toggle("ended", time.ended);
  });
  updateIndexStatus();
  updateReindexStatus();
  updateProgress();
}

function renderResults(results, query) {
  if (!results.length) {
    resultsList.innerHTML = emptyState("No products found.", query ? "Try a broader search term." : "Use the search box to find matching lots.");
    return;
  }
  resultsList.innerHTML = results.map((result) => productResultCard(result)).join("");
updateTimeLeft();
configForm?.addEventListener("submit", saveConfig);
}

function setLoading(loading) {
  submitButton.disabled = loading;
  clearButton.disabled = loading;
  queryInput.disabled = loading;
  sortSelect.disabled = loading;
  if (loading) {
    resultsList.innerHTML = Array.from({ length: 4 }, () => resultSkeleton()).join("");
    resultStatus.textContent = "Searching...";
  } else {
    resultStatus.textContent = "";
  }
}

function updateSummary(query, count) {
  if (!resultsTitle || !resultsSubtitle) return;
  if (!query) {
    resultsTitle.textContent = "All indexed lots";
    resultsSubtitle.textContent = `${count} indexed lots available to browse`;
    return;
  }
  resultsTitle.textContent = "Results";
  resultsSubtitle.textContent = `${count} ${count === 1 ? "match" : "matches"} for "${query}"`;
}

function updatePagination(query, sort, total, offset, count) {
  if (!paginationShell || !paginationPrevious || !paginationNext || !paginationNote) return;
  const limit = initialState.limit || 50;
  const hasPagination = total > limit;
  paginationShell.hidden = !hasPagination;
  if (!hasPagination) {
    return;
  }

  const previousOffset = Math.max(0, offset - limit);
  const nextOffset = offset + limit;
  const buildUrl = (pageOffset) => {
    const url = new URL(window.location.href);
    if (query) url.searchParams.set("q", query);
    else url.searchParams.delete("q");
    if (sort) url.searchParams.set("sort", sort);
    else url.searchParams.delete("sort");
    url.searchParams.set("limit", String(limit));
    if (pageOffset > 0) url.searchParams.set("offset", String(pageOffset));
    else url.searchParams.delete("offset");
    url.searchParams.delete("source");
    initialState.sources.forEach((source) => url.searchParams.append("source", source));
    if (initialState.endingWithin) url.searchParams.set("ending_within", String(initialState.endingWithin));
    else url.searchParams.delete("ending_within");
    return url.pathname + "?" + url.searchParams.toString();
  };

  paginationPrevious.href = buildUrl(previousOffset);
  paginationPrevious.classList.toggle("disabled", offset <= 0);
  paginationPrevious.setAttribute("aria-disabled", offset <= 0 ? "true" : "false");

  paginationNext.href = buildUrl(nextOffset);
  paginationNext.classList.toggle("disabled", nextOffset >= total);
  paginationNext.setAttribute("aria-disabled", nextOffset >= total ? "true" : "false");

  const start = total === 0 ? 0 : offset + 1;
  const end = Math.min(offset + count, total);
  paginationNote.textContent = `Showing ${start}-${end} of ${total}`;
}

function collectFilterState() {
  initialState.sources = sourceInputs.filter((node) => node.checked).map((node) => node.value);
  initialState.endingWithin = endingWithinSelect?.value || "";
  initialState.homePostalCode = homePostalCodeInput?.value || "";
  initialState.radiusKm = radiusKmInput?.value || "";
}

function syncUrl(query, sort, offset = 0) {
  const url = new URL(window.location.href);
  if (query) url.searchParams.set("q", query);
  else url.searchParams.delete("q");
  if (sort) url.searchParams.set("sort", sort);
  else url.searchParams.delete("sort");
  if (initialState.limit) url.searchParams.set("limit", String(initialState.limit));
  if (offset > 0) url.searchParams.set("offset", String(offset));
  else url.searchParams.delete("offset");
  url.searchParams.delete("source");
  initialState.sources.forEach((source) => url.searchParams.append("source", source));
  if (initialState.endingWithin) url.searchParams.set("ending_within", String(initialState.endingWithin));
  else url.searchParams.delete("ending_within");
  if (initialState.homePostalCode) url.searchParams.set("home_postal_code", initialState.homePostalCode);
  else url.searchParams.delete("home_postal_code");
  if (initialState.radiusKm) url.searchParams.set("radius_km", initialState.radiusKm);
  else url.searchParams.delete("radius_km");
  window.history.replaceState({}, "", url);
}

async function runSearch(query, sort, offset = 0) {
  collectFilterState();
  setLoading(true);
  syncUrl(query, sort, offset);
  try {
    const params = new URLSearchParams({
      q: query,
      sort,
      limit: String(initialState.limit || 50),
      offset: String(offset),
    });
    initialState.sources.forEach((source) => params.append("source", source));
    if (initialState.endingWithin) params.set("ending_within", String(initialState.endingWithin));
    if (initialState.homePostalCode) params.set("home_postal_code", initialState.homePostalCode);
    if (initialState.radiusKm) params.set("radius_km", initialState.radiusKm);
    const response = await fetch(`${initialState.apiUrl}?${params.toString()}`);
    if (!response.ok) {
      throw new Error(`Search failed (${response.status})`);
    }
    const payload = await response.json();
    renderResults(payload.results || [], payload.query || query);
    updateSummary(payload.query || query, payload.total ?? payload.count ?? 0);
    updatePagination(payload.query || query, payload.sort || sort, payload.total ?? 0, payload.offset ?? offset, payload.count ?? 0);
    resultStatus.textContent = payload.last_run_summary || "";
  } catch (error) {
    resultsList.innerHTML = emptyState("Search unavailable.", error instanceof Error ? error.message : "Unable to load search results.");
    resultStatus.textContent = "";
  } finally {
    setLoading(false);
    updateTimeLeft();
  }
}

async function triggerReindex() {
  if (!reindexButtons.length) return;
  reindexButtons.forEach((button) => {
    button.disabled = true;
  });
  if (reindexStatus) reindexStatus.textContent = "Starting reindex...";
  try {
    const response = await fetch("/api/reindex", { method: "POST" });
    const payload = await response.json();
    if (!response.ok && response.status !== 409) {
      throw new Error(`Reindex failed (${response.status})`);
    }
    if (payload?.indexing_history) renderHistory(payload.indexing_history);
    updateReindexStatus(payload);
    updateProgress(payload);
    if (payload?.indexing_history) renderHistory(payload.indexing_history);
    if (response.status === 202 || payload?.status === "started") {
      const poll = window.setInterval(async () => {
        try {
          const statusResponse = await fetch("/api/status");
          const statusPayload = await statusResponse.json();
          if (statusPayload?.indexing_history) renderHistory(statusPayload.indexing_history);
          updateReindexStatus(statusPayload);
          updateProgress(statusPayload);
          if (!statusPayload.indexing) {
            window.clearInterval(poll);
            updateReindexStatus(statusPayload);
            updateProgress(statusPayload);
          }
        } catch (error) {
          window.clearInterval(poll);
          if (reindexStatus) reindexStatus.textContent = "Reindex status unavailable";
        }
      }, 2000);
    }
  } catch (error) {
    if (reindexStatus) reindexStatus.textContent = error instanceof Error ? error.message : "Reindex failed";
    reindexButtons.forEach((button) => {
      button.disabled = false;
    });
  }
}

function initialize() {
  const savedTheme = window.localStorage.getItem("auction-hunter-theme");
  const preferredDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const setTheme = (theme) => {
    document.documentElement.dataset.theme = theme;
    themeToggle?.setAttribute("aria-pressed", String(theme === "dark"));
    themeToggle?.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} theme`);
  };
  setTheme(savedTheme || (preferredDark ? "dark" : "light"));
  themeToggle?.addEventListener("click", () => {
    const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    window.localStorage.setItem("auction-hunter-theme", nextTheme);
    setTheme(nextTheme);
  });
  const hasSearchUi = Boolean(queryInput && sortSelect && resultsList);
  if (hasSearchUi) {
    queryInput.value = initialState.query;
    sortSelect.value = initialState.sort || "ending_soonest";
    endingWithinSelect.value = initialState.endingWithin || "";
    if (homePostalCodeInput) homePostalCodeInput.value = initialState.homePostalCode || "";
    if (radiusKmInput) radiusKmInput.value = initialState.radiusKm || "";
    sourceInputs.forEach((input) => {
      input.checked = initialState.sources.includes(input.value);
    });
    updateIndexStatus();
    updateTimeLeft();
    updateReindexStatus();
    updateProgress();
    renderHistory(initialState.indexingHistory);
    renderResults(initialState.results || [], initialState.query);
    updateSummary(initialState.query, initialState.total || initialState.results.length || 0);
    updatePagination(initialState.query, initialState.sort || "ending_soonest", initialState.total || 0, initialState.offset || 0, initialState.results.length || 0);

    form?.addEventListener("submit", (event) => {
      event.preventDefault();
      collectFilterState();
      void runSearch(queryInput.value.trim(), sortSelect.value, 0);
    });

    sortSelect?.addEventListener("change", () => {
      void runSearch(queryInput.value.trim(), sortSelect.value, 0);
    });

    endingWithinSelect?.addEventListener("change", () => {
      initialState.endingWithin = endingWithinSelect.value;
      void runSearch(queryInput.value.trim(), sortSelect.value, 0);
    });

    sourceInputs.forEach((input) => {
      input.addEventListener("change", () => {
        initialState.sources = sourceInputs.filter((node) => node.checked).map((node) => node.value);
        void runSearch(queryInput.value.trim(), sortSelect.value, 0);
      });
    });

    clearButton?.addEventListener("click", () => {
      queryInput.value = "";
      queryInput.focus();
      collectFilterState();
      void runSearch("", sortSelect.value, 0);
    });
  } else {
    updateReindexStatus();
    updateProgress();
    renderHistory(initialState.indexingHistory);
  }

  reindexButtons.forEach((button) => button.addEventListener("click", () => {
    void triggerReindex();
  }));

  window.setInterval(updateTimeLeft, TIMELEFT_UPDATE_MS);
}

initialize();
