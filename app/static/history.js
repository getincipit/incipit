/* The browser's Back and Forward for a site that never leaves its one page.
 *
 * Each screen's owner (app.js for the queue and documents, workspace.js for Team) reports where
 * the reader is with note(), and registers how to get back there with
 * owns(). Back then asks the screen being left whether it may go (unsaved edits), and hands the
 * route to its owner, which reopens it through the same functions its own buttons use.
 */
(() => {
  "use strict";

  const owners = {};        // area -> { restore(route), leave() }
  let restoring = false;
  let ready = false;

  const same = (a, b) => JSON.stringify(a || null) === JSON.stringify(b || null);
  const part = (v) => (v === null || v === undefined || v === "" ? "~" : encodeURIComponent(String(v)));
  const unpart = (v) => (v === "~" || v === undefined ? null : decodeURIComponent(v));

  // A readable address for each place, so a reload or a pasted link lands on it too.
  function hashOf(route) {
    if (!route) return "";
    if (route.area === "team") return "#team";
    const bits = [route.tab || (window.__tabs__ || [])[0] || "new"];
    if (route.folder !== null && route.folder !== undefined || route.doc) bits.push(part(route.folder));
    if (route.doc) bits.push(part(route.doc));
    return "#" + bits.join("/");
  }

  function routeOf(hash) {
    const bits = (hash || "").replace(/^#/, "").split("/").filter((b) => b !== "");
    if (!bits.length) return null;
    if (bits[0] === "team") return { area: "team" };
    // The tabs app.js was given (window.__tabs__); a slug this reader cannot see is not a route.
    if (!(window.__tabs__ || []).includes(bits[0])) return null;
    const doc = unpart(bits[2]);
    return { area: doc ? "doc" : "queue", tab: bits[0], folder: unpart(bits[1]), doc };
  }

  function areaOwner(route) {
    return owners[route.area === "doc" ? "queue" : route.area];
  }

  /* Called by a screen whenever what it shows changes; `replace` for a move that is not a step,
     such as the next document in the same folder. */
  function note(route, { replace = false } = {}) {
    if (!ready || restoring || !route) return;
    const entry = { app: true, route };
    if (same(history.state?.route, route)) return;
    const url = location.pathname + location.search + hashOf(route);
    try {
      if (replace || !history.state?.app) history.replaceState(entry, "", url);
      else history.pushState(entry, "", url);
    } catch { /* a browser refusing history changes still has a working site */ }
  }

  function owns(area, handlers) { owners[area] = handlers; }

  async function mayLeave(route) {
    for (const owner of Object.values(owners)) {
      try { if (owner.leave && !(await owner.leave(route))) return false; }
      catch (error) {
        console.warn("Could not confirm the current edits are saved", error);
        return false;
      }
    }
    return true;
  }

  async function go(route) {
    const owner = route && areaOwner(route);
    if (!owner) return { ok: false, changed: false };
    restoring = true;
    try {
      if (!(await mayLeave(route))) return { ok: false, changed: false };
      try {
        await owner.restore(route);
        return { ok: true, changed: true };
      } catch (error) {
        console.warn("Could not restore browser history route", error);
        return { ok: false, changed: true };
      }
    } finally {
      restoring = false;
    }
  }

  let shown = null;          // the route the screen is showing, to put back if Back is refused
  const remember = () => { shown = history.state?.route || shown; };

  async function restorePrevious(route) {
    const owner = route && areaOwner(route);
    if (!owner) return;
    restoring = true;
    try { await owner.restore(route); }
    catch (error) { console.error("Could not recover the previous screen", error); }
    finally { restoring = false; }
  }

  // A normal tab click must obey the same unsaved-edit guard as browser Back. Capture it before
  // the independent queue and Team listeners can hide their screens. The replayed
  // click then uses those existing owners' handlers; it does not invent a second navigation path.
  let replayingClick = false, waitingForLeave = false;
  document.addEventListener("click", async (event) => {
    if (restoring || replayingClick) return;
    const tab = event.target.closest("#tab-team, .workflow-tab[data-workflow]");
    if (!tab) return;
    const current = shown || history.state?.route;
    const destination = tab.id === "tab-team" ? "team" : "queue";
    const sameWorkflow = destination === "queue" && (current?.area === "queue" || current?.area === "doc")
      && current.tab === tab.dataset.slug;
    if (!current || current.area === destination || sameWorkflow) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (waitingForLeave) return;
    waitingForLeave = true;
    try {
      if (!(await mayLeave({ area: destination, tab: tab.dataset.slug }))) return;
      replayingClick = true;
      try { tab.click(); }
      finally { replayingClick = false; }
    } finally { waitingForLeave = false; }
  }, true);

  window.addEventListener("popstate", async (event) => {
    let route;
    try { route = event.state?.app ? event.state.route : routeOf(location.hash); }
    catch { return; } // A malformed hand-edited hash is not a site route.
    if (!route) return;
    const previous = shown;
    const result = await go(route);
    if (!result.ok && previous) {
      if (result.changed) await restorePrevious(previous);
      // Refused (or nothing could open it): the address goes back to what is still on screen.
      try { history.pushState({ app: true, route: previous }, "", location.pathname + location.search + hashOf(previous)); }
      catch { /* nothing more to do */ }
    }
    if (result.ok) remember();
  });

  /* app.js calls this once its first screen is drawn: a link or a reload then opens its place. */
  async function start(current) {
    if (ready) return;
    // The Team tab exists only once the role is known, so a link to it waits for that (up to 5 s).
    for (let i = 0; i < 50 && !/\brole-(admin|reviewer)\b/.test(document.body.className); i++) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (ready) return;
    let wanted = null;
    try { wanted = routeOf(location.hash); }
    catch { /* Ignore a malformed hand-edited hash. */ }
    ready = true;
    try { history.replaceState({ app: true, route: current }, "", location.pathname + location.search + hashOf(current)); }
    catch { /* history unavailable */ }
    remember();
    if (wanted && !same(wanted, current)) {
      const result = await go(wanted);
      if (result.ok) {
        try { history.replaceState({ app: true, route: wanted }, "", location.pathname + location.search + hashOf(wanted)); }
        catch { /* history unavailable */ }
        remember();
      } else if (result.changed) await restorePrevious(current);
    }
  }

  const api = { note: (route, options) => { if (!restoring) { note(route, options); remember(); } }, owns, start, hashOf, routeOf,
                get restoring() { return restoring; } };
  window.__route__ = api;
})();
