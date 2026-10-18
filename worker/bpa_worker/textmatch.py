"""Offline name/address normalisation and fuzzy matching (RapidFuzz).

Normalisation: lowercase, strip punctuation, join runs of single letters
("M G Road" -> "mg road"), expand the configurable abbreviation list.
No geocoder or network service is used.
"""

import re

from rapidfuzz import fuzz

from bpa_common.config import get

_PUNCT = re.compile(r"[^0-9a-z]+")


class TextMatcher(object):
    def __init__(self, cfg):
        abbrev = get(cfg, "text.abbreviations", {}) or {}
        self.abbrev = {str(k).lower().replace("-", ""): str(v).lower() for k, v in abbrev.items()}
        self.generic = set(str(t).lower() for t in (get(cfg, "text.generic_name_tokens", []) or []))
        self.street_generic = set(str(t).lower() for t in (get(cfg, "text.generic_street_tokens", []) or
                                                            ["road", "street", "lane", "path", "cross", "highway",
                                                             "avenue", "way"]))
        self.noise_floor = float(get(cfg, "scoring.text_noise_floor", 0.0) or 0.0)
        self.ramp = bool(get(cfg, "scoring.text_ramp", False))
        self.org_ignore = set(str(t).lower() for t in (get(cfg, "text.organisation_ignore_tokens", []) or []))
        self.brands = sorted(((self._plain(a), self._plain(c))
                              for a, c in (get(cfg, "text.brand_aliases", {}) or {}).items()),
                             key=lambda ac: -len(ac[0]))     # "kotak bank" before "kotak"
        self._cache = {}
        self._generic_cache = {}

    # -- normalisation ---------------------------------------------------------
    def tokens(self, text):
        if not text:
            return []
        key = str(text)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        t = key.lower().replace("&", " and ")
        t = re.sub(r"(?<=[a-z])-(?=[a-z])", "", t)   # co-op -> coop
        raw = [w for w in _PUNCT.split(t) if w]
        merged = []
        run = []
        for w in raw:
            if len(w) == 1 and w.isalpha():
                run.append(w)
                continue
            if run:
                merged.append("".join(run))
                run = []
            merged.append(w)
        if run:
            merged.append("".join(run))
        out = []
        for w in merged:
            exp = self.abbrev.get(w)
            out.extend(exp.split() if exp else [w])
        if len(self._cache) < 200000:
            self._cache[key] = out
        return out

    def normalise(self, text):
        return " ".join(self.tokens(text))

    def _is_generic(self, w):
        """Generic building word, including common misspellings ("appartment", "bulding")."""
        if w in self.generic:
            return True
        hit = self._generic_cache.get(w)
        if hit is None:
            hit = len(w) >= 6 and any(len(g) >= 6 and fuzz.ratio(w, g) >= 85 for g in self.generic)
            self._generic_cache[w] = hit
        return hit

    def core_name(self, text):
        """Normalised name without generic building-type words."""
        return " ".join(w for w in self.tokens(text) if not self._is_generic(w))

    # -- similarity -------------------------------------------------------------
    @staticmethod
    def _sim(a, b):
        """token_set_ratio / 100, also trying the space-free forms ("sai dham" = "saidham")."""
        if not a or not b:
            return 0.0
        s = fuzz.token_set_ratio(a, b) / 100.0
        a2, b2 = a.replace(" ", ""), b.replace(" ", "")
        if len(a2) >= 4 and len(b2) >= 4:
            s = max(s, fuzz.ratio(a2, b2) / 100.0)
        return s

    @staticmethod
    def name_sim(a, b):
        """Building-name similarity that does not reward a short name hidden inside a long one.

        token_set_ratio (the spec's measure) scores 1.0 when one name's words are
        a subset of the other, so a place called just "Dahisar" would fully match
        "Dahisar Madhuram Hall". Here that score is multiplied by the share of the
        survey name's words found in the candidate name (typos of 1-2 letters
        allowed). The space-free comparison still catches "Mangalmurthi" =
        "Mangal Murti" and "Saidham" = "Sai Dham".
        """
        if not a or not b:
            return 0.0
        ta, tb = a.split(), b.split()
        covered = sum(1 for x in ta if any(x == y or (len(x) >= 4 and fuzz.ratio(x, y) >= 80) for y in tb))
        s = fuzz.token_set_ratio(a, b) / 100.0 * covered / len(ta)
        a2, b2 = a.replace(" ", ""), b.replace(" ", "")
        # Spacing variants only ("Sai Dham" = "Saidham"): lengths must be close, so a
        # short name inside a long one ("Darshan" in "Deep Darshan") is not a match.
        if len(a2) >= 4 and len(b2) >= 4 and min(len(a2), len(b2)) / float(max(len(a2), len(b2))) >= 0.8:
            s = max(s, fuzz.ratio(a2, b2) / 100.0)
        return s

    def floor(self, s):
        """Similarity below the noise floor is no evidence; with ``ramp`` the rest is
        rescaled so a borderline match (0.61) weighs little and an exact one still 1.0."""
        if s < self.noise_floor:
            return 0.0
        if self.ramp and self.noise_floor < 1.0:
            return (s - self.noise_floor) / (1.0 - self.noise_floor)
        return s

    def name_similarity(self, record_name, candidate_names):
        """s_name: best similarity between the building name and any candidate name."""
        a = self.core_name(record_name)
        if not a:
            return 0.0, None
        best, best_name = 0.0, None
        for n in candidate_names or []:
            b = self.core_name(n)
            if not b:
                continue
            s = self.name_sim(a, b)
            if s > best:
                best, best_name = s, n
        best = self.floor(best)
        return best, (best_name if best > 0 else None)

    # -- customer / organisation ---------------------------------------------------
    @staticmethod
    def _plain(text):
        t = re.sub(r"(?<=[a-z])-(?=[a-z])", "", str(text or "").lower().replace("&", " and "))
        return [w for w in _PUNCT.split(t) if w]

    def org_tokens(self, text):
        """Organisation name without legal and outlet words, brand short forms expanded.

        "ICICI Bank Limited" and "ICICI Bank ATM" -> icici bank; "SBI ATM" -> state bank of
        india; "Saraswat Co-op. Bank Ltd" -> saraswat bank. The brand list is in the config.
        """
        toks = [w for w in self.tokens(text) if w not in self.org_ignore]
        for alias, canon in self.brands:
            n = len(alias)
            if alias and toks[:n] == alias:
                rest = toks[n:]
                if canon[:n] == alias:             # "kotak" -> "kotak mahindra bank": keep "Kotak Mahindra"
                    tail = canon[n:]
                    j = 0
                    while j < min(len(rest), len(tail)) and rest[j] == tail[j]:
                        j += 1
                    rest = rest[j:]
                toks = canon + rest
                break
        return toks

    def customer_similarity(self, customer, candidate_names):
        """How well a place in or at the building carries the customer's name (0..1).

        A survey for "ICICI Bank Limited" whose building holds "ICICI Bank ATM" is strong
        evidence; "Bank of India" must not match "State Bank of India", so the place name has
        to start with the customer's name (extra words such as the locality are allowed).
        """
        c = self.org_tokens(customer)
        if not c:
            return 0.0, None
        cs = " ".join(c)
        best, best_name = 0.0, None
        for name in candidate_names or []:
            n = self.org_tokens(name)
            if not n:
                continue
            if n[:len(c)] == c:
                s = 1.0
            elif len(n) >= 2 and c[:len(n)] == n and len(n) / float(len(c)) >= 0.6:
                s = 0.9                            # "Kotak Mahindra" for "Kotak Mahindra Bank"
            else:
                s = fuzz.ratio(cs, " ".join(n)) / 100.0
                s = s if s >= 0.92 else 0.0        # spelling slips only
            if s > best:
                best, best_name = s, name
        best = self.floor(best)
        return best, (best_name if best > 0 else None)

    def street_core(self, text):
        """Normalised street name without generic words ("Kolbad Road" -> "kolbad")."""
        return " ".join(w for w in self.tokens(text) if w not in self.street_generic)

    def street_similarity(self, street_a, street_b):
        """Similarity of two street names on their distinctive words only.

        "Kolbad Road" and "Gokhale Road" share only "road", which is no evidence.
        Falls back to the full names when one of them is nothing but generic words.
        """
        a, b = self.street_core(street_a), self.street_core(street_b)
        if a and b:
            return self._sim(a, b)
        na, nb = self.normalise(street_a), self.normalise(street_b)
        return 1.0 if na and na == nb else 0.0

    def address_mentions_street(self, record_street, address, threshold=0.8):
        """Does a free-form address contain the survey street (typo tolerant)?"""
        core = self.street_core(record_street)
        if len(core.replace(" ", "")) < 3:
            return False
        return fuzz.partial_ratio(core, self.normalise(address)) / 100.0 >= threshold

    def address_similarity(self, record_street, record_locality_tokens, candidate_addresses,
                           requires_street=True, street_threshold=0.8):
        """s_addr: street + locality + pincode tokens vs candidate address data.

        With ``requires_street`` the locality/pincode tokens only count when the
        candidate address also names the survey street; locality and pincode
        alone are shared by every neighbouring building and prove nothing.
        """
        street = self.normalise(record_street)
        full = " ".join(x for x in [street] + [self.normalise(t) for t in record_locality_tokens] if x)
        if not full:
            return 0.0
        best = 0.0
        for addr in candidate_addresses or []:
            b = self.normalise(addr)
            if not b:
                continue
            if requires_street and not self.address_mentions_street(record_street, addr, street_threshold):
                continue
            best = max(best, fuzz.token_set_ratio(full, b) / 100.0)
        return self.floor(best)
