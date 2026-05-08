# Transnetyx Mouse Pedigree Explorer

A Streamlit app for exploring mouse colony history exported from Transnetyx.

## What it does

- Uploads a Transnetyx Excel export.
- Cleans animal IDs, including extra spaces in Mouse ID, Father ID, and Mother ID.
- Builds parent-child relationships from Father ID and Mother ID.
- Lets you filter by DOB, strain, sex, use, owner, genotype, and animal ID.
- Shows an interactive pedigree for a selected mouse.
- Shows colony timelines by month.
- Reports data quality issues, including missing parents, duplicate Mouse IDs, and parent IDs that do not appear in the file.
- Exports cleaned animal tables and parent-child edge tables as CSV.

## Expected input

The Excel file should contain columns like:

- Mouse ID
- Father ID
- Mother ID
- DOB
- Sex
- Strain
- Genotype
- Use
- Owner
- Cage ID

The app is designed for Transnetyx exports that include grouping rows such as `Strain: ...`. Those rows are removed automatically.

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

## Suggested GitHub structure

```text
transnetyx-pedigree-explorer/
├── app.py
├── requirements.txt
├── README.md
└── .gitignore
```

Suggested `.gitignore`:

```text
.venv/
__pycache__/
*.pyc
.DS_Store
*.xlsx
*.xls
*.csv
```

Do not commit lab animal Excel exports to a public repository.

## Deploy on Streamlit Community Cloud

1. Create a GitHub repo.
2. Add `app.py`, `requirements.txt`, and `README.md`.
3. Go to Streamlit Community Cloud.
4. Create a new app from your GitHub repo.
5. Select `app.py` as the main file.

The app will ask users to upload the Excel file each time, so the animal data does not need to be stored in GitHub.

## Notes

For very large pedigrees, avoid rendering all animals at once. Use the Animal pedigree tab and filter by Mouse ID, strain, date range, owner, or genotype.
