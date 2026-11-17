"""
Build and validate Gemini-safe profiles while preserving useful column semantics.

Strict Privacy Mandate:
- Zero raw data leakage: Never send cell values, sample rows, category label dictionaries,
  clusters, or full CSV data to external AI models.
- Centralized outbound payload validation enforcing strictly allowlisted metadata.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

# Minimum sample size threshold to compute fine-grained numeric statistics (min, max, mean)
# Sub-threshold datasets suppress statistics to prevent micro-data re-identification.
MIN_SAMPLE_FOR_STATS = int(os.getenv("MIN_SAMPLE_FOR_STATS", "5"))

# Sensitive column header patterns & classifications
SENSITIVE_HEADER_PATTERNS = [
    # Explicit PII embedded in header string
    (re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"), "pii_email"),
    (re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"), "pii_phone"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "government_id"),
    # Clinical / Medical / Health Identifiers (supporting snake_case boundaries)
    (re.compile(r"(?:^|_|[\s\W])(?:diagnosis|hiv|cancer|medical_condition|symptoms|prescription|biopsy|clinical_trial|disease_status)(?:$|_|[\s\W])", re.IGNORECASE), "medical"),
    # National & Government Identification
    (re.compile(r"(?:^|_|[\s\W])(?:ssn|national_id|passport_num|tax_id)(?:$|_|[\s\W])", re.IGNORECASE), "government_id"),
    # Financial Account Numbers & Payment Cards
    (re.compile(r"(?:^|_|[\s\W])(?:credit_card|bank_account|routing_number|iban|cvv)(?:$|_|[\s\W])", re.IGNORECASE), "financial"),
    # Direct personal contact identifiers with explicit personal/customer prefixes
    (re.compile(r"(?:^|_|[\s\W])(?:personal_email|customer_email_address|user_email_address)(?:$|_|[\s\W])", re.IGNORECASE), "contact_email"),
    (re.compile(r"(?:^|_|[\s\W])(?:personal_phone|customer_phone|mobile_number)(?:$|_|[\s\W])", re.IGNORECASE), "contact_phone"),
    # Explicit Individual Names combined with identifiers
    (re.compile(r"(?:^|_|[\s\W])(?:patient_[a-zA-Z]+|employee_[a-zA-Z]+|client_[a-zA-Z]+|doctor_[a-zA-Z]+|full_name_of_[a-zA-Z]+)(?:$|_|[\s\W])", re.IGNORECASE), "name"),
]


def classify_and_sanitize_column_header(col_name: str, index: int) -> Tuple[str, bool]:
    """
    Inspect column name for sensitive PII, medical diagnoses, government identifiers, or personal names.
    Returns: (outbound_name, is_sanitized)
    """
    raw = str(col_name).strip()
    for pattern, category in SENSITIVE_HEADER_PATTERNS:
        if pattern.search(raw):
            sanitized_name = f"col_{index}_{category}"
            return sanitized_name, True
    return raw, False


class PrivacyViolationError(ValueError):
    """Raised when an outbound payload destined for external AI violates privacy constraints."""
    pass


PII_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PII_PHONE_PATTERN = re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
PII_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _check_no_pii_strings(obj: Any) -> None:
    """Scan strings recursively to ensure no raw email, phone, or SSN values leak to external AI."""
    if isinstance(obj, str):
        if PII_EMAIL_PATTERN.search(obj):
            raise PrivacyViolationError(f"Potential raw email address detected in outbound AI payload: {obj}")
        if PII_SSN_PATTERN.search(obj):
            raise PrivacyViolationError(f"Potential SSN detected in outbound AI payload: {obj}")
        if PII_PHONE_PATTERN.search(obj):
            raise PrivacyViolationError(f"Potential raw phone number detected in outbound AI payload: {obj}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_no_pii_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            _check_no_pii_strings(item)


_ALLOWED_ROOT_KEYS: Set[str] = {
    "rows",
    "columns",
    "duplicate_rows",
    "column_name_issue_count",
    "column_profiles",
    "privacy_note",
}

_ALLOWED_DETAIL_KEYS: Set[str] = {
    "dtype",
    "missing_count",
    "missing_percentage",
    "unique_count",
    "empty_column",
    "constant_column",
    "semantic_hints",
    "semantic_type",
    "sensitive_field",
    "statistics",
    "invalid_format_count",
    "category_count",
    "most_frequent_category_count",
    "inconsistent_category_count",
}

_FORBIDDEN_KEY_PATTERNS: Set[str] = {
    "sample",
    "sample_values",
    "value_counts",
    "raw_data",
    "rows_sample",
    "cells",
    "records",
    "data",
    "cluster_members",
    "candidates",
}


def validate_outbound_payload(payload: Dict[str, Any]) -> None:
    """
    Centralized validation enforcing that NO raw row values, samples, or unauthorized
    keys can be transmitted to external LLM providers.
    """
    if not isinstance(payload, dict):
        raise PrivacyViolationError("Outbound AI payload must be a JSON dictionary.")

    # 1. Enforce allowed root keys
    for key in payload.keys():
        if key not in _ALLOWED_ROOT_KEYS:
            raise PrivacyViolationError(
                f"Unauthorized root key '{key}' in outbound AI payload. "
                f"Allowed keys are: {sorted(_ALLOWED_ROOT_KEYS)}"
            )

    column_profiles = payload.get("column_profiles", {})
    if not isinstance(column_profiles, dict):
        raise PrivacyViolationError("'column_profiles' must be a dictionary.")

    # 2. Inspect each column profile
    for col_name, col_meta in column_profiles.items():
        if not isinstance(col_meta, dict):
            raise PrivacyViolationError(f"Column profile for '{col_name}' must be a dictionary.")

        for key, val in col_meta.items():
            if key not in _ALLOWED_DETAIL_KEYS:
                raise PrivacyViolationError(
                    f"Unauthorized column metadata key '{key}' for column '{col_name}'. "
                    f"Forbidden from AI payload."
                )

            # Check for forbidden patterns
            lower_key = str(key).lower()
            if any(forbidden in lower_key for forbidden in _FORBIDDEN_KEY_PATTERNS):
                raise PrivacyViolationError(
                    f"Forbidden data leakage key '{key}' detected in column '{col_name}'."
                )

            # If statistics, only numeric/null/bool allowed
            if key == "statistics":
                if not isinstance(val, dict):
                    raise PrivacyViolationError(f"Column '{col_name}' statistics must be a dictionary.")
                for stat_k, stat_v in val.items():
                    if stat_v is not None and not isinstance(stat_v, (int, float, bool)):
                        raise PrivacyViolationError(
                            f"Non-numeric statistic '{stat_k}={stat_v}' found in column '{col_name}'. "
                            f"Only aggregate numbers are permitted."
                        )

            # Ensure no lists of raw text values exist
            if isinstance(val, (list, set, tuple)):
                if key not in ("semantic_hints",):
                    raise PrivacyViolationError(
                        f"Non-scalar list key '{key}' found in column '{col_name}'. "
                        f"Raw value arrays are strictly disallowed in AI payloads."
                    )

    # 3. Deep regex scan to prevent raw PII (emails, SSNs, phone numbers) in payload
    _check_no_pii_strings(payload)


def build_ai_safe_profile(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Return aggregate metadata only; sanitize sensitive column names and suppress small-dataset statistics."""
    source_columns = profile.get("column_profiles", {})
    safe_columns: dict[str, Any] = {}
    token_to_column: dict[str, str] = {}

    row_count = profile.get("rows")
    suppress_small_sample_stats = (row_count is not None and row_count < MIN_SAMPLE_FOR_STATS)

    for idx, (original_name, raw_details) in enumerate(source_columns.items()):
        details = raw_details if isinstance(raw_details, dict) else {}
        safe_details: dict[str, Any] = {}
        for key in _ALLOWED_DETAIL_KEYS:
            if key in details:
                safe_details[key] = details[key]

        # 1. Small Sample Size Statistical Suppression (N < MIN_SAMPLE_FOR_STATS)
        if suppress_small_sample_stats:
            safe_details.pop("statistics", None)
            safe_details.pop("category_count", None)
            safe_details.pop("most_frequent_category_count", None)
            safe_details.pop("inconsistent_category_count", None)
        else:
            stats = safe_details.get("statistics")
            if isinstance(stats, dict):
                safe_details["statistics"] = {
                    str(key): value for key, value in stats.items()
                    if isinstance(value, (int, float, bool)) or value is None
                }

            for key in ("category_count", "most_frequent_category_count", "inconsistent_category_count"):
                if key in details and isinstance(details[key], (int, float, bool)):
                    safe_details[key] = details[key]

        # 2. Sensitive Column Header Sanitization
        outbound_col_name, is_sanitized = classify_and_sanitize_column_header(str(original_name), idx)
        safe_columns[outbound_col_name] = safe_details
        token_to_column[outbound_col_name] = str(original_name)

    privacy_notes = [
        "Locally detected semantic types and aggregate structural metadata are included.",
        "Zero raw cell values, sample rows, category label arrays, or value-frequency dictionaries are included.",
    ]
    if suppress_small_sample_stats:
        privacy_notes.append(
            f"Small dataset (rows={row_count} < {MIN_SAMPLE_FOR_STATS}) detected: "
            "individual numeric statistics and category frequencies are suppressed to prevent cell value re-identification."
        )

    safe_profile = {
        "rows": profile.get("rows"),
        "columns": profile.get("columns", len(source_columns)),
        "duplicate_rows": profile.get("duplicate_rows"),
        # Do not send original/suggested name pairs: they are unnecessary for planning.
        "column_name_issue_count": len(profile.get("column_name_issues", [])),
        "column_profiles": safe_columns,
        "privacy_note": " ".join(privacy_notes),
    }

    # Centralized Outbound Privacy Verification
    validate_outbound_payload(safe_profile)

    return safe_profile, token_to_column


def remap_plan_columns(plan_data: dict[str, Any], token_to_column: dict[str, str]) -> dict[str, Any]:
    """Remap sanitized column tokens back to original column names and repair casing mismatches."""
    actions = plan_data.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError("Cleaning plan actions must be a list")

    valid_columns = list(token_to_column.values())
    exact_columns = set(valid_columns)

    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("Each cleaning action must be an object")

        column = action.get("column")
        if column is None:
            continue

        # 1. Remap sanitized token if present (e.g. col_0_medical -> Patient_John_Doe_Diagnosis)
        if column in token_to_column:
            column = token_to_column[column]
            action["column"] = column

        if not valid_columns or column in exact_columns:
            continue

        # 2. Safely repair casing-only mismatches (e.g. join_date -> Join_Date)
        matches = [name for name in valid_columns if str(name).casefold() == str(column).casefold()]
        if len(matches) == 1:
            action["column"] = matches[0]
        else:
            raise ValueError(f"The AI referenced unknown column '{column}' or an ambiguous casing match. No repair was performed.")

    return plan_data
