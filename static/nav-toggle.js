/* Adds a hamburger button to the site header on small screens. */
(function () {
  "use strict";
  function init() {
    var header = document.querySelector("header");
    var nav = header && header.querySelector("nav");
    if (!nav || header.querySelector(".nav-toggle")) return;

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "nav-toggle";
    btn.setAttribute("aria-label", "Open menu");
    btn.setAttribute("aria-expanded", "false");
    btn.innerHTML = "&#9776;";
    header.insertBefore(btn, nav);

    btn.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.setAttribute("aria-label", open ? "Close menu" : "Open menu");
      btn.innerHTML = open ? "&#10005;" : "&#9776;";
    });

    var drops = nav.querySelectorAll(".dropdown");
    for (var i = 0; i < drops.length; i++) {
      (function (d) {
        var b = d.querySelector(".dropbtn");
        if (b) b.addEventListener("click", function (e) {
          if (window.matchMedia("(max-width: 980px)").matches) { e.preventDefault(); d.classList.toggle("open"); }
        });
      })(drops[i]);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
