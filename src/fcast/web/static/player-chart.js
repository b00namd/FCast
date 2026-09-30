// Renders the 7-day price chart on the player page. Data: {source: [[timestampMs, price], ...]}
(function () {
  const dataElement = document.getElementById("chart-data");
  const canvas = document.getElementById("price-chart");
  if (!dataElement || !canvas || typeof Chart === "undefined") return;

  const series = JSON.parse(dataElement.textContent);
  const colors = { futbin: "#3fa9f5", futnext: "#f39c12", manual: "#2ecc71" };
  const coins = new Intl.NumberFormat("de-DE");
  const time = new Intl.DateTimeFormat("de-DE", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });

  const datasets = Object.entries(series).map(([source, points]) => ({
    label: source,
    data: points.map(([x, y]) => ({ x, y })),
    borderColor: colors[source] || "#9b59b6",
    backgroundColor: colors[source] || "#9b59b6",
    pointRadius: points.length > 60 ? 0 : 2,
    borderWidth: 2,
    tension: 0.2,
  }));

  // Pin the x axis to the data so Chart.js does not pad it to "nice" values.
  const xs = datasets.flatMap((d) => d.data.map((p) => p.x));
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);

  new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      maintainAspectRatio: false,
      parsing: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: {
          type: "linear",
          min: xMin,
          max: xMax,
          ticks: { callback: (value) => time.format(new Date(value)), maxTicksLimit: 7 },
        },
        y: { ticks: { callback: (value) => coins.format(value) } },
      },
      plugins: {
        tooltip: {
          callbacks: {
            title: (items) => time.format(new Date(items[0].parsed.x)),
            label: (item) => `${item.dataset.label}: ${coins.format(item.parsed.y)}`,
          },
        },
      },
    },
  });
})();

// Hour-of-day and weekday profiles: {hours: {h: pct}, weekdays: {d: pct}}
(function () {
  const dataElement = document.getElementById("profile-data");
  if (!dataElement || typeof Chart === "undefined") return;
  const profiles = JSON.parse(dataElement.textContent);
  const weekdayNames = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
  const pct = (value) => `${value > 0 ? "+" : ""}${value.toFixed(1).replace(".", ",")} %`;

  function bars(canvasId, entries, labelFor) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || entries.length === 0) return;
    new Chart(canvas, {
      type: "bar",
      data: {
        labels: entries.map(([key]) => labelFor(key)),
        datasets: [{
          data: entries.map(([, value]) => value),
          backgroundColor: entries.map(([, value]) => (value >= 0 ? "#2ecc71" : "#e74c3c")),
        }],
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (item) => pct(item.parsed.y) } } },
        scales: { y: { ticks: { callback: (value) => pct(Number(value)) } } },
      },
    });
  }

  const byKey = (object) => Object.entries(object).map(([k, v]) => [Number(k), v]).sort((a, b) => a[0] - b[0]);
  bars("hour-chart", byKey(profiles.hours), (hour) => `${hour} h`);
  bars("weekday-chart", byKey(profiles.weekdays), (day) => weekdayNames[day]);
})();
