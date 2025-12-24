import re
import unicodedata
import calendar
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple, Callable, List, Pattern
import dateparser

class TemporalQueryNormalizer:
    """
    Extracts temporal expressions from queries and normalizes them to date ranges.

    Design principles:
    - Deterministic (no guessing from bare numbers)
    - Multilingual via explicit unit mappings
    - Language-agnostic for numeric / ISO dates
    - Full-day UTC-aligned ranges
    """

    def __init__(self, prefer_dd_mm: bool = True):
        # Ordered: higher precision first. Compile regexes once to avoid repeated
        # compilation on every query which can be costly for long queries.
        self.universal_patterns: List[Tuple[Pattern, Callable]] = [
            # ISO date: 2024-01-15 or 2024-01-15T10:30
            (re.compile(r'(\d{4})-(\d{2})-(\d{2})(?:T[\d:]+)?'), self._parse_iso_date),

            # Day + month name, e.g. '5 Jan' or '5 janvier' (month name parsing uses dateparser if available)
            (re.compile(r"\b(\d{1,2})\s+([A-Za-zÀ-ÖØ-öø-ÿ\-']+)(?:\s+(\d{4}))?\b"), self._parse_day_month_name),

            # Numeric dates: 15/01/2024, 01-15-2024, 15.01.2024
            (re.compile(r'\b(\d{1,2})[\/\.\-](\d{1,2})[\/\.\-](\d{4})\b'), self._parse_numeric_date),

            # Month-year: 01/2024 or 01-2024
            (re.compile(r'\b(\d{1,2})[\/\-](\d{4})\b'), self._parse_month_year),

            # Year-month: 2024/01 or 2024-01
            (re.compile(r'\b(\d{4})[\/\-](\d{1,2})\b'), self._parse_year_month),

            # Year only
            (re.compile(r'\b(20\d{2})\b'), self._parse_year_only),
        ]

        # Explicit multilingual time units (no guessing)
        self.time_units = {
            # days
            "day": 1, "days": 1,
            "jour": 1, "jours": 1,
            "día": 1, "días": 1,
            "tag": 1, "tage": 1,
            "giorno": 1, "giorni": 1,
            "dia": 1, "dias": 1,

            # weeks
            "week": 7, "weeks": 7,
            "semaine": 7, "semaines": 7,
            "semana": 7, "semanas": 7,
            "woche": 7, "wochen": 7,

            # months (approximate)
            "month": 30, "months": 30,
            "mois": 30,
            "mes": 30, "meses": 30,
            "monat": 30, "monate": 30,

            # years
            "year": 365, "years": 365,
            "an": 365, "ans": 365,
            "año": 365, "años": 365,
            "jahr": 365, "jahre": 365,
        }

        self.relative_prefix_words = [
            # English
            "last", "past", "previous", "in the last", "in the past", "over the past",
            "within the past", "during the past", "for the past", "over the last", "in last",

            # Spanish
            "hace", "últimos", "últimas", "último", "última",

            # French
            "il y a", "derniers", "dernières", "dernier", "dernière",

            # German
            "vor", "letzten", "letzte", "letztes",

            # Portuguese
            "há", "ultimos", "últimos", "ultimas", "últimas",

            # Italian
            "fa", "ultimi", "ultime", "ultimo", "ultima",
        ]

        self.relative_suffix_words = [
            # English
            "ago",

            # Spanish/Portuguese
            "atrás",

            # Italian
            "fa",
        ]
        # Build regexes from folded (accent-stripped) forms so accents are optional
        def _fold_list(words):
            folded = []
            for w in words:
                f = unicodedata.normalize('NFD', w)
                f = ''.join(ch for ch in f if not unicodedata.combining(ch))
                folded.append(f)
            return folded

        folded_prefixes = _fold_list(self.relative_prefix_words)
        folded_suffixes = _fold_list(self.relative_suffix_words)

        # Accept either digits or simple hyphenated/word number tokens (e.g. "two", "twenty-one")
        number_token = r'([0-9]+|[A-Za-z-]+)'
        unit_token = r'(\w+)'

        prefix_re = r"\b(?:" + "|".join(re.escape(p) for p in folded_prefixes) + r")\b\s*" + number_token + r"\s*" + unit_token
        suffix_re = r"\b" + number_token + r"\s*" + unit_token + r"\s*(?:" + "|".join(re.escape(s) for s in folded_suffixes) + r")\b"

        self.relative_prefix_pattern = re.compile(prefix_re, re.IGNORECASE)
        self.relative_suffix_pattern = re.compile(suffix_re, re.IGNORECASE)

        # Build a normalized time_units mapping (folded, lowercase) and add simple
        # plural/singular variants to handle common pluralization across languages.
        self.normalized_time_units = {}
        for k, v in self.time_units.items():
            fk = unicodedata.normalize('NFD', k)
            fk = ''.join(ch for ch in fk if not unicodedata.combining(ch)).lower()
            # base form
            self.normalized_time_units[fk] = v
            # add simple plural (append 's') if not already
            if not fk.endswith('s'):
                self.normalized_time_units[fk + 's'] = v
            # if it ends with 's', add singular by removing trailing 's'
            else:
                singular = fk[:-1]
                if singular:
                    self.normalized_time_units[singular] = v
            # handle common suffixes: 'es' -> remove, 'ies' -> y
            if fk.endswith('es') and len(fk) > 2:
                self.normalized_time_units[fk[:-2]] = v
            if fk.endswith('ies') and len(fk) > 3:
                self.normalized_time_units[fk[:-3] + 'y'] = v

        # Low-ambiguity multilingual keywords
        self.keyword_ranges = {
            "today": 0,
            "aujourd'hui": 0,
            "heute": 0,
            "hoy": 0,
            "oggi": 0,
            "hoje": 0,

            "yesterday": 1,
            "hier": 1,
            "ayer": 1,
            "ieri": 1,
            "ontem": 1,
        }
        # Preference for interpreting ambiguous numeric dates like 01/02/2024:
        # - True: prefer DD/MM (day=01, month=02)
        # - False: prefer MM/DD (month=01, day=02)
        self.prefer_dd_mm = prefer_dd_mm

    # -------------------- Parsing helpers --------------------

    def _parse_iso_date(self, match):
        y, m, d = map(int, match.groups()[:3])
        return self._specific_date(y, m, d)

    def _parse_numeric_date(self, match):
        a, b, y = map(int, match.groups())

        # Disambiguate ambiguous numeric dates according to preference.
        # If prefer_dd_mm is True, interpret as DD/MM/YYYY when possible.
        if self.prefer_dd_mm:
            # Prefer DD/MM if the second group is a valid month
            if 1 <= b <= 12 and 1 <= a <= 31:
                return self._specific_date(y, b, a)
            # Fallback to MM/DD if the first group looks like a month
            if 1 <= a <= 12 and 1 <= b <= 31:
                return self._specific_date(y, a, b)
        else:
            # Prefer MM/DD if the first group is a valid month
            if 1 <= a <= 12 and 1 <= b <= 31:
                return self._specific_date(y, a, b)
            # Fallback to DD/MM
            if 1 <= b <= 12 and 1 <= a <= 31:
                return self._specific_date(y, b, a)

        raise ValueError

    def _parse_month_year(self, match):
        m, y = map(int, match.groups())
        return self._month_range(y, m)

    def _parse_year_month(self, match):
        y, m = map(int, match.groups())
        return self._month_range(y, m)

    def _parse_year_only(self, match):
        return self._year_range(int(match.group(1)))

    def _parse_day_month_name(self, match):
        # match groups: day, monthname, optional year
        day = int(match.group(1))
        month_token = match.group(2)
        year = None
        if match.group(3):
            year = int(match.group(3))
        else:
            year = datetime.now(timezone.utc).year

        month = self._month_name_to_int(month_token)
        if month is None:
            raise ValueError
        return self._specific_date(year, month, day)

    def _month_name_to_int(self, token: str) -> Optional[int]:
        """Convert a month name (multilingual) to its month number (1-12).

        Tries `dateparser` if available, otherwise falls back to a small
        multilingual lookup using folded month names.
        """
        if not token:
            return None
        folded = unicodedata.normalize('NFD', token)
        folded = ''.join(ch for ch in folded if not unicodedata.combining(ch)).lower()

        try:
            dt = dateparser.parse(f"1 {token} 2000")
            if dt and isinstance(dt, datetime):
                return dt.month
        except Exception:
            pass

        # fallback small multilingual map (folded forms)
        months = {
            'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
            'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
            # Spanish
            'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5, 'junio': 6,
            'julio': 7, 'agosto': 8, 'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
            # French (folded)
            'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5, 'juin': 6,
            'juillet': 7, 'aout': 8, 'septembre': 9, 'octobre': 10, 'novembre': 11, 'decembre': 12,
            # German (folded)
            'januar': 1, 'februar': 2, 'marz': 3, 'april': 4, 'mai': 5, 'juni': 6,
            'juli': 7, 'august': 8, 'september': 9, 'oktober': 10, 'november': 11, 'dezember': 12,
            # Italian
            'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4, 'maggio': 5, 'giugno': 6,
            'luglio': 7, 'agosto': 8, 'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
            # Portuguese
            'janeiro': 1, 'fevereiro': 2, 'marco': 3, 'abril': 4, 'maio': 5, 'junho': 6,
            'julho': 7, 'agosto': 8, 'setembro': 9, 'outubro': 10, 'novembro': 11, 'dezembro': 12,
        }

        # accept short forms
        if len(folded) <= 4:
            # try month abbreviations in english and folded languages
            abbrev_map = {k[:3]: v for k, v in months.items()}
            if folded in abbrev_map:
                return abbrev_map[folded]

        return months.get(folded)

    # -------------------- Range builders --------------------

    def _specific_date(self, year: int, month: int, day: int):
        start = datetime(year, month, day, tzinfo=timezone.utc)
        end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start, end

    def _month_range(self, year: int, month: int):
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        # use calendar.monthrange to get the last day of the month reliably
        last_day = calendar.monthrange(year, month)[1]
        end = datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=timezone.utc)
        return start, end

    def _year_range(self, year: int):
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
        return start, end

    def _last_n_days(self, days: int):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        return start, end

    # -------------------- Extraction logic --------------------

    def _extract_relative(self, query: str):
        # Fold accents in the query so accents are optional in user input
        folded_query = unicodedata.normalize('NFD', query)
        folded_query = ''.join(ch for ch in folded_query if not unicodedata.combining(ch))

        # Prefer explicit contextual prefixes like "last", "past", etc.
        m = self.relative_prefix_pattern.search(folded_query)
        if m:
            raw_value = m.group(1)
            unit = m.group(2)
            # fold unit
            uf = unicodedata.normalize('NFD', unit)
            uf = ''.join(ch for ch in uf if not unicodedata.combining(ch)).lower()
            value = None
            if raw_value.isdigit():
                value = int(raw_value)
            else:
                value = self._word_to_int(raw_value)
            if value is None:
                return None
            if uf in self.normalized_time_units:
                days = value * self.normalized_time_units[uf]
                return self._last_n_days(days)

        # Also accept suffixes like "5 years ago"
        m = self.relative_suffix_pattern.search(folded_query)
        if m:
            raw_value = m.group(1)
            unit = m.group(2)
            uf = unicodedata.normalize('NFD', unit)
            uf = ''.join(ch for ch in uf if not unicodedata.combining(ch)).lower()
            value = None
            if raw_value.isdigit():
                value = int(raw_value)
            else:
                value = self._word_to_int(raw_value)
            if value is None:
                return None
            if uf in self.normalized_time_units:
                days = value * self.normalized_time_units[uf]
                return self._last_n_days(days)

        return None

    def _extract_keywords(self, query: str):
        q = unicodedata.normalize('NFD', query)
        q = ''.join(ch for ch in q if not unicodedata.combining(ch)).lower()
        for word, offset in self.keyword_ranges.items():
            # fold keyword before searching
            kw = unicodedata.normalize('NFD', word)
            kw = ''.join(ch for ch in kw if not unicodedata.combining(ch)).lower()
            if kw in q:
                day = datetime.now(timezone.utc) - timedelta(days=offset)
                start = day.replace(hour=0, minute=0, second=0, microsecond=0)
                end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
                return start, end
        return None

    def _word_to_int(self, token: str) -> Optional[int]:
        """Convert a (folded) number-word or hyphenated number-word to an int.

        Supports small numbers in several languages (common words up to 20 and tens).
        Returns None if unknown.
        """
        t = token.lower()
        # normalize hyphens/spaces
        t = t.replace('\u2013', '-')
        t = t.replace('\u2014', '-')
        t = t.replace('–', '-')
        t = t.replace('—', '-')

        # simple vocab mapping (folded words)
        mapping = {
            # English
            'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
            'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
            'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
            'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20,
            'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
            'a': 1, 'an': 1, 'couple': 2,

            # Spanish
            'uno': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5, 'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10,
            'once': 11, 'doce': 12, 'trece': 13, 'catorce': 14, 'quince': 15, 'dieciseis': 16, 'diecisiete': 17, 'dieciocho': 18, 'diecinueve': 19, 'veinte': 20,

            # French (folded)
            'un': 1, 'deux': 2, 'trois': 3, 'quatre': 4, 'cinq': 5, 'six': 6, 'sept': 7, 'huit': 8, 'neuf': 9, 'dix': 10,
            'onze': 11, 'douze': 12, 'treize': 13, 'quatorze': 14, 'quinze': 15, 'seize': 16, 'vingt': 20,

            # German
            'eins': 1, 'zwei': 2, 'drei': 3, 'vier': 4, 'funf': 5, 'sechs': 6, 'sieben': 7, 'acht': 8, 'neun': 9, 'zehn': 10,
            'elf': 11, 'zwolf': 12, 'dreizehn': 13, 'vierzehn': 14, 'funfzehn': 15, 'sechzehn': 16, 'siebzehn': 17, 'achtzehn': 18, 'neunzehn': 19, 'zwanzig': 20,

            # Italian
            'uno': 1, 'due': 2, 'tre': 3, 'quattro': 4, 'cinque': 5, 'sei': 6, 'sette': 7, 'otto': 8, 'nove': 9, 'dieci': 10,

            # Portuguese
            'um': 1, 'dois': 2, 'tres': 3, 'quatro': 4, 'cinco': 5, 'seis': 6, 'sete': 7, 'oito': 8, 'nove': 9, 'dez': 10,
        }

        # scale words mapping (folded)
        scales = {
            # English
            'hundred': 100, 'thousand': 1000, 'million': 1000000,
            # Spanish
            'cien': 100, 'ciento': 100, 'mil': 1000, 'millon': 1000000, 'millones': 1000000,
            # French
            'cent': 100, 'mille': 1000, 'million': 1000000, 'millions': 1000000,
            # German
            'hundert': 100, 'tausend': 1000, 'million': 1000000,
            # Italian
            'cento': 100, 'mille': 1000, 'milione': 1000000, 'milioni': 1000000,
            # Portuguese
            'cem': 100, 'cento': 100, 'mil': 1000, 'milhao': 1000000, 'milhoes': 1000000,
        }

        # quick exact match
        if t in mapping:
            return mapping[t]

        # split into tokens (spaces and hyphens)
        parts = re.split('[-\s]+', t)
        if not parts:
            return None

        total = 0
        current = 0
        any_matched = False

        for p in parts:
            if not p or p == 'and':
                continue
            any_matched = True
            if p in mapping:
                current += mapping[p]
                continue
            if p in scales:
                scale = scales[p]
                if current == 0:
                    current = 1
                current *= scale
                # for thousand/million we add to total and reset current
                if scale >= 1000:
                    total += current
                    current = 0
                continue
            # unknown token -> cannot parse
            return None

        result = total + current
        if any_matched and result > 0:
            return result
        return None

    def _extract_explicit_date_range(self, query: str) -> Optional[Tuple[datetime, datetime]]:
        """Find two explicit dates in the query and return them as a range if
        they are separated by a connector like 'to'/'until' or preceded by 'from'.
        Uses the compiled universal patterns to find date tokens.
        """
        q = query
        # collect matches from all universal patterns
        matches = []  # list of (start_idx, end_idx, (start_dt, end_dt))
        for pattern, handler in self.universal_patterns:
            for m in pattern.finditer(q):
                try:
                    start_dt, end_dt = handler(m)
                except Exception:
                    continue
                matches.append((m.start(), m.end(), start_dt, end_dt))

        if not matches:
            return None

        # sort and filter overlapping, keep earliest non-overlapping matches
        matches.sort(key=lambda x: x[0])
        filtered = []
        last_end = -1
        for sidx, eidx, sd, ed in matches:
            if sidx >= last_end:
                filtered.append((sidx, eidx, sd, ed))
                last_end = eidx

        if len(filtered) < 2:
            return None

        # check pairwise for connectors between consecutive date matches
        connectors = ["to", "until", "through", "-", "–", "and"]
        folded = unicodedata.normalize('NFD', q)
        folded = ''.join(ch for ch in folded if not unicodedata.combining(ch)).lower()

        ranges = []
        for i in range(len(filtered) - 1):
            s1, e1, sd1, ed1 = filtered[i]
            s2, e2, sd2, ed2 = filtered[i + 1]

            between = folded[e1:s2].strip()
            # quick connector check
            has_connector = any(c in between for c in connectors)
            # or 'from' before first date
            before = folded[max(0, s1 - 10):s1]
            has_from = 'from' in before or 'between' in before

            if has_connector or has_from:
                ranges.append((sd1, ed2))

        if not ranges:
            return None

        # return the first range (backwards-compatible single-range helper)
        return ranges[0]

    def _extract_explicit_date_ranges(self, query: str) -> List[Tuple[datetime, datetime]]:
        """Return all explicit date ranges found in the query as a list of
        (start_dt, end_dt) tuples. This scans for adjacent date matches with
        connectors and returns every pair that forms a range.
        """
        q = query
        matches = []
        for pattern, handler in self.universal_patterns:
            for m in pattern.finditer(q):
                try:
                    start_dt, end_dt = handler(m)
                except Exception:
                    continue
                matches.append((m.start(), m.end(), start_dt, end_dt))

        if not matches:
            return []

        matches.sort(key=lambda x: x[0])
        filtered = []
        last_end = -1
        for sidx, eidx, sd, ed in matches:
            if sidx >= last_end:
                filtered.append((sidx, eidx, sd, ed))
                last_end = eidx

        if len(filtered) < 2:
            return []

        connectors = ["to", "until", "through", "-", "–", "and"]
        folded = unicodedata.normalize('NFD', q)
        folded = ''.join(ch for ch in folded if not unicodedata.combining(ch)).lower()

        ranges = []
        for i in range(len(filtered) - 1):
            s1, e1, sd1, ed1 = filtered[i]
            s2, e2, sd2, ed2 = filtered[i + 1]
            between = folded[e1:s2].strip()
            has_connector = any(c in between for c in connectors)
            before = folded[max(0, s1 - 10):s1]
            has_from = 'from' in before or 'between' in before
            if has_connector or has_from:
                ranges.append((sd1, ed2))

        return ranges

    # -------------------- Public API --------------------

    def extract_temporal_filter(self, query: str) -> Optional[Dict[str, str]]:
        # Prefer explicit ranges; collect all ranges but keep backward compatibility
        # by returning only the first range from this function. Use
        # `extract_temporal_filters` to get all ranges.
        explicit_ranges = self._extract_explicit_date_ranges(query)
        if explicit_ranges:
            start, end = explicit_ranges[0]
            return {
                "created_after": start.isoformat(),
                "created_before": end.isoformat(),
            }

        for pattern, handler in self.universal_patterns:
            # `pattern` is a compiled regex Pattern; use its search() method.
            match = pattern.search(query)
            if match:
                try:
                    start, end = handler(match)
                    return {
                        "created_after": start.isoformat(),
                        "created_before": end.isoformat(),
                    }
                except ValueError:
                    pass

        relative = self._extract_relative(query)
        if relative:
            start, end = relative
            return {
                "created_after": start.isoformat(),
                "created_before": end.isoformat(),
            }

        keyword = self._extract_keywords(query)
        if keyword:
            start, end = keyword
            return {
                "created_after": start.isoformat(),
                "created_before": end.isoformat(),
            }

        return None

    def extract_temporal_filters(self, query: str) -> List[Dict[str, str]]:
        """Return all temporal ranges found in `query` as a list of dicts with
        `created_after`/`created_before` ISO strings. This includes explicit
        date ranges and relative/keyword ranges where appropriate.
        """
        results: List[Dict[str, str]] = []

        # explicit ranges first
        explicit_ranges = self._extract_explicit_date_ranges(query)
        for s, e in explicit_ranges:
            results.append({"created_after": s.isoformat(), "created_before": e.isoformat()})

        # keep previous single-match behavior for numeric/universal patterns
        # only if no explicit ranges found
        if not results:
            for pattern, handler in self.universal_patterns:
                match = pattern.search(query)
                if match:
                    try:
                        s, e = handler(match)
                        results.append({"created_after": s.isoformat(), "created_before": e.isoformat()})
                        break
                    except ValueError:
                        pass

        # relative and keyword ranges (only add if none found)
        if not results:
            relative = self._extract_relative(query)
            if relative:
                s, e = relative
                results.append({"created_after": s.isoformat(), "created_before": e.isoformat()})
            else:
                keyword = self._extract_keywords(query)
                if keyword:
                    s, e = keyword
                    results.append({"created_after": s.isoformat(), "created_before": e.isoformat()})

        return results

    def augment_query(self, query: str, temporal_filter: Optional[Dict[str, str]] = None) -> str:
        if temporal_filter is None:
            temporal_filter = self.extract_temporal_filter(query)

        if not temporal_filter:
            return query

        try:
            parts = []
            after = before = None
            
            if "created_after" in temporal_filter:
                after = datetime.fromisoformat(temporal_filter["created_after"])
            if "created_before" in temporal_filter:
                before = datetime.fromisoformat(temporal_filter["created_before"])
            
            # Check if it's a single day (start and end on same date)
            # Use portable day formatting: avoid %-d (not supported on Windows)
            def _fmt(dt: datetime) -> str:
                return dt.strftime('%d %B %Y').lstrip('0')

            if after and before and after.date() == before.date():
                parts.append(f"on {_fmt(after)}")
            else:
                if after:
                    parts.append(f"from {_fmt(after)}")
                if before:
                    parts.append(f"until {_fmt(before)}")
            
            return f"{query} ({' '.join(parts)})" if parts else query
        except (ValueError, KeyError):
            # Invalid temporal filter, return original query
            return query
            

        return f"{query} ({' '.join(parts)})"
