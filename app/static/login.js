document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("btn"), err = document.getElementById("err");
  btn.disabled = true; err.hidden = true;
  try {
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify({ password: document.getElementById("pw").value }) });
    if (r.ok) { location.href = "/"; return; }
    const d = await r.json().catch(() => ({}));
    err.textContent = d.detail || "Sign-in failed"; err.hidden = false;
  } catch { err.textContent = "Network error"; err.hidden = false; }
  btn.disabled = false;
});
