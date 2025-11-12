async function fetchJSON(url, options = {}) {
  const opts = {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {})
    }
  };
  const res = await fetch(url, opts);
  if (!res.ok) {
    let err;
    try {
      err = await res.json();
    } catch (e) {
      err = { detail: res.statusText };
    }
    alert(err.detail || 'Error');
    throw new Error(err.detail || 'Error');
  }
  return await res.json();
}

async function postForm(url, data) {
  const fd = new FormData();
  for (const k in data) {
    fd.append(k, data[k]);
  }
  const res = await fetch(url, { method: 'POST', body: fd });
  if (!res.ok) {
    let err;
    try {
      err = await res.json();
    } catch (e) {
      err = { detail: res.statusText };
    }
    alert(err.detail || 'Error');
    throw new Error(err.detail || 'Error');
  }
  return await res.json();
}