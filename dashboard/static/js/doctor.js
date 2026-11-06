/* Doctor portal. Every call goes through /api/doctor/* → API Gateway (JWT attached server-side). */
(() => {
  const { api, toast, esc, $, $$, initials, errorMessage, hhmm, time12, money, DAYS, isoDateIn, addDays, prettyDate, withBusy, tagInput, fillTimezones, confirmDialog } = MS;
  const page = $("#page").dataset.page;

  const EP = {
    profile: "doctor/profile",
    types: "doctor/consultation-types",
    patterns: "doctor/availability-patterns",
    blackouts: "doctor/blackout-dates",
    schedule: "doctor/schedule",
  };

 /* ---------- shared ---------- */
  let profilePromise = null;

  const hms = (t) => hhmm(t) + ":00";

  function loadProfile(force) {
    if (force || !profilePromise) {
      profilePromise = api("GET", EP.profile).then((r) => {
        if (!r.ok) return null;

        $("#who-name").textContent =
          "Dr. " + String(r.data.full_name || "").replace(/^Dr\.?\s+/i, "");

        $("#who-avatar").textContent = initials(r.data.full_name);

        showProfilePhoto($("#who-avatar"));

        return r.data;
      }).catch((error) => {
        console.error("Profile loading failed:", error);
        profilePromise = null;
        return null;
      });
    }

    return profilePromise;
  }

  async function showProfilePhoto(element) {
  if (!element) return;

  try {
    const res = await fetch("/doctor-photo", {
      method: "GET",
      credentials: "same-origin"
    });

    if (!res.ok) return;

    const data = await res.json();
    if (!data.photo_url) return;

    // Make the avatar container circular
    element.style.width = "96px";
    element.style.height = "96px";
    element.style.minWidth = "96px";
    element.style.minHeight = "96px";
    element.style.borderRadius = "50%";
    element.style.overflow = "hidden";
    element.style.padding = "0";
    element.style.display = "block";
    element.style.flexShrink = "0";

    // Create the profile image
    const img = document.createElement("img");
    img.src = data.photo_url;
    img.alt = "Doctor profile photo";

    img.style.width = "100%";
    img.style.height = "100%";
    img.style.display = "block";
    img.style.objectFit = "cover";
    img.style.objectPosition = "center";
    img.style.borderRadius = "50%";

    element.replaceChildren(img);

  } catch (error) {
    console.error("Could not load doctor photo:", error);
  }
}

  const statusBadge = (s) => ({
    AVAILABLE: '<span class="badge badge-green">Open</span>',
    BOOKED: '<span class="badge badge-blue">Booked</span>',
    BLOCKED: '<span class="badge badge-grey">Blocked</span>'
  }[s] || `<span class="badge">${esc(s)}</span>`);

  const emptyState = (icon, title, text, action = "") =>
    `<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${icon}</svg><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;

  const ICON_CAL =
    '<rect x="3" y="5" width="18" height="16" rx="2.5"/><path d="M3 10h18M8 3v4M16 3v4"/>';

  const ICON_CLOCK =
    '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>';

  const failBox = (r, retry) =>
    `<div class="card-body"><div class="alert alert-error" role="alert">${esc(errorMessage(r))} ${retry ? '<a href="" onclick="location.reload();return false">Retry</a>' : ""}</div></div>`;

  function slotRows(slots) {
    return slots.map((s) => `
      <div class="tl-row ${s.status === "BOOKED" ? "booked" : s.status === "BLOCKED" ? "blocked" : ""}">
        <div class="tl-time">
          ${time12(s.slot_start.slice(11, 16))}
          <small>to ${time12(s.slot_end.slice(11, 16))}</small>
        </div>
        <div class="tl-body">
          <strong>${esc(s.consultation_type)}</strong>
          <span>Slot #${esc(s.slot_id)}</span>
        </div>
        ${statusBadge(s.status)}
      </div>
    `).join("");
  }

  /* ---------- Overview ---------- */
  const pages = {};
  pages.overview = async () => {
    const prof = await loadProfile();
    const tz = prof?.timezone || "UTC";
    $("#greeting").textContent = prof ? `Good ${new Date().getHours() < 12 ? "morning" : new Date().getHours() < 18 ? "afternoon" : "evening"}, Dr. ${String(prof.full_name).replace(/^Dr\.?\s+/i, "").split(" ").slice(-1)[0]}` : "Welcome back";
    const today = isoDateIn(tz);
    // Sequential on purpose: keeps token refresh simple and the API calm.
    const types = await api("GET", EP.types);
    const pats = await api("GET", EP.patterns);
    const bos = await api("GET", EP.blackouts);
    const sched = await api("GET", EP.schedule, undefined, { date: today });

    const typeItems = types.ok ? types.data.items || [] : [];
    const patItems = pats.ok ? (pats.data.items || []).filter((p) => p.is_active) : [];
    $("#st-types").textContent = types.ok ? typeItems.filter((t) => t.is_active).length : "–";
    $("#st-patterns").textContent = pats.ok ? patItems.length : "–";

    if (sched.ok) {
      const slots = sched.data.slots || [];
      $("#st-today").textContent = slots.length;
      $("#st-booked").textContent = slots.filter((s) => s.status === "BOOKED").length;
      $("#today-sub").textContent = prettyDate(today, { weekday: "long", day: "numeric", month: "long", year: undefined });
      $("#today-list").innerHTML = slots.length ? slotRows(slots)
        : emptyState(ICON_CAL, "No slots today", "Nothing is scheduled for today. Slots appear once your weekly hours are set up.");
    } else {
      $("#st-today").textContent = "–";
      $("#today-sub").textContent = "";
      $("#today-list").innerHTML = failBox(sched, true);
    }

    const steps = [
      { done: !!prof, label: "Complete your professional profile", href: "/doctor/profile" },
      { done: typeItems.some((t) => t.is_active), label: "Add a consultation type", href: "/doctor/consultations" },
      { done: patItems.length > 0, label: "Publish your weekly availability", href: "/doctor/availability" },
    ];
    $("#checklist").innerHTML = steps.map((s) => `<li class="${s.done ? "done" : ""}"><span class="dot">${s.done ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>' : ""}</span>${esc(s.label)}${s.done ? "" : `<a href="${s.href}">Do this</a>`}</li>`).join("");
  };

  /* ---------- Profile ---------- */
  pages.profile = async () => {
    const form = $("#profile-form"), body = $("#profile-body");
    const p = await loadProfile();
    if (!p) { body.innerHTML = failBox({ data: { message: "We couldn't load your profile." } }, true); return; }
    body.innerHTML = ""; body.appendChild($("#profile-tpl").content.cloneNode(true));
    const f = (n) => $(`[name=${n}]`, form);
    const quals = tagInput($("#f-qual"), p.qualifications || []);
    const langs = tagInput($("#f-lang"), p.languages_spoken || []);
    fillTimezones($("#tz-list"));
    $("#p-avatar").textContent = initials(p.full_name);
    showProfilePhoto($("#p-avatar"));
    $("#p-name").textContent = p.full_name;
    if (p.photo_key) $("#p-photo-note").textContent = "A photo is saved on your profile. Upload a new one to replace it.";
    f("licence_number").value = p.licence_number || "";
    f("specialization").value = p.specialization || "";
    f("experience_years").value = p.experience_years ?? "";
    f("consultation_fee").value = p.consultation_fee ?? "";
    f("hospital_name").value = p.hospital_name || "";
    f("hospital_address").value = p.hospital_address || "";
    f("timezone").value = p.timezone || "UTC";
    f("bio").value = p.bio || "";

    $("#photo-file").addEventListener("change", async (e) => {
      const file = e.target.files[0]; if (!file) return;
      if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) return toast("Photos must be JPEG, PNG or WebP.", "error");
      if (file.size > 5 * 1024 * 1024) return toast("Photos must be 5 MB or smaller.", "error");
      const fd = new FormData(); fd.append("photo", file);
      $("#p-photo-note").textContent = "Uploading…";
      try {
        const res = await fetch("/doctor-photo", { method: "POST", body: fd, headers: { "X-CSRF-Token": document.querySelector('meta[name=csrf]').content }, credentials: "same-origin" });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
          toast("Photo updated.");
          $("#p-photo-note").textContent =
            "Photo saved. Upload another to replace it.";

          showProfilePhoto($("#p-avatar"));
          showProfilePhoto($("#who-avatar"));
        }
        else { toast(data.message || "Upload failed.", "error"); $("#p-photo-note").textContent = "JPEG, PNG or WebP, up to 5 MB."; }
      } catch (err) { toast("Upload failed. Check your connection.", "error"); }
      e.target.value = "";
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#profile-err"); err.classList.add("hidden");
      const exp = f("experience_years").value, fee = f("consultation_fee").value;
      const q = quals.get(), l = langs.get();
      const problems = [];
      if (!q.length) problems.push("Add at least one qualification.");
      if (!l.length) problems.push("Add at least one language.");
      if (!/^\d+$/.test(exp) || +exp > 80) problems.push("Experience must be a whole number from 0 to 80.");
      if (fee !== "" && !/^\d+(\.\d{1,2})?$/.test(fee)) problems.push("Fee must be a positive amount with up to 2 decimals.");
      if (problems.length) { err.textContent = problems.join(" "); err.classList.remove("hidden"); return; }
      const payload = {
        licence_number: f("licence_number").value.trim(), specialization: f("specialization").value.trim(),
        qualifications: q, experience_years: parseInt(exp, 10), hospital_name: f("hospital_name").value.trim(),
        hospital_address: f("hospital_address").value.trim(), languages_spoken: l,
        timezone: f("timezone").value.trim(), bio: f("bio").value.trim() || null,
      };
      if (fee !== "") payload.consultation_fee = parseFloat(fee);
      await withBusy($("button[type=submit]", form), async () => {
        const res = await api("PUT", EP.profile, payload);
        if (res.ok) { toast("Profile saved."); loadProfile(true); }
        else { err.textContent = errorMessage(res, "Could not save your profile."); err.classList.remove("hidden"); }
      });
    });
  };

  /* ---------- Consultation types (fixed catalogue: choose what you offer + set a fee) ---------- */
  pages.consultations = async () => {
    const body = $("#types-body");
    let items = [];
    const validFee = (v) => /^(?:0|[1-9]\d{0,7})(?:\.\d{1,2})?$/.test(String(v).trim());

    async function load() {
      body.innerHTML = `<div class="skel-block"><div class="skeleton"></div><div class="skeleton" style="width:75%"></div></div>`;
      const r = await api("GET", EP.types);
      if (!r.ok) { body.innerHTML = failBox(r, true); return; }
      items = r.data.items || [];
      if (!items.length) {
        body.innerHTML = emptyState(ICON_CLOCK, "No consultation types available", "The consultation catalogue is empty. Please contact support.");
        return;
      }
      body.innerHTML = `<div class="consultation-fee-grid">${items.map((t) => {
        const id = esc(t.consultation_type_id);
        const on = Number(t.doctor_is_active) === 1;
        const fee = t.fee ?? "";
        return `<div class="consultation-fee-card">
          <div class="consultation-fee-card-head"><div><h3>${esc(t.name)}</h3><p>${esc(t.duration_minutes)} minutes</p></div></div>
          <label class="check"><input type="checkbox" id="on-${id}" ${on ? "checked" : ""}> <span>I offer this consultation</span></label>
          <div class="field">
            <label for="fee-${id}">Your fee (₹)</label>
            <input class="input fee-input" id="fee-${id}" type="number" min="0" max="99999999.99" step="0.01" placeholder="Enter fee" value="${esc(fee)}">
            <div class="err hidden" id="fee-error-${id}" role="alert"></div>
          </div>
          <button class="btn btn-primary btn-sm" type="button" data-save-fee="${id}">Save</button>
        </div>`;
      }).join("")}</div>`;
    }

    body.addEventListener("click", async (e) => {
      const button = e.target.closest("[data-save-fee]");
      if (!button) return;
      const id = button.dataset.saveFee;
      const input = $(`#fee-${CSS.escape(id)}`, body);
      const error = $(`#fee-error-${CSS.escape(id)}`, body);
      const offered = $(`#on-${CSS.escape(id)}`, body).checked;
      let fee = input.value.trim();
      error.classList.add("hidden"); error.textContent = "";
      if (offered && !fee) error.textContent = "Enter your fee to offer this consultation.";
      else if (fee && !validFee(fee)) error.textContent = "Enter a non-negative fee with up to 2 decimal places.";
      if (error.textContent) { error.classList.remove("hidden"); input.focus(); return; }
      if (!fee) fee = "0";
      await withBusy(button, async () => {
        const r = await api("PATCH", `${EP.types}/${encodeURIComponent(id)}`, { fee, is_active: offered });
        if (r.ok) { toast(offered ? "Saved. Patients can book this consultation." : "Saved. This consultation is hidden from patients."); await load(); }
        else { error.textContent = errorMessage(r, "Could not save this consultation."); error.classList.remove("hidden"); }
      });
    });

    await load();
  };

  /* ---------- Availability ---------- */
  pages.availability = async () => {
    const body = $("#week-body"), dlg = $("#pat-dlg"), form = $("#pat-form");
    const prof = await loadProfile();
    const tz = prof?.timezone || "UTC";
    $("#tz-note").textContent = `Times are in ${tz}. Change this on your profile.`;
    let items = [], editing = null;
    $("#pat-days").innerHTML = DAYS.map((d, i) => `<label><input type="checkbox" value="${i}"><span>${d.slice(0, 3)}</span></label>`).join("");

    async function load() {
      const r = await api("GET", EP.patterns);
      if (!r.ok) { body.innerHTML = failBox(r, true); return; }
      items = r.data.items || [];
      body.innerHTML = `<div class="weekgrid">${DAYS.map((d, i) => {
        const list = items.filter((p) => p.day_of_week === i);
        return `<div class="weekcol"><h3>${d}</h3>${list.length ? list.map((p) => `<div class="pattern ${p.is_active ? "" : "off"}">${time12(p.start_time)} – ${time12(p.end_time)}${p.is_active ? "" : " (paused)"}<small>
          <button data-act="toggle" data-id="${p.pattern_id}">${p.is_active ? "Pause" : "Resume"}</button><button data-act="edit" data-id="${p.pattern_id}">Edit</button><button data-act="delete" data-id="${p.pattern_id}" aria-label="Delete ${d} ${time12(p.start_time)}">Delete</button></small></div>`).join("") : '<span class="none">Not working</span>'}</div>`;
      }).join("")}</div>`;
    }

    function open(p) {
      editing = p || null;
      $("#pat-title").textContent = p ? `Edit ${DAYS[p.day_of_week]} hours` : "Add hours";
      $$("#pat-days input").forEach((c) => { c.checked = p ? +c.value === p.day_of_week : false; c.disabled = !!p; });
      $("#pat-start").value = p ? hhmm(p.start_time) : "09:00"; $("#pat-end").value = p ? hhmm(p.end_time) : "17:00";
      $("#pat-err").classList.add("hidden"); dlg.showModal();
    }
    $("#add-pattern").onclick = () => open();
    $("#pat-cancel").onclick = () => dlg.close();

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#pat-err"); err.classList.add("hidden");
      const days = editing ? [editing.day_of_week] : $$("#pat-days input:checked").map((c) => +c.value);
      const start = $("#pat-start").value, end = $("#pat-end").value;
      if (!days.length) { err.textContent = "Choose at least one day."; err.classList.remove("hidden"); return; }
      if (!start || !end || start >= end) { err.textContent = "The end time must be after the start time."; err.classList.remove("hidden"); return; }
      await withBusy($("button[type=submit]", form), async () => {
        const base = { start_time: start + ":00", end_time: end + ":00" };
        let failed = null;
        if (editing) {
          const res = await api("PATCH", `${EP.patterns}/${editing.pattern_id}`, base); // base already zero-padded
          if (!res.ok) failed = res;
        } else {
          for (const d of days) {
            const res = await api("POST", EP.patterns, { ...base, day_of_week: d, timezone: tz, is_active: true });
            if (!res.ok) { failed = res; break; }
          }
        }
        if (failed) { err.textContent = errorMessage(failed); err.classList.remove("hidden"); load(); return; }
        dlg.close(); toast(editing ? "Hours updated." : "Availability added."); load();
      });
    });

    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-act]"); if (!b) return;
      const p = items.find((x) => String(x.pattern_id) === b.dataset.id); if (!p) return;
      if (b.dataset.act === "edit") return open(p);
      if (b.dataset.act === "toggle") {
        const res = await api("PATCH", `${EP.patterns}/${p.pattern_id}`, { start_time: hms(p.start_time), end_time: hms(p.end_time), is_active: !p.is_active });
        res.ok ? (toast(p.is_active ? "Hours paused." : "Hours resumed."), load()) : toast(errorMessage(res), "error");
      }
      if (b.dataset.act === "delete" && await confirmDialog("Remove these hours?", `${DAYS[p.day_of_week]}, ${time12(p.start_time)} – ${time12(p.end_time)} will stop repeating weekly.`, "Remove")) {
        const res = await api("DELETE", `${EP.patterns}/${p.pattern_id}`);
        res.ok ? (toast("Hours removed."), load()) : toast(errorMessage(res), "error");
      }
    });
    load();
  };

  /* ---------- Blackout dates ---------- */
  pages.blackouts = async () => {
    const body = $("#bo-body"), form = $("#bo-form"), err = $("#bo-err");
    const prof = await loadProfile();
    const today = isoDateIn(prof?.timezone || "UTC");
    $("#bo-date").min = today;
    let items = [];

    async function load() {
      const r = await api("GET", EP.blackouts);
      if (!r.ok) { body.innerHTML = failBox(r, true); return; }
      items = r.data.blackout_dates || [];
      if (!items.length) { body.innerHTML = emptyState(ICON_CAL, "No blackout dates", "Add a date above when you're away. Patients won't see slots on that day."); return; }
      body.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Reason</th><th></th><th class="actions"><span class="sr-only">Actions</span></th></tr></thead><tbody>${items.map((b) => `
        <tr><td><strong>${esc(prettyDate(b.blackout_date, { weekday: "short", year: "numeric", month: "short", day: "numeric" }))}</strong></td>
        <td>${b.reason ? esc(b.reason) : '<span class="muted">No reason given</span>'}</td>
        <td>${b.blackout_date < today ? '<span class="badge badge-grey">Past</span>' : '<span class="badge badge-amber">Upcoming</span>'}</td>
        <td class="actions"><span class="row-actions"><button class="btn btn-danger btn-sm" data-id="${b.blackout_id}">Remove</button></span></td></tr>`).join("")}</tbody></table></div>`;
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault(); err.classList.add("hidden");
      const date = $("#bo-date").value;
      if (!date) { err.textContent = "Choose a date."; err.classList.remove("hidden"); return; }
      await withBusy($("button[type=submit]", form), async () => {
        const res = await api("POST", EP.blackouts, { blackout_date: date, reason: $("#bo-reason").value.trim() || null });
        if (res.ok) { toast("Blackout date added."); form.reset(); load(); }
        else { err.textContent = res.status === 409 || res.status === 500 ? "That date may already be blocked. Check the list below." : errorMessage(res); err.classList.remove("hidden"); }
      });
    });
    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-id]"); if (!b) return;
      const item = items.find((x) => String(x.blackout_id) === b.dataset.id);
      if (!await confirmDialog("Remove this blackout date?", `Slots can be offered again on ${item ? item.blackout_date : "that day"}.`, "Remove")) return;
      const res = await api("DELETE", `${EP.blackouts}/${b.dataset.id}`);
      res.ok ? (toast("Blackout date removed."), load()) : toast(errorMessage(res), "error");
    });
    load();
  };

  /* ---------- Schedule (day view) ---------- */
  pages.schedule = async () => {
    const prof = await loadProfile();
    const tz = prof?.timezone || "UTC";
    const today = isoDateIn(tz);
    const qs = new URLSearchParams(location.search);
    let day = /^\d{4}-\d{2}-\d{2}$/.test(qs.get("date") || "") ? qs.get("date") : today;
    let token = 0;

    function strip() {
      const start = addDays(day, -((new Date(day + "T00:00:00Z").getUTCDay() + 6) % 7)); // Monday
      $("#weekstrip").innerHTML = Array.from({ length: 7 }, (_, i) => {
        const d = addDays(start, i);
        return `<button type="button" data-d="${d}" aria-pressed="${d === day}" aria-label="${prettyDate(d)}">${DAYS[i].slice(0, 3)}<b>${+d.slice(8)}</b></button>`;
      }).join("");
    }
    async function load() {
      const mine = ++token;
      $("#day-pick").value = day;
      $("#day-title").textContent = day === today ? "Today" : prettyDate(day, { weekday: "long" });
      $("#day-sub").textContent = prettyDate(day, { weekday: undefined }) + ` · ${tz}`;
      strip();
      $("#day-body").innerHTML = '<div class="skel-block"><div class="skeleton"></div><div class="skeleton" style="width:70%"></div><div class="skeleton" style="width:50%"></div></div>';
      const r = await api("GET", EP.schedule, undefined, { date: day });
      if (mine !== token) return;
      if (!r.ok) { $("#day-body").innerHTML = failBox(r, false); return; }
      const slots = r.data.slots || [];
      const booked = slots.filter((s) => s.status === "BOOKED").length;
      $("#day-sub").textContent += ` · ${slots.length} slot${slots.length === 1 ? "" : "s"}, ${booked} booked`;
      $("#day-body").innerHTML = slots.length ? `<div class="timeline">${slotRows(slots)}</div>`
        : emptyState(ICON_CAL, "No slots on this day", "This could be a day off, a blackout date, or a day outside the 30-day booking window.");
    }
    const go = (d) => { day = d; history.replaceState(null, "", "?date=" + d); load(); };
    $("#prev-day").onclick = () => go(addDays(day, -1));
    $("#next-day").onclick = () => go(addDays(day, 1));
    $("#go-today").onclick = () => go(today);
    $("#day-pick").onchange = (e) => e.target.value && go(e.target.value);
    $("#weekstrip").onclick = (e) => { const b = e.target.closest("button[data-d]"); if (b) go(b.dataset.d); };
    load();
  };

  /* ---------- Appointments (booked slots, next 7 days) ---------- */
  pages.appointments = async () => {
    const prof = await loadProfile();
    const tz = prof?.timezone || "UTC";
    const start = isoDateIn(tz);
    const groups = [];
    let firstError = null;
    for (let i = 0; i < 7; i++) {
      const d = addDays(start, i);
      const r = await api("GET", EP.schedule, undefined, { date: d });
      if (!r.ok) { firstError = firstError || r; continue; }
      const booked = (r.data.slots || []).filter((s) => s.status === "BOOKED");
      if (booked.length) groups.push({ d, booked });
    }
    if (!groups.length) {
      $("#appt-body").innerHTML = firstError ? failBox(firstError, true)
        : emptyState(ICON_CAL, "No booked appointments yet", "When patients book your open slots, they'll show up here by day.");
      return;
    }
    $("#appt-body").innerHTML = groups.map((g) => `<div style="padding:12px 24px 4px;font-weight:700;background:var(--mist);border-top:1px solid var(--line)">${esc(prettyDate(g.d, { year: undefined }))}</div><div class="timeline">${slotRows(g.booked)}</div>`).join("");
  };

  /* ---------- Settings ---------- */
  pages.settings = async () => {
    const p = await loadProfile();
    $("#set-name").textContent = p ? p.full_name : "Unavailable";
    $("#set-tz").textContent = p ? p.timezone : "Unavailable";
  };

  loadProfile(); // fills the sidebar name; shared by every page
  (pages[page] || (() => {}))();
})();