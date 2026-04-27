# Frontend Runtime Notes

## Desktop Runtime (No Dev Server)
Use `run_UI_battle(...)` from `psai/app/main.py` and run:

```bash
cd showdownAIproject
source .venv/bin/activate
python3 -m psai.app.main
```

That flow now launches:
- FastAPI bridge (`/state`, `/ui/prompt`, `/ui/response`, `/ui/logs`)
- frontend build (`npm run build`)
- Electron desktop window (`npm run electron`)

No browser tab and no separate dev server are required for this path.

## Mock Data Testing (Optional)
- `PYTHONPATH=src python -m psai.app.mock_stream`
- `PYTHONPATH=src uvicorn psai.app.ui_server:app --reload`

This is only for fake/offline UI data tests; real runtime uses live battle state from `main.py`.
