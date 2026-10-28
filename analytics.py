"""Data preparation and approval-ratio calculations for the Streamlit app.

The source file is deliberately not stored in this repository.  This module only
works with a CSV supplied at runtime or by a local developer during testing.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

try:  # pycountry is installed from requirements.txt in the deployed app.
    import pycountry
except ImportError:  # Keeps the calculation module testable without optional UI deps.
    pycountry = None


# These are conservative, standard card-decline defaults. A PSP-specific
# override file always takes precedence. Values not matching a hard rule are
# treated as soft, so the app can show a binary rate as requested.
HARD_DECLINE_CODES = frozenset(
    {
        "04",  # Pick up card
        "07",  # Pick up card / special condition
        "14",  # Invalid card number
        "15",  # No such issuer
        "41",  # Lost card
        "43",  # Stolen card
        "54",  # Expired card
        "57",  # Transaction not permitted to cardholder
        "58",  # Transaction not permitted to terminal
        "59",  # Suspected fraud
        "62",  # Restricted card
        "63",  # Security violation
        "78",  # No account / account closed
        "83",  # Fraud
        "93",  # Violation / cannot complete
        "60110",  # Common processor code for banned card
    }
)

HARD_REASON_RULES: tuple[tuple[str, str], ...] = (
    (r"\b(?:suspected\s+)?fraud\b", "Fraud or suspected fraud"),
    (r"\b(?:lost|stolen)\s+card\b", "Lost or stolen card"),
    (r"\b(?:invalid\s+(?:card|account|payment)|card\s+number\s+invalid)\b", "Invalid card or account"),
    (r"\b(?:expired\s+card|card\s+expired)\b", "Expired card"),
    (r"\b(?:banned|blacklist(?:ed)?|blocked)\s+card\b", "Banned or blocked card"),
    (r"\b(?:restricted\s+card|security\s+violation|pick\s*up\s+card)\b", "Restricted card or security violation"),
    (r"\b(?:transaction\s+not\s+permitted|not\s+permitted\s+for\s+this\s+card)\b", "Transaction not permitted"),
    (r"\b(?:no\s+such\s+(?:account|issuer)|account\s+(?:closed|does\s+not\s+exist))\b", "Invalid or closed account"),
)

DEFAULT_HARD_RULES = pd.DataFrame(
    [
        {"Trigger type": "Decline code", "Trigger": code, "Classification": "Hard"}
        for code in sorted(HARD_DECLINE_CODES, key=lambda value: (len(value), value))
    ]
    + [
        {"Trigger type": "Reason pattern", "Trigger": description, "Classification": "Hard"}
        for _, description in HARD_REASON_RULES
    ]
)

# pycountry produces formally correct names that are sometimes less useful in
# an operational dashboard. Use common full country names for frequently seen
# exceptions, then use pycountry for all remaining ISO alpha-2 codes.
COUNTRY_NAME_OVERRIDES: Mapping[str, str] = {
    "BO": "Bolivia",
    "BN": "Brunei",
    "CD": "Democratic Republic of the Congo",
    "CG": "Republic of the Congo",
    "CV": "Cabo Verde",
    "CZ": "Czechia",
    "IR": "Iran",
    "KR": "South Korea",
    "LA": "Laos",
    "MD": "Moldova",
    "PS": "Palestine",
    "RU": "Russia",
    "SY": "Syria",
    "TW": "Taiwan",
    "TZ": "Tanzania",
    "US": "United States",
    "VE": "Venezuela",
    "VN": "Vietnam",
    "XK": "Kosovo",
}


class DataValidationError(ValueError):
    """Raised when a report cannot support the requested calculations."""


@dataclass(frozen=True)
class PreparationDiagnostics:
    source_rows: int
    usable_rows: int
    excluded_unknown_status: int
    excluded_missing_timestamp: int
    timestamp_source: str
    ignored_statuses: tuple[str, ...]
    override_warnings: tuple[str, ...]


def read_transaction_csv(file_bytes: bytes) -> pd.DataFrame:
    """Read a transaction export without persisting its contents to disk."""

    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(file_bytes), encoding=encoding, low_memory=False)
        except UnicodeDecodeError as error:
            last_error = error
    raise DataValidationError("The CSV could not be decoded as UTF-8 or Latin-1.") from last_error


def read_override_csv(file_bytes: bytes) -> pd.DataFrame:
    """Read a small user-supplied hard/soft mapping without coercing codes."""

    try:
        return pd.read_csv(
            io.BytesIO(file_bytes),
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except UnicodeDecodeError:
        return pd.read_csv(
            io.BytesIO(file_bytes),
            dtype=str,
            keep_default_na=False,
            encoding="latin-1",
        )


def _find_column(frame: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    lookup = {str(column).casefold(): str(column) for column in frame.columns}
    for candidate in candidates:
        resolved = lookup.get(candidate.casefold())
        if resolved is not None:
            return resolved
    return None


def _clean_text(series: pd.Series, fallback: str = "") -> pd.Series:
    cleaned = series.astype("string").fillna("").str.strip()
    return cleaned.mask(cleaned.eq(""), fallback)


def _first_nonempty(frame: pd.DataFrame, candidates: Sequence[str]) -> pd.Series:
    result = pd.Series("", index=frame.index, dtype="string")
    for candidate in candidates:
        column = _find_column(frame, [candidate])
        if column is not None:
            values = _clean_text(frame[column])
            result = result.mask(result.eq(""), values)
    return result


def country_name(code: str) -> str:
    """Return a user-facing full country name for an ISO alpha-2 code."""

    normalized = str(code or "").strip().upper()
    if not normalized or normalized in {"<NA>", "NAN"}:
        return "Country not provided"
    if normalized in COUNTRY_NAME_OVERRIDES:
        return COUNTRY_NAME_OVERRIDES[normalized]
    if pycountry is not None:
        match = pycountry.countries.get(alpha_2=normalized)
        if match is not None:
            return str(match.name)
    return f"Unknown country ({normalized})"


def _prepare_overrides(overrides: pd.DataFrame | None) -> tuple[pd.DataFrame, list[str]]:
    empty = pd.DataFrame(columns=["decline_code", "reason_contains", "classification", "note"])
    if overrides is None or overrides.empty:
        return empty, []

    aliases = {
        "decline_code": ("decline_code", "code", "declinecode"),
        "reason_contains": ("reason_contains", "reason", "declinereason"),
        "classification": ("classification", "decline_class", "class"),
        "note": ("note", "comment", "description"),
    }
    resolved = {key: _find_column(overrides, values) for key, values in aliases.items()}
    missing = [key for key in ("decline_code", "reason_contains", "classification") if resolved[key] is None]
    if missing:
        return empty, ["Override CSV ignored: missing column(s): " + ", ".join(missing)]

    prepared = pd.DataFrame(
        {
            key: _clean_text(overrides[column]) if column is not None else ""
            for key, column in resolved.items()
        }
    )
    prepared["classification"] = prepared["classification"].str.casefold().str.capitalize()
    valid = prepared["classification"].isin(["Hard", "Soft"])
    has_matcher = prepared["decline_code"].ne("") | prepared["reason_contains"].ne("")
    rejected = int((~valid | ~has_matcher).sum())
    prepared = prepared.loc[valid & has_matcher].copy()
    warnings: list[str] = []
    if rejected:
        warnings.append(f"Ignored {rejected} invalid override row(s).")
    return prepared, warnings


def _classify_declines(
    frame: pd.DataFrame, overrides: pd.DataFrame | None
) -> tuple[pd.DataFrame, list[str]]:
    """Assign each decline to Hard or Soft with explainable rule provenance."""

    result = frame.copy()
    result["decline_class"] = "Not declined"
    result["decline_rule"] = "Not declined"

    declined = result["is_declined"]
    result.loc[declined, "decline_class"] = "Soft"
    result.loc[declined, "decline_rule"] = "Default: unmatched declines are treated as soft"

    prepared_overrides, warnings = _prepare_overrides(overrides)
    claimed = pd.Series(False, index=result.index)
    for _, rule in prepared_overrides.iterrows():
        code = str(rule["decline_code"]).strip().casefold()
        phrase = str(rule["reason_contains"]).strip().casefold()
        code_match = (
            result["decline_code"].str.casefold().eq(code)
            if code
            else pd.Series(True, index=result.index)
        )
        reason_match = (
            result["decline_reason"].str.casefold().str.contains(phrase, regex=False, na=False)
            if phrase
            else pd.Series(True, index=result.index)
        )
        matched = declined & ~claimed & code_match & reason_match
        if matched.any():
            result.loc[matched, "decline_class"] = rule["classification"]
            result.loc[matched, "decline_rule"] = "Override"
            claimed = claimed | matched

    code_pattern = r"(?<!\d)(?:" + "|".join(
        re.escape(code) for code in sorted(HARD_DECLINE_CODES, key=len, reverse=True)
    ) + r")(?!\d)"
    hard_by_code = declined & ~claimed & result["decline_code"].str.contains(
        code_pattern, regex=True, na=False
    )
    result.loc[hard_by_code, "decline_class"] = "Hard"
    result.loc[hard_by_code, "decline_rule"] = "Default hard decline code"

    claimed = claimed | hard_by_code
    hard_by_reason = pd.Series(False, index=result.index)
    for pattern, _ in HARD_REASON_RULES:
        hard_by_reason = hard_by_reason | result["decline_reason"].str.contains(
            pattern, case=False, regex=True, na=False
        )
    hard_by_reason = declined & ~claimed & hard_by_reason
    result.loc[hard_by_reason, "decline_class"] = "Hard"
    result.loc[hard_by_reason, "decline_rule"] = "Default hard decline reason"

    result["is_hard_decline"] = result["decline_class"].eq("Hard")
    result["is_soft_decline"] = result["decline_class"].eq("Soft")
    return result, warnings


def prepare_transactions(
    raw: pd.DataFrame, overrides: pd.DataFrame | None = None
) -> tuple[pd.DataFrame, PreparationDiagnostics]:
    """Normalize a transaction report into the calculation schema.

    Each valid input row represents one payment attempt. The source contains both
    `processing_date` (ISO UTC timestamp) and `processingDate` (epoch seconds),
    but the function supports either field to make future report uploads safer.
    """

    if raw.empty:
        raise DataValidationError("The transaction CSV does not contain any rows.")

    status_column = _find_column(raw, ["status"])
    if status_column is None:
        raise DataValidationError("Missing required column: status.")

    iso_timestamp_column = _find_column(
        raw, ["processing_date", "processing_datetime", "processed_at"]
    )
    epoch_timestamp_column = _find_column(raw, ["processingDate", "processing_date_epoch"])
    if iso_timestamp_column is None and epoch_timestamp_column is None:
        raise DataValidationError("Missing required processing timestamp column.")
    merchant_order_column = _find_column(raw, ["merchantOrderId", "merchant_order_id"])
    if merchant_order_column is None:
        raise DataValidationError(
            "Missing required column: merchantOrderId. It is required to collapse PSP retries into checkout sessions."
        )
    merchant_order_id = _clean_text(raw[merchant_order_column])
    missing_merchant_orders = merchant_order_id.eq("")
    if missing_merchant_orders.any():
        raise DataValidationError(
            f"{int(missing_merchant_orders.sum()):,} row(s) have no merchantOrderId. "
            "A cascaded approval ratio cannot be calculated safely."
        )

    status = _clean_text(raw[status_column]).str.casefold()
    is_approved = status.eq("approved")
    is_declined = status.eq("declined")

    timestamp = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns, UTC]")
    source_parts: list[str] = []
    if iso_timestamp_column is not None:
        iso_values = pd.to_datetime(raw[iso_timestamp_column], utc=True, errors="coerce")
        timestamp = timestamp.fillna(iso_values)
        source_parts.append(iso_timestamp_column)
    if epoch_timestamp_column is not None:
        epoch_values = pd.to_datetime(
            pd.to_numeric(raw[epoch_timestamp_column], errors="coerce"),
            unit="s",
            utc=True,
            errors="coerce",
        )
        timestamp = timestamp.fillna(epoch_values)
        source_parts.append(epoch_timestamp_column)

    usable_status = is_approved | is_declined
    missing_timestamp = timestamp.isna()
    usable = usable_status & ~missing_timestamp
    ignored_statuses = tuple(sorted(status.loc[~usable_status].dropna().unique().tolist()))

    data = pd.DataFrame(index=raw.index)
    data["processing_time_utc"] = timestamp
    data["status"] = status
    data["is_approved"] = is_approved
    data["is_declined"] = is_declined
    data["psp_name"] = _first_nonempty(raw, ["pspName", "psp", "provider"]).replace("", "PSP not provided")
    data["mid_alias"] = _first_nonempty(raw, ["midAlias", "mid", "merchant_id"]).replace("", "MID not provided")
    data["country_code"] = _first_nonempty(raw, ["country", "countryCode"]).str.upper().replace("", "Country not provided")
    generated_row_id = pd.Series(
        [f"source-row-{position}" for position in range(len(raw))], index=raw.index, dtype="string"
    )
    data["checkout_session_id"] = merchant_order_id
    data["source_row_id"] = generated_row_id
    data["decline_code"] = _first_nonempty(raw, ["declineCode", "decline_code"]).replace("", "Code not provided")
    data["decline_reason"] = _first_nonempty(
        raw, ["declineReason", "statusReason", "decline_reason", "status_reason"]
    ).replace("", "Reason not provided")
    data["country_name"] = data["country_code"].map(country_name)
    data["country_label"] = data["country_name"] + " (" + data["country_code"] + ")"

    data = data.loc[usable].copy()
    data, override_warnings = _classify_declines(data, overrides)
    diagnostics = PreparationDiagnostics(
        source_rows=int(len(raw)),
        usable_rows=int(len(data)),
        excluded_unknown_status=int((~usable_status).sum()),
        excluded_missing_timestamp=int((usable_status & missing_timestamp).sum()),
        timestamp_source=" then ".join(source_parts),
        ignored_statuses=ignored_statuses,
        override_warnings=tuple(override_warnings),
    )
    return data, diagnostics


def collapse_checkout_sessions(data: pd.DataFrame) -> pd.DataFrame:
    """Collapse PSP retries into one checkout outcome per merchant order.

    A checkout session is approved if any attempt sharing ``merchantOrderId`` is
    approved. Otherwise, the latest declined attempt is its terminal outcome.
    This implements the cascaded approval-ratio definition:

    approved checkout sessions / (approved checkout sessions + unique failed
    checkout sessions).
    """

    if data.empty:
        result = data.copy()
        result["Attempts in session"] = pd.Series(dtype="int")
        result["PSPs tried"] = pd.Series(dtype="int")
        return result

    ordered = data.sort_values(
        ["checkout_session_id", "processing_time_utc", "source_row_id"], kind="stable"
    ).copy()
    any_success = ordered.groupby("checkout_session_id", dropna=False, observed=True)[
        "is_approved"
    ].transform("any")
    # For a successful session, report the latest successful attempt. For a
    # session with no approval, report the terminal decline.
    outcome_candidates = ordered.loc[(any_success & ordered["is_approved"]) | ~any_success].copy()
    outcomes = (
        outcome_candidates.groupby("checkout_session_id", dropna=False, observed=True, sort=False)
        .tail(1)
        .copy()
    )
    session_details = (
        ordered.groupby("checkout_session_id", dropna=False, observed=True)
        .agg(
            **{
                "Attempts in session": ("status", "size"),
                "PSPs tried": ("psp_name", "nunique"),
            }
        )
        .reset_index()
    )
    outcomes = outcomes.merge(session_details, on="checkout_session_id", how="left")
    outcomes["is_approved"] = outcomes["is_approved"].astype(bool)
    outcomes["is_declined"] = ~outcomes["is_approved"]
    outcomes["status"] = outcomes["is_approved"].map({True: "approved", False: "declined"})
    successful = outcomes["is_approved"]
    outcomes.loc[successful, "decline_class"] = "Not declined"
    outcomes.loc[successful, "decline_rule"] = "Cascade outcome: approved"
    outcomes["is_hard_decline"] = outcomes["is_declined"] & outcomes["decline_class"].eq("Hard")
    outcomes["is_soft_decline"] = outcomes["is_declined"] & outcomes["decline_class"].eq("Soft")
    outcomes["Attempts in session"] = outcomes["Attempts in session"].astype(int)
    outcomes["PSPs tried"] = outcomes["PSPs tried"].astype(int)
    return outcomes.reset_index(drop=True)


def with_local_time(data: pd.DataFrame, timezone_name: str) -> pd.DataFrame:
    """Convert the UTC processing timestamp before applying local date filters."""

    try:
        ZoneInfo(timezone_name)
    except Exception as error:  # pragma: no cover - Streamlit supplies known values.
        raise DataValidationError(f"Unknown timezone: {timezone_name}") from error
    result = data.copy()
    result["processing_time_local"] = result["processing_time_utc"].dt.tz_convert(timezone_name)
    result["local_date"] = result["processing_time_local"].dt.date
    return result


def apply_filters(
    data: pd.DataFrame,
    start_date: object,
    end_date: object,
    psps: Sequence[str] | None = None,
    countries: Sequence[str] | None = None,
    mids: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Filter already-localized data. Empty selector lists intentionally mean All."""

    mask = data["local_date"].between(start_date, end_date)
    if psps:
        mask &= data["psp_name"].isin(psps)
    if countries:
        mask &= data["country_code"].isin(countries)
    if mids:
        mask &= data["mid_alias"].isin(mids)
    return data.loc[mask].copy()


