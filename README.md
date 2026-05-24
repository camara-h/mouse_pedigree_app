# Transnetyx Mouse Pedigree Explorer

A Streamlit app for exploring Transnetyx mouse history exports.

## Features

- Upload a Transnetyx Excel export.
- Clean Mouse ID, Father ID, Mother ID, and Cage ID values.
- Filter by date range, strain, sex, status, use, owner, genotype, animal ID, parent ID, or cage ID.
- Build interactive pedigree networks for one or more selected animals.
- Export displayed pedigree edges as CSV and a static PNG.
- View animal timelines by DOB.
- Reorder timeline rows alphabetically, by most recent birth, by oldest birth, or by most animals.
- Color monthly birth counts by owner, strain, sex, status, or use.
- Calculate mice alive anytime during a selected date range.
- Count unique cages with alive animals during a selected date range.
- Summarize cage counts by owner category and export monthly-report-ready tables.
- Run data QC for missing parents, missing cage IDs, duplicated IDs, and parent IDs not found in the file.

## Alive-anytime logic

A mouse is considered alive during a selected range if:

```text
DOB <= range end
AND
(DOD is blank OR DOD >= range start)
```

The cage counter uses the same alive-anytime logic, then counts unique Cage IDs among those alive animals.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

On Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Privacy

Do not commit real animal export files to a public GitHub repository. Add Excel and CSV files to `.gitignore`.

```text
*.xlsx
*.xls
*.csv
```
