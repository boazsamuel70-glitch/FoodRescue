"""
FoodResQ sampling engine
========================
Seven textbook sampling methods that work on any list of records
(dicts).  No database code lives here, so every method is unit-tested
on its own.

PROBABILITY  (every member has a known chance of selection)
    simple_random   equal chance for everyone
    systematic      random start, then every k-th record
    stratified      split into subgroups, sample inside each subgroup
    cluster         split into natural groups, pick whole groups

NON-PROBABILITY  (no random chance, cheaper but can be biased)
    convenience     the easiest / most recent records
    quota           fill a fixed number per category, first come first served
    snowball        start from seed records and follow their connections

Every function returns a `Result` with the chosen records plus the
facts an auditor needs: population size, sample size, sampling
fraction, per-group breakdown and a plain-language bias note.
"""

import math
import random
import re
from collections import OrderedDict, deque

PROBABILITY = "Probability"
NON_PROBABILITY = "Non-probability"

MAX_SAMPLE = 5000


class SamplingError(ValueError):
    """A user-facing problem with the requested sample."""


class Result:
    def __init__(self, method, label, family, population, sample,
                 groups=None, note="", details=None):
        self.method = method
        self.label = label
        self.family = family
        self.population_size = len(population)
        self.sample = sample
        self.sample_size = len(sample)
        self.groups = groups or []          # [{name, population, sampled}]
        self.note = note
        self.details = details or []        # short "how it was drawn" lines

    @property
    def fraction(self):
        if not self.population_size:
            return 0.0
        return round(self.sample_size / self.population_size * 100, 2)

    def as_dict(self):
        return {"method": self.method, "label": self.label,
                "family": self.family, "population_size": self.population_size,
                "sample_size": self.sample_size, "fraction": self.fraction,
                "groups": self.groups, "note": self.note,
                "details": self.details}


# ---------------------------------------------------------------- helpers

def _check_n(population, n):
    if not population:
        raise SamplingError("There are no records to sample from.")
    if not isinstance(n, int) or n < 1:
        raise SamplingError("Sample size must be a whole number of 1 or more.")
    if n > MAX_SAMPLE:
        raise SamplingError(f"Sample size cannot be more than {MAX_SAMPLE}.")
    if n > len(population):
        raise SamplingError(
            f"Sample size ({n}) is larger than the population ({len(population)}).")


def _group(population, key):
    groups = OrderedDict()
    for record in population:
        value = record.get(key)
        value = "Unspecified" if value in (None, "") else str(value)
        groups.setdefault(value, []).append(record)
    return groups


def _rng(seed):
    return seed if isinstance(seed, random.Random) else random.Random(seed)


def _allocate(sizes, n):
    """Proportional allocation with the largest-remainder rule.

    Guarantees: the parts add up to exactly n, no part exceeds its
    group size, and (when n allows) every group gets at least one.
    """
    total = sum(sizes)
    raw = [s * n / total for s in sizes]
    alloc = [min(int(math.floor(r)), s) for r, s in zip(raw, sizes)]

    # every group represented when the sample is big enough
    if n >= len(sizes):
        alloc = [max(a, 1) for a in alloc]

    # trim if the minimum-one rule overshot
    while sum(alloc) > n:
        i = max(range(len(alloc)), key=lambda j: alloc[j])
        alloc[i] -= 1

    # hand out the remainder by largest fractional part
    order = sorted(range(len(sizes)),
                   key=lambda j: raw[j] - math.floor(raw[j]), reverse=True)
    while sum(alloc) < n:
        progressed = False
        for j in order:
            if sum(alloc) >= n:
                break
            if alloc[j] < sizes[j]:
                alloc[j] += 1
                progressed = True
        if not progressed:
            break
    return alloc


# ------------------------------------------------------------ probability

def simple_random(population, n, seed=None):
    _check_n(population, n)
    rng = _rng(seed)
    sample = rng.sample(population, n)
    return Result(
        "simple_random", "Simple random sampling", PROBABILITY, population, sample,
        note=("Every record had exactly the same chance "
              f"({n}/{len(population)}) of being picked. Unbiased, but a small "
              "sample can by luck miss a rare group."),
        details=["Records were drawn at random without replacement."])


