# What changed

* **Access queue** - `core.py`, `templates/access_queue.html`, `static/access_queue.{css,js}`, routes in `app1.py`.
* **Analytics** - months start at August 2026 and run 12 months ahead; new chart and picker (`static/analytics_modern.css`); admin and NGO pages.
* **Validation** - server-side checks on every registration/login/profile/donation/request form; `static/input-guard.js` blocks wrong characters in the browser.
* **Sampling** - `sampling.py`, `/admin_sampling`, `templates/admin_sampling.html`.
* **Security** - secrets moved to environment variables; CSRF; rate limits; security headers; role gate; hidden admin area; private NGO documents; POST-only destructive actions; secure random OTP/codes; session hardening.
* **Mobile** - viewport tags everywhere, `static/responsive.css`, hamburger menu (`static/nav-toggle.js`).
* **Hosting** - `requirements.txt`, `Procfile`, `.env.example`, `README_DEPLOY.md`, `sql/002_access_queue.sql`.
* **Extras** - `seed_sample_data.py`, `loadtest_queue.py`, `tests/`.
