/* FoodResQ input guard
 * Stops wrong characters at the keyboard, on paste and on mobile IMEs.
 * (The server re-checks everything; this only makes forms pleasant.)
 *
 *  digits : phone, contact, pincode, otp, year, counts, quantities
 *  letters: person / organisation-contact names
 */
(function () {
  "use strict";

  var DIGITS = {
    phone:                { max: 10, first: /[6-9]/, hint: "10 digits, starting with 6-9" },
    contact:              { max: 10, first: /[6-9]/, hint: "10 digits, starting with 6-9" },
    pincode:              { max: 6,  first: /[1-9]/, hint: "6 digits" },
    otp:                  { max: 6,  hint: "6 digits" },
    year_established:     { max: 4,  hint: "4-digit year" },
    people_count:         { max: 6 },
    people_served:        { max: 7 },
    quantity:             { max: 6 },
    quantity_distributed: { max: 6 }
  };
  var LETTERS = { name: 60, person_name: 60, designation: 60, requester_name: 60 };

  function apply(input) {
    var key = input.getAttribute("name");
    if (!key || input.dataset.guarded) return;

    if (DIGITS[key]) {
      var rule = DIGITS[key];
      input.dataset.guarded = "1";
      var wasNumber = input.type === "number";
      if (wasNumber) input.type = "text";           // number boxes accept "e", "+", "-"
      input.setAttribute("inputmode", "numeric");
      input.setAttribute("autocomplete", input.getAttribute("autocomplete") || (key === "otp" ? "one-time-code" : key === "phone" || key === "contact" ? "tel-national" : "off"));
      input.setAttribute("maxlength", rule.max);
      if (!input.getAttribute("pattern")) {
        var re = rule.max === 10 ? "[6-9][0-9]{9}" : rule.max === 6 && key === "pincode" ? "[1-9][0-9]{5}" : "[0-9]{" + (rule.max === 4 || key === "otp" ? rule.max : "1," + rule.max) + "}";
        input.setAttribute("pattern", re);
      }
      if (rule.hint && !input.getAttribute("title")) input.setAttribute("title", rule.hint);

      var clean = function () {
        var v = input.value.replace(/\D/g, "");
        if (rule.first) { while (v && !rule.first.test(v.charAt(0))) v = v.slice(1); }
        v = v.slice(0, rule.max);
        if (v !== input.value) input.value = v;
      };
      input.addEventListener("keypress", function (e) {
        if (e.ctrlKey || e.metaKey || e.key.length > 1) return;
        if (!/\d/.test(e.key)) e.preventDefault();
      });
      input.addEventListener("input", clean);
      input.addEventListener("paste", function () { setTimeout(clean, 0); });
      input.addEventListener("drop", function (e) { e.preventDefault(); });
      clean();
    }

    if (LETTERS[key] && !input.dataset.guarded) {
      input.dataset.guarded = "1";
      input.setAttribute("maxlength", LETTERS[key]);
      input.setAttribute("autocomplete", input.getAttribute("autocomplete") || "name");
      var cleanText = function () {
        var v = input.value.replace(/[^A-Za-z\u00C0-\u024F .'\-]/g, "").replace(/\s{2,}/g, " ");
        if (v !== input.value) input.value = v;
      };
      input.addEventListener("keypress", function (e) {
        if (e.ctrlKey || e.metaKey || e.key.length > 1) return;
        if (!/[A-Za-z\u00C0-\u024F .'\-]/.test(e.key)) e.preventDefault();
      });
      input.addEventListener("input", cleanText);
      input.addEventListener("paste", function () { setTimeout(cleanText, 0); });
      cleanText();
    }
  }

  function init() {
    var nodes = document.querySelectorAll("input[name]");
    for (var i = 0; i < nodes.length; i++) apply(nodes[i]);

    // Block double submits (also protects the login/queue endpoints from click storms)
    var forms = document.querySelectorAll("form");
    for (var j = 0; j < forms.length; j++) {
      forms[j].addEventListener("submit", function (e) {
        var f = e.currentTarget;
        if (f.dataset.busy === "1") { e.preventDefault(); return; }
        setTimeout(function () {
          if (!e.defaultPrevented && (!f.checkValidity || f.checkValidity())) {
            f.dataset.busy = "1";
            var b = f.querySelector('button[type="submit"],button:not([type])');
            if (b) { b.disabled = true; b.style.opacity = ".7"; }
            setTimeout(function () { f.dataset.busy = ""; if (b) { b.disabled = false; b.style.opacity = ""; } }, 8000);
          }
        }, 0);
      });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
