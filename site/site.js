/* The landing page's behaviour: sections that rise into view, the screen that follows the four steps,
   the estimator, and the newest release read from GitHub. The page reads fully without any of it. */
(function () {
  "use strict";

  const REPOSITORY = "parthaped/incipit";
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Sections rise as they come into view; at rest for readers who prefer less motion.
  const reveals = document.querySelectorAll(".reveal");
  if (still || !("IntersectionObserver" in window)) {
    reveals.forEach((el) => el.classList.add("in"));
  } else {
    const seen = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) { entry.target.classList.add("in"); seen.unobserve(entry.target); }
      });
    }, { rootMargin: "0px 0px -10% 0px" });
    reveals.forEach((el) => seen.observe(el));
  }

  // The four steps: the screen beside them follows whichever step is in the middle of the window.
  const steps = [...document.querySelectorAll(".step")];
  const shots = [...document.querySelectorAll(".stage-shot")];
  function show(screen) {
    steps.forEach((s) => s.classList.toggle("is-on", s.dataset.screen === screen));
    shots.forEach((s) => s.classList.toggle("is-on", s.dataset.for === screen));
  }
  if (steps.length && "IntersectionObserver" in window) {
    const middle = new IntersectionObserver((entries) => {
      entries.forEach((entry) => { if (entry.isIntersecting) show(entry.target.dataset.screen); });
    }, { rootMargin: "-45% 0px -45% 0px" });
    steps.forEach((s) => middle.observe(s));
    show(steps[0].dataset.screen);
  }

  // The estimator answers as its numbers change.
  const form = document.getElementById("estimator");
  const hours = (h) => (h >= 100 ? `${Math.round(h).toLocaleString()} hours` : `${h.toFixed(h >= 10 ? 0 : 1)} hours`);
  function answer() {
    if (!form || !window.Estimator) return;
    const inputs = Object.fromEntries([...form.querySelectorAll("input")].map((i) => [i.name, i.value]));
    const r = window.Estimator.estimate(inputs);
    form.querySelector('[data-out="saved"]').textContent = hours(r.people.savedHours);
    form.querySelector('[data-out="peopleNote"]').textContent =
      `${hours(r.people.byHandHours)} by hand, against ${hours(r.people.withIncipitHours)} reviewing drafts.`;
    form.querySelector('[data-out="machine"]').textContent = hours(r.machine.hours);
    form.querySelector('[data-out="cost"]').textContent = `$${r.cost.dollars.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
  }
  if (form) { form.addEventListener("input", answer); answer(); }

  // The newest release: its version and notes, and the installer itself behind every download button.
  fetch(`https://api.github.com/repos/${REPOSITORY}/releases/latest`, { headers: { Accept: "application/vnd.github+json" } })
    .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
    .then((release) => {
      const version = String(release.tag_name || "").replace(/^v/, "");
      const installer = (release.assets || []).find((a) => /setup\.exe$/i.test(a.name));
      if (installer) document.querySelectorAll("[data-download]").forEach((a) => { a.href = installer.browser_download_url; });
      const date = release.published_at ? new Date(release.published_at).toLocaleDateString(undefined, { dateStyle: "long" }) : "";
      const size = installer ? ` · ${Math.round(installer.size / 1048576)} MB` : "";
      const line = `Version ${version}${date ? ` · ${date}` : ""}${size} · Windows 10 and 11, 64-bit`;
      document.querySelectorAll("[data-version], [data-release]").forEach((el) => { el.textContent = line; });
      const notes = document.querySelector("[data-notes]");
      if (notes && release.body) { notes.querySelector("[data-notes-body]").textContent = release.body; notes.hidden = false; }
    })
    .catch(() => {
      const release = document.querySelector("[data-release]");
      if (release) release.innerHTML = `See the newest release <a href="https://github.com/${REPOSITORY}/releases/latest">on GitHub</a>.`;
    });
})();
