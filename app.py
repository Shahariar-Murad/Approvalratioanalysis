"""Approval Ratio Analysis dashboard.

Run locally with `streamlit run app.py`. The deployed app accepts transaction
reports through Streamlit's uploader and never requires a production CSV in Git.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import available_timezones

import pandas as pd
import plotly.express as px
import streamlit as st

from analytics import (
    DEFAULT_HARD_RULES,
    DataValidationError,
    apply_filters,
    collapse_checkout_sessions,
    daily_performance,
    decline_catalog,
    performance_by_country,
    performance_by_mid,
    performance_by_psp,
    prepare_transactions,
    read_override_csv,
    read_transaction_csv,
    summarize_metrics,
    with_local_time,
)


st.set_page_config(
    page_title="Approval Ratio Analysis dashboard",
    page_icon="✓",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(show_spinner=False)
def prepare_uploaded_report(
    transaction_bytes: bytes, override_bytes: bytes | None
) -> tuple[pd.DataFrame, object]:
    raw = read_transaction_csv(transaction_bytes)
    overrides = read_override_csv(override_bytes) if override_bytes else None
    attempts, diagnostics = prepare_transactions(raw, overrides)
    return collapse_checkout_sessions(attempts), diagnostics


def format_ratio(value: object) -> str:
    return "—" if pd.isna(value) else f"{float(value):.2%}"


def ratio_styler(frame: pd.DataFrame) -> pd.io.formats.style.Styler:
    percentage_columns = [
        column
        for column in (
            "Approval ratio (with hard)",
            "Approval ratio (without hard)",
            "Best PSP rate",
            "Worst PSP rate",
            "Share of all declines",
        )
        if column in frame.columns
    ]
    count_columns = [
        column
        for column in (
            "Checkout sessions",
            "Approved",
            "Declined",
            "Hard declines",
            "Soft declines",
            "Eligible checkout sessions",
            "Top reason count",
            "PSPs compared",
            "Declines",
            "PSPs affected",
            "Countries affected",
        )
        if column in frame.columns
    ]
    formats = {column: "{:.2%}" for column in percentage_columns}
    formats.update({column: "{:,.0f}" for column in count_columns})
    return frame.style.format(formats, na_rep="—")


def show_metric_cards(summary: pd.DataFrame) -> None:
    if summary.empty:
        st.warning("No transactions match the selected filters.")
        return
    metrics = summary.iloc[0]
    cards = st.columns(6)
    cards[0].metric("Checkout sessions", f"{metrics['Checkout sessions']:,.0f}")
    cards[1].metric("Approved", f"{metrics['Approved']:,.0f}")
    cards[2].metric("Approval ratio (with hard)", format_ratio(metrics["Approval ratio (with hard)"]))
    cards[3].metric(
        "Approval ratio (without hard)", format_ratio(metrics["Approval ratio (without hard)"])
    )
    cards[4].metric("Hard declines", f"{metrics['Hard declines']:,.0f}")
    cards[5].metric("Soft declines", f"{metrics['Soft declines']:,.0f}")


def approval_ratio_chart(frame: pd.DataFrame, category: str, title: str) -> None:
    if frame.empty:
        return
    figure = px.bar(
        frame,
        x=category,
        y=["Approval ratio (with hard)", "Approval ratio (without hard)"],
        barmode="group",
        labels={
            "value": "Approval ratio",
            "variable": "Measure",
            category: category.replace("_", " ").title(),
        },
        title=title,
    )
    figure.update_yaxes(tickformat=".0%", range=[0, 1])
    figure.update_layout(legend_title_text="", height=410, margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(figure, use_container_width=True, config={"displaylogo": False})


def download_button(frame: pd.DataFrame, filename: str, label: str) -> None:
    st.download_button(
        label,
        data=frame.to_csv(index=False).encode("utf-8"),
        file_name=filename,
        mime="text/csv",
    )


def main() -> None:
    st.title("Approval Ratio Analysis dashboard")
    st.caption(
        "Approval ratio (with hard) = approved checkout sessions ÷ all checkout sessions. "
        "Approval ratio (without hard) = approved checkout sessions ÷ "
        "(approved checkout sessions + soft-declined checkout sessions)."
    )

    st.sidebar.header("Data source")
    transaction_upload = st.sidebar.file_uploader(
        "Transaction report CSV",
        type=["csv"],
        help="Upload a report with processing date, merchant order ID, status, PSP, country, MID, and decline fields.",
    )
    override_upload = st.sidebar.file_uploader(
        "Optional hard/soft override CSV",
        type=["csv"],
        help="Overrides default classifications for this browser session. Use the example file in config/ as a template.",
    )

    if transaction_upload is None:
        st.info("Upload a transaction report CSV to begin the analysis.")
        with st.expander("Expected source fields"):
            st.markdown(
                "Required: `status`, `merchantOrderId`, and either `processing_date` (UTC timestamp) or "
                "`processingDate` (Unix seconds). The dashboard also uses `pspName`, `country`, `midAlias`, "
                "`declineReason`, `statusReason`, and `declineCode` when available."
            )
        st.stop()

    try:
        data, diagnostics = prepare_uploaded_report(
            transaction_upload.getvalue(), override_upload.getvalue() if override_upload else None
        )
    except (DataValidationError, pd.errors.ParserError) as error:
        st.error(f"This report cannot be analyzed: {error}")
        st.stop()
    except Exception as error:  # Keeps parser failures readable to the operator.
        st.error(f"Unable to read the uploaded report: {error}")
        st.stop()

    if data.empty:
        st.warning("The file contains no usable approved or declined transactions with a processing timestamp.")
        st.stop()

    st.sidebar.header("Reporting timezone")
    preferred_timezones = ["UTC", "Asia/Dhaka", "Europe/London", "America/New_York", "America/Los_Angeles"]
    timezone_options = preferred_timezones + sorted(
        set(available_timezones()).difference(preferred_timezones)
    )
    timezone_name = st.sidebar.selectbox("Timezone", timezone_options, index=0)
    localized = with_local_time(data, timezone_name)

    min_date = localized["local_date"].min()
    max_date = localized["local_date"].max()
    date_selection = st.sidebar.date_input(
        f"Processing date ({timezone_name})",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
    )
    if isinstance(date_selection, tuple) and len(date_selection) == 2:
        start_date, end_date = date_selection
    elif isinstance(date_selection, list) and len(date_selection) == 2:
        start_date, end_date = date_selection
    else:
        start_date = end_date = date_selection

    in_date_range = apply_filters(localized, start_date, end_date)
    st.sidebar.header("Filters")
    psp_options = sorted(in_date_range["psp_name"].dropna().unique().tolist())
    chosen_psps = st.sidebar.multiselect("PSP", psp_options, placeholder="All PSPs")
    country_labels = (
        in_date_range[["country_code", "country_label"]]
        .drop_duplicates()
        .sort_values("country_label")
    )
    label_to_code = dict(zip(country_labels["country_label"], country_labels["country_code"]))
    chosen_country_labels = st.sidebar.multiselect(
        "Country", country_labels["country_label"].tolist(), placeholder="All countries"
    )
    chosen_countries = [label_to_code[label] for label in chosen_country_labels]
    mid_options = sorted(in_date_range["mid_alias"].dropna().unique().tolist())
    chosen_mids = st.sidebar.multiselect("MID", mid_options, placeholder="All MIDs")

    filtered = apply_filters(
        localized,
        start_date,
        end_date,
        psps=chosen_psps,
        countries=chosen_countries,
        mids=chosen_mids,
    )

    st.sidebar.header("Country comparison")
    ranking_choice = st.sidebar.radio(
        "Rank PSPs by",
        options=["Approval ratio (with hard)", "Approval ratio (without hard)"],
        index=0,
    )
    minimum_attempts = st.sidebar.number_input(
        "Minimum PSP checkout sessions per country",
        min_value=1,
        max_value=100_000,
        value=20,
        step=1,
        help="PSPs below this session volume are excluded from the country best/worst comparison.",
    )

    show_metric_cards(summarize_metrics(filtered))
    st.caption(
        f"{len(filtered):,} checkout sessions after filters. Date boundaries use {timezone_name}; "
        "empty PSP, country, and MID selectors mean all values."
    )

    overview_tab, psp_tab, country_tab, mid_tab, decline_tab = st.tabs(
        ["Overview", "PSP performance", "Country performance", "MID performance", "Decline lists"]
    )

    with overview_tab:
        st.subheader("Approval ratio over time")
        daily = daily_performance(filtered)
        if daily.empty:
            st.info("No transactions match the selected filters.")
        else:
            timeline = px.line(
                daily,
                x="local_date",
                y=["Approval ratio (with hard)", "Approval ratio (without hard)"],
                markers=True,
                labels={"value": "Approval ratio", "variable": "Measure", "local_date": "Processing date"},
            )
            timeline.update_yaxes(tickformat=".0%", range=[0, 1])
            timeline.update_layout(legend_title_text="", height=400, margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(timeline, use_container_width=True, config={"displaylogo": False})

        with st.expander("Data quality and calculation definitions"):
            detail = pd.DataFrame(
                [
                    ("Source rows", diagnostics.source_rows),
                    ("Usable approved/declined source attempts", diagnostics.usable_rows),
                    ("Checkout sessions after PSP-retry cascade", len(data)),
                    ("Excluded unknown statuses", diagnostics.excluded_unknown_status),
                    ("Excluded missing timestamps", diagnostics.excluded_missing_timestamp),
                    ("Timestamp column(s) used", diagnostics.timestamp_source),
                ],
                columns=["Check", "Value"],
            )
            st.dataframe(detail, hide_index=True, use_container_width=True)
            if diagnostics.ignored_statuses:
                st.caption("Ignored statuses: " + ", ".join(diagnostics.ignored_statuses))
            for warning in diagnostics.override_warnings:
                st.warning(warning)
            st.markdown(
                "Each `merchantOrderId` is counted once. A checkout session becomes approved if any "
                "attempt succeeds; otherwise its latest declined attempt is the terminal outcome. The app "
                "applies the timezone conversion before filtering on the terminal outcome date. Hard/soft "
                "classification is rule-based and can be overridden with the optional mapping upload."
            )

    with psp_tab:
        st.subheader("PSP-wise approval ratio")
        psp = performance_by_psp(filtered)
        approval_ratio_chart(psp, "psp_name", "Approval ratio by PSP")
        psp_display = psp.rename(columns={"psp_name": "PSP"})
        st.dataframe(ratio_styler(psp_display), hide_index=True, use_container_width=True, height=420)
        download_button(psp_display, "psp_approval_ratio.csv", "Download PSP analysis")

    with country_tab:
        st.subheader("Country-wise approval ratio")
        country = performance_by_country(filtered, int(minimum_attempts), ranking_choice)
        chart_data = country.head(20).sort_values("Checkout sessions", ascending=True)
        approval_ratio_chart(chart_data, "country_label", "Approval ratio by country (top 20 by checkout sessions)")
        country_display = country.rename(
            columns={"country_label": "Country", "country_name": "Country name", "country_code": "Country code"}
        ).drop(columns=["Country name", "Country code"], errors="ignore")
        st.caption(
            f"Best and worst PSPs use {ranking_choice.lower()} and require at least {int(minimum_attempts):,} checkout sessions."
        )
        st.dataframe(ratio_styler(country_display), hide_index=True, use_container_width=True, height=520)
        download_button(country_display, "country_approval_ratio.csv", "Download country analysis")

    with mid_tab:
        st.subheader("MID-wise approval ratio")
        mid = performance_by_mid(filtered)
        approval_ratio_chart(mid, "mid_alias", "Approval ratio by MID")
        mid_display = mid.rename(columns={"mid_alias": "MID"})
        st.dataframe(ratio_styler(mid_display), hide_index=True, use_container_width=True, height=480)
        download_button(mid_display, "mid_approval_ratio.csv", "Download MID analysis")

    with decline_tab:
        st.subheader("Hard and soft decline lists")
        st.caption(
            "Lists are aggregated only by decline code, reason, rule source, PSP count, and country count. No customer data is displayed."
        )
        hard_tab, soft_tab, rules_tab = st.tabs(["Hard declines", "Soft declines", "Default hard rules"])
        with hard_tab:
            hard = decline_catalog(filtered, "Hard")
            st.dataframe(ratio_styler(hard), hide_index=True, use_container_width=True, height=520)
            download_button(hard, "hard_decline_list.csv", "Download hard decline list")
        with soft_tab:
            soft = decline_catalog(filtered, "Soft")
            st.dataframe(ratio_styler(soft), hide_index=True, use_container_width=True, height=520)
            download_button(soft, "soft_decline_list.csv", "Download soft decline list")
        with rules_tab:
            st.dataframe(DEFAULT_HARD_RULES, hide_index=True, use_container_width=True, height=420)
            st.info(
                "Default classification is a starting point. Upload a PSP-approved override CSV when your "
                "operational hard/soft definitions differ."
            )


if __name__ == "__main__":
    main()
