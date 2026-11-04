
  /* ---------- Consultation fees ---------- */
  pages.consultations = async () => {
    const body = $("#types-body");
    let items = [];

    function validFee(value) {
      return /^(?:0|[1-9]\d{0,7})(?:\.\d{1,2})?$/.test(
        String(value).trim()
      );
    }

    async function load() {
      body.innerHTML = `
        <div class="skel-block">
          <div class="skeleton"></div>
          <div class="skeleton" style="width:75%"></div>
        </div>
      `;

      const r = await api("GET", EP.types);

      if (!r.ok) {
        body.innerHTML = failBox(r, true);
        return;
      }

      items = r.data.items || [];

      if (!items.length) {
        body.innerHTML = emptyState(
          ICON_CLOCK,
          "No consultation types available",
          "Please contact support if the consultation catalogue is unavailable."
        );
        return;
      }

      body.innerHTML = `
        <div class="consultation-fee-grid">
          ${items.map((t) => {
            const id = t.consultation_type_id;
            const fee = t.fee ?? "";

            return `
              <div class="consultation-fee-card">
                <div class="consultation-fee-card-head">
                  <div>
                    <h3>${esc(t.name)}</h3>
                    <p>${esc(t.duration_minutes)} minutes</p>
                  </div>
                </div>

                <div class="field">
                  <label for="fee-${esc(id)}">
                    Your consultation fee (₹)
                  </label>

                  <input
                    class="input fee-input"
                    id="fee-${esc(id)}"
                    type="number"
                    min="0"
                    max="99999999.99"
                    step="0.01"
                    placeholder="Enter fee"
                    value="${esc(fee)}"
                  >

                  <div
                    class="err hidden"
                    id="fee-error-${esc(id)}"
                    role="alert">
                  </div>
                </div>

                <button
                  class="btn btn-primary btn-sm"
                  type="button"
                  data-save-fee="${esc(id)}">
                  Save Fee
                </button>
              </div>
            `;
          }).join("")}
        </div>
      `;
    }

    body.addEventListener("click", async (e) => {
      const button = e.target.closest("[data-save-fee]");
      if (!button) return;

      const id = button.dataset.saveFee;
      const input = $(`#fee-${CSS.escape(id)}`, body);
      const error = $(`#fee-error-${CSS.escape(id)}`, body);
      const fee = input.value.trim();

      error.classList.add("hidden");
      error.textContent = "";

      if (!fee) {
        error.textContent = "Please enter a consultation fee.";
      } else if (!validFee(fee)) {
        error.textContent =
          "Enter a non-negative fee with up to 2 decimal places.";
      }

      if (error.textContent) {
        error.classList.remove("hidden");
        input.focus();
        return;
      }

      await withBusy(button, async () => {
        const r = await api(
          "PATCH",
          `${EP.types}/${encodeURIComponent(id)}`,
          { fee: fee }
        );

        if (r.ok) {
          toast("Consultation fee updated.");
          await load();
        } else {
          error.textContent = errorMessage(
            r,
            "Could not update consultation fee."
          );
          error.classList.remove("hidden");
        }
      });
    });

    await load();
  };
