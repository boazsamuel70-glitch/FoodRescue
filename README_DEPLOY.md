# FoodResQ - run it and put it online

## 1. Run on your own computer

```bash
python -m venv venv
venv\Scripts\activate            # Windows      (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env           # Mac/Linux: cp .env.example .env   -> then fill it in
```

1. Open MySQL and run `sqldatabases.sql` (creates the tables, including `access_queue`).
2. In `.env` put your MySQL password in `DB_PASSWORD` and any 64 random characters in `SECRET_KEY`
   (`python -c "import secrets; print(secrets.token_hex(32))"`).
3. `python app1.py` and open http://127.0.0.1:5000. On your phone (same Wi-Fi) use
   `http://<your-computer-ip>:5000` after setting `HOST=0.0.0.0` in `.env`.

Optional: `python seed_sample_data.py --yes` adds demo donors/NGOs/donations (Aug 2026 onward, some upcoming)
so the analytics and sampling pages have something to show. Remove them again with `--remove --yes`.

## 2. Before going live - do these first

* **Change your old passwords.** The original project had the MySQL root password and a Gmail app
  password written in the code. Treat both as leaked: change the MySQL password, delete the old Gmail
  app password and create a new one.
* Set `FLASK_ENV=production`. This turns on HTTPS-only cookies and refuses to start without a real `SECRET_KEY`.
* Do not upload `.env`, `private/` or `static/uploads/` to GitHub (the `.gitignore` already excludes them).
* If your admin account still uses a sample password such as `admin123`, change it before going live.

## 3. Hosting options

FoodResQ needs three things online: the Python app, a MySQL database, and disk space that survives
restarts (NGO documents live in `private/ngo_documents`, other uploads in `static/uploads`).
Check each provider's current free-tier limits before choosing; they change often.

**Easiest - PythonAnywhere (app + MySQL + permanent disk together)**
1. Create an account, open *Databases*, set a MySQL password, create database `<username>$foodresq`.
2. In a Bash console: `git clone` (or upload the zip) then `pip install --user -r requirements.txt`.
3. Import the tables: `mysql -u <username> -h <username>.mysql.pythonanywhere-services.com -p '<username>$foodresq' < sqldatabases.sql`
4. *Web* tab -> add a Flask app -> point the WSGI file at `from app1 import app as application`.
5. Put your settings in a `.env` file in the project folder (`DB_HOST`, `DB_USER`, `DB_NAME=<username>$foodresq`, `SECRET_KEY`, `FLASK_ENV=production`).
6. Enable HTTPS (Force HTTPS) and press *Reload*.
Note: free accounts restrict outgoing internet, which can block sending e-mail. A paid plan removes that limit.

**Railway or Render (git deploy)**
1. Push the project to a private GitHub repo.
2. Create a MySQL database (Railway has a MySQL plugin; Render needs an external MySQL such as Aiven), import `sqldatabases.sql`.
3. Create a web service from the repo. Start command is already in `Procfile`:
   `gunicorn app1:app --workers 3 --threads 4 --timeout 60`.
4. Add every value from `.env.example` as environment variables (`DB_SSL=1` if the database demands SSL).
5. **Attach a persistent disk/volume** and mount it at `private/` and `static/uploads/`; otherwise uploaded
   documents disappear on every redeploy.
6. Use the health check path `/health`.

## 4. The access queue

* `MAX_ACTIVE_USERS` people can be logged in at once; the rest wait in a first-come-first-served waiting room
  and enter automatically. A slot is released on logout or after `ACTIVE_IDLE_SECONDS` of inactivity.
* Home, About and Contact stay open to everyone; only logged-in areas are queued. Admins are never queued.
* Choose the number from what your server can handle, then prove it: run the app with a small limit and
  `python loadtest_queue.py --users 40` (see the top of that file). With `MAX_ACTIVE_USERS=10` you should see
  10 inside and 30 waiting.
* Rate limits (login attempts etc.) are counted per server process. With 3 gunicorn workers an attacker gets
  roughly 3x the limit; the account lock-out in the database still applies across all workers.

## 5. Sampling (Admin -> Sampling)

Pick Donations, Donors or NGOs, then one of seven methods and download the result as CSV.
Probability: simple random, systematic, stratified, cluster. Non-probability: convenience, quota, snowball.
Random draws show their seed, so you can reproduce exactly the same sample later for an audit.
Unit tests: `python -m unittest tests.test_sampling -v`.

## 6. Tests you can run

`python -m unittest tests.test_sampling -v` (no database needed).