def systematic(population, n, seed=None):
    _check_n(population, n)
    rng = _rng(seed)
    size = len(population)
    k = size // n                                   # sampling interval
    start = rng.randrange(k) if k > 1 else 0
    sample = [population[start + i * k] for i in range(n)]
    return Result(
        "systematic", "Systematic sampling", PROBABILITY, population, sample,
        note=("Random starting point, then every k-th record. Easy to audit. "
              "Can mislead if the list has a hidden repeating pattern that "
              "lines up with the interval."),
        details=[f"Sampling interval k = {size} \u00f7 {n} = {k}.",
                 f"Random start = record #{start + 1}, then every {k}"
                 f"{_ordinal(k)} record."])


def stratified(population, n, key, seed=None):
    _check_n(population, n)
    if not key:
        raise SamplingError("Choose what to split the population by.")
    rng = _rng(seed)
    groups = _group(population, key)
    names = list(groups)
    alloc = _allocate([len(groups[g]) for g in names], n)
    sample, table = [], []
    for name, take in zip(names, alloc):
        picked = rng.sample(groups[name], take) if take else []
        sample.extend(picked)
        table.append({"name": name, "population": len(groups[name]),
                      "sampled": take})
    return Result(
        "stratified", "Stratified sampling", PROBABILITY, population, sample,
        groups=table,
        note=("The population was split into subgroups and each subgroup "
              "was sampled at the same rate, so small groups are still "
              "represented. Best when the groups differ from each other."),
        details=[f"Strata: {len(names)} groups by \u201c{key}\u201d.",
                 "Proportional allocation (largest-remainder rule)."])


def cluster(population, n_clusters, key, seed=None):
    if not population:
        raise SamplingError("There are no records to sample from.")
    if not key:
        raise SamplingError("Choose what defines a cluster.")
    groups = _group(population, key)
    if not isinstance(n_clusters, int) or n_clusters < 1:
        raise SamplingError("Number of clusters must be a whole number of 1 or more.")
    if n_clusters > len(groups):
        raise SamplingError(
            f"Only {len(groups)} clusters exist by \u201c{key}\u201d; "
            f"you asked for {n_clusters}.")
    rng = _rng(seed)
    names = list(groups)
    chosen = rng.sample(names, n_clusters)
    sample = [r for name in chosen for r in groups[name]]
    table = [{"name": g, "population": len(groups[g]),
              "sampled": len(groups[g]) if g in chosen else 0} for g in names]
    if len(sample) > MAX_SAMPLE:
        raise SamplingError(
            f"The chosen clusters hold {len(sample)} records "
            f"(limit {MAX_SAMPLE}). Pick fewer clusters.")
    return Result(
        "cluster", "Cluster sampling", PROBABILITY, population, sample,
        groups=table,
        note=("Whole natural groups were picked at random and every record "
              "inside them is included. Cheap to follow up (one city / one "
              "NGO at a time) but less precise if clusters differ a lot."),
        details=[f"{len(names)} clusters by \u201c{key}\u201d, "
                 f"{n_clusters} chosen at random: " + ", ".join(chosen) + "."])


# --------------------------------------------------------- non-probability

def convenience(population, n, seed=None):
    _check_n(population, n)
    sample = population[:n]                          # list is newest-first
    return Result(
        "convenience", "Convenience sampling", NON_PROBABILITY, population, sample,
        note=("Simply the first records on the list (the most recent ones). "
              "Fast and cheap, but there is no random chance involved, so "
              "results may not represent everyone."),
        details=[f"Took the first {n} records as listed (newest first)."])


_QUOTA_RE = re.compile(r"^\s*([^=,]{1,60}?)\s*[=:]\s*(\d{1,5})\s*$")


def parse_quotas(text):
    """'Completed=5, Pending=3' -> OrderedDict."""
    quotas = OrderedDict()
    for part in (text or "").split(","):
        if not part.strip():
            continue
        m = _QUOTA_RE.match(part)
        if not m:
            raise SamplingError(
                "Quotas must look like  Completed=5, Pending=3  "
                "(a category, an equals sign, then a number).")
        name, count = m.group(1).strip(), int(m.group(2))
        if count < 1:
            raise SamplingError("Each quota must be at least 1.")
        quotas[name] = quotas.get(name, 0) + count
    if not quotas:
        raise SamplingError("Enter at least one quota, for example  Completed=5, Pending=3.")
    return quotas


