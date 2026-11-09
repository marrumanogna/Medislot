/* Patient portal (Milestone 5: booking, cancellation and refunds on top of search).
 * Live endpoints: patient/profile, doctors, doctors/filters, doctors/{id}, doctors/{id}/slots.
 * Milestone 5: POST appointments, GET appointments, POST appointments/{id}/cancel, GET patient/refunds.
 */
(() => {
  const { api, toast, esc, $, errorMessage, time12, money, isoDateIn, addDays, prettyDate, withBusy, initials } = MS;
  const page = $("#page").dataset.page;
  const EP = { doctors: "doctors", appointments: "appointments", profile: "patient/profile", refunds: "patient/refunds" };

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
  const isOpen = (a) => ["CONFIRMED", "BOOKED"].includes(String(a.status || "CONFIRMED"));
  const isUpcoming = (a) => isOpen(a) && (!startOf(a) || new Date(startOf(a)).getTime() >= Date.now());
  const refundLine = (a) => (a.refund ? `<small class="muted">Refund: ${esc(money(a.refund.amount))} (${esc(a.refund.percent)}%)</small>` : "");
  const apptCard = (a) => {
    const status = String(pick(a, "status") || "CONFIRMED");
    const cls = (status === "CONFIRMED" || status === "BOOKED") ? "badge-blue" : status === "COMPLETED" ? "badge-green" : status === "NO_SHOW" ? "badge-amber" : "badge-grey";
    return `<div class="tl-row" style="grid-template-columns:1fr auto"><div class="tl-body"><strong>${esc(pick(a, "doctorName", "doctor_name") || "Appointment")}</strong><span>${esc(fmtWhen(startOf(a)))}${pick(a, "consultationType", "consultation_type") ? " · " + esc(pick(a, "consultationType", "consultation_type")) : ""}</span></div><span style="display:flex;gap:8px;align-items:center"><span class="badge ${cls}">${esc(status.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()))}</span>${isUpcoming(a) ? `<button class="btn btn-ghost btn-sm" type="button" data-cancel="${esc(a.appointment_id)}">Cancel</button>` : ""}</span></div>`
      .replace("</span></div><span", "</span>" + refundLine(a) + "</div><span");
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
    const up = all.filter((a) => isUpcoming(a));
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
    const up = all.filter((a) => isUpcoming(a)).sort((a, b) => String(startOf(a)).localeCompare(String(startOf(b))));
    const past = all.filter((a) => !up.includes(a)).sort((a, b) => String(startOf(b)).localeCompare(String(startOf(a))));
    $("#upcoming").innerHTML = up.length ? `<div class="timeline">${up.map(apptCard).join("")}</div>` : empty(ICON_CAL, "No upcoming appointments", "Book a time with a doctor and it will show up here.", '<a class="btn btn-primary btn-sm" href="/patient/doctors">Find a doctor</a>');
    const render = () => pages.appointments();
    document.body.onclick = async (e) => {
      const b = e.target.closest("button[data-cancel]");
      if (!b || !window.confirm("Cancel this appointment? Your refund depends on how close it is to the start time.")) return;
      await withBusy(b, async () => {
        const c = await api("DELETE", `${EP.appointments}/${encodeURIComponent(b.dataset.cancel)}`);
        if (!c.ok) { toast(errorMessage(c, "Could not cancel."), "error"); return; }
        const rf = c.data.appointment?.refund;
        toast(rf ? `Cancelled. Refund: ${money(rf.amount)} (${rf.percent}%).` : "Cancelled.");
        render();
      });
    };
    const rr = await api("GET", EP.refunds);
    if ($("#refunds")) $("#refunds").innerHTML = !rr.ok ? problem(rr) : (rr.data.items || []).length
      ? `<div class="timeline">${rr.data.items.map((f) => `<div class="tl-row" style="grid-template-columns:1fr auto"><div class="tl-body"><strong>${esc(f.doctor_name || "Appointment")}</strong><span>${esc(fmtWhen(f.slot_start))} · ${esc(f.consultation_type || "")} · ${esc(f.tier.replace(/_/g, " ").toLowerCase())}</span></div><strong>${esc(money(f.amount))}</strong></div>`).join("")}</div><p class="muted" style="margin:10px 24px">Total refunded: ${esc(money(rr.data.total_refunded))}</p>`
      : empty(ICON_CAL, "No refunds yet", "Refunds for cancelled appointments appear here.");
    $("#history").innerHTML = past.length ? `<div class="timeline">${past.map(apptCard).join("")}</div>` : empty(ICON_CAL, "No past appointments", "Completed and cancelled visits are listed here.");
  };

  /* ---------------------------------------------------------------- doctor search */
  pages.doctors = () => {
    const form = $("#search-form"), out = $("#results"), pager = $("#pager"), dlg = $("#doc-dlg");
    const state = { page: 1, docs: [] };
    const PAGE_SIZE = 9;
    const tz = "Asia/Kolkata";
    const fmtDay = (iso) => prettyDate(iso, { year: undefined });
    const seatLabel = (n) => (n > 0 ? `${n} seat${n === 1 ? "" : "s"} free` : "Full");

    // Offer only specialisations / cities that exist (the API matches specialisation exactly).
    (async () => {
      const r = await api("GET", `${EP.doctors}/filters`);
      if (!r.ok) return;
      const spec = $("#q-spec");
      (r.data.specializations || []).forEach((s) => spec.insertAdjacentHTML("beforeend", `<option>${esc(s)}</option>`));
      $("#city-list").innerHTML = (r.data.cities || []).map((c) => `<option value="${esc(c)}"></option>`).join("");
    })();

    const filters = () => Object.fromEntries([...new FormData(form)].map(([k, v]) => [k, String(v).trim()]).filter(([, v]) => v));

    async function search(page = 1) {
      state.page = page;
      await withBusy($("button[type=submit]", form), async () => {
        out.innerHTML = '<div class="card"><div class="skel-block"><div class="skeleton"></div><div class="skeleton" style="width:70%"></div></div></div>';
        pager.innerHTML = "";
        const r = await api("GET", EP.doctors, undefined, { ...filters(), page, page_size: PAGE_SIZE });
        if (!r.ok) { out.innerHTML = `<div class="card">${problem(r)}</div>`; return; }
        const { items, total, total_pages: pages_, has_next: hasNext } = r.data;
        state.docs = items;
        if (!items.length) {
          out.innerHTML = `<div class="card">${empty(ICON_SEARCH, "No doctors found", "Try a broader name or hospital, or clear some filters.")}</div>`;
          return;
        }
        out.innerHTML = `<div class="pcards">${items.map((d, i) => {
          const name = d.full_name || "Doctor";
          const fee = d.fee_from != null ? `From ${money(d.fee_from)}` : "Fee on request";
          const next = d.next_available ? `Next free: ${esc(fmtWhen(d.next_available))}` : "No open slots right now";
          return `<article class="pcard"><header><div class="avatar">${esc(initials(name))}</div><div><h3>${esc(name)}</h3><span class="muted">${esc(d.specialization || "")}${d.experience_years ? " · " + esc(d.experience_years) + (+d.experience_years === 1 ? " yr" : " yrs") : ""}</span></div></header>
            <div class="meta"><span>${esc(d.hospital_name || "")}</span><span>${esc(d.hospital_city || "")}</span></div>
            <div class="meta"><span>${next}</span></div>
            <footer><strong>${esc(fee)}</strong><button class="btn btn-ghost btn-sm" type="button" data-i="${i}">View profile &amp; slots</button></footer></article>`;
        }).join("")}</div>`;
        const from = (page - 1) * PAGE_SIZE + 1, to = from + items.length - 1;
        pager.innerHTML = `<span class="muted">Showing ${from}–${to} of ${total}</span>
          <span><button class="btn btn-ghost btn-sm" type="button" data-p="${page - 1}" ${page <= 1 ? "disabled" : ""}>Previous</button>
          <span class="muted" style="margin:0 10px">Page ${page} of ${pages_}</span>
          <button class="btn btn-ghost btn-sm" type="button" data-p="${page + 1}" ${hasNext ? "" : "disabled"}>Next</button></span>`;
      });
    }

    form.addEventListener("submit", (e) => { e.preventDefault(); search(1); });
    $("#clear-filters").addEventListener("click", () => { form.reset(); search(1); });
    pager.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-p]");
      if (b && !b.disabled) { search(+b.dataset.p); window.scrollTo({ top: 0, behavior: "smooth" }); }
    });
    search(1);

    /* ------------------------------------------------------- profile + slot dialog */
    const dlgState = { id: null, from: null, slots: [], chosen: null };
    const body = $("#dd-body");

    const renderProfile = (p) => {
      let html = "";
      if (p.bio) html += `<p>${esc(p.bio)}</p>`;
      const facts = [
        p.qualifications?.length ? ["Qualifications", p.qualifications.join(", ")] : null,
        p.languages_spoken?.length ? ["Languages", p.languages_spoken.join(", ")] : null,
        p.hospital_address ? ["Address", p.hospital_address] : null,
      ].filter(Boolean);
      if (facts.length) html += `<dl class="kv">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
      const types = p.consultation_types || [];
      html += types.length
        ? `<div><strong>Consultation types</strong><ul style="margin:6px 0 0;padding-left:18px">${types.map((t) => `<li>${esc(t.name)} — ${esc(t.duration_minutes)} min, ${money(t.fee)}</li>`).join("")}</ul></div>`
        : `<p class="muted">This doctor hasn't published consultation fees yet.</p>`;
      return html;
    };

    const rangeBar = () => {
      const to = addDays(dlgState.from, 6);
      return `<div class="range-bar"><button class="btn btn-ghost btn-sm" type="button" data-range="-7">← Earlier</button>
        <label class="sr-only" for="slot-from">From date</label>
        <input class="input" id="slot-from" type="date" value="${esc(dlgState.from)}" min="${esc(isoDateIn(tz))}">
        <span class="muted">to ${esc(prettyDate(to, { weekday: undefined, year: undefined }))}</span>
        <button class="btn btn-ghost btn-sm" type="button" data-range="7">Later →</button></div>`;
    };

    const renderSlots = (data) => {
      dlgState.slots = data.items;
      dlgState.chosen = null;
      $("#dd-book").disabled = true;
      $("#dd-pick").textContent = "";
      if (!data.items.length) {
        return empty(ICON_CAL, "No slots in this range", "Try the next week — this doctor has nothing scheduled for these dates.");
      }
      const byDay = {};
      data.items.forEach((s, i) => (byDay[s.date] ||= []).push([s, i]));
      return Object.keys(byDay).sort().map((day) => {
        const d = data.days.find((x) => x.date === day);
        return `<div class="slot-day"><div class="slot-day-head"><strong>${esc(fmtDay(day))}</strong><span class="muted">${esc(d ? seatLabel(d.free_seats) : "")}</span></div>
          <div class="slot-pick" role="group" aria-label="Slots on ${esc(fmtDay(day))}">${byDay[day].map(([s, i]) =>
            `<button type="button" data-s="${i}" aria-pressed="false" ${s.seats_free > 0 ? "" : "disabled"} title="${esc(s.consultation_type)} · ${esc(s.duration_minutes)} min · ${esc(money(s.fee))}">
              <span>${esc(time12(s.slot_start.slice(11, 16)))}</span><small>${esc(seatLabel(s.seats_free))}</small></button>`).join("")}</div></div>`;
      }).join("");
    };

    async function loadSlots() {
      const target = $("#slots-area");
      target.innerHTML = '<div class="skeleton"></div><div class="skeleton" style="width:60%"></div>';
      const r = await api("GET", `${EP.doctors}/${dlgState.id}/slots`, undefined, { from: dlgState.from, to: addDays(dlgState.from, 6) });
      target.innerHTML = r.ok ? renderSlots(r.data) : problem(r);
    }

    out.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-i]"); if (!b) return;
      const d = state.docs[+b.dataset.i];
      dlgState.id = d.doctor_id;
      dlgState.from = isoDateIn(tz);
      $("#dd-name").textContent = d.full_name || "Doctor";
      $("#dd-meta").textContent = [d.specialization, d.hospital_name, d.hospital_city].filter(Boolean).join(" · ");
      body.innerHTML = '<div class="skeleton"></div><div class="skeleton" style="width:60%"></div>';
      $("#dd-book").disabled = true; $("#dd-pick").textContent = "";
      dlg.showModal();
      const prof = await api("GET", `${EP.doctors}/${d.doctor_id}`);
      body.innerHTML = (prof.ok ? renderProfile(prof.data) : problem(prof)) +
        `<div><strong>Available slots</strong><div id="range-host"></div><div id="slots-area" style="margin-top:10px"></div></div>`;
      $("#range-host").innerHTML = rangeBar();
      loadSlots();
    });

    const shift = (days) => {
      const next = addDays(dlgState.from, days);
      dlgState.from = next < isoDateIn(tz) ? isoDateIn(tz) : next;
      $("#range-host").innerHTML = rangeBar();
      loadSlots();
    };
    body.addEventListener("click", (e) => {
      const r = e.target.closest("button[data-range]");
      if (r) return shift(+r.dataset.range);
      const b = e.target.closest("button[data-s]"); if (!b) return;
      dlgState.chosen = dlgState.slots[+b.dataset.s];
      body.querySelectorAll("[data-s]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      const s = dlgState.chosen;
      $("#dd-pick").textContent = `Selected: ${fmtWhen(s.slot_start)} · ${s.consultation_type}`;
      $("#dd-book").disabled = false; $("#dd-book").textContent = "Book this slot"; $("#dd-book").title = "";
    });
    body.addEventListener("change", (e) => {
      if (e.target.id !== "slot-from" || !e.target.value) return;
      dlgState.from = e.target.value < isoDateIn(tz) ? isoDateIn(tz) : e.target.value;
      $("#range-host").innerHTML = rangeBar();
      loadSlots();
    });
    $("#dd-book").onclick = (e) => withBusy(e.currentTarget, async () => {
      const s = dlgState.chosen; if (!s) return;
      // One key per click on this slot: retries of the same click replay the same appointment.
      dlgState.keys ||= {}; const key = (dlgState.keys[s.slot_id] ||= (crypto.randomUUID ? crypto.randomUUID() : `k-${Date.now()}-${Math.random().toString(16).slice(2)}`));
      const r = await api("POST", EP.appointments, { slot_id: s.slot_id, idempotency_key: key });
      if (r.ok) { delete dlgState.keys[s.slot_id]; toast(r.data.idempotent_replay ? "Already booked: showing your original appointment." : "Appointment booked."); }
      else { if (r.status !== 409 || r.data?.code !== "REQUEST_IN_PROGRESS") delete dlgState.keys[s.slot_id]; toast(errorMessage(r, "Could not book this slot."), "error"); }
      loadSlots();
    });
    $("#dd-close").onclick = () => dlg.close();
  };

  /* ---------------------------------------------------------------- patient profile */
  pages.profile = async () => {
    const form = $("#pform"), note = $("#pnote"), err = $("#perr"), submit = $("button[type=submit]", form);
    const f = (n) => $(`[name=${n}]`, form);
    const FIELDS = ["gender", "phone", "city", "address", "emergencyContact"];
    let exists = false;

    function fill(d) {
      f("fullName").value = pick(d, "fullName") || "";
      f("dateOfBirth").value = String(pick(d, "dateOfBirth") || "").slice(0, 10);
      FIELDS.forEach((n) => { f(n).value = pick(d, n) || ""; });
    }
    function mode(profileExists) {
      exists = profileExists;
      submit.firstChild.textContent = exists ? "Save changes" : "Create profile";
      $("#pdelete-card").classList.toggle("hidden", !exists);
      $("#pform-title").textContent = exists ? "Your details" : "Create your profile";
      note.classList.toggle("hidden", exists);
      if (!exists) note.textContent = "You don't have a profile yet. Add your name below to create one.";
    }

    const r = await api("GET", EP.profile);
    if (r.ok) { fill(r.data); mode(true); }
    else if (r.status === 404 && !r.data?.notAvailable) mode(false);
    else { mode(false); note.textContent = errorMessage(r); note.classList.remove("hidden"); }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.classList.add("hidden");
      const payload = { fullName: f("fullName").value.trim() };
      if (!payload.fullName) { err.textContent = "Please enter your name."; err.classList.remove("hidden"); return; }
      payload.dateOfBirth = f("dateOfBirth").value || null;
      FIELDS.forEach((n) => { payload[n] = f(n).value.trim() || null; });
      await withBusy(submit, async () => {
        const res = await api(exists ? "PUT" : "POST", EP.profile, payload);
        if (res.ok) { toast(exists ? "Profile saved." : "Profile created."); fill(res.data); mode(true); }
        else { err.textContent = errorMessage(res); err.classList.remove("hidden"); }
      });
    });

    $("#pdelete").addEventListener("click", async () => {
      if (!(await MS.confirmDialog("Delete your profile?", "Your saved details will be removed. Your login stays, so you can create a new profile later.", "Delete profile"))) return;
      const res = await api("DELETE", EP.profile);
      if (res.ok) { form.reset(); mode(false); toast("Profile deleted."); }
      else toast(errorMessage(res), "error");
    });
  };

  (pages[page] || (() => {}))();
})();
