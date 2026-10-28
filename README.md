# Approval Ratio Analysis dashboard

This Streamlit dashboard calculates approval performance from a transaction-report CSV. It supports date and timezone controls, multi-select filters for PSP, country, and MID, two approval-ratio definitions, PSP decline reasons, country-level PSP comparisons, and hard/soft decline lists.

## Data protection

Do not commit production transaction exports to GitHub. The supplied reports can contain customer and cardholder information. This repository intentionally contains no production data and ignores `data/*.csv`.

The dashboard accepts a CSV at runtime. Use a private GitHub repository and a private Streamlit deployment, then follow your company's approved data-handling policy. The application does not write uploaded reports into this repository; hosting-platform handling and retention remain subject to your selected deployment settings.

## Required source fields

The dashboard needs:

- `status`, with `approved` and `declined` values
- One processing-time field: `processing_date` (UTC timestamp) or `processingDate` (Unix seconds)

It uses these fields when available:

- `pspName` for PSP analysis
- `country` for full country-name analysis
- `midAlias` for MID analysis
- `declineReason`, `statusReason`, and `declineCode` for decline lists and classification

Each usable source row is treated as one payment attempt.

## Approval-ratio definitions

| Measure | Formula |
| --- | --- |
| Approval ratio (with hard) | Approved ÷ all attempts |
| Approval ratio (without hard) | Approved ÷ (Approved + soft declines) |

The second measure removes hard declines from the denominator only. This is shown on the overview and every PSP, country, and MID table.

## Hard/soft decline classification

The dashboard includes conservative default hard-decline codes and reason patterns. Every declined transaction not matching a hard rule is treated as soft so both ratios remain available. This is a starting point, not a substitute for each PSP's approved retry policy.

To change the classification for an analysis session, upload an override CSV using the same columns as [config/decline_overrides.example.csv](config/decline_overrides.example.csv):

```csv
decline_code,reason_contains,classification,note
59,,Hard,Suspected fraud
51,,Soft,Insufficient funds
```

- `classification` must be `Hard` or `Soft`.
- Supply a code, a case-insensitive reason substring, or both.
- When both are supplied, both conditions must match.
- The first matching override row wins and overrides the default rule.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Publish through GitHub and Streamlit Community Cloud

1. Create a **private** GitHub repository named `approval-ratio-dashboard`. From this project folder, push the code (the `.gitignore` blocks CSV reports):

   ```bash
   git init
   git add .
   git commit -m "Create approval ratio dashboard"
   git branch -M main
   git remote add origin https://github.com/<your-org>/approval-ratio-dashboard.git
   git push -u origin main
   ```

2. Sign in to [Streamlit Community Cloud](https://share.streamlit.io/) with the GitHub account that can access the repository.
3. Select **Create app**, choose the repository and `main` branch, then set the main file path to `app.py`.
4. Deploy. Streamlit installs the packages from `requirements.txt` automatically.
5. Open the application, upload a transaction report, select the reporting timezone and date range, then use the PSP, country, and MID filters.

For a recurring production workflow, keep the repository private and replace session uploads with your approved secure data source before broad access is granted.
