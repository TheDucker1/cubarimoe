/* Library operates on stored DB identities; it never resolves upstream data. */
(() => {
    "use strict";
    const root = document.querySelector(".d-library");
    if (!root) return;
    const get = name => root.querySelector(`.d-library-${name}`);
    const rows = Array.from(root.querySelectorAll("li[data-key]"));
    const csrf = root.querySelector("[name=csrfmiddlewaretoken]").value;
    let submitting = false;
    const selected = () => rows.filter(row => !row.hidden && row.querySelector("input").checked);
    function update() {
        get("count").textContent = `${selected().length} selected`;
        get("remove").disabled = submitting || !selected().length;
        rows.forEach(row => { row.querySelector("input").disabled = submitting; });
        get("all").disabled = get("clear").disabled = submitting;
    }
    root.addEventListener("change", update);
    get("all").onclick = () => { rows.forEach(row => { if (!row.hidden) row.querySelector("input").checked = true; }); update(); };
    get("clear").onclick = () => { rows.forEach(row => { row.querySelector("input").checked = false; }); update(); };
    get("remove").onclick = async () => {
        const chosen = selected();
        if (!chosen.length || !window.confirm(`Permanently remove ${chosen.length} stored chapter(s) from remote storage?`)) return;
        submitting = true; update();
        const size = Number(root.dataset.maxBulk);
        let accepted = 0;
        const failures = [];
        try {
            for (let i = 0; i < chosen.length; i += size) {
                const response = await fetch("/downloads/chapters/bulk-delete/", {
                    method: "POST", credentials: "same-origin",
                    headers: {"Content-Type": "application/json", "Accept": "application/json", "X-CSRFToken": csrf},
                    body: JSON.stringify({keys: chosen.slice(i, i + size).map(row => row.dataset.key)}),
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || "Removal request failed.");
                data.results.forEach(result => {
                    if (result.result === "failed") failures.push(result.error);
                    else { accepted++; const row = chosen.find(item => item.dataset.key === result.key); if (row) row.hidden = true; }
                });
            }
        } catch (error) { failures.push(error.message); }
        finally {
            submitting = false; update();
            root.querySelectorAll(".d-library-title").forEach(title => {
                const remaining = Array.from(title.querySelectorAll("li")).filter(row => !row.hidden).length;
                if (!remaining) title.hidden = true;
                else title.querySelector("summary small").textContent = `${remaining} stored`;
            });
            get("message").textContent = `${accepted} removal(s) queued. See the download queue for completion.` +
                (failures.length ? `\n${failures.join("\n")}` : "");
        }
    };
    update();
})();
