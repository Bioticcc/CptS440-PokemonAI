/* global __PSAI_BACKEND_ORIGIN__ */
const backendOrigin =
  (typeof __PSAI_BACKEND_ORIGIN__ === 'string' && __PSAI_BACKEND_ORIGIN__.trim()) ||
  'http://127.0.0.1:8000';

export async function fetchBattleState() {
  const response = await fetch(`${backendOrigin}/state`);
  if (!response.ok) {
    throw new Error(`state_fetch_failed:${response.status}`);
  }
  return response.json();
}

export async function fetchPrompt() {
  const response = await fetch(`${backendOrigin}/ui/prompt`);
  if (!response.ok) {
    throw new Error(`prompt_fetch_failed:${response.status}`);
  }
  return response.json();
}

export async function submitPromptResponse(payload) {
  const response = await fetch(`${backendOrigin}/ui/response`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body?.detail || `http_${response.status}`;
    throw new Error(`prompt_submit_failed:${detail}`);
  }

  return response.json();
}

export async function fetchUiLogs(cursor = 0) {
  const response = await fetch(`${backendOrigin}/ui/logs?since=${encodeURIComponent(cursor)}`);
  if (!response.ok) {
    throw new Error(`logs_fetch_failed:${response.status}`);
  }
  return response.json();
}
