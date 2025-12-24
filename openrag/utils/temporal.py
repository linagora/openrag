import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple, Callable, List


class TemporalQueryNormalizer:
    """
    Extracts temporal expressions from queries and normalizes them to date ranges.

    Design principles:
    - Deterministic (no guessing from bare numbers)
    - Multilingual via explicit unit mappings
    - Language-agnostic for numeric / ISO dates
    - Full-day UTC-aligned ranges
    """

    def __init__(self):
        # Ordered: higher precision first
        self.universal_patterns: List[Tuple[str, Callable]] = [
            # ISO date: 2024-01-15 or 2024-01-15T10:30
            (r'(\d{4})-(\d{2})-(\d{2})(?:T[\d:]+)?', self._parse_iso_date),

            # Numeric dates: 15/01/2024, 01-15-2024, 15.01.2024
            (r'\b(\d{1,2})[\/\.\-](\d{1,2})[\/\.\-](\d{4})\b', self._parse_numeric_date),

            # Month-year: 01/2024 or 01-2024
            (r'\b(\d{1,2})[\/\-](\d{4})\b', self._parse_month_year),

            # Year-month: 2024/01 or 2024-01
            (r'\b(\d{4})[\/\-](\d{1,2})\b', self._parse_year_month),

            # Year only
            (r'\b(20\d{2})\b', self._parse_year_only),
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

        prefix_re = r"\b(?:" + "|".join(re.escape(p) for p in folded_prefixes) + r")\b\s*(\d+)\s*(\w+)"
        suffix_re = r"\b(\d+)\s*(\w+)\s*(?:" + "|".join(re.escape(s) for s in folded_suffixes) + r")\b"

        self.relative_prefix_pattern = re.compile(prefix_re, re.IGNORECASE)
        self.relative_suffix_pattern = re.compile(suffix_re, re.IGNORECASE)

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

    # -------------------- Parsing helpers --------------------

    def _parse_iso_date(self, match):
        y, m, d = map(int, match.groups()[:3])
        return self._specific_date(y, m, d)

    def _parse_numeric_date(self, match):
        a, b, y = map(int, match.groups())

        # Prefer DD/MM/YYYY if valid
        # Note: I have no idea how to correctly disambiguate MM/DD/YYYY vs DD/MM/YYYY
        if 1 <= b <= 12:
            return self._specific_date(y, b, a)

        # Fallback MM/DD/YYYY
        if 1 <= a <= 12:
            return self._specific_date(y, a, b)

        raise ValueError

    def _parse_month_year(self, match):
        m, y = map(int, match.groups())
        return self._month_range(y, m)

    def _parse_year_month(self, match):
        y, m = map(int, match.groups())
        return self._month_range(y, m)

    def _parse_year_only(self, match):
        return self._year_range(int(match.group(1)))

    # -------------------- Range builders --------------------

    def _specific_date(self, year: int, month: int, day: int):
        start = datetime(year, month, day, tzinfo=timezone.utc)
        end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start, end

    def _month_range(self, year: int, month: int):
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            end = datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
        else:
            end = (datetime(year, month + 1, 1, tzinfo=timezone.utc)
                   - timedelta(days=1)).replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
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
            value = int(m.group(1))
            unit = m.group(2).lower()
            if unit in self.time_units:
                days = value * self.time_units[unit]
                return self._last_n_days(days)

        # Also accept suffixes like "5 years ago"
        m = self.relative_suffix_pattern.search(folded_query)
        if m:
            value = int(m.group(1))
            unit = m.group(2).lower()
            if unit in self.time_units:
                days = value * self.time_units[unit]
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

    # -------------------- Public API --------------------

    def extract_temporal_filter(self, query: str) -> Optional[Dict[str, str]]:
        for pattern, handler in self.universal_patterns:
            match = re.search(pattern, query)
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
            if after and before and after.date() == before.date():
                parts.append(f"on {after.strftime('%-d %B %Y')}")
            else:
                if after:
                    parts.append(f"from {after.strftime('%-d %B %Y')}")
                if before:
                    parts.append(f"until {before.strftime('%-d %B %Y')}")
            
            return f"{query} ({' '.join(parts)})" if parts else query
        except (ValueError, KeyError):
            # Invalid temporal filter, return original query
            return query
            

        return f"{query} ({' '.join(parts)})"
