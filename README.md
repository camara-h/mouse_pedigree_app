# Transnetyx Mouse Pedigree Explorer

A Streamlit app to explore mouse colony history exported from Transnetyx.

## What it does

- Upload a Transnetyx Excel export.
- Clean Mouse ID, Father ID, Mother ID, and Cage ID fields.
- Parse DOB, DOD, and Wean Date when those columns are present.
- Filter by date, strain, sex, status, use, owner, genotype, and ID.
- Search one or multiple Mouse IDs at once.
- Build interactive pedigree networks with ancestors, descendants, and optional siblings.
- Export displayed pedigree edges as CSV.
- Export a static PNG of the displayed pedigree.
- Show colony timeline plots by DOB.
- Reorder timeline rows alphabetically, by most recent birth, by oldest birth, or by largest group.
- Plot monthly births as a single-color bar chart or colored by owner/strain/sex/status/use.
- Calculate Alive at Range using DOB and DOD.
- Generate an Alive at Range report for monthly or custom financial summaries.
- Run basic data QC checks for missing parents, parent IDs not found in file, duplicate IDs, and potential founders.

## Alive at Range definition

An animal is marked as alive within the selected range when:

```text
DOB <= range end
AND
(DOD is blank OR DOD >= range start)
```

This means the animal was alive at any point during the selected date window.

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

## Suggested repository structure

```text
mouse_pedigree_app/
├── app.py
├── requirements.txt
├── README.md
└── .gitignore
```

Suggested `.gitignore`:

```text
*.xlsx
*.xls
*.csv
__pycache__/
.venv/
```

Do not commit real animal exports to a public GitHub repository.
