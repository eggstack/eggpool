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
    return "$" + (Number(value) / 1_000_000).toFixed(2);
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
    return "$" + (Number(microdollars) / 1_000_000).toFixed(2);
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
    const canvas = document.getElementById("timeseries-chart");
    if (!canvas) return;
    if (typeof window.Chart === "undefined") return;

    destroyChartOn(canvas);

    function periodForFetch() {
      const fromCanvas =
        (canvas && canvas.getAttribute("data-period")) || "";
      const fromScript = document
        .getElementById("timeseries-initial-data")
        ?.getAttribute("data-period");
      const fromUrl =
        new URLSearchParams(window.location.search).get("period") || "";
      return fromCanvas || fromScript || fromUrl || "24h";
    }

    function renderRows(rows) {
      const list = Array.isArray(rows) ? rows : [];
      const labels = list.map(function (d) {
        return d.bucket;
      });
      const requests = list.map(function (d) {
        return Number(d.request_count || 0);
      });
      const errors = list.map(function (d) {
        return Number(d.error_count || 0);
      });
      destroyChartOn(canvas);
      canvas.__eggpoolChart = new window.Chart(canvas, {
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
    }

    const dataScript = document.getElementById("timeseries-initial-data");
    let inlineRows = null;
    if (dataScript && dataScript.textContent) {
      try {
        const parsed = JSON.parse(dataScript.textContent);
        if (Array.isArray(parsed)) {
          inlineRows = parsed;
        }
      } catch (err) {
        console.error(
          "EggPoolDashboard: failed to parse timeseries payload",
          err
        );
      }
    }

    if (inlineRows !== null) {
      renderRows(inlineRows);
    } else {
      const period = periodForFetch();
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
          renderRows(data);
        })
        .catch(function (err) {
          console.error("Failed to load timeseries:", err);
        });
    }

    // Re-arm the in-place 60s refresh that the original inline IIFE
    // registered. Cleared and re-registered on each reinit so successive
    // auto-refresh ticks do not stack intervals on the same canvas.
    if (canvas.__eggpoolRefreshHandle) {
      window.clearInterval(canvas.__eggpoolRefreshHandle);
      canvas.__eggpoolRefreshHandle = null;
    }
    canvas.__eggpoolRefreshHandle = window.setInterval(function () {
      const period = periodForFetch();
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
          if (!canvas.__eggpoolChart) return;
          const list = Array.isArray(data) ? data : [];
          const labels = list.map(function (d) {
            return d.bucket;
          });
          const requests = list.map(function (d) {
            return Number(d.request_count || 0);
          });
          const errors = list.map(function (d) {
            return Number(d.error_count || 0);
          });
          canvas.__eggpoolChart.data.labels = labels;
          canvas.__eggpoolChart.data.datasets[0].data = requests;
          canvas.__eggpoolChart.data.datasets[1].data = errors;
          canvas.__eggpoolChart.update();
        })
        .catch(function (err) {
          console.error("Failed to refresh timeseries:", err);
        });
    }, 60000);
  };

  namespace.initStaticCharts = function initStaticCharts() {
    if (typeof window.Chart === "undefined") {
      console.warn("EggPoolDashboard: Chart.js not loaded");
      return;
    }
    const dataScripts = document.querySelectorAll(
      "script.static-chart-data[data-chart-id]"
    );
    for (let i = 0; i < dataScripts.length; i++) {
      const script = dataScripts[i];
      const chartId = script.getAttribute("data-chart-id");
      if (!chartId) continue;
      const canvas = document.getElementById(chartId);
      if (!canvas) {
        console.warn(
          "EggPoolDashboard: no canvas found for static chart",
          chartId
        );
        continue;
      }
      let payload;
      try {
        payload = JSON.parse(script.textContent || "{}");
      } catch (err) {
        console.error(
          "EggPoolDashboard: failed to parse static chart payload",
          chartId,
          err
        );
        continue;
      }
      const chartType = String(payload.type || "bar");
      const labels = Array.isArray(payload.labels) ? payload.labels : [];
      const datasets = Array.isArray(payload.datasets) ? payload.datasets : [];
      const options =
        payload.options && typeof payload.options === "object"
          ? payload.options
          : {};
      destroyChartOn(canvas);
      canvas.__eggpoolChart = new window.Chart(canvas, {
        type: chartType,
        data: { labels: labels, datasets: datasets },
        options: options,
      });
    }
  };

  function bootstrap() {
    try {
      namespace.initStaticCharts();
    } catch (err) {
      console.error(
        "EggPoolDashboard: initStaticCharts failed",
        err
      );
    }
    try {
      namespace.initGroupedTimeseriesCharts();
    } catch (err) {
      console.error(
        "EggPoolDashboard: initGroupedTimeseriesCharts failed",
        err
      );
    }
    try {
      namespace.initTimeseriesControls();
    } catch (err) {
      console.error(
        "EggPoolDashboard: initTimeseriesControls failed",
        err
      );
    }
    try {
      namespace.initNavToggle();
    } catch (err) {
      console.error("EggPoolDashboard: initNavToggle failed", err);
    }
    try {
      namespace.initUpdateCommandCopy();
    } catch (err) {
      console.error("EggPoolDashboard: initUpdateCommandCopy failed", err);
    }
    try {
      if (document.getElementById("timeseries-chart")) {
        namespace.reinitTimeseriesChart();
      }
    } catch (err) {
      console.error("EggPoolDashboard: reinitTimeseriesChart failed", err);
    }
  }

  function fetchGroupedTimeseries(params) {
    const search = new URLSearchParams();
    for (const key in params) {
      if (!Object.prototype.hasOwnProperty.call(params, key)) continue;
      const value = params[key];
      if (value === null || value === undefined || value === "") continue;
      search.set(key, String(value));
    }
    const url = "/api/timeseries/grouped" + (search.toString() ? "?" + search.toString() : "");
    return fetch(url, {
      cache: "no-store",
      headers: { "x-dashboard-refresh": "1" },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error(
            "grouped timeseries fetch failed: " + response.status
          );
        }
        return response.json();
      });
  }

  function readTimeseriesParams(form) {
    const data = new FormData(form);
    const get = function (name) {
      const value = data.get(name);
      return value === null ? "" : String(value);
    };
    const periodSelect = document.querySelector(
      'form[data-period-selector] select[name="period"]'
    );
    const period =
      (periodSelect && periodSelect.value) ||
      get("period") ||
      new URLSearchParams(window.location.search).get("period") ||
      "24h";
    return {
      period: period,
      bucket: get("bucket") || "hour",
      group_by: get("group_by") || "provider_model",
      metric: get("metric") || "tokens",
      limit: get("limit") || "12",
      account: get("account") || "",
      model: get("model") || "",
    };
  }

  function syncTimeseriesPeriod(form) {
    if (!form) return;
    const periodSelect = document.querySelector(
      'form[data-period-selector] select[name="period"]'
    );
    if (!periodSelect) return;
    const periodInput = form.querySelector('input[name="period"]');
    if (periodInput) {
      periodInput.value = periodSelect.value;
    }
  }

  function setChartData(chartId, payload) {
    const script = document.querySelector(
      'script.grouped-timeseries-data[data-chart-id="' + chartId + '"]'
    );
    if (script) {
      script.textContent = JSON.stringify(payload);
    }
    const panel = script ? script.closest(".timeseries-chart-panel") : null;
    const container = panel
      ? panel.querySelector(".chart-container, .chart-container-compact")
      : null;
    const empty = panel ? panel.querySelector(".grouped-timeseries-empty") : null;
    const points = payload && Array.isArray(payload.points) ? payload.points : [];
    const buckets = payload && Array.isArray(payload.buckets) ? payload.buckets : [];
    const hasData = points.length > 0 && buckets.length > 0;
    if (container) {
      container.style.display = hasData ? "" : "none";
    }
    if (empty) {
      empty.style.display = hasData ? "none" : "";
    }
  }

  namespace.refreshGroupedTimeseriesChart = function refreshGroupedTimeseriesChart(
    form
  ) {
    syncTimeseriesPeriod(form);
    const params = readTimeseriesParams(form);
    const canvas = document.querySelector(
      'canvas.grouped-timeseries-chart[data-chart-id="grouped-timeseries-chart"]'
    );
    if (!canvas) return Promise.resolve();
    const chartId = canvas.getAttribute("data-chart-id");
    if (form.dataset && form.dataset.timeseriesBusy === "1") {
      return Promise.resolve();
    }
    if (form.dataset) form.dataset.timeseriesBusy = "1";
    return fetchGroupedTimeseries(params)
      .then(function (payload) {
        setChartData(chartId, payload || {});
        if (typeof namespace.initGroupedTimeseriesCharts === "function") {
          namespace.initGroupedTimeseriesCharts();
        }
      })
      .catch(function (err) {
        console.error("Failed to refresh grouped timeseries chart:", err);
      })
      .then(function () {
        if (form.dataset) form.dataset.timeseriesBusy = "0";
      });
  };

  namespace.initTimeseriesControls = function initTimeseriesControls() {
    const forms = document.querySelectorAll("form[data-timeseries-controls]");
    for (let i = 0; i < forms.length; i++) {
      const form = forms[i];
      if (form.__eggpoolTimeseriesWired) continue;
      form.__eggpoolTimeseriesWired = true;
      const onChange = function () {
        namespace.refreshGroupedTimeseriesChart(form);
      };
      const selects = form.querySelectorAll("select");
      for (let s = 0; s < selects.length; s++) {
        selects[s].addEventListener("change", onChange);
      }
      const accountInput = form.querySelector(
        'input[name="account"], select[name="account"]'
      );
      const modelInput = form.querySelector(
        'input[name="model"], select[name="model"]'
      );
      if (accountInput && accountInput.tagName === "INPUT") {
        let lastValue = accountInput.value;
        accountInput.addEventListener("input", function () {
          const value = accountInput.value;
          if (value === lastValue) return;
          lastValue = value;
          onChange();
        });
      }
      if (modelInput && modelInput.tagName === "INPUT") {
        let lastValue = modelInput.value;
        modelInput.addEventListener("input", function () {
          const value = modelInput.value;
          if (value === lastValue) return;
          lastValue = value;
          onChange();
        });
      }
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        namespace.refreshGroupedTimeseriesChart(form);
      });
    }

    const periodForms = document.querySelectorAll(
      "form[data-period-selector]"
    );
    const timeseriesForm = document.querySelector(
      "form[data-timeseries-controls]"
    );
    for (let p = 0; p < periodForms.length; p++) {
      const periodForm = periodForms[p];
      if (periodForm.__eggpoolPeriodWired) continue;
      periodForm.__eggpoolPeriodWired = true;
      const select = periodForm.querySelector('select[name="period"]');
      if (select) {
        select.addEventListener("change", function () {
          if (
            timeseriesForm
            && typeof namespace.refreshGroupedTimeseriesChart === "function"
          ) {
            syncTimeseriesPeriod(timeseriesForm);
            namespace.refreshGroupedTimeseriesChart(timeseriesForm);
          } else {
            periodForm.submit();
          }
        });
      }
      // Auto-submit any other `data-auto-submit` selects inside the
      // same form (e.g. the "Show disabled accounts" toggle on the
      // Accounts page).  Wire independently of whether the period
      // select exists so a filter-only GET form also gets the
      // auto-submit treatment without depending on the period select.
      const autoSubmits = periodForm.querySelectorAll(
        "select[data-auto-submit]"
      );
      for (let s = 0; s < autoSubmits.length; s++) {
        const autoSelect = autoSubmits[s];
        if (autoSelect === select) continue;
        autoSelect.addEventListener("change", function () {
          if (
            timeseriesForm
            && typeof namespace.refreshGroupedTimeseriesChart === "function"
          ) {
            namespace.refreshGroupedTimeseriesChart(timeseriesForm);
          } else {
            periodForm.submit();
          }
        });
      }
    }
  };

  // Wire the mobile burger button that toggles the page-link menu
  // dropdown.  On viewports ≥761px the burger is hidden via CSS and
  // the menu is always rendered inline, so the JS is a no-op there.
  // On narrower viewports the burger is visible and the menu only
  // shows when `.topnav-open` is set on the ancestor `nav.topnav`.
  //
  // Dismissal rules:
  // - click the burger again to close
  // - press Escape inside the nav to close and return focus to the
  //   burger
  // - click any menu link to close (lets navigation proceed)
  // - click anywhere outside the nav to close
  //
  // Re-init is idempotent via the `__eggpoolNavWired` flag, matching
  // the pattern used by `initUpdateCommandCopy` and the timeseries
  // controls so the auto-refresh loop does not stack handlers.
  namespace.initNavToggle = function initNavToggle() {
    const burger = document.querySelector("nav.topnav .topnav-burger");
    const menu = document.querySelector("nav.topnav .topnav-menu");
    if (!burger || !menu) return;
    const nav = burger.closest("nav.topnav");
    if (!nav) return;
    if (burger.__eggpoolNavWired) return;
    burger.__eggpoolNavWired = true;

    // Cache the original tooltip + aria-label rendered by
    // `_render_nav` so we can restore them when the menu closes.
    // The expanded state should not advertise "Open page menu" —
    // hovering the X icon after the menu is open otherwise shows
    // stale copy that no longer matches the button's behaviour.
    const originalTooltip = burger.getAttribute("data-tooltip") || "";
    const openTooltip =
      burger.getAttribute("data-tooltip-open-label") || originalTooltip;
    const originalAriaLabel = burger.getAttribute("aria-label") || "";

    const setOpen = function (open) {
      if (open) {
        nav.classList.add("topnav-open");
        burger.setAttribute("aria-expanded", "true");
        if (openTooltip) {
          burger.setAttribute("data-tooltip", openTooltip);
        } else {
          burger.removeAttribute("data-tooltip");
        }
        burger.setAttribute("aria-label", "Close page menu");
      } else {
        nav.classList.remove("topnav-open");
        burger.setAttribute("aria-expanded", "false");
        if (originalTooltip) {
          burger.setAttribute("data-tooltip", originalTooltip);
        } else {
          burger.removeAttribute("data-tooltip");
        }
        burger.setAttribute("aria-label", originalAriaLabel);
      }
    };

    burger.addEventListener("click", function (event) {
      event.preventDefault();
      const expanded = burger.getAttribute("aria-expanded") === "true";
      setOpen(!expanded);
    });

    nav.addEventListener("keydown", function (event) {
      if (event.key === "Escape" || event.key === "Esc") {
        const wasOpen = burger.getAttribute("aria-expanded") === "true";
        if (!wasOpen) return;
        event.preventDefault();
        setOpen(false);
        try {
          burger.focus();
        } catch (_err) {
          /* ignore */
        }
      }
    });

    const links = menu.querySelectorAll("a");
    for (let i = 0; i < links.length; i++) {
      links[i].addEventListener("click", function () {
        setOpen(false);
      });
    }

    document.addEventListener("click", function (event) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (nav.contains(target)) return;
      setOpen(false);
    });
  };

  // Wire click-to-copy on every element marked with
  // ``data-update-command``.  Each click selects the text and copies
  // it via the async Clipboard API; falls back to the legacy
  // ``document.execCommand("copy")`` path when the modern API is
  // unavailable (older browsers, insecure contexts).  A short
  // "copied!" affordance fades in next to the command for ~1.6s and
  // also announces the success to assistive tech via the role=status
  // sibling.
  namespace.initUpdateCommandCopy = function initUpdateCommandCopy() {
    const commands = document.querySelectorAll("[data-update-command]");
    for (let i = 0; i < commands.length; i++) {
      const el = commands[i];
      if (el.__eggpoolCopyWired) continue;
      el.__eggpoolCopyWired = true;
      const handle = function (event) {
        if (event) {
          event.preventDefault();
        }
        const text = el.textContent || "";
        const indicator =
          el.parentElement &&
          el.parentElement.querySelector("[data-update-copied]");
        const flash = function (label) {
          if (!indicator) return;
          indicator.textContent = label;
          indicator.classList.add("is-visible");
          window.setTimeout(function () {
            indicator.classList.remove("is-visible");
            window.setTimeout(function () {
              indicator.textContent = "";
            }, 200);
          }, 1400);
        };
        const fallback = function () {
          try {
            const range = document.createRange();
            range.selectNodeContents(el);
            const selection = window.getSelection();
            if (selection) {
              selection.removeAllRanges();
              selection.addRange(range);
            }
            const ok = document.execCommand && document.execCommand("copy");
            if (selection) {
              selection.removeAllRanges();
            }
            flash(ok ? "copied!" : "press Ctrl+C");
          } catch (_err) {
            flash("press Ctrl+C");
          }
        };
        if (
          typeof navigator !== "undefined" &&
          navigator.clipboard &&
          typeof navigator.clipboard.writeText === "function"
        ) {
          navigator.clipboard.writeText(text).then(
            function () {
              flash("copied!");
            },
            function () {
              fallback();
            }
          );
        } else {
          fallback();
        }
      };
      el.addEventListener("click", handle);
      el.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          handle(event);
        }
      });
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