METRIC_COLUMNS = [
    "Checkout sessions",
    "Approved",
    "Declined",
    "Hard declines",
    "Soft declines",
    "Eligible checkout sessions",
    "Approval ratio (with hard)",
    "Approval ratio (without hard)",
]


def summarize_metrics(data: pd.DataFrame, group_columns: Sequence[str] = ()) -> pd.DataFrame:
    """Calculate both requested approval ratios from the same attempt universe."""

    groups = list(group_columns)
    if data.empty:
        return pd.DataFrame(columns=groups + METRIC_COLUMNS)

    if groups:
        summary = (
            data.groupby(groups, dropna=False, observed=True)
            .agg(
                **{
                    "Checkout sessions": ("status", "size"),
                    "Approved": ("is_approved", "sum"),
                    "Declined": ("is_declined", "sum"),
                    "Hard declines": ("is_hard_decline", "sum"),
                    "Soft declines": ("is_soft_decline", "sum"),
                }
            )
            .reset_index()
        )
    else:
        summary = pd.DataFrame(
            [
                {
                    "Checkout sessions": len(data),
                    "Approved": int(data["is_approved"].sum()),
                    "Declined": int(data["is_declined"].sum()),
                    "Hard declines": int(data["is_hard_decline"].sum()),
                    "Soft declines": int(data["is_soft_decline"].sum()),
                }
            ]
        )

    count_columns = ["Checkout sessions", "Approved", "Declined", "Hard declines", "Soft declines"]
    summary[count_columns] = summary[count_columns].astype(int)
    summary["Eligible checkout sessions"] = summary["Checkout sessions"] - summary["Hard declines"]
    summary["Approval ratio (with hard)"] = summary["Approved"].div(summary["Checkout sessions"])
    summary["Approval ratio (without hard)"] = summary["Approved"].div(
        summary["Eligible checkout sessions"].where(summary["Eligible checkout sessions"].gt(0))
    )
    return summary


