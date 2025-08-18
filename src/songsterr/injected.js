// ==UserScript==
// @name         SCASO Patcher
// @namespace    com.songsterr.scaso
// @version      3.2
// @description  Script script that intercepts Songsterr state/profile responses
//               and rewrites them. Demonstrates fetch/XHR interception & DOM patching.
// @author       
// @license      MIT
// @match        *://www.songsterr.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(() => {
    const TAG = "[SCASO]";
    const log  = (...args) => console.log(TAG, ...args);
    const warn = (...args) => console.warn(TAG, ...args);
    const err  = (...args) => console.error(TAG, ...args);

    // ----------- Handshake with content script -----------
    try {
        // Reply when the cotent script pings
        window.addEventListener("message", (e) => {
            if (e?.source !== window) return;
            if (e?.data?.type === "SCASO:ping") {
                window.postMessage({ type: "SCASO:enabled", version: "1.1.0" }, "*");
            }
        });

        // Also proctively announce we're alive (covers race where ping comes early/late)
        window.postMessage({ type: "SCASO:enabled", version: "1.1.0" }, "*");
    } catch (e) {
        // Not fatal if this fails; badge will just show “injected (no ack)”
        err("Handshake setup failed:", e);
    }

    log("Injected at", new Date().toISOString()); // Debugging info

    /**
     * Example function that modifies a Songsterr profile/state object.
     * In this case, it force-sets fields related to subscription/membership.
     *
     * NOTE: This is just a demo of object patching logic, there might be other things I don't know about :D
     */
    const patchProfile = (obj) => {
        if (!obj || typeof obj !== "object") return obj;

        // Overwrite top-level membership flags
        Object.assign(obj, {
            plan: "plus",
            hasPlus: true,
            sra_license: "plus",
            isLoggedIn: true,
        });

        // If a nested `user` object exists, patch it too
        if (obj.user) {
            Object.assign(obj.user, {
                hasPlus: true,
                isLoggedIn: true,
            });

            // If user.profile exists, overwrite subscription plan/license
            if (obj.user.profile) {
                Object.assign(obj.user.profile, {
                    plan: "plus",
                    sra_license: "plus"
                });
            }
        }
        return obj;
    };

    // ---------- Intercept Fetch API ----------
    // Wrap native fetch to intercept requests to `/auth/profile`
    const nativeFetch = window.fetch;
    window.fetch = async (...args) => {
        const res = await nativeFetch(...args);
        const url = String(args[0] ?? "");

        if (url.includes("/auth/profile")) {
            try {
                log("Intercepted fetch:", url);
                const data = await res.clone().json();
                const patched = patchProfile(data);

                // Return a modified Response object with patched JSON
                return new Response(JSON.stringify(patched), {
                    status: res.status,
                    statusText: res.statusText,
                    headers: res.headers
                });
            } catch (e) {
                err("Fetch interception failed:", e);
            }
        }
        return res;
    };

    // ---------- Intercept XMLHttpRequest ----------
    // Patch open() to store request URL
    const nativeOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (...args) {
        this._targetUrl = args[1];
        return nativeOpen.apply(this, args);
    };

    // Patch send() to modify responses for /auth/profile
    const nativeSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function (...args) {
        this.addEventListener("load", () => {
            if (!this._targetUrl?.includes("/auth/profile")) return;

            try {
                const raw = JSON.parse(this.responseText);
                const patched = patchProfile(raw);
                log("Intercepted XHR:", this._targetUrl);

                // Redefine response & responseText to contain modified JSON
                Object.defineProperty(this, "responseText", { value: JSON.stringify(patched) });
                Object.defineProperty(this, "response", { value: JSON.stringify(patched) });
            } catch (e) {
                err("XHR interception failed:", e);
            }
        });
        return nativeSend.apply(this, args);
    };

    // ---------- Patch Initial Bootstrap State ----------
    document.addEventListener("DOMContentLoaded", () => {
        try {
            // Some Songsterr state is bootstrapped via #state script tag
            const stateScript = document.querySelector("#state");
            if (stateScript) {
                const json = JSON.parse(stateScript.textContent.trim());
                stateScript.textContent = JSON.stringify(patchProfile(json));
                log("Patched bootstrap state");
            }
        } catch (e) {
            err("Failed to patch #state:", e);
        }

        // If tablature element is missing, force rebuild by removing #apptab
        const tablature = document.querySelector("#tablature");
        const appTab = document.querySelector("#apptab");

        if (!tablature && appTab) {
            warn("Tablature missing, forcing rebuild by removing #apptab");
            appTab.remove();
        }
    });
})();
