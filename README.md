# Zimed Sampling Competition — live dashboard

Auto-generated. `index.html` is rebuilt from the live JotForm "Zimed PF Sample
Request Form" by `.github/workflows/refresh.yml` on a schedule and published via
GitHub Pages: https://zimedpf.github.io/zimed-sampling-competition/

Public page = aggregate competition standings only (no patient/physician PII).
Full per-submission detail is generated privately, on demand, never committed here.

- `generate.py` — pulls JotForm, builds the page. `--public-only --out .` for CI.
- JotForm API key is stored as the `JOTFORM_API_KEY` Actions secret.