def _add_top_decline_reason(
    summary: pd.DataFrame, data: pd.DataFrame, group_columns: Sequence[str]
) -> pd.DataFrame:
    groups = list(group_columns)
    if summary.empty:
        return summary.assign(**{"Top decline reason": pd.Series(dtype="string"), "Top reason count": pd.Series(dtype="int")})
    declined = data.loc[data["is_declined"]].copy()
    if declined.empty:
        summary["Top decline reason"] = "No declines"
        summary["Top reason count"] = 0
        return summary
    reasons = (
        declined.groupby(groups + ["decline_reason"], dropna=False, observed=True)
        .size()
        .reset_index(name="Top reason count")
        .sort_values(groups + ["Top reason count", "decline_reason"], ascending=[True] * len(groups) + [False, True])
        .drop_duplicates(groups, keep="first")
        .rename(columns={"decline_reason": "Top decline reason"})
    )
    result = summary.merge(reasons, on=groups, how="left")
    result["Top decline reason"] = result["Top decline reason"].fillna("No declines")
    result["Top reason count"] = result["Top reason count"].fillna(0).astype(int)
    return result


def performance_by_psp(data: pd.DataFrame) -> pd.DataFrame:
    summary = summarize_metrics(data, ["psp_name"])
    summary = _add_top_decline_reason(summary, data, ["psp_name"])
    return summary.sort_values(["Approval ratio (with hard)", "Checkout sessions", "psp_name"], ascending=[False, False, True])


