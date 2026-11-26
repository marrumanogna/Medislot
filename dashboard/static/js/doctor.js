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


  // After any availability-pattern change, rebuild unpublished draft slots from the new hours.
  const regenDrafts = async () => (await api("POST", "doctor/slots", { action: "regenerate", days_ahead: 14 })).ok;

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

  function slotRows(slots, reorder) {
    const drafts = slots.filter((s) => s.status === "DRAFT");
    const moveBtns = (s) => {
      if (!reorder || s.status !== "DRAFT") return "";
      const i = drafts.findIndex((d) => d.slot_id === s.slot_id);
      return `<span style="display:inline-flex;gap:4px;margin-left:8px">
        <button class="btn btn-ghost btn-sm" type="button" data-move="up" data-id="${esc(s.slot_id)}" aria-label="Move up" ${i === 0 ? "disabled" : ""}>↑</button>
        <button class="btn btn-ghost btn-sm" type="button" data-move="down" data-id="${esc(s.slot_id)}" aria-label="Move down" ${i === drafts.length - 1 ? "disabled" : ""}>↓</button></span>`;
    };
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
        <div style="display:flex;align-items:center">${statusBadge(s.status)}${moveBtns(s)}</div>
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
          ${Number(t.max_capacity) > 1 ? `<div class="field"><label for="cap-${id}">Seats per session (2 to ${esc(t.max_capacity)})</label><input class="input" id="cap-${id}" type="number" min="2" max="${esc(t.max_capacity)}" step="1" value="${esc(t.capacity ?? t.max_capacity)}"></div>` : ""}
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
      const capInput = $(`#cap-${CSS.escape(id)}`, body);
      const payload = { fee, is_active: offered };
      if (capInput) {
        const cap = Number(capInput.value);
        if (!Number.isInteger(cap) || cap < 2 || cap > Number(capInput.max)) { error.textContent = `Seats must be a whole number from 2 to ${capInput.max}.`; error.classList.remove("hidden"); return; }
        payload.capacity = cap;
      }
      await withBusy(button, async () => {
        const r = await api("PATCH", `${EP.types}/${encodeURIComponent(id)}`, payload);
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
        await regenDrafts(); dlg.close(); toast(editing ? "Hours updated. Slots updated; booked slots were kept." : "Availability added. Slots updated; booked slots were kept."); load();
      });
    });

    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-act]"); if (!b) return;
      const p = items.find((x) => String(x.pattern_id) === b.dataset.id); if (!p) return;
      if (b.dataset.act === "edit") return open(p);
      if (b.dataset.act === "toggle") {
        const res = await api("PATCH", `${EP.patterns}/${p.pattern_id}`, { start_time: hms(p.start_time), end_time: hms(p.end_time), is_active: !p.is_active });
        res.ok ? (await regenDrafts(), toast(p.is_active ? "Hours paused. Slots updated; booked slots were kept." : "Hours resumed. Slots updated; booked slots were kept."), load()) : toast(errorMessage(res), "error");
      }
      if (b.dataset.act === "delete" && await confirmDialog("Remove these hours?", `${DAYS[p.day_of_week]}, ${time12(p.start_time)} – ${time12(p.end_time)} will stop repeating weekly.`, "Remove")) {
        const res = await api("DELETE", `${EP.patterns}/${p.pattern_id}`);
        res.ok ? (await regenDrafts(), toast("Hours removed. Slots updated; booked slots were kept."), load()) : toast(errorMessage(res), "error");
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

    const conflict = $("#bo-conflict"), addBtn = $("button[type=submit]", form);
    function showConflict(date, slots) {
      addBtn.disabled = slots.length > 0;
      if (!slots.length) { conflict.classList.add("hidden"); conflict.innerHTML = ""; return; }
      conflict.innerHTML = `<div class="alert alert-error" role="alert" style="margin-bottom:12px">Patients are booked on ${esc(prettyDate(date, { weekday: "short", year: "numeric", month: "short", day: "numeric" }))}. Cancel these slots on the Appointments page (every patient is refunded in full), then come back and add the blackout date.</div>
        <div class="table-wrap"><table><thead><tr><th>Time</th><th>Patient(s)</th><th></th></tr></thead><tbody>${slots.map((x) => `
          <tr><td><strong>${esc(x.slot_start)}–${esc(x.slot_end)}</strong></td><td>${x.patients.map(esc).join(", ")}</td>
          <td><a class="btn btn-ghost btn-sm" href="/doctor/appointments">Cancel on Appointments page</a></td></tr>`).join("")}</tbody></table></div>`;
      conflict.classList.remove("hidden");
    }
    async function checkDate() {
      const date = $("#bo-date").value;
      err.classList.add("hidden");
      if (!date) { showConflict("", []); return; }
      const r = await api("GET", EP.blackouts, undefined, { check_date: date });
      if (r.ok && $("#bo-date").value === date) showConflict(date, r.data.booked_slots || []);
    }
    $("#bo-date").addEventListener("change", checkDate);
    window.addEventListener("focus", checkDate);

    form.addEventListener("submit", async (e) => {
      e.preventDefault(); err.classList.add("hidden");
      const date = $("#bo-date").value;
      if (!date) { err.textContent = "Choose a date."; err.classList.remove("hidden"); return; }
      await withBusy(addBtn, async () => {
        const res = await api("POST", EP.blackouts, { blackout_date: date, reason: $("#bo-reason").value.trim() || null });
        if (res.ok) { toast("Blackout date added."); form.reset(); showConflict("", []); load(); }
        else if (res.status === 409 && res.data && res.data.code === "BLACKOUT_HAS_BOOKINGS") showConflict(date, res.data.booked_slots || []);
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
    let daySlots = [];

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
      daySlots = slots;
      const booked = slots.filter((s) => s.status === "BOOKED").length;
      $("#day-sub").textContent += ` · ${slots.length} slot${slots.length === 1 ? "" : "s"}, ${booked} booked`;
      const drafts = slots.filter((s) => s.status === "DRAFT").length;
      const pubDay = $("#pub-day");
      pubDay.classList.toggle("hidden", !drafts);
      pubDay.textContent = `Publish ${drafts} draft slot${drafts === 1 ? "" : "s"}`;
      $("#day-body").innerHTML = (drafts ? '<p style="margin:0 0 12px">These slots are drafts. Publish them so patients can book.</p>' : "")
        + (slots.length ? `<div class="timeline">${slotRows(slots, true)}</div>`
          : emptyState(ICON_CAL, "No slots on this day", "Click Generate slots to create slots from your weekly hours. Days off and blackout dates have none."));
    }
    const go = (d) => { day = d; history.replaceState(null, "", "?date=" + d); load(); };
    $("#prev-day").onclick = () => go(addDays(day, -1));
    $("#next-day").onclick = () => go(addDays(day, 1));
    $("#go-today").onclick = () => go(today);
    $("#day-pick").onchange = (e) => e.target.value && go(e.target.value);
    $("#weekstrip").onclick = (e) => { const b = e.target.closest("button[data-d]"); if (b) go(b.dataset.d); };
    $("#day-body").onclick = async (e) => {
      const b = e.target.closest("button[data-move]");
      if (!b) return;
      const ids = daySlots.filter((s) => s.status === "DRAFT").map((s) => s.slot_id);
      const i = ids.indexOf(Number(b.dataset.id));
      const k = b.dataset.move === "up" ? i - 1 : i + 1;
      if (i < 0 || k < 0 || k >= ids.length) return;
      [ids[i], ids[k]] = [ids[k], ids[i]];
      b.disabled = true;
      const r = await api("PUT", "doctor/slots", { date: day, slot_ids: ids });
      if (!r.ok) toast(errorMessage(r, "Could not reorder slots."), "error");
      load();
    };
    $("#gen-slots").onclick = (e) => withBusy(e.currentTarget, async () => {
      const r = await api("POST", "doctor/slots/generate", { days_ahead: 14 });
      if (!r.ok) { toast(errorMessage(r, "Could not generate slots."), "error"); return; }
      const n = r.data.slots_created || 0;
      if (n) toast(`Created ${n} draft slot${n === 1 ? "" : "s"}. Review and publish them.`);
      else if (r.data.doctors_without_selected_types) toast("Tick at least one consultation type first (Consultation types page).", "error");
      else toast("No new slots. Add weekly hours on the Availability page, or slots already exist.");
      load();
    });
    $("#pub-day").onclick = (e) => withBusy(e.currentTarget, async () => {
      const r = await api("POST", "doctor/slots", { action: "publish", date: day });
      if (r.ok) toast(`Published ${r.data.slots_published || 0} slot(s).`); else toast(errorMessage(r, "Could not publish."), "error");
      load();
    });
    $("#pub-all").onclick = (e) => withBusy(e.currentTarget, async () => {
      const dates = Array.from({ length: 15 }, (_, i) => addDays(today, i));
      const rs = await Promise.all(dates.map((d) => api("POST", "doctor/slots", { action: "publish", date: d })));
      const n = rs.reduce((a, r) => a + (r.ok ? (r.data.slots_published || 0) : 0), 0);
      toast(n ? `Published ${n} slot${n === 1 ? "" : "s"}.` : "No draft slots to publish.");
      load();
    });
    load();
  };

  /* ---------- Appointments: mark checked / no-show ---------- */
  pages.appointments = async () => {
    const body = $("#appt-body");
    const badge = { CONFIRMED: ["badge-blue", "Upcoming"], COMPLETED: ["badge-green", "Patient checked"], NO_SHOW: ["badge-red", "No-show"] };
    let items = [];

    async function load() {
      const r = await api("GET", "doctor/appointments");
      if (!r.ok) { body.innerHTML = failBox(r, true); return; }
      items = (r.data.items || []).filter((a) => a.status === "CONFIRMED");
      if (!items.length) { body.innerHTML = emptyState(ICON_CAL, "No open appointments", "Booked patients appear here. Finished visits move to History."); return; }
      const open = items.filter((a) => a.status === "CONFIRMED").sort((x, y) => x.slot_start.localeCompare(y.slot_start));
      const now = Date.now();
      const row = (a) => {
        const started = new Date(a.slot_start).getTime() <= now;
        const [cls, label] = badge[a.status];
        const act = a.status === "CONFIRMED" && started
          ? `<button class="btn btn-primary btn-sm" data-outcome="COMPLETED" data-id="${a.appointment_id}">Patient checked</button> <button class="btn btn-danger btn-sm" data-outcome="NO_SHOW" data-id="${a.appointment_id}">No-show</button>`
          : (a.status === "CONFIRMED" ? `<button class="btn btn-ghost btn-sm" data-cancel-slot="${esc(a.slot_id)}">Cancel slot</button>` : "");
        return `<tr><td><strong>${esc(prettyDate(a.slot_start.slice(0, 10), { weekday: "short", year: undefined }))}</strong> ${esc(a.slot_start.slice(11, 16))}</td>
          <td>${esc(a.patient_name || "Patient")}</td><td>${esc(a.consultation_type || "")}</td>
          <td><span class="badge ${cls}">${label}</span></td><td class="actions"><span class="row-actions">${act}</span></td></tr>`;
      };
      body.innerHTML = `<div class="table-wrap"><table><thead><tr><th>When</th><th>Patient</th><th>Type</th><th>Status</th><th class="actions"><span class="sr-only">Actions</span></th></tr></thead><tbody>${open.map(row).join("")}</tbody></table></div>`;
    }

    body.addEventListener("click", async (e) => {
      const cs = e.target.closest("button[data-cancel-slot]");
      if (cs) {
        if (!await confirmDialog("Cancel this slot?", "Every patient booked in it is refunded in full and notified.", "Cancel slot")) return;
        await withBusy(cs, async () => {
          const r = await api("POST", `doctor/slots/${encodeURIComponent(cs.dataset.cancelSlot)}/cancel`, {});
          if (r.ok) { toast(`Slot cancelled. ${r.data.cancelled_appointments} patient(s) refunded in full.`); await load(); }
          else toast(errorMessage(r, "Could not cancel the slot."), "error");
        });
        return;
      }
      const b = e.target.closest("button[data-outcome]"); if (!b) return;
      const noShow = b.dataset.outcome === "NO_SHOW";
      if (!await confirmDialog(noShow ? "Mark as no-show?" : "Mark patient as checked?", noShow ? "The patient did not attend. No refund is issued." : "The visit is recorded as completed.", noShow ? "No-show" : "Confirm")) return;
      await withBusy(b, async () => {
        const r = await api("POST", `doctor/appointments/${encodeURIComponent(b.dataset.id)}/outcome`, { outcome: b.dataset.outcome });
        if (r.ok) { toast(noShow ? "Marked as no-show." : "Marked as checked."); await load(); }
        else toast(errorMessage(r, "Could not update the appointment."), "error");
      });
    });
    load();
  };

  /* ---------- History and Refunds (separate pages) ---------- */
  const CLOSED = { COMPLETED: ["badge-green", "Patient checked"], NO_SHOW: ["badge-red", "No-show"],
    CANCELLED_BY_PATIENT: ["badge-grey", "Cancelled by patient"], CANCELLED_BY_DOCTOR: ["badge-amber", "Cancelled by you"] };
  const whenCell = (a) => `<strong>${esc(prettyDate(a.slot_start.slice(0, 10), { weekday: "short", year: "numeric" }))}</strong> ${esc(a.slot_start.slice(11, 16))}`;
  async function closedAppointments(body) {
    const r = await api("GET", "doctor/appointments");
    if (!r.ok) { body.innerHTML = failBox(r, true); return null; }
    return (r.data.items || []).filter((a) => CLOSED[a.status]).sort((x, y) => y.slot_start.localeCompare(x.slot_start));
  }
  pages.history = async () => {
    const body = $("#hist-body"), items = await closedAppointments(body); if (!items) return;
    if (!items.length) { body.innerHTML = emptyState(ICON_CAL, "No history yet", "Completed, no-show and cancelled appointments appear here."); return; }
    body.innerHTML = `<div class="table-wrap"><table><thead><tr><th>When</th><th>Patient</th><th>Type</th><th>Outcome</th></tr></thead><tbody>${items.map((a) => `
      <tr><td>${whenCell(a)}</td><td>${esc(a.patient_name || "Patient")}</td><td>${esc(a.consultation_type || "")}</td>
      <td><span class="badge ${CLOSED[a.status][0]}">${CLOSED[a.status][1]}</span></td></tr>`).join("")}</tbody></table></div>`;
  };
  pages.refunds = async () => {
    const body = $("#ref-body"), all = await closedAppointments(body); if (!all) return;
    const items = all.filter((a) => a.refund);
    if (!items.length) { body.innerHTML = emptyState(ICON_CAL, "No refunds yet", "Refunds for cancelled appointments appear here."); return; }
    body.innerHTML = `<div class="table-wrap"><table><thead><tr><th>When</th><th>Patient</th><th>Fee</th><th>Refunded</th><th>Reason</th></tr></thead><tbody>${items.map((a) => `
      <tr><td>${whenCell(a)}</td><td>${esc(a.patient_name || "Patient")}</td><td>${MS.money(a.fee)}</td>
      <td><strong>${MS.money(a.refund.amount)}</strong> (${esc(a.refund.percent)}%)</td>
      <td>${esc(a.cancel_reason || CLOSED[a.status][1])}</td></tr>`).join("")}</tbody></table></div>`;
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