/* Helpers the settings, people and setup scripts share: escaping text into markup, calling the
   site's JSON API so that a refusal arrives as an Error carrying the server's own words, and
   mounting a panel again without doubling its listeners. */
(function () {
  "use strict";

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /** api("PUT", "/api/site/settings", {...}); FormData goes as it is, anything else as JSON. */
  async function api(method, url, body) {
    const options = { method, headers: {} };
    if (body instanceof FormData) options.body = body;
    else if (body !== undefined && body !== null) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.detail || `${response.status} ${response.statusText}`);
      error.status = response.status;
      error.code = data.code;
      throw error;
    }
    return data;
  }

  // A panel mounted again into the same container (Settings reopens it) first ends the listeners its
  // last mount attached, which would otherwise answer each click once per opening: duplicate saves.
  const mounted = new WeakMap();

  /** const signal = remount(container); container.addEventListener("click", fn, { signal }); */
  function remount(container) {
    mounted.get(container)?.abort();
    const controller = new AbortController();
    mounted.set(container, controller);
    return controller.signal;
  }

  window.pageKit = { esc, api, remount };
})();