def performance_by_mid(data: pd.DataFrame) -> pd.DataFrame:
    summary = summarize_metrics(data, ["mid_alias"])
    summary = _add_top_decline_reason(summary, data, ["mid_alias"])
    return summary.sort_values(["Approval ratio (with hard)", "Checkout sessions", "mid_alias"], ascending=[False, False, True])


def performance_by_country(
    data: pd.DataFrame,
    minimum_psp_sessions: int,
    ranking_column: str,
) -> pd.DataFrame:
    """Show country-level ratios and the best/worst qualified PSP in each country."""

    country = summarize_metrics(data, ["country_name", "country_code", "country_label"])
    country = _add_top_decline_reason(country, data, ["country_name", "country_code", "country_label"])
    if country.empty:
        return country

    psp_country = summarize_metrics(data, ["country_label", "psp_name"])
    eligible = psp_country.loc[
        psp_country["Checkout sessions"].ge(max(int(minimum_psp_sessions), 1))
    ].copy()
    if eligible.empty:
        country["PSPs compared"] = 0
        country["Best PSP"] = "No PSP met the minimum"
        country["Best PSP rate"] = float("nan")
        country["Worst PSP"] = "No PSP met the minimum"
        country["Worst PSP rate"] = float("nan")
        return country.sort_values(["Checkout sessions", "country_label"], ascending=[False, True])

    rank_value = eligible[ranking_column].fillna(-1.0)
    eligible = eligible.assign(_rank_value=rank_value)
    psp_counts = eligible.groupby("country_label", observed=True).size().rename("PSPs compared").reset_index()
    best = (
        eligible.sort_values(["country_label", "_rank_value", "Checkout sessions", "psp_name"], ascending=[True, False, False, True])
        .drop_duplicates("country_label", keep="first")
        [["country_label", "psp_name", ranking_column]]
        .rename(columns={"psp_name": "Best PSP", ranking_column: "Best PSP rate"})
    )
    worst = (
        eligible.sort_values(["country_label", "_rank_value", "Checkout sessions", "psp_name"], ascending=[True, True, False, True])
        .drop_duplicates("country_label", keep="first")
        [["country_label", "psp_name", ranking_column]]
        .rename(columns={"psp_name": "Worst PSP", ranking_column: "Worst PSP rate"})
    )
    result = country.merge(psp_counts, on="country_label", how="left").merge(best, on="country_label", how="left").merge(worst, on="country_label", how="left")
    result["PSPs compared"] = result["PSPs compared"].fillna(0).astype(int)
    one_psp = result["PSPs compared"].eq(1)
    result.loc[one_psp, "Worst PSP"] = "One qualified PSP"
    result.loc[one_psp, "Worst PSP rate"] = float("nan")
    result["Best PSP"] = result["Best PSP"].fillna("No PSP met the minimum")
    result["Worst PSP"] = result["Worst PSP"].fillna("No PSP met the minimum")
    return result.sort_values(["Checkout sessions", "country_label"], ascending=[False, True])


