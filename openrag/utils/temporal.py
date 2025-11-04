"""
Temporal utilities for query normalization and date extraction.
Language-agnostic temporal expression extraction.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple


class TemporalQueryNormalizer:
    """
    Extracts temporal expressions from queries and normalizes them to date ranges.
    Works across languages by using:
    1. Universal date patterns (ISO dates, numeric formats)
    2. Number-based relative time extraction
    3. Common English patterns as fallback
    """

    def __init__(self):
        # Universal date patterns (work across all languages)
        # Order matters - more specific patterns first!
        self.universal_patterns = {
            # ISO date format: 2024-01-15, 2024-01-15T10:30:00 (HIGHEST PRIORITY)
            r'(\d{4})-(\d{2})-(\d{2})(?:T[\d:]+)?': self._parse_iso_date,
            
            # Numeric date formats: 15/01/2024, 01/15/2024, 15.01.2024 (BEFORE year-only!)
            r'\b(\d{1,2})[/\.\-](\d{1,2})[/\.\-](\d{4})\b': self._parse_numeric_date,
            
            # Month-year: 01/2024, 01-2024
            r'\b(\d{1,2})[/\-](\d{4})\b': self._parse_month_year,
            
            # Year-month: 2024/01, 2024-01
            r'\b(\d{4})[/\-](\d{1,2})\b': self._parse_month_year_reverse,
            
            # Year only: 2024, 2023 (LOWEST PRIORITY - catches lone years)
            r'\b(20\d{2})\b': self._parse_year_only,
        }
        
        # Pattern to extract numbers for relative time (language-agnostic)
        # Examples: "7 días", "30 jours", "14 Tage", "últimos 7", "derniers 30"
        # Matches: number + optional space + word OR word + optional space + number
        self.relative_number_pattern = r'(\d+)\s*\w+|\w+\s+(\d+)'
        
        # English patterns for backward compatibility
        self.english_patterns = {
            r'\b(today|aujourd\'hui|heute|hoy|oggi|hoje)\b': lambda: self._get_today(),
            r'\b(yesterday|hier|ayer|ieri|ontem)\b': lambda: self._get_yesterday(),
            r'\b(last|past|recent)\b': lambda: self._get_last_n_days(30),
        }

    def _parse_iso_date(self, match) -> Tuple[datetime, datetime]:
        """Parse ISO date format: YYYY-MM-DD or YYYY/MM/DD"""
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return self._get_specific_date(f"{year:04d}-{month:02d}-{day:02d}")
    
    def _parse_year_only(self, match) -> Tuple[datetime, datetime]:
        """Parse year only: 2024"""
        year = int(match.group(1))
        return self._get_year_range(year)
    
    def _parse_numeric_date(self, match) -> Tuple[datetime, datetime]:
        """Parse numeric date: DD/MM/YYYY or MM/DD/YYYY"""
        # Try DD/MM/YYYY format first (more common internationally)
        try:
            day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return self._get_specific_date(f"{year:04d}-{month:02d}-{day:02d}")
        except:
            pass
        
        # Try MM/DD/YYYY format (US)
        try:
            month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return self._get_specific_date(f"{year:04d}-{month:02d}-{day:02d}")
        except:
            pass
        
        # Default to current month if parsing fails
        return self._get_this_month()
    
    def _parse_month_year(self, match) -> Tuple[datetime, datetime]:
        """Parse month-year: MM/YYYY or MM-YYYY"""
        month, year = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return self._get_month_year_range_numeric(month, year)
        return self._get_year_range(year)
    
    def _parse_month_year_reverse(self, match) -> Tuple[datetime, datetime]:
        """Parse year-month: YYYY/MM or YYYY-MM"""
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return self._get_month_year_range_numeric(month, year)
        return self._get_year_range(year)

    def _get_today(self):
        """Get date range for today in UTC."""
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start, end

    def _get_yesterday(self):
        """Get date range for yesterday in UTC."""
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start, end

    def _get_this_week(self):
        """Get date range for current week (Monday to Sunday) in UTC."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=now.weekday())  # Monday
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=6, hours=23, minutes=59, seconds=59, microseconds=999999)
        return start, end

    def _get_this_month(self):
        """Get date range for current month in UTC."""
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # Last day of current month
        if now.month == 12:
            end = now.replace(day=31, hour=23, minute=59, second=59, microsecond=999999)
        else:
            end = (now.replace(month=now.month + 1, day=1) - timedelta(days=1)).replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
        return start, end

    def _get_this_year(self):
        """Get date range for current year in UTC."""
        now = datetime.now(timezone.utc)
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=999999)
        return start, end

    def _get_last_n_days(self, n: int):
        """Get date range for last N days from now in UTC."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=n)
        return start, end

    def _get_year_range(self, year: int):
        """Get date range for a specific year in UTC."""
        start = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
        return start, end

    def _get_month_year_range_numeric(self, month: int, year: int):
        """Get date range for a specific month and year (numeric input) in UTC."""
        start = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
        
        # Get last day of month
        if month == 12:
            end = datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
        else:
            end = (datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)).replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
        return start, end

    def _get_specific_date(self, date_str: str):
        """Get date range for a specific date (full day) in UTC."""
        date = datetime.fromisoformat(date_str)
        # Ensure timezone is UTC
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = date.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start, end

    def _extract_relative_time_from_number(self, query: str) -> Optional[Tuple[datetime, datetime]]:
        """
        Extract relative time from numbers in the query (language-agnostic).
        Handles both "7 days" and "últimos 7" patterns.
        Examples: "7 días", "30 jours", "14 Tage", "últimos 7", "derniers 30"
        """
        matches = re.findall(self.relative_number_pattern, query)
        
        for match in matches:
            try:
                # Extract number from either position (before or after word)
                number = None
                if match[0] and match[0].strip():  # Number before word: "7 días"
                    number = int(match[0])
                elif match[1] and match[1].strip():  # Number after word: "últimos 7"
                    number = int(match[1])
                
                if number is None:
                    continue
                
                # If number is reasonable for days/weeks/months
                if 1 <= number <= 365:
                    # Most likely referring to days if small number
                    if number <= 31:
                        return self._get_last_n_days(number)
                    # Larger numbers likely weeks or days
                    elif number <= 52:
                        # Could be weeks, treat as days
                        return self._get_last_n_days(number)
                    else:
                        # Large number, likely days
                        return self._get_last_n_days(number)
                        
            except (ValueError, IndexError):
                continue
        
        return None

    def extract_temporal_filter(self, query: str) -> Optional[Dict[str, str]]:
        """
        Extract temporal expressions from a query and convert to filter dict.
        Works across multiple languages.
        
        Returns:
            Dict with keys like 'created_after', 'created_before'
            or None if no temporal expression is found.
        """
        # 1. Try universal date patterns first (ISO dates, numeric dates, years)
        for pattern, parse_func in self.universal_patterns.items():
            match = re.search(pattern, query)
            if match:
                try:
                    start, end = parse_func(match)
                    return {
                        'created_after': start.isoformat(),
                        'created_before': end.isoformat(),
                    }
                except:
                    continue
        
        # 2. Try extracting numbers for relative time (language-agnostic)
        relative_result = self._extract_relative_time_from_number(query)
        if relative_result:
            start, end = relative_result
            return {
                'created_after': start.isoformat(),
                'created_before': end.isoformat(),
            }
        
        # 3. Try English patterns as fallback
        query_lower = query.lower()
        for pattern, date_func in self.english_patterns.items():
            match = re.search(pattern, query_lower, re.IGNORECASE)
            if match:
                try:
                    start, end = date_func()
                    return {
                        'created_after': start.isoformat(),
                        'created_before': end.isoformat(),
                    }
                except:
                    continue
        
        return None

    def augment_query(self, query: str, temporal_filter: Optional[Dict[str, str]] = None) -> str:
        """
        Augment query with temporal context if temporal filter is detected.
        
        Args:
            query: Original user query
            temporal_filter: Extracted temporal filter dict
            
        Returns:
            Augmented query string
        """
        if not temporal_filter:
            temporal_filter = self.extract_temporal_filter(query)
        
        if temporal_filter:
            # Add temporal context to the query for better retrieval
            date_context = []
            if 'created_after' in temporal_filter:
                date_after = datetime.fromisoformat(temporal_filter['created_after'])
                date_context.append(f"from {date_after.strftime('%B %Y')}")
            if 'created_before' in temporal_filter:
                date_before = datetime.fromisoformat(temporal_filter['created_before'])
                date_context.append(f"until {date_before.strftime('%B %Y')}")
            
            if date_context:
                return f"{query} ({' '.join(date_context)})"
        
        return query
