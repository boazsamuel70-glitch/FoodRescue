"""
Prove the access queue works: many people log in at the same moment.

1. python seed_sample_data.py --yes
2. Start the app with a small limit and no rate limiting, e.g.
       MAX_ACTIVE_USERS=10 RATE_LIMIT=0 python app1.py
3. python loadtest_queue.py --users 40 --url http://127.0.0.1:5000

Expected: exactly MAX_ACTIVE_USERS people reach the dashboard, the rest are
sent to the waiting room in order.  Standard library only.
"""
import argparse
import re
import threading
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

PASSWORD = "Sample@1234"


def one(base, i, out):
    jar = CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        page = op.open(base + "/user_login", timeout=15).read().decode()
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        data = urllib.parse.urlencode({
            "email": f"donor{i}@sample.foodresq.test", "password": PASSWORD,
            "csrf_token": token}).encode()
        resp = op.open(base + "/login_user", data, timeout=30)
        out[i] = "waiting" if "/access_queue" in resp.geturl() else (
            "inside" if "user_dashboard" in resp.geturl() else "failed:" + resp.geturl())
    except Exception as exc:
        out[i] = f"error:{exc}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=40)
    ap.add_argument("--url", default="http://127.0.0.1:5000")
    a = ap.parse_args()
    out, threads = {}, []
    for i in range(1, a.users + 1):
        t = threading.Thread(target=one, args=(a.url.rstrip("/"), i, out))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    inside = sum(v == "inside" for v in out.values())
    waiting = sum(v == "waiting" for v in out.values())
    bad = [v for v in out.values() if v not in ("inside", "waiting")]
    print(f"users: {a.users}   inside: {inside}   waiting room: {waiting}   problems: {len(bad)}")
    for v in bad[:5]:
        print("  ", v)


if __name__ == "__main__":
    main()
