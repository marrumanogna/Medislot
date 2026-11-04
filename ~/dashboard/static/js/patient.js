/* Patient portal.
 * Endpoints follow the project brief's Patient API list. The deployed backend (Milestone 2) does not
 * serve them yet; every page therefore handles "not available" gracefully and starts working as soon
 * as the routes are deployed. Field names are read tolerantly (camelCase or snake_case).
 */
(() => {
  const { api, toast, esc, $, errorMessage, time12, money, isoDateIn, addDays, prettyDate, withBusy, initials } = MS;
  const page = $("#page").dataset.page;
  const EP = { doctors: "doctors", appointments: "appointments", me: "patients/me" };

  const pick = (o, ...keys) => { for (const k of keys) if (o && o[k] != null && o[k] !== "") return o[k]; return null; };
  const listOf = (d) => (Array.isArray(d) ? d : d?.items || d?.doctors || d?.appointments || d?.slots || []);
  const ICON_CAL = '<rect x="3" y="5" width="18" height="16" rx="2.5"/><path d="M3 10h18M8 3v4M16 3v4"/>';
  const ICON_SEARCH = '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>';
  const empty = (icon, title, text, action = "") => `<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${icon}</svg><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
  const unavailable = (what) => empty(ICON_CAL, `${what} isn't switched on yet`, "This part of MediSlot is still being connected on the server. Nothing is wrong with your account — check back soon.");
  const problem = (r) => (r.data?.notAvailable ? unavailable("This feature") : `<div class="card-body"><div class="alert alert-error" role="alert">${esc(errorMessage(r))}</div></div>`);

  const startOf = (a) => pick(a, "slotStart", "slot_start", "startTime", "start_time", "start");
  const fmtWhen = (iso) => {
    if (!iso) return "Time to be confirmed";
    const d = String(iso);
    return `${prettyDate(d.slice(0, 10), { year: undefined })} · ${time12(d.slice(11, 16))}`;
  };
  const apptCard = (a) => {
    const status = String(pick(a, "status") || "CONFIRMED");
    const cls = status === "CONFIRMED" ? "badge-blue" : status === "COMPLETED" ? "badge-green" : status === "NO_SHOW" ? "badge-amber" : "badge-grey";
    return `<div class="tl-row" style="grid-template-columns:1fr auto"><div class="tl-body"><strong>${esc(pick(a, "doctorName", "doctor_name") || "Appointment")}</strong><span>${esc(fmtWhen(startOf(a)))}${pick(a, "consultationType", "consultation_type") ? " · " + esc(pick(a, "consultationType", "consultation_type")) : ""}</span></div><span class="badge ${cls}">${esc(status.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()))}</span></div>`;
  };

  const pages = {};

  pages.overview = async () => {
    const r = await api("GET", EP.appointments);
    if (!r.ok) {
      $("#next-appt").innerHTML = problem(r);
      return;
    }
    const all = listOf(r.data);
    const now = new Date().toISOString().slice(0, 16);
    const up = all.filter((a) => String(a.status || "CONFIRMED") === "CONFIRMED" && String(startOf(a) || "9999") >= now);
    $("#st-up").textContent = up.length;
    $("#st-past").textContent = all.length - up.length;
    up.sort((a, b) => String(startOf(a)).localeCompare(String(startOf(b))));
    $("#next-appt").innerHTML = up.length ? `<div class="timeline">${apptCard(up[0])}</div>` : empty(ICON_CAL, "No upcoming appointments", "Find a doctor and book your first visit.", '<a class="btn btn-primary btn-sm" href="/patient/doctors">Find a doctor</a>');
  };

  pages.appointments = async () => {
    const r = await api("GET", EP.appointments);
    if (!r.ok) { $("#upcoming").innerHTML = problem(r); $("#history").innerHTML = ""; return; }
    const all = listOf(r.data);
    const now = new Date().toISOString().slice(0, 16);
    const up = all.filter((a) => String(a.status || "CONFIRMED") === "CONFIRMED" && String(startOf(a) || "9999") >= now).sort((a, b) => String(startOf(a)).localeCompare(String(startOf(b))));
    const past = all.filter((a) => !up.includes(a)).sort((a, b) => String(startOf(b)).localeCompare(String(startOf(a))));
    $("#upcoming").innerHTML = up.length ? `<div class="timeline">${up.map(apptCard).join("")}</div>` : empty(ICON_CAL, "No upcoming appointments", "Book a time with a doctor and it will show up here.", '<a class="btn btn-primary btn-sm" href="/patient/doctors">Find a doctor</a>');
    $("#history").innerHTML = past.length ? `<div class="timeline">${past.map(apptCard).join("")}</div>` : empty(ICON_CAL, "No past appointments", "Completed and cancelled visits are listed here.");
  };

  pages.doctors = () => {
    const form = $("#search-form"), out = $("#results"), dlg = $("#doc-dlg");
    let chosen = null;

    async function search(e) {
      e?.preventDefault();
      const params = Object.fromEntries(new FormData(form));
      await withBusy($("button[type=submit]", form), async () => {
        out.innerHTML = '<div class="card"><div class="skel-block"><div class="skeleton"></div><div class="skeleton" style="width:70%"></div></div></div>';
        const r = await api("GET", EP.doctors, undefined, params);
        if (!r.ok) { out.innerHTML = `<div class="card">${problem(r)}</div>`; return; }
        const docs = listOf(r.data);
        if (!docs.length) { out.innerHTML = `<div class="card">${empty(ICON_SEARCH, "No doctors found", "Try a broader specialization, or clear the other filters.")}</div>`; return; }
        out.innerHTML = `<div class="pcards">${docs.map((d, i) => {
          const name = pick(d, "fullName", "full_name", "name") || "Doctor";
          return `<article class="pcard"><header><div class="avatar">${esc(initials(name))}</div><div><h3>${esc(name)}</h3><span class="muted">${esc(pick(d, "specialisation", "specialization") || "")}</span></div></header>
            <div class="meta"><span>${esc(pick(d, "hospitalName", "hospital_name") || "")}</span><span>${esc(pick(d, "city") || "")}</span></div>
            <footer><strong>${pick(d, "consultationFee", "consultation_fee") != null ? money(pick(d, "consultationFee", "consultation_fee")) : ""}</strong><button class="btn btn-ghost btn-sm" data-i="${i}">View profile &amp; slots</button></footer></article>`;
        }).join("")}</div>`;
        out._docs = docs;
      });
    }
    form.addEventListener("submit", search);

    out.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-i]"); if (!b) return;
      const d = out._docs[+b.dataset.i];
      const id = pick(d, "doctorId", "doctor_id", "userId", "user_id", "id");
      $("#dd-name").textContent = pick(d, "fullName", "full_name", "name") || "Doctor";
      $("#dd-meta").textContent = [pick(d, "specialisation", "specialization"), pick(d, "hospitalName", "hospital_name")].filter(Boolean).join(" · ");
      $("#dd-body").innerHTML = '<div class="skeleton"></div><div class="skeleton" style="width:60%"></div>';
      $("#dd-book").disabled = true; chosen = null;
      dlg.showModal();
      const from = isoDateIn("UTC"), to = addDays(from, 14);
      const [prof, slots] = [await api("GET", `${EP.doctors}/${id}`), await api("GET", `${EP.doctors}/${id}/slots`, undefined, { from, to })];
      let html = "";
      if (prof.ok) {
        const bio = pick(prof.data, "bio");
        if (bio) html += `<p>${esc(bio)}</p>`;
        const types = prof.data.consultationTypes || prof.data.consultation_types || [];
        if (types.length) html += `<div><strong>Consultation types</strong><ul style="margin:6px 0 0;padding-left:18px">${types.map((t) => `<li>${esc(t.name)} — ${esc(pick(t, "durationMinutes", "duration_minutes"))} min, ${money(t.fee)}</li>`).join("")}</ul></div>`;
      }
      if (!slots.ok) html += problem(slots);
      else {
        const list = listOf(slots.data).filter((s) => String(pick(s, "status") || "AVAILABLE") === "AVAILABLE");
        html += list.length ? `<div><strong>Open slots (next 14 days)</strong><div class="slot-pick" style="margin-top:10px" role="group" aria-label="Open slots">${list.map((s, i) => `<button type="button" aria-pressed="false" data-s="${i}">${esc(fmtWhen(startOf(s)))}</button>`).join("")}</div></div>`
          : empty(ICON_CAL, "No open slots", "This doctor has nothing free in the next 14 days. Check back later.");
        $("#dd-body")._slots = list;
      }
      $("#dd-body").innerHTML = html || '<p class="muted">No further details.</p>';
      $("#dd-body")._slots = slots.ok ? listOf(slots.data).filter((s) => String(pick(s, "status") || "AVAILABLE") === "AVAILABLE") : [];
    });

    $("#dd-body").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-s]"); if (!b) return;
      chosen = $("#dd-body")._slots[+b.dataset.s];
      document.querySelectorAll("#dd-body [data-s]").forEach((x) => x.setAttribute("aria-pressed", x === b));
      $("#dd-book").disabled = false;
    });
    $("#dd-close").onclick = () => dlg.close();
    $("#dd-book").onclick = () => withBusy($("#dd-book"), async () => {
      if (!chosen) return;
      const slotId = pick(chosen, "slotId", "slot_id", "id");
      const key = (crypto.randomUUID && crypto.randomUUID()) || String(Date.now()) + Math.random();
      const body = { slotId };
      const ct = pick(chosen, "consultationTypeId", "consultation_type_id");
      if (ct != null) body.consultationTypeId = ct;
      const r = await api("POST", EP.appointments, body, undefined, { "Idempotency-Key": key });
      if (r.ok) { dlg.close(); toast("Appointment booked."); }
      else if (r.status === 409) toast("That slot was just taken. Pick another time.", "error");
      else toast(errorMessage(r), "error");
    });
  };

  pages.profile = async () => {
    const form = $("#pform"), note = $("#pnote");
    const f = (n) => $(`[name=${n}]`, form);
    const r = await api("GET", EP.me);
    if (r.ok) {
      const d = r.data;
      f("dateOfBirth").value = String(pick(d, "dateOfBirth", "date_of_birth") || "").slice(0, 10);
      f("gender").value = pick(d, "gender") || "";
      f("phone").value = pick(d, "phone") || "";
      f("city").value = pick(d, "city") || "";
      f("address").value = pick(d, "address") || "";
      f("emergencyContact").value = pick(d, "emergencyContact", "emergency_contact") || "";
    } else {
      note.textContent = r.data?.notAvailable ? "Saving your details isn't switched on yet. You can still fill them in, and they'll save once it is." : errorMessage(r);
      note.classList.remove("hidden");
    }
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#perr"); err.classList.add("hidden");
      const payload = {};
      ["dateOfBirth", "gender", "phone", "city", "address", "emergencyContact"].forEach((n) => { payload[n] = f(n).value.trim() || null; });
      await withBusy($("button[type=submit]", form), async () => {
        const res = await api("PUT", EP.me, payload);
        if (res.ok) toast("Profile saved.");
        else { err.textContent = res.data?.notAvailable ? "Saving profiles isn't switched on yet. Please try again later." : errorMessage(res); err.classList.remove("hidden"); }
      });
    });
  };

  (pages[page] || (() => {}))();
})();
