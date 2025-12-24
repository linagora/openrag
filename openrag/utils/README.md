# TemporalQueryNormalizer

`TemporalQueryNormalizer` enables **extracting temporal expressions** from user queries and normalizing them into precise UTC date ranges. It supports **multilingual keywords**, universal numeric formats, and ISO dates.

---

## Features

- Extracts **ISO, numeric, month-year, and year-only dates** globally.  
- Supports **relative time expressions** with explicit multilingual units: days, weeks, months, years.  
- Recognizes **low-ambiguity multilingual keywords** like "today" and "yesterday".  
- Provides **full-day UTC-aligned ranges** for extracted temporal expressions.  
- Allows **query augmentation** with temporal context for search or retrieval systems.  

---

## Supported Languages

- English  
- Spanish  
- French  
- German  
- Italian  
- Portuguese  

> Custom units can be added in `self.time_units` for additional languages.

---

## Usage

```python
from temporal_query_normalizer import TemporalQueryNormalizer
normalizer = TemporalQueryNormalizer()
query = "Show sales reports from 15/03/2025 to today"
temporal_filter = normalizer.extract_temporal_filter(query)
print(temporal_filter)
#Output:
#{
#'created_after': '2025-03-15T00:00:00+00:00',
#'created_before': '2025-12-24T23:59:59.999999+00:00'
#}

augmented_query = normalizer.augment_query(query, temporal_filter)
print(augmented_query)

#Output:
#"Show sales reports from 15/03/2025 to today (from March 2025 until December 2025)"

```


---

## Supported Formats

1. **ISO dates**: `2025-03-15`, `2025-03-15T10:30`  
2. **Numeric dates**: `15/03/2025`, `03-15-2025`, `15.03.2025`  
3. **Month-Year**: `03/2025`, `03-2025`  
4. **Year-Month ISO**: `2025/03`, `2025-03`  
5. **Year only**: `2025`  
6. **Relative units with multilingual support**:
   - `7 days`, `7 días`, `7 jours`, `7 tage`, `7 giorni`, `7 dias`  
   - `2 weeks`, `3 months`, `1 year`  
7. **Keywords**: `today`, `yesterday`, `aujourd'hui`, `hier`, `hoy`, `ieri`, `hoje`, `ontem`  

---

## Adding Custom Units or Keywords

```python
normalizer.time_units['fortnight'] = 14 # adds a new unit
normalizer.keyword_ranges['day before yesterday'] = 2
```