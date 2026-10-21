/* The Team tab's People and Invite cards: who has an account, what they may do, and the join codes
   that let someone create their own. The server applies the permission matrix; a button the
   viewer may not use is simply not drawn, and a refusal is shown as the server words it. */
(function () {
  "use strict";

  const { esc, api } = window.pageKit;
  const when = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "—");


  /** What the viewer may do to this person: the matrix in docs/specs/M2, mirrored for the buttons. */
  function allowed(viewer, person) {
    const self = viewer.id === person.id;
    const manage = !self && person.role !== "owner"
      && (viewer.role === "owner" || (viewer.role === "admin" && person.role === "reviewer"));
    return { manage, role: viewer.role === "owner" && !self && person.role !== "owner",
             transfer: viewer.role === "owner" && !self && person.active };
  }

  function peopleCard(viewer, data) {
    const rows = data.people.map((person) => {
      const can = allowed(viewer, person);
      const actions = [];
      if (can.manage) {
        actions.push(person.active
          ? `<button type="button" class="btn btn-secondary btn-small" data-person="${person.id}" data-act="deactivate">Deactivate</button>`
          : `<button type="button" class="btn btn-secondary btn-small" data-person="${person.id}" data-act="reactivate">Reactivate</button>`);
        actions.push(`<button type="button" class="btn btn-secondary btn-small" data-person="${person.id}" data-act="reset">Reset password</button>`);
      }
      if (can.role) {
        actions.push(`<select data-person="${person.id}" data-act="role" aria-label="Role of ${esc(person.display_name)}">
          ${["admin", "reviewer"].map((r) => `<option value="${r}" ${person.role === r ? "selected" : ""}>${esc(term(r))}</option>`).join("")}
        </select>`);
      }
      if (can.transfer) {
        actions.push(`<button type="button" class="btn btn-ghost btn-small" data-person="${person.id}" data-act="transfer"
          data-name="${esc(person.display_name)}">Make ${esc(term("owner", { lower: true }))}</button>`);
      }
      return `<tr class="${person.active ? "" : "is-inactive"}">
        <td class="roster-person">${esc(person.display_name)}<div class="people-username">${esc(person.username)}</div></td>
        <td>${esc(term(person.role))}${person.must_change_password ? ' <span class="people-flag">must change password</span>' : ""}</td>
        <td>${person.active ? "Active" : "Deactivated"}</td>
        <td class="roster-when">${esc(when(person.last_login_at))}</td>
        <td class="roster-person">${esc(person.created_by || "—")}</td>
        <td class="people-actions">${actions.join("")}</td>
      </tr>`;
    }).join("");
    return `
      <section class="team-card" aria-labelledby="people-title">
        <header class="team-card-head">
          <h3 id="people-title">People</h3>
          <span class="team-card-hint">Everyone with an account here</span>
        </header>
        <div class="people-status pipeline-status" hidden></div>
        <div class="team-table-scroll">
          <table class="roster"><thead><tr>
            <th>Name</th><th>Role</th><th>Status</th><th>Last signed in</th><th>Added by</th>
            <th class="roster-actions-head" aria-label="Actions"></th>
          </tr></thead><tbody>${rows}</tbody></table>
        </div>
        <form class="assign-card people-add" data-add-person>
          <h3 class="assign-card-title">Add someone</h3>
          <div class="assign-fields">
            <label class="assign-field"><span>Full name</span><input name="display_name" required /></label>
            <label class="assign-field"><span>Username</span><input name="username" required autocomplete="off" /></label>
            <label class="assign-field"><span>Temporary password</span><input name="password" required minlength="8" autocomplete="new-password" /></label>
            <label class="assign-field"><span>Role</span><select name="role">
              ${data.may_add.map((r) => `<option value="${r}" ${r === "reviewer" ? "selected" : ""}>${esc(term(r))}</option>`).join("")}
            </select></label>
            <button class="btn btn-accept" type="submit">Add</button>
          </div>
          <p class="setting-note">They choose their own password the first time they sign in.</p>
        </form>
      </section>`;
  }

  function inviteCard(viewer, codes, mayAdd) {
    const rows = codes.map((c) => `<tr class="${c.usable ? "" : "is-inactive"}">
        <td>${esc(term(c.role))}</td><td>${esc(c.label || "—")}</td>
        <td>${c.uses}${c.max_uses ? ` of ${c.max_uses}` : ""}</td>
        <td class="roster-when">${c.expires_at ? esc(when(c.expires_at)) : "Never"}</td>
        <td>${c.usable ? "Usable" : "Spent or revoked"}</td>
        <td class="people-actions">${c.usable ? `<button type="button" class="btn btn-ghost btn-small" data-revoke="${c.id}">Revoke</button>` : ""}</td>
      </tr>`).join("");
    return `
      <section class="team-card" aria-labelledby="invite-title">
        <header class="team-card-head">
          <h3 id="invite-title">Invite</h3>
          <span class="team-card-hint">A join code lets someone create their own account from the sign-in page</span>
        </header>
        <form class="assign-card" data-add-code>
          <div class="assign-fields">
            <label class="assign-field"><span>Joins as</span><select name="role">
              ${mayAdd.map((r) => `<option value="${r}" ${r === "reviewer" ? "selected" : ""}>${esc(term(r))}</option>`).join("")}
            </select></label>
            <label class="assign-field"><span>Label <em>optional</em></span><input name="label" maxlength="80" placeholder="Spring volunteers" /></label>
            <label class="assign-field"><span>Days valid</span><input name="expires_days" type="number" min="1" max="365" value="14" /></label>
            <label class="assign-field"><span>Uses <em>blank: any</em></span><input name="max_uses" type="number" min="1" max="1000" /></label>
            <button class="btn btn-accept" type="submit">Make a code</button>
          </div>
        </form>
        <div class="new-code" hidden></div>
        ${codes.length ? `<div class="team-table-scroll"><table class="roster"><thead><tr>
          <th>Role</th><th>Label</th><th>Used</th><th>Expires</th><th>State</th><th aria-label="Actions"></th>
        </tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      </section>`;
  }

  async function mount(host, viewer) {
    const [data, codes] = await Promise.all([api("GET", "/api/team/users"), api("GET", "/api/team/join-codes")]);
    host.innerHTML = peopleCard(viewer, data) + inviteCard(viewer, codes.codes, data.may_add);
    const say = (message, isError) => {
      const box = host.querySelector(".people-status");
      box.textContent = message;
      box.classList.toggle("is-error", !!isError);
      box.hidden = !message;
    };
    const again = () => mount(host, viewer).catch((error) => say(error.message, true));

    host.querySelectorAll("button[data-act]").forEach((button) => button.addEventListener("click", async () => {
      const id = Number(button.dataset.person);
      try {
        if (button.dataset.act === "reset") {
          const temporary = window.prompt("A temporary password for them (at least 8 characters). "
            + "They will choose their own at their next sign-in, and every session they have open ends now.");
          if (!temporary) return;
          await api("POST", `/api/team/users/${id}/reset-password`, { password: temporary });
        } else if (button.dataset.act === "transfer") {
          if (!window.confirm(`Make ${button.dataset.name} the ${term("owner", { lower: true })}? `
            + `You become an ${term("admin", { lower: true })}, and only they can undo this.`)) return;
          await api("POST", "/api/team/owner/transfer", { user_id: id });
          window.location.reload();
          return;
        } else {
          await api("POST", `/api/team/users/${id}/${button.dataset.act}`);
        }
        await again();
      } catch (error) { say(error.message, true); }
    }));
    host.querySelectorAll('select[data-act="role"]').forEach((select) => select.addEventListener("change", async () => {
      try {
        await api("POST", `/api/team/users/${select.dataset.person}/role`, { role: select.value });
        await again();
      } catch (error) { say(error.message, true); }
    }));
    host.querySelector("[data-add-person]").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = Object.fromEntries(new FormData(event.target));
      try {
        await api("POST", "/api/team/users", form);
        await again();
      } catch (error) { say(error.message, true); }
    });
    host.querySelector("[data-add-code]").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = Object.fromEntries(new FormData(event.target));
      try {
        const made = await api("POST", "/api/team/join-codes", {
          role: form.role, label: form.label, expires_days: form.expires_days || null, max_uses: form.max_uses || null,
        });
        await again();
        const shown = host.querySelector(".new-code");
        shown.hidden = false;
        shown.innerHTML = `<p>The code, shown this once: <code class="join-code">${esc(made.code)}</code>
          <button type="button" class="btn btn-secondary btn-small" data-copy>Copy</button></p>
          <p class="setting-note">Send it with the sign-in link: <code>${esc(location.origin)}/login?join=1</code></p>`;
        shown.querySelector("[data-copy]").addEventListener("click", () => navigator.clipboard?.writeText(made.code));
      } catch (error) { say(error.message, true); }
    });
    host.querySelectorAll("[data-revoke]").forEach((button) => button.addEventListener("click", async () => {
      if (!window.confirm("Revoke this join code? Nobody can create an account with it afterwards.")) return;
      try {
        await api("DELETE", `/api/team/join-codes/${button.dataset.revoke}`);
        await again();
      } catch (error) { say(error.message, true); }
    }));
  }

  window.PeopleCards = { mount };
})();