def quota(population, key, quotas, seed=None):
    if not population:
        raise SamplingError("There are no records to sample from.")
    if not key:
        raise SamplingError("Choose the trait the quotas apply to.")
    if isinstance(quotas, str):
        quotas = parse_quotas(quotas)
    lookup = {name.lower(): name for name in quotas}
    remaining = {name: count for name, count in quotas.items()}
    counted = {name: 0 for name in quotas}
    available = {name: 0 for name in quotas}
    sample = []
    for record in population:                        # first come, first served
        value = str(record.get(key) if record.get(key) not in (None, "") else "Unspecified")
        name = lookup.get(value.lower())
        if name is None:
            continue
        available[name] += 1
        if remaining[name] > 0:
            sample.append(record)
            remaining[name] -= 1
            counted[name] += 1
    if not sample:
        raise SamplingError(
            "No records matched those categories. Check the spelling "
            f"against the values in \u201c{key}\u201d.")
    table = [{"name": g, "population": available[g], "sampled": counted[g],
              "target": quotas[g]} for g in quotas]
    short = [g for g in quotas if counted[g] < quotas[g]]
    details = [f"Quotas by \u201c{key}\u201d: "
               + ", ".join(f"{g} = {c}" for g, c in quotas.items()) + "."]
    if short:
        details.append("Quota not fully met for: " + ", ".join(short)
                       + " (not enough matching records).")
    return Result(
        "quota", "Quota sampling", NON_PROBABILITY, population, sample,
        groups=table,
        note=("A fixed number was filled for each category, taking whoever "
              "came first. The mix looks right, but who is chosen inside "
              "each category is not random."),
        details=details)


def snowball(population, n, seeds=None, seed=None):
    """Start from seed records and follow their connections.

    Each record may carry a `links` set (for example {"ngo:4", "user:9"}).
    Two records are 'connected' when they share a link.
    """
    _check_n(population, n)
    index = {r["id"]: r for r in population}
    by_link = {}
    for r in population:
        for link in r.get("links", ()):
            by_link.setdefault(link, []).append(r["id"])

    def neighbours(rid):
        out = []
        for link in index[rid].get("links", ()):
            for other in by_link.get(link, ()):
                if other != rid:
                    out.append(other)
        return out

    seed_ids = [s for s in (seeds or []) if s in index]
    auto = False
    if not seed_ids:
        auto = True
        best = max(population, key=lambda r: (len(neighbours(r["id"])), -r["id"]))
        seed_ids = [best["id"]]

    picked, seen, waves = [], set(seed_ids), []
    frontier = deque(seed_ids)
    wave_no = 0
    while frontier and len(picked) < n:
        wave_no += 1
        this_wave = []
        for _ in range(len(frontier)):
            rid = frontier.popleft()
            if len(picked) >= n:
                break
            picked.append(index[rid])
            this_wave.append(rid)
            for nb in neighbours(rid):
                if nb not in seen:
                    seen.add(nb)
                    frontier.append(nb)
        waves.append((wave_no, len(this_wave)))

    for r, wave in zip(picked, _wave_labels(waves)):
        r["_wave"] = wave

    note = ("Recruitment spread through connections (donors who gave to the "
            "same NGO, NGOs that served the same donor). Useful for reaching "
            "hard-to-find people, but it can only reach those who are "
            "connected to the starting records, so it is the most biased method.")
    details = [("Started from the most connected record"
                if auto else "Started from your chosen seed record(s)")
               + f" (id {', '.join(str(s) for s in seed_ids)})."]
    details += [f"Wave {w}: {c} record(s) reached." for w, c in waves]
    if len(picked) < n:
        details.append(f"Only {len(picked)} connected records exist; "
                       f"the network ran out before reaching {n}.")
    table = [{"name": f"Wave {w}", "population": "", "sampled": c} for w, c in waves]
    return Result("snowball", "Snowball (network) sampling", NON_PROBABILITY,
                  population, picked, groups=table, note=note, details=details)


def _wave_labels(waves):
    labels = []
    for wave, count in waves:
        labels.extend([wave] * count)
    return labels


def _ordinal(k):
    if 10 <= k % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(k % 10, "th")


METHODS = OrderedDict([
    ("simple_random", ("Simple random", PROBABILITY,
                       "Every record has an equal chance.")),
    ("systematic", ("Systematic", PROBABILITY,
                    "Random start, then every k-th record.")),
    ("stratified", ("Stratified", PROBABILITY,
                    "Split into subgroups, sample each one.")),
    ("cluster", ("Cluster", PROBABILITY,
                 "Pick whole natural groups at random.")),
    ("convenience", ("Convenience", NON_PROBABILITY,
                     "Take the easiest / most recent records.")),
    ("quota", ("Quota", NON_PROBABILITY,
               "Fill a set number for each category.")),
    ("snowball", ("Snowball", NON_PROBABILITY,
                  "Follow connections from seed records.")),
])
