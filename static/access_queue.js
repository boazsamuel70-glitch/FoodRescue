/* FoodResQ waiting room: polls the server and lets the person in when a slot frees up. */
(function () {
  "use strict";

  var startPosition = parseInt(window.FOODRESQ_QUEUE_POSITION || 1, 10);
  var el = function (id) { return document.getElementById(id); };
  var failures = 0;
  var timer = null;

  function setProgress(position) {
    // Position 1 = nearly full; farther back fills more slowly. Visual only.
    var pct = Math.max(6, Math.min(96, 100 - (position - 1) * (90 / Math.max(startPosition, 10))));
    el("progress-fill").style.width = pct + "%";
  }

  function text(id, value) { var n = el(id); if (n) n.textContent = value; }

  function poll() {
    fetch("/access_heartbeat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": window.FOODRESQ_CSRF || "" },
      body: "{}"
    })
      .then(function (r) {
        if (r.status === 401) { window.location.href = "/"; throw new Error("logged out"); }
        if (r.status === 429) { throw new Error("slow down"); }
        return r.json();
      })
      .then(function (d) {
        failures = 0;
        if (d.active === true) {
          text("queue-status-text", "It's your turn!");
          text("live-text", "Taking you in now\u2026");
          el("progress-fill").style.width = "100%";
          document.querySelector(".queue-card").classList.add("granted");
          window.location.href = "/access_redirect";
          return;
        }
        if (d.gone) { window.location.reload(); return; }
        if (d.position !== undefined) {
          text("queue-position", d.position);
          setProgress(d.position);
        }
        if (d.waiting_total !== undefined) text("queue-total", d.waiting_total);
        if (d.eta_minutes !== undefined) text("queue-eta", d.eta_minutes);
        text("live-text", "Updating automatically \u2014 you can keep this tab open.");
      })
      .catch(function () {
        failures += 1;
        text("live-text", "Connection interrupted. Retrying\u2026");
      })
      .then(schedule);
  }

  function schedule() {
    // Back off gently if the network is flaky; never faster than 4 s.
    var delay = Math.min(4000 + failures * 2000, 20000);
    clearTimeout(timer);
    timer = setTimeout(poll, delay);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) { clearTimeout(timer); poll(); }
  });

  setProgress(startPosition);
  poll();
})();
