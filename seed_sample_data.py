"""
Sample data for FoodResQ demos and for trying the Sampling / Analytics pages.

    python seed_sample_data.py --yes            add sample rows
    python seed_sample_data.py --remove --yes   delete ONLY the sample rows

Every sample account uses an e-mail ending in @sample.foodresq.test, so real
data is never touched.  Donor and NGO password for all samples: Sample@1234
"""
import random
import sys
from datetime import date, datetime, time, timedelta

from werkzeug.security import generate_password_hash

from core import get_db

DOMAIN = "@sample.foodresq.test"
PASSWORD = "Sample@1234"
CITIES = [("Solapur", 17.6599, 75.9064), ("Pune", 18.5204, 73.8567),
          ("Mumbai", 19.0760, 72.8777), ("Nashik", 19.9975, 73.7898),
          ("Nagpur", 21.1458, 79.0882)]
FIRST = ["Asha", "Ravi", "Meera", "Kiran", "Sunita", "Arjun", "Neha", "Vikram",
         "Pooja", "Rahul", "Anita", "Sanjay", "Divya", "Manoj", "Kavita", "Rohit"]
LAST = ["Patil", "Shah", "Deshmukh", "Kulkarni", "Jadhav", "Joshi", "More", "Pawar"]
FOODS = [("Cooked meals", "Cooked"), ("Bread and buns", "Bakery"),
         ("Rice and dal", "Cooked"), ("Vegetables", "Raw"),
         ("Fruit boxes", "Raw"), ("Sandwiches", "Bakery")]
NGO_NAMES = ["Annapurna Seva Trust", "Hope Kitchen Foundation", "Roti Bank",
             "Sahara Care Society", "Anna Daan Mandal", "Jeevan Jyoti Trust"]


def add(cur, n_donors, n_ngos, n_donations, rng):
    ph = generate_password_hash(PASSWORD)
    donors = []
    for i in range(n_donors):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        cur.execute("INSERT INTO users (name,email,phone,password,status) VALUES (%s,%s,%s,%s,'Active')",
                    (name, f"donor{i + 1}{DOMAIN}", f"9{rng.randint(100000000, 999999999)}", ph))
        donors.append(cur.lastrowid)

    ngos = []
    for i in range(n_ngos):
        city, lat, lng = CITIES[i % len(CITIES)]
        cur.execute("""INSERT INTO ngos (ngo_name,registration_number,person_name,email,phone,password,
                       latitude,longitude,address,status,service_area,organization_type,pincode,designation)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'Active',%s,'Trust',%s,'Manager')""",
                    (NGO_NAMES[i % len(NGO_NAMES)] + f" {i + 1}", f"SMP/2026/{1000 + i}",
                     f"{rng.choice(FIRST)} {rng.choice(LAST)}", f"ngo{i + 1}{DOMAIN}",
                     f"8{rng.randint(100000000, 999999999)}", ph, lat, lng,
                     f"{city} central", city, "413001"))
        ngos.append(cur.lastrowid)

    start, today = date(2026, 8, 1), date(2026, 9, 30)
    for _ in range(n_donations):
        city, lat, lng = rng.choice(CITIES)
        food, kind = rng.choice(FOODS)
        pickup = start + timedelta(days=rng.randint(0, (today - start).days + 60))
        if pickup > today:                       # future pickups are still pending
            status, ngo, rating = "Pending", None, None
        else:
            status = rng.choices(["Completed", "Pending", "Cancelled"], [6, 2, 1])[0]
            ngo = rng.choice(ngos) if status == "Completed" else None
            rating = rng.randint(3, 5) if status == "Completed" else None
        cur.execute("""INSERT INTO donations (user_id,food_name,food_type,quantity,quantity_unit,address,city,
                       latitude,longitude,pickup_date,pickup_time,contact,status,ngo_id,rating)
                       VALUES (%s,%s,%s,%s,'plates',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (rng.choice(donors), food, kind, rng.randint(5, 80), f"{city} main road", city,
                     lat, lng, pickup, time(rng.randint(9, 20), rng.choice([0, 30])),
                     f"9{rng.randint(100000000, 999999999)}", status, ngo, rating))


def remove(cur):
    cur.execute("SELECT id FROM users WHERE email LIKE %s", ("%" + DOMAIN,))
    uids = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT ngo_id FROM ngos WHERE email LIKE %s", ("%" + DOMAIN,))
    nids = [r[0] for r in cur.fetchall()]
    if uids:
        cur.execute("DELETE FROM donations WHERE user_id IN (%s)" % ",".join(["%s"] * len(uids)), uids)
    cur.execute("DELETE FROM users WHERE email LIKE %s", ("%" + DOMAIN,))
    cur.execute("DELETE FROM ngos WHERE email LIKE %s", ("%" + DOMAIN,))
    return len(uids), len(nids)


def main():
    if "--yes" not in sys.argv:
        print(__doc__)
        print("Add --yes to confirm.")
        return
    db = get_db()
    cur = db.cursor()
    try:
        if "--remove" in sys.argv:
            u, n = remove(cur)
            db.commit()
            print(f"Removed {u} sample donors, {n} sample NGOs and their donations.")
        else:
            add(cur, 40, 6, 300, random.Random(2026))
            db.commit()
            print("Added 40 donors, 6 NGOs, 300 donations (Aug 2026 onward, some upcoming).")
            print(f"Sample login password: {PASSWORD}   e.g. donor1{DOMAIN}")
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


if __name__ == "__main__":
    main()