def daily_performance(data: pd.DataFrame) -> pd.DataFrame:
    return summarize_metrics(data, ["local_date"]).sort_values("local_date")


def decline_catalog(data: pd.DataFrame, classification: str) -> pd.DataFrame:
    """Return an aggregated, non-PII hard or soft decline list."""

    declined = data.loc[data["is_declined"] & data["decline_class"].eq(classification)].copy()
    columns = [
        "Decline code",
        "Decline reason",
        "Rule source",
        "Declines",
        "PSPs affected",
        "Countries affected",
        "Share of all declines",
    ]
    if declined.empty:
        return pd.DataFrame(columns=columns)
    catalog = (
        declined.groupby(["decline_code", "decline_reason", "decline_rule"], dropna=False, observed=True)
        .agg(
            **{
                "Declines": ("status", "size"),
                "PSPs affected": ("psp_name", "nunique"),
                "Countries affected": ("country_code", "nunique"),
            }
        )
        .reset_index()
        .rename(
            columns={
                "decline_code": "Decline code",
                "decline_reason": "Decline reason",
                "decline_rule": "Rule source",
            }
        )
    )
    total_declines = int(data["is_declined"].sum())
    catalog["Share of all declines"] = catalog["Declines"].div(total_declines) if total_declines else 0.0
    return catalog.sort_values(["Declines", "Decline reason"], ascending=[False, True]).reset_index(drop=True)
