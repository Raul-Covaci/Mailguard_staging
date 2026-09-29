# Teste

Rulare (din rădăcina repo-ului, după `venv/bin/pip install -r requirements.txt -r requirements-dev.txt`):
`venv/bin/python -m pytest tests -q -p no:cacheprovider`

`tests/test_ai_cache.py` pornește un Postgres local efemer (`pgserver`) și aplică migrația din fișier;
fără `pgserver` instalat, testele acelea se sar (skip), restul rulează.
