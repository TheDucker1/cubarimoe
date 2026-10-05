/* Persistent server jobs; browser polling never downloads upstream images. */
(() => {
    "use strict";
    const storedError = document.querySelector(".stored-chapter-error");
    const fallback = document.querySelector(".d-source-fallback");
    if (fallback) {
        const target = new URL(window.location.href);
        target.searchParams.set("upstream", "1");
        fallback.href = target.href;
    }
    window.addEventListener("error", event => {
        if (storedError && event.target instanceof HTMLImageElement &&
            new URL(event.target.src, window.location.href).pathname.startsWith("/stored-chapters/")) {
            storedError.hidden = false;
        }
    }, true);
    const root = document.querySelector(".chapter-downloads");
    if (!root) return;
    const get = name => root.querySelector(`.d-${name}`);
    const source = root.dataset.source;
    const series = root.dataset.series;
    const isReader = Boolean(document.getElementById("rdr-main"));
    const rows = Array.from(document.querySelectorAll("#chapterTable tr[data-chapter]"));
    let catalog = {groups: {}, downloads: {}};
    let statuses = new Map();
    let timer = null;
    let polling = false;
    let active = false;
    let delay = 2000;
    let maxBulk = 100;
    let submitting = false;
    const csrf = root.querySelector("[name=csrfmiddlewaretoken]").value;
    const message = text => { get("message").textContent = text; };

    async function api(path, body, method = "GET") {
        const options = {method, credentials: "same-origin", headers: {"Accept": "application/json"}};
        if (body !== undefined) {
            options.headers["Content-Type"] = "application/json";
            options.headers["X-CSRFToken"] = csrf;
            options.body = JSON.stringify(body);
        }
        const response = await fetch(path, options);
        let data;
        try { data = await response.json(); }
        catch (_) { throw new Error("The server could not complete this request."); }
        if (!response.ok) throw new Error(data.error || "The server could not complete this request.");
        return data;
    }

    function rowSelection(row) {
        const number = row.dataset.chapter;
        return (catalog.downloads[number] || {})[get("group").value];
    }

    function selectedRows() {
        return rows.filter(row => {
            const checkbox = row.querySelector(".d-select");
            return checkbox && checkbox.checked;
        });
    }

    function updateSelection() {
        const selected = selectedRows();
        get("count").textContent = `${selected.length} selected`;
        get("download").disabled = submitting || !selected.some(row => {
            const item = rowSelection(row);
            return item && statuses.get(item.key) !== "ready" && statuses.get(item.key) !== "deleting";
        });
        get("remove").disabled = submitting || !selected.some(row => {
            const item = rowSelection(row);
            return item && statuses.get(item.key) === "ready";
        });
        rows.forEach(row => {
            const item = rowSelection(row);
            const status = row.querySelector(".d-chapter-status");
            if (!status) return;
            status.textContent = item ?
                ({ready: "Stored", deleting: "Removing", missing: ""}[statuses.get(item.key)] || "") :
                "Selected group unavailable";
            row.querySelector(".d-row-download").disabled = submitting || !item || ["ready", "deleting"].includes(statuses.get(item.key));
            row.querySelector(".d-row-remove").disabled = submitting || !item || statuses.get(item.key) !== "ready";
        });
    }

    function changeSelection(all, checked) {
        rows.forEach(row => {
            if (all || row.style.display !== "none") row.querySelector(".d-select").checked = checked;
        });
        updateSelection();
    }

    async function submitKeys(keys, removing, skipped = 0) {
        keys = Array.from(new Set(keys));
        if (!keys.length) { message("No eligible chapters selected."); return; }
        if (removing && !window.confirm(`Permanently remove ${keys.length} stored chapter(s) for the selected group from storage?`)) return;
        submitting = true;
        updateSelection();
        const results = [];
        try {
            for (let i = 0; i < keys.length; i += maxBulk) {
                const data = await api(removing ? "/downloads/chapters/bulk-delete/" : "/downloads/jobs/",
                    {keys: keys.slice(i, i + maxBulk)}, "POST");
                results.push(...data.results);
            }
            const failed = new Set(results.filter(item => item.result === "failed").map(item => item.key));
            rows.forEach(row => {
                const item = rowSelection(row);
                if (item && keys.includes(item.key) && !failed.has(item.key)) row.querySelector(".d-select").checked = false;
            });
            const failures = results.filter(item => item.result === "failed");
            message(`${results.length - failures.length} selection(s) accepted or already available.` +
                (skipped ? `\n${skipped} skipped: selected group unavailable, already stored, or being removed.` : "") +
                (failures.length ? `\n${failures.length} failed; retained for retry.\n` + failures.map(item => item.error).join("\n") : ""));
            active = true;
            await poll();
        } catch (error) { message(error.message); }
        finally { submitting = false; updateSelection(); }
    }

    function bulk(removing) {
        const selected = selectedRows();
        const items = selected.map(rowSelection).filter(Boolean);
        const keys = items.filter(item => removing ? statuses.get(item.key) === "ready" :
            !["ready", "deleting"].includes(statuses.get(item.key))).map(item => item.key);
        const skipped = selected.length - keys.length;
        if (skipped) message(`${skipped} chapter(s) skipped: group unavailable, already stored, or being removed.`);
        return submitKeys(keys, removing, skipped);
    }

    function renderQueue(data) {
        statuses = new Map(data.chapters.map(item => [item.chapter_key, item.status]));
        maxBulk = data.max_bulk_items;
        active = data.active;
        get("queue-summary").textContent = `${data.jobs.filter(j => !["accepted", "failed", "cancelled"].includes(j.state)).length} pending · ` +
            `${data.counts.accepted} completed · ${data.counts.failed} failed` +
            (data.paused ? " · Upstream paused; explicit retry is required." : "");
        get("queue").replaceChildren();
        data.jobs.forEach(job => {
            const li = document.createElement("li");
            const title = document.createElement("span");
            let phase = job.state === "accepted" ? "Completed" : job.state;
            if (job.state === "downloading") phase = `Downloading ${job.pages}/${job.total_pages} pages`;
            if (job.state === "cooldown" && job.next_attempt) phase = `Cooling down until ${new Date(job.next_attempt).toLocaleTimeString()}`;
            title.textContent = `${job.title || job.series} · Ch. ${job.chapter} · ${job.group} · ${job.kind}: ${phase}` +
                (job.queue_position ? ` (queue ${job.queue_position})` : "") + (job.error ? ` — ${job.error}` : "");
            if (job.bytes) title.textContent += ` · ${(job.bytes / 1048576).toFixed(1)} MB`;
            li.appendChild(title);
            if (job.state === "failed" || (!["accepted", "cancelled"].includes(job.state) && job.kind === "download")) {
                const button = document.createElement("button");
                const action = job.state === "failed" ? "retry" : "cancel";
                button.textContent = job.cancel_requested ? "Cancelling…" : action === "retry" ? "Retry" : "Cancel";
                button.disabled = job.cancel_requested;
                button.onclick = async () => {
                    button.disabled = true;
                    try { await api(`/downloads/jobs/${job.id}/${action}/`, {}, "POST"); await poll(); }
                    catch (error) { message(error.message); button.disabled = false; }
                };
                li.appendChild(button);
            }
            get("queue").appendChild(li);
        });
        get("cache-usage").textContent = `${data.cache.files}/${data.cache.max_files} files · ${(data.cache.bytes / 1048576).toFixed(1)} MB`;
        updateSelection();
    }

    async function poll() {
        clearTimeout(timer);
        if (polling || document.hidden) return;
        polling = true;
        try {
            const query = source && series ? `?source=${encodeURIComponent(source)}&series=${encodeURIComponent(series)}` : "";
            renderQueue(await api("/downloads/jobs/" + query));
            delay = 2000;
        } catch (error) { message(error.message); delay = Math.min(30000, delay * 2); active = true; }
        finally {
            polling = false;
            if (active && !document.hidden) timer = setTimeout(poll, delay);
        }
    }

    async function currentChapter(removing) {
        try {
            // init.js exposes Reader as a global lexical variable, not a window property.
            if (typeof Reader === "undefined" || !Reader.current || !Reader.SCP) throw new Error("Reader is still loading.");
            const number = String(Reader.SCP.chapter);
            const group = String(Reader.getGroup(number));
            const item = (Reader.current.downloads[number] || {})[group];
            if (!item) throw new Error("Current chapter/group is unavailable for storage.");
            await submitKeys([item.key], removing);
        } catch (error) { message(error.message); }
    }

    async function initialize() {
        if (source && series && !isReader) {
            try {
                catalog = await api(`/downloads/catalog/${encodeURIComponent(source)}/${encodeURIComponent(series)}/`);
                Object.entries(catalog.groups).forEach(([id, label]) => {
                    const option = document.createElement("option");
                    option.value = id; option.textContent = label; get("group").appendChild(option);
                });
                rows.forEach(row => {
                    const cell = document.createElement("td"); cell.className = "d-select-cell";
                    const checkbox = document.createElement("input");
                    checkbox.type = "checkbox"; checkbox.className = "d-select";
                    checkbox.setAttribute("aria-label", `Select chapter ${row.dataset.chapter}`);
                    checkbox.onchange = updateSelection;
                    const status = document.createElement("span"); status.className = "d-chapter-status";
                    const store = document.createElement("button");
                    store.type = "button"; store.className = "d-row-download"; store.textContent = "Store";
                    store.onclick = () => { const item = rowSelection(row); if (item) submitKeys([item.key], false); };
                    const remove = document.createElement("button");
                    remove.type = "button"; remove.className = "d-row-remove"; remove.textContent = "Remove";
                    remove.onclick = () => { const item = rowSelection(row); if (item) submitKeys([item.key], true); };
                    cell.append(checkbox, status, store, remove); row.prepend(cell);
                });
                const header = document.querySelector("#chapters thead tr");
                if (header) header.prepend(document.createElement("th"));
                get("group").onchange = updateSelection;
                get("visible").onclick = () => changeSelection(false, true);
                get("all").onclick = () => changeSelection(true, true);
                get("clear-selection").onclick = () => changeSelection(true, false);
                get("download").onclick = () => bulk(false);
                get("remove").onclick = () => bulk(true);
            } catch (error) { message(error.message); get("selection-controls").hidden = true; }
        } else {
            get("selection-controls").hidden = true;
            if (isReader) {
                get("reader-controls").hidden = false;
                get("current-download").onclick = () => currentChapter(false);
                get("current-remove").onclick = () => currentChapter(true);
            }
        }
        get("clear-cache").onclick = async () => {
            if (!window.confirm("Clear all local archive cache? Stored chapters will remain in remote storage.")) return;
            try { await api("/downloads/cache/clear/", {}, "POST"); await poll(); }
            catch (error) { message(error.message); }
        };
        document.addEventListener("visibilitychange", () => {
            clearTimeout(timer);
            if (!document.hidden) poll();
        });
        await poll();
    }
    initialize();
})();
