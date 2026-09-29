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
