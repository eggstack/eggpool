(() => {
  "use strict";

  const namespace = window.EggPoolDashboard || (window.EggPoolDashboard = {});

  namespace.fetchStats = async function fetchStats(path) {
    const response = await fetch(path, {
      cache: "no-store",
      headers: { "x-dashboard-refresh": "1" },
    });
    if (!response.ok) {
      throw new Error(
        "stats request failed: " + response.status + " " + response.statusText
      );
    }
    return await response.json();
  };

  namespace.formatDurationMs = function formatDurationMs(ms) {
    if (ms === null || ms === undefined || Number.isNaN(Number(ms))) {
      return "—";
    }
    const value = Number(ms);
    if (value < 0) {
      return "—";
    }
    if (value < 1000) {
      return value.toFixed(0) + " ms";
    }
    const seconds = value / 1000;
    if (seconds < 60) {
      return seconds.toFixed(1) + " s";
    }
    const minutesTotal = Math.floor(seconds / 60);
    const secs = Math.floor(seconds - minutesTotal * 60);
    if (minutesTotal < 60) {
      return minutesTotal + "m" + secs + "s";
    }
    const hoursTotal = Math.floor(minutesTotal / 60);
    const mins = minutesTotal - hoursTotal * 60;
    if (hoursTotal < 24) {
      return hoursTotal + "h" + mins + "m";
    }
    const days = Math.floor(hoursTotal / 24);
    const hrs = hoursTotal - days * 24;
    return days + "d" + hrs + "h";
  };

  namespace.formatAgeSeconds = function formatAgeSeconds(seconds) {
    if (
      seconds === null ||
      seconds === undefined ||
      Number.isNaN(Number(seconds))
    ) {
      return "—";
    }
    const value = Number(seconds);
    if (value < 0) {
      return "—";
    }
    if (value < 1) {
      return "<1s";
    }
    if (value < 60) {
      return value.toFixed(0) + "s";
    }
    const minutesTotal = Math.floor(value / 60);
    const secs = Math.floor(value - minutesTotal * 60);
    if (minutesTotal < 60) {
      return minutesTotal + "m" + secs + "s";
    }
    const hoursTotal = Math.floor(minutesTotal / 60);
    const mins = minutesTotal - hoursTotal * 60;
    if (hoursTotal < 24) {
      return hoursTotal + "h" + mins + "m";
    }
    const days = Math.floor(hoursTotal / 24);
    const hrs = hoursTotal - days * 24;
    return days + "d" + hrs + "h";
  };

  namespace.formatPercent = function formatPercent(value, fraction) {
    if (
      value === null ||
      value === undefined ||
      Number.isNaN(Number(value))
    ) {
      return "—";
    }
    const number = Number(value);
    if (fraction === false) {
      return number.toFixed(1) + "%";
    }
    return (number * 100).toFixed(1) + "%";
  };

  namespace.formatCount = function formatCount(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) {
      return "—";
    }
    const value = Number(n);
    const abs = Math.abs(value);
    if (abs < 1000) {
      return value.toFixed(0);
    }
    if (abs < 1_000_000) {
      return (value / 1000).toFixed(1) + "k";
    }
    if (abs < 1_000_000_000) {
      return (value / 1_000_000).toFixed(1) + "M";
    }
    return (value / 1_000_000_000).toFixed(1) + "B";
  };

  namespace.formatBytes = function formatBytes(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "—";
    }
    const bytes = Number(value);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let val = bytes;
    for (let i = 0; i < units.length; i++) {
      if (Math.abs(val) < 1000 || i === units.length - 1) {
        if (units[i] === "B") {
          return val.toFixed(0) + " B";
        }
        return val.toFixed(1) + " " + units[i];
      }
      val /= 1000;
    }
    return val.toFixed(1) + " PB";
  };

  namespace.formatMicrodollars = function formatMicrodollars(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "—";
    }
    return "$" + (Number(value) / 1_000_000).toFixed(6);
  };

  namespace.formatTokens = function formatTokens(tokens) {
    if (
      tokens === null ||
      tokens === undefined ||
      Number.isNaN(Number(tokens))
    ) {
      return "—";
    }
    const value = Number(tokens);
    const abs = Math.abs(value);
    if (abs < 1000) {
      return value.toFixed(0);
    }
    if (abs < 1_000_000) {
      return (value / 1000).toFixed(1) + "k";
    }
    if (abs < 1_000_000_000) {
      return (value / 1_000_000).toFixed(1) + "M";
    }
    if (abs < 1_000_000_000_000) {
      return (value / 1_000_000_000).toFixed(1) + "B";
    }
    return (value / 1_000_000_000_000).toFixed(1) + "T";
  };

  namespace.formatDollarsFromMicro = function formatDollarsFromMicro(microdollars) {
    if (
      microdollars === null ||
      microdollars === undefined ||
      Number.isNaN(Number(microdollars))
    ) {
      return "—";
    }
    return "$" + (Number(microdollars) / 1_000_000).toFixed(6);
  };

  const GROUPED_TIMESERIES_PALETTE = [
    "rgb(75, 192, 192)",
    "rgb(255, 99, 132)",
    "rgb(54, 162, 235)",
    "rgb(255, 206, 86)",
    "rgb(153, 102, 255)",
    "rgb(255, 159, 64)",
    "rgb(199, 199, 199)",
    "rgb(83, 102, 89)",
    "rgb(255, 99, 71)",
    "rgb(144, 238, 144)",
    "rgb(186, 85, 211)",
    "rgb(255, 215, 0)",
  ];

  function metricValue(point, metric) {
    if (!point) {
      return 0;
    }
    switch (metric) {
      case "tokens":
        return Number(point.total_tokens || 0);
      case "cost":
        return Number(point.cost_microdollars || 0) / 1_000_000;
      case "errors":
        return Number(point.error_count || 0);
      case "bytes":
        return (
          Number(point.bytes_received || 0) + Number(point.bytes_emitted || 0)
        );
      case "latency":
        return Number(point.avg_latency_ms || 0);
      case "ttft":
        return Number(point.avg_ttft_ms || 0);
      case "requests":
      default:
        return Number(point.request_count || 0);
    }
  }

  function destroyChartOn(canvas) {
    if (canvas && canvas.__eggpoolChart) {
      try {
        canvas.__eggpoolChart.destroy();
      } catch (_err) {
        /* ignore */
      }
      canvas.__eggpoolChart = null;
    }
  }

  namespace.initGroupedTimeseriesCharts = function initGroupedTimeseriesCharts() {
    if (typeof window.Chart === "undefined") {
      console.warn("EggPoolDashboard: Chart.js not loaded");
      return;
    }
    const canvases = document.querySelectorAll(
      "canvas.grouped-timeseries-chart"
    );
    for (let i = 0; i < canvases.length; i++) {
      const canvas = canvases[i];
      const chartId = canvas.getAttribute("data-chart-id");
      if (!chartId) continue;
      const dataScript = document.querySelector(
        'script.grouped-timeseries-data[data-chart-id="' + chartId + '"]'
      );
      if (!dataScript) continue;
      let payload;
      try {
        payload = JSON.parse(dataScript.textContent || "{}");
      } catch (err) {
        console.error(
          "EggPoolDashboard: failed to parse grouped-timeseries payload",
          err
        );
        continue;
      }
      const metric = canvas.getAttribute("data-metric") || "requests";
      const buckets = Array.isArray(payload.buckets) ? payload.buckets : [];
      const series = Array.isArray(payload.series) ? payload.series : [];
      const points = Array.isArray(payload.points) ? payload.points : [];
      const bucketTotals = Array.isArray(payload.bucket_totals)
        ? payload.bucket_totals
        : [];

      const pointIndex = new Map();
      for (let p = 0; p < points.length; p++) {
        const pt = points[p];
        const key = String(pt.series_key || "") + "::" + String(pt.bucket || "");
        pointIndex.set(key, pt);
      }

      const nonStackedMetric = metric === "latency" || metric === "ttft";
      const datasets = series.map((s, idx) => {
        const key = String(s.key || "");
        const data = buckets.map((b) => {
          const pt = pointIndex.get(key + "::" + String(b || ""));
          return metricValue(pt, metric);
        });
        const color =
          GROUPED_TIMESERIES_PALETTE[
            idx % GROUPED_TIMESERIES_PALETTE.length
          ];
        const dataset = {
          label: String(s.label || key || "series"),
          data: data,
          backgroundColor: color,
          borderColor: color,
        };
        if (!nonStackedMetric) {
          dataset.stack = "usage";
        }
        return dataset;
      });

      destroyChartOn(canvas);
      const totalsByBucket = new Map();
      for (let t = 0; t < bucketTotals.length; t++) {
        const bt = bucketTotals[t];
        totalsByBucket.set(String(bt.bucket || ""), bt);
      }

      const yTitle = (() => {
        switch (metric) {
          case "tokens":
            return "Tokens";
          case "cost":
            return "Cost ($)";
          case "errors":
            return "Errors";
          case "bytes":
            return "Bytes";
          case "latency":
            return "Avg latency (ms)";
          case "ttft":
            return "Avg TTFT (ms)";
          case "requests":
          default:
            return "Requests";
        }
      })();

      canvas.__eggpoolChart = new window.Chart(canvas, {
        type: "bar",
        data: { labels: buckets, datasets: datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { position: "bottom" },
            tooltip: {
              callbacks: {
                title: function (items) {
                  if (!items || !items.length) return "";
                  const idx = items[0].dataIndex;
                  return String(buckets[idx] || "");
                },
                label: function (item) {
                  const label = item.dataset.label || "";
                  const value = item.parsed && item.parsed.y;
                  if (metric === "cost") {
                    return (
                      label +
                      ": $" +
                      (Number(value) || 0).toFixed(6)
                    );
                  }
                  return (
                    label + ": " + namespace.formatCount(Number(value) || 0)
                  );
                },
                afterBody: function (items) {
                  if (!items || !items.length) return [];
                  const idx = items[0].dataIndex;
                  const bucket = String(buckets[idx] || "");
                  const total = totalsByBucket.get(bucket) || {};
                  const lines = [
                    "",
                    "Bucket totals:",
                    "  Requests: " +
                      namespace.formatCount(total.request_count || 0),
                    "  Errors: " +
                      namespace.formatCount(total.error_count || 0),
                    "  Tokens: " +
                      namespace.formatTokens(total.total_tokens || 0),
                    "  Cost: " +
                      namespace.formatDollarsFromMicro(
                        total.cost_microdollars || 0
                      ),
                    "  Avg latency: " +
                      namespace.formatDurationMs(total.avg_latency_ms || 0),
                    "  Avg TTFT: " +
                      namespace.formatDurationMs(total.avg_ttft_ms || 0),
                  ];
                  return lines;
                },
              },
            },
          },
          scales: {
            x: {
              stacked: !nonStackedMetric,
              title: { display: true, text: "Time" },
            },
            y: {
              stacked: !nonStackedMetric,
              beginAtZero: true,
              title: { display: true, text: yTitle },
            },
          },
        },
      });
    }
  };

  namespace.reinitTimeseriesChart = function reinitTimeseriesChart() {
    const ctx = document.getElementById("timeseries-chart");
    if (!ctx) return;
    if (typeof window.Chart === "undefined") return;

    destroyChartOn(ctx);

    const params = new URLSearchParams(window.location.search);
    const period = params.get("period") || "24h";
    fetch("/api/timeseries?period=" + encodeURIComponent(period), {
      cache: "no-store",
      headers: { "x-dashboard-refresh": "1" },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("timeseries fetch failed: " + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        const rows = Array.isArray(data) ? data : [];
        const labels = rows.map(function (d) {
          return d.bucket;
        });
        const requests = rows.map(function (d) {
          return Number(d.request_count || 0);
        });
        const errors = rows.map(function (d) {
          return Number(d.error_count || 0);
        });
        ctx.__eggpoolChart = new window.Chart(ctx, {
          type: "line",
          data: {
            labels: labels,
            datasets: [
              {
                label: "Requests",
                data: requests,
                borderColor: "rgb(75, 192, 192)",
                tension: 0.1,
              },
              {
                label: "Errors",
                data: errors,
                borderColor: "rgb(255, 99, 132)",
                tension: 0.1,
              },
            ],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
              x: { title: { display: true, text: "Time" } },
              y: {
                title: { display: true, text: "Count" },
                beginAtZero: true,
              },
            },
          },
        });
      })
      .catch(function (err) {
        console.error("Failed to load timeseries:", err);
      });
  };

  function bootstrap() {
    try {
      namespace.initGroupedTimeseriesCharts();
    } catch (err) {
      console.error(
        "EggPoolDashboard: initGroupedTimeseriesCharts failed",
        err
      );
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();