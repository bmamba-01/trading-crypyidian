(() => {
  const bundleSelect = document.getElementById("bundle-hash");
  const intervalSelect = document.getElementById("execution-interval");
  const bundleHelp = document.getElementById("bundle-hash-help");
  if (!(bundleSelect instanceof HTMLSelectElement) || !(intervalSelect instanceof HTMLSelectElement)) {
    return;
  }

  const updateIntervals = () => {
    const selected = bundleSelect.selectedOptions.item(0);
    if (bundleHelp && selected?.dataset.summary) {
      bundleHelp.textContent = selected.dataset.summary;
    }
    let intervals = [];
    try {
      const parsed = JSON.parse(selected?.dataset.intervals || "[]");
      if (Array.isArray(parsed)) {
        intervals = parsed.filter(
          (value) => typeof value === "string" && /^[0-9]+[smhdwM]$/.test(value),
        );
      }
    } catch {
      intervals = [];
    }

    const previous = intervalSelect.value;
    intervalSelect.replaceChildren(
      ...intervals.map((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        return option;
      }),
    );
    if (intervals.includes(previous)) intervalSelect.value = previous;
    intervalSelect.disabled = intervals.length === 0;
  };

  bundleSelect.addEventListener("change", updateIntervals);
  updateIntervals();
})();
