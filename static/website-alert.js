/* =========================================================
   FOODRESQ WEBSITE MESSAGE SYSTEM
   In-page modal alerts / confirms. No native browser dialogs.

   Public API:
     foodresqAlert(message, options)
     foodresqConfirm(message, onConfirm, options)

   options = { title, type, confirmText, cancelText, onCancel }
   type = "info" | "success" | "error" | "warning" | "confirm"

   Native alert() is replaced automatically.
   Native confirm() inside onclick / onsubmit attributes is
   detected and upgraded automatically (see INTERCEPTOR below).
========================================================= */

(function () {

    "use strict";

    if (window.__foodresqAlertReady) {
        return;
    }

    window.__foodresqAlertReady = true;


    /* =====================================================
       STYLES
       Injected here so the modal can never appear unstyled,
       and so no page needs an extra <link> tag.
    ===================================================== */

    var STYLES = `

    #foodresq-modal-overlay {
        position: fixed;
        inset: 0;
        z-index: 2147483000;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 20px;
        background: rgba(12, 32, 22, 0.45);
        backdrop-filter: blur(7px) saturate(.9);
        -webkit-backdrop-filter: blur(7px) saturate(.9);
        opacity: 0;
        visibility: hidden;
        transition: opacity .18s ease, visibility .18s ease;
    }

    #foodresq-modal-overlay.show {
        opacity: 1;
        visibility: visible;
    }

    body.foodresq-modal-open {
        overflow: hidden;
    }

    /* Fallback for browsers without backdrop-filter: blur the
       page content itself instead of the overlay. */
    @supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {

        body.foodresq-modal-open > *:not(#foodresq-modal-overlay) {
            filter: blur(4px);
        }

    }

    .foodresq-modal {
        width: 100%;
        max-width: 430px;
        background: #FFFFFF;
        border-radius: 16px;
        box-shadow: 0 18px 50px rgba(0, 0, 0, .28);
        padding: 30px 26px 24px;
        text-align: center;
        font-family: "Segoe UI", Roboto, system-ui, -apple-system, sans-serif;
        transform: translateY(14px) scale(.97);
        transition: transform .2s cubic-bezier(.2, .9, .3, 1.2);
        max-height: calc(100vh - 40px);
        overflow-y: auto;
    }

    #foodresq-modal-overlay.show .foodresq-modal {
        transform: translateY(0) scale(1);
    }

    .foodresq-modal-icon {
        width: 62px;
        height: 62px;
        margin: 0 auto 16px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        background: #E8F4EC;
        color: #2E7D32;
    }

    .foodresq-modal-icon svg {
        width: 32px;
        height: 32px;
        stroke: currentColor;
        stroke-width: 2.4;
        fill: none;
        stroke-linecap: round;
        stroke-linejoin: round;
    }

    .foodresq-modal[data-type="success"] .foodresq-modal-icon {
        background: #E4F6E7;
        color: #2E7D32;
    }

    .foodresq-modal[data-type="error"] .foodresq-modal-icon {
        background: #FDEAEA;
        color: #C62828;
    }

    .foodresq-modal[data-type="warning"] .foodresq-modal-icon {
        background: #FFF4E0;
        color: #B26A00;
    }

    .foodresq-modal[data-type="info"] .foodresq-modal-icon {
        background: #E7F1FB;
        color: #1565C0;
    }

    .foodresq-modal[data-type="confirm"] .foodresq-modal-icon {
        background: #E8F4EC;
        color: #2E7D32;
    }

    .foodresq-modal h3 {
        margin: 0 0 8px;
        font-size: 1.25rem;
        font-weight: 700;
        color: #1B4332;
        letter-spacing: .2px;
    }

    .foodresq-modal p {
        margin: 0;
        font-size: .98rem;
        line-height: 1.6;
        color: #47584F;
        word-wrap: break-word;
    }

    .foodresq-modal-buttons {
        display: flex;
        gap: 10px;
        justify-content: center;
        margin-top: 24px;
    }

    .foodresq-modal-buttons button {
        flex: 1 1 0;
        max-width: 190px;
        padding: 11px 18px;
        border-radius: 9px;
        border: none;
        font-size: .95rem;
        font-weight: 600;
        font-family: inherit;
        cursor: pointer;
        transition: background .15s ease, transform .1s ease;
    }

    .foodresq-modal-buttons button:active {
        transform: translateY(1px);
    }

    #foodresq-modal-confirm {
        background: #2E7D32;
        color: #FFFFFF;
    }

    #foodresq-modal-confirm:hover {
        background: #256A29;
    }

    .foodresq-modal[data-type="error"] #foodresq-modal-confirm {
        background: #C62828;
    }

    .foodresq-modal[data-type="error"] #foodresq-modal-confirm:hover {
        background: #AD2222;
    }

    .foodresq-modal[data-type="warning"] #foodresq-modal-confirm {
        background: #B26A00;
    }

    .foodresq-modal[data-type="warning"] #foodresq-modal-confirm:hover {
        background: #965A00;
    }

    #foodresq-modal-cancel {
        background: #EDF1EE;
        color: #40514A;
    }

    #foodresq-modal-cancel:hover {
        background: #DFE6E1;
    }

    .foodresq-modal-buttons button:focus-visible {
        outline: 3px solid rgba(46, 125, 50, .35);
        outline-offset: 2px;
    }

    @media (max-width: 480px) {

        .foodresq-modal {
            padding: 26px 20px 20px;
        }

        .foodresq-modal-buttons {
            flex-direction: column-reverse;
        }

        .foodresq-modal-buttons button {
            max-width: none;
            width: 100%;
        }

    }

    @media (prefers-reduced-motion: reduce) {

        #foodresq-modal-overlay,
        .foodresq-modal {
            transition: none;
        }

    }

    `;


    function injectStyles() {

        if (document.getElementById("foodresq-modal-styles")) {
            return;
        }

        var style = document.createElement("style");

        style.id = "foodresq-modal-styles";
        style.textContent = STYLES;

        (document.head || document.documentElement)
            .appendChild(style);

    }


    /* =====================================================
       ICONS
    ===================================================== */

    var ICONS = {

        success:
            '<svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>',

        error:
            '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/>' +
            '<path d="M15 9l-6 6M9 9l6 6"/></svg>',

        warning:
            '<svg viewBox="0 0 24 24">' +
            '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>' +
            '<path d="M12 9v4M12 17h.01"/></svg>',

        info:
            '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/>' +
            '<path d="M12 16v-4M12 8h.01"/></svg>',

        confirm:
            '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/>' +
            '<path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/></svg>'

    };


    /* =====================================================
       STATE
    ===================================================== */

    var overlay = null;
    var confirmAction = null;
    var cancelAction = null;
    var lastFocused = null;


    /* =====================================================
       BUILD MODAL
    ===================================================== */

    function buildModal() {

        if (overlay) {
            return overlay;
        }

        injectStyles();

        overlay = document.createElement("div");
        overlay.id = "foodresq-modal-overlay";

        overlay.innerHTML =
            '<div class="foodresq-modal"' +
            '     role="alertdialog"' +
            '     aria-modal="true"' +
            '     aria-labelledby="foodresq-modal-title"' +
            '     aria-describedby="foodresq-modal-message"' +
            '     data-type="info">' +

            '  <div class="foodresq-modal-icon"' +
            '       id="foodresq-modal-icon"' +
            '       aria-hidden="true"></div>' +

            '  <div class="foodresq-modal-content">' +
            '    <h3 id="foodresq-modal-title">FoodResQ</h3>' +
            '    <p id="foodresq-modal-message"></p>' +
            '  </div>' +

            '  <div class="foodresq-modal-buttons">' +
            '    <button type="button" id="foodresq-modal-cancel">Cancel</button>' +
            '    <button type="button" id="foodresq-modal-confirm">OK</button>' +
            '  </div>' +

            '</div>';

        document.body.appendChild(overlay);


        overlay.querySelector("#foodresq-modal-cancel")
            .addEventListener("click", function () {

                var fn = cancelAction;

                closeModal();

                if (fn) {
                    fn();
                }

            });


        overlay.querySelector("#foodresq-modal-confirm")
            .addEventListener("click", function () {

                var fn = confirmAction;

                closeModal();

                if (fn) {
                    fn();
                }

            });


        overlay.addEventListener("click", function (event) {

            if (event.target === overlay) {
                closeModal();
            }

        });


        overlay.addEventListener("keydown", function (event) {

            if (event.key !== "Tab") {
                return;
            }

            var focusable = Array.prototype.filter.call(
                overlay.querySelectorAll("button"),
                function (b) {
                    return b.offsetParent !== null;
                }
            );

            if (!focusable.length) {
                return;
            }

            var first = focusable[0];
            var last = focusable[focusable.length - 1];

            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            }
            else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }

        });

        return overlay;

    }


    /* =====================================================
       CLOSE
    ===================================================== */

    function closeModal() {

        confirmAction = null;
        cancelAction = null;

        if (!overlay) {
            return;
        }

        overlay.classList.remove("show");

        document.body.classList.remove("foodresq-modal-open");

        if (lastFocused && lastFocused.focus) {

            try {
                lastFocused.focus();
            }
            catch (e) { /* ignore */ }

        }

        lastFocused = null;

    }

    window.foodresqCloseModal = closeModal;


    /* =====================================================
       GUESS A TYPE FROM THE MESSAGE TEXT
    ===================================================== */

    function guessType(message) {

        var text = String(message || "").toLowerCase();

        if (/success|approved|successfully|sent|updated|saved|completed|thank you|registered successfully/.test(text)) {
            return "success";
        }

        if (/invalid|error|failed|not found|unable|incorrect|expired|denied|cannot|wrong|do not match|must contain/.test(text)) {
            return "error";
        }

        if (/already|too many|locked|deactivated|warning|no longer|please enter|required|select/.test(text)) {
            return "warning";
        }

        return "info";

    }


    function defaultTitle(type) {

        if (type === "success") {
            return "Success";
        }

        if (type === "error") {
            return "Something Went Wrong";
        }

        if (type === "warning") {
            return "Please Note";
        }

        if (type === "confirm") {
            return "Please Confirm";
        }

        return "FoodResQ";

    }


    /* =====================================================
       OPEN
    ===================================================== */

    function openModal(config) {

        // If the body is not ready yet, wait for it.
        if (!document.body) {

            document.addEventListener("DOMContentLoaded", function () {
                openModal(config);
            });

            return;

        }

        buildModal();

        var type = config.type || guessType(config.message);

        var modal = overlay.querySelector(".foodresq-modal");

        modal.setAttribute("data-type", type);

        overlay.querySelector("#foodresq-modal-icon").innerHTML =
            ICONS[type] || ICONS.info;

        overlay.querySelector("#foodresq-modal-title").textContent =
            config.title || defaultTitle(type);

        overlay.querySelector("#foodresq-modal-message").textContent =
            String(config.message === undefined ? "" : config.message);


        var cancelBtn = overlay.querySelector("#foodresq-modal-cancel");
        var confirmBtn = overlay.querySelector("#foodresq-modal-confirm");

        if (config.showCancel) {

            cancelBtn.style.display = "";
            cancelBtn.textContent = config.cancelText || "Cancel";

        }
        else {

            cancelBtn.style.display = "none";

        }

        confirmBtn.textContent =
            config.confirmText || (config.showCancel ? "Yes, Continue" : "OK");


        confirmAction = config.onConfirm || null;
        cancelAction = config.onCancel || null;

        if (document.activeElement !== document.body) {
            lastFocused = document.activeElement;
        }

        overlay.classList.add("show");
        document.body.classList.add("foodresq-modal-open");

        window.requestAnimationFrame(function () {
            confirmBtn.focus();
        });

    }


    /* =====================================================
       PUBLIC API
    ===================================================== */

    window.foodresqAlert = function (message, options) {

        // Backwards compatible: foodresqAlert(msg, "Some Title")
        if (typeof options === "string") {
            options = { title: options };
        }

        options = options || {};

        openModal({
            message: message,
            title: options.title,
            type: options.type,
            confirmText: options.confirmText,
            showCancel: false,
            onConfirm: options.onConfirm || options.onClose || null
        });

    };


    window.foodresqConfirm = function (message, onConfirm, options) {

        // Backwards compatible: foodresqConfirm(msg, fn, "Some Title")
        if (typeof options === "string") {
            options = { title: options };
        }

        options = options || {};

        openModal({
            message: message,
            title: options.title || "Please Confirm",
            type: options.type || "confirm",
            confirmText: options.confirmText || "Yes, Continue",
            cancelText: options.cancelText || "Cancel",
            showCancel: true,
            onConfirm: onConfirm || null,
            onCancel: options.onCancel || null
        });

    };


    /* =====================================================
       REPLACE NATIVE alert()
    ===================================================== */

    window.alert = function (message) {

        window.foodresqAlert(message);

    };


    /* =====================================================
       ESC KEY
    ===================================================== */

    document.addEventListener("keydown", function (event) {

        if (event.key !== "Escape") {
            return;
        }

        if (!overlay || !overlay.classList.contains("show")) {
            return;
        }

        var fn = cancelAction;

        closeModal();

        if (fn) {
            fn();
        }

    });


    /* =====================================================
       INTERCEPTOR

       Native confirm() cannot be replaced directly, because it
       blocks and returns a boolean while our modal is async.

       Instead we find markup such as

           onsubmit="return confirm('Are you sure?')"
           onclick="return confirm('Are you sure?')"

       strip the inline handler, and re-wire the element to use
       the website modal instead.
    ===================================================== */

    var CONFIRM_PATTERN =
        /confirm\s*\(\s*(['"`])((?:\\.|[^\\])*?)\1\s*\)/;


    function extractMessage(attributeValue) {

        var match = CONFIRM_PATTERN.exec(attributeValue || "");

        if (!match) {
            return null;
        }

        return match[2]
            .replace(/\\'/g, "'")
            .replace(/\\"/g, '"')
            .replace(/\\n/g, " ")
            .replace(/\\\\/g, "\\")
            .replace(/\s+/g, " ")
            .trim();

    }


    /* Submit a form, keeping native validation and the
       submit button's name/value intact. */

    function submitForm(form, submitter) {

        form.__foodresqConfirmed = true;

        if (form.requestSubmit) {

            try {

                // requestSubmit keeps the submitter, so the server
                // still receives the button's name/value.
                form.requestSubmit(
                    (submitter && submitter.form === form)
                        ? submitter
                        : undefined
                );

                return;

            }
            catch (e) { /* fall through to the manual path */ }

        }

        // Fallback: form.submit() drops the submitter, so add it back.
        if (submitter && submitter.name) {

            var hidden = document.createElement("input");

            hidden.type = "hidden";
            hidden.name = submitter.name;
            hidden.value = submitter.value || "";

            form.appendChild(hidden);

        }

        form.submit();

    }


    /* Returns false and shows the browser's own field hints when
       a required field is empty, so confirming can't skip it. */

    function isValid(form) {

        if (!form || typeof form.checkValidity !== "function") {
            return true;
        }

        if (form.noValidate || form.checkValidity()) {
            return true;
        }

        if (form.reportValidity) {
            form.reportValidity();
        }

        return false;

    }


    function upgradeElement(el) {

        if (!el || el.dataset.foodresqConfirmReady) {
            return;
        }

        var tag = el.tagName;

        var attributeName =
            (tag === "FORM") ? "onsubmit" : "onclick";

        var raw = el.getAttribute(attributeName);

        if (!raw || raw.indexOf("confirm(") === -1) {
            return;
        }

        var message = extractMessage(raw);

        if (!message) {
            return;
        }

        el.dataset.foodresqConfirmReady = "1";

        // Strip the inline handler so the native dialog
        // can never be triggered.
        el.removeAttribute(attributeName);
        el[attributeName] = null;


        if (tag === "FORM") {

            el.addEventListener("submit", function (event) {

                // Let the confirmed submit through.
                if (el.__foodresqConfirmed) {

                    el.__foodresqConfirmed = false;
                    return;

                }

                event.preventDefault();

                if (!isValid(el)) {
                    return;
                }

                var submitter = event.submitter;

                window.foodresqConfirm(message, function () {
                    submitForm(el, submitter);
                });

            });

            return;

        }


        el.addEventListener("click", function (event) {

            event.preventDefault();
            event.stopPropagation();

            var form = el.form ||
                (el.closest ? el.closest("form") : null);

            if (form && !isValid(form)) {
                return;
            }

            window.foodresqConfirm(message, function () {

                if (form) {
                    submitForm(form, el);
                    return;
                }

                if (el.tagName === "A" && el.href) {
                    window.location.href = el.href;
                }

            });

        });

    }


    function scan(root) {

        if (!root || !root.querySelectorAll) {
            return;
        }

        var nodes = root.querySelectorAll(
            "form[onsubmit], [onclick]"
        );

        Array.prototype.forEach.call(nodes, upgradeElement);

        if (root.matches &&
            root.matches("form[onsubmit], [onclick]")) {

            upgradeElement(root);

        }

    }


    function start() {

        injectStyles();
        scan(document);
        showFlashed();

        // Catch anything added to the page later on.
        if (window.MutationObserver) {

            new MutationObserver(function (mutations) {

                mutations.forEach(function (mutation) {

                    Array.prototype.forEach.call(
                        mutation.addedNodes,
                        function (node) {

                            if (node.nodeType === 1) {
                                scan(node);
                            }

                        }
                    );

                });

            }).observe(document.documentElement, {
                childList: true,
                subtree: true
            });

        }

    }


    /* =====================================================
       FLASHED SERVER MESSAGES

       Flask flashes a message, the page it redirects to sets
       window.FOODRESQ_FLASH, and it is shown here as a popup
       over that page. Several messages are shown in turn.
    ===================================================== */

    function showFlashed() {

        var items = window.FOODRESQ_FLASH;

        if (!items || !items.length) {
            return;
        }

        // Only ever show them once.
        window.FOODRESQ_FLASH = null;

        var queue = items.slice();

        function next() {

            if (!queue.length) {
                return;
            }

            var item = queue.shift();

            // Flask gives [category, message]; a bare string also works.
            var category = Array.isArray(item) ? item[0] : null;
            var text = Array.isArray(item) ? item[1] : item;

            var known = ["success", "error", "warning", "info"];

            var type =
                (known.indexOf(String(category)) !== -1)
                    ? category
                    : guessType(text);

            window.foodresqAlert(text, {
                type: type,
                onConfirm: next
            });

        }

        next();

    }


    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    }
    else {
        start();
    }

})();
