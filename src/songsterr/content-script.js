// - shows a small status badge (bottom-right)
// - toggles extension enabled state (persists in chrome.storage)
// - injects injected.js when enabled
// - waits for a handshake from injected.js

(() => {
    // Only run in top frame so you don't spawn 10 badges in iframes
    if (window !== window.top) return;

    const TAG = "[SCASO]";
    const ENABLE_KEY = "scasoEnabled";
    const HIDE_KEY = "scasoBadgeHidden";
    const BADGE_ID = "scaso-badge";

    const log = (...a) => console.log(TAG, ...a);
    const warn = (...a) => console.warn(TAG, ...a);
    const err = (...a) => console.error(TAG, ...a);

    // Safe storage helpers (won't crash if storage is missing)
    async function getStore(key, def) {
    if (!chrome?.storage?.local) return def;
    return new Promise((resolve) =>
      chrome.storage.local.get({ [key]: def }, (r) => resolve(r[key]))
    );
    }
    async function setStore(obj) {
    if (!chrome?.storage?.local) return;
    return new Promise((resolve) => chrome.storage.local.set(obj, resolve));
    }


    function ensureBadge() {
    let el = document.getElementById(BADGE_ID);
    if (el) return el;

    el = document.createElement("div");
    el.id = BADGE_ID;
    el.style.cssText = [
      "position:fixed",
      "bottom:14px",
      "right:14px",
      "z-index:2147483647",
      "font:12px/1.2 -apple-system,system-ui,Segoe UI,Roboto,Arial,sans-serif",
      "background:#111",
      "color:#fff",
      "padding:8px 10px",
      "border-radius:10px",
      "box-shadow:0 6px 18px rgba(0,0,0,.35)",
      "display:flex",
      "align-items:center",
      "gap:8px",
      "cursor:default",
      "user-select:none",
    ].join(";");

    const dot = document.createElement("span");
    dot.id = "scaso-dot";
    dot.style.cssText =
      "width:8px;height:8px;border-radius:999px;background:#999;display:inline-block;flex:0 0 8px";

    const label = document.createElement("span");
    label.id = "scaso-label";
    label.textContent = "SCASO: loading…";

    const toggle = document.createElement("button");
    toggle.id = "scaso-toggle";
    toggle.textContent = "ON";
    toggle.style.cssText =
      "margin-left:8px;padding:2px 8px;border-radius:6px;background:#222;border:1px solid rgba(255,255,255,.18);color:#fff;font-weight:700;cursor:pointer";

    const close = document.createElement("button");
    close.title = "Hide badge";
    close.textContent = "×";
    close.style.cssText =
      "margin-left:6px;border:none;background:transparent;color:#888;font-size:14px;line-height:1;padding:0 2px;cursor:pointer";

    el.append(dot, label, toggle, close);
    (document.documentElement || document.body || document.head).appendChild(el);

    // Drag to move
    let sx, sy, bx, by, dragging = false;
    el.addEventListener("mousedown", (e) => {
      if (e.target === close || e.target === toggle) return;
      dragging = true;
      sx = e.clientX; sy = e.clientY;
      const r = el.getBoundingClientRect();
      bx = r.left; by = r.top;
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const x = bx + (e.clientX - sx);
      const y = by + (e.clientY - sy);
      el.style.left = x + "px";
      el.style.top = y + "px";
      el.style.right = "auto";
      el.style.bottom = "auto";
    });
    window.addEventListener("mouseup", () => (dragging = false));

    close.addEventListener("click", async (e) => {
      e.stopPropagation();
      el.remove();
      await setStore({ [HIDE_KEY]: true });
    });

    toggle.addEventListener("click", async (e) => {
      e.stopPropagation();
      const current = await getStore(ENABLE_KEY, true);
      const next = !current;
      await setStore({ [ENABLE_KEY]: next });
      setStatus(next ? "warn" : "off", next ? "SCASO: will inject on reload" : "SCASO: disabled");
      // Quick + reliable way to apply: reload the page
      location.reload();
    });

    return el;
    }

    function setStatus(kind, text) {
    const dot = document.getElementById("scaso-dot");
    const label = document.getElementById("scaso-label");
    const toggle = document.getElementById("scaso-toggle");
    if (!dot || !label || !toggle) return;

    const color =
      kind === "on" ? "#44c759" :
      kind === "off" ? "#666" :
      kind === "warn" ? "#ffcc00" :
      kind === "error" ? "#ff453a" : "#999";

    dot.style.background = color;
    label.textContent = text;
    toggle.textContent = kind === "off" ? "OFF" : "ON";
    }

    // Main flow shit
    async function boot() {
    const [enabled, hidden] = await Promise.all([
      getStore(ENABLE_KEY, true),
      getStore(HIDE_KEY, false),
    ]);

    if (!hidden) ensureBadge();
    if (!hidden) setStatus(enabled ? "warn" : "off", enabled ? "SCASO: injecting…" : "SCASO: disabled");

    if (!enabled) {
      log("Disabled via storage; not injecting.");
      return;
    }

    // Inject page script (so it runs in the page's JS world, not isolated)
    try {
      const s = document.createElement("script");
      s.src = chrome.runtime.getURL("injected.js");
      s.async = false;
      s.onload = () => s.remove();
      (document.head || document.documentElement).appendChild(s);
      log("Injected injected.js");
    } catch (e) {
      err("Inject failed:", e);
      if (!hidden) setStatus("error", "SCASO: inject failed");
      return;
    }

    // Handshake: wait for injected.js to ack
    let acked = false;
    function onAckViaMsg(e) {
      if (e?.source !== window) return;
      const d = e.data;
      if (d && d.type === "SCASO:enabled") {
        acked = true;
        const v = d.version ? ` v${d.version}` : "";
        setStatus("on", `SCASO${v}: active`);
        window.removeEventListener("message", onAckViaMsg);
      }
    }
    window.addEventListener("message", onAckViaMsg);

    // Ping the page so injected.js can reply
    try {
      window.postMessage({ type: "SCASO:ping" }, "*");
    } catch (e) {
      warn("Ping postMessage failed:", e);
    }

    // Fallback if we never hear back
    setTimeout(() => {
      if (!acked) setStatus("warn", "SCASO: injected (no ack)");
    }, 3000);
    }

    // Run ASAP at document_start - no need to wait for DOMContentLoaded
    try {
        boot();
    } catch (e) {
        err("Boot crashed:", e);
    }
})();
