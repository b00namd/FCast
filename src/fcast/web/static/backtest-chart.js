// Average price reaction around promo starts and TOTW releases. Data: [{label, points: [[hours, pct], ...]}]
(function () {
  const dataElement = document.getElementById("curve-data");
  const canvas = document.getElementById("curve-chart");
  if (!dataElement || !canvas || typeof Chart === "undefined") return;

  Chart.defaults.color = "#8594a8";
  Chart.defaults.borderColor = "#1f2a37";
  Chart.defaults.font.family = "Inter, system-ui, sans-serif";
  Chart.defaults.font.size = 11;

  const palette = ["#38bdf8", "#fb923c", "#34d399", "#c084fc", "#f87171", "#facc15"];
  const pct = (value) => `${value > 0 ? "+" : ""}${value.toFixed(1).replace(".", ",")} %`;
  const day = (hours) => `T${hours >= 0 ? "+" : ""}${hours / 24} Tg`;

  const datasets = JSON.parse(dataElement.textContent).map((series, i) => ({
    label: series.label,
    data: series.points.map(([x, y]) => ({ x, y })),
    borderColor: palette[i % palette.length],
    backgroundColor: palette[i % palette.length],
    pointRadius: 3,
    borderWidth: 2,
    tension: 0.2,
  }));

  new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      maintainAspectRatio: false,
      parsing: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: { type: "linear", min: -72, max: 48, ticks: { stepSize: 24, callback: day } },
        y: { ticks: { callback: pct } },
      },
      plugins: {
        tooltip: {
          callbacks: {
            title: (items) => day(items[0].parsed.x),
            label: (item) => `${item.dataset.label}: ${pct(item.parsed.y)}`,
          },
        },
      },
    },
  });
})();
