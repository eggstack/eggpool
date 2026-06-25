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
})();