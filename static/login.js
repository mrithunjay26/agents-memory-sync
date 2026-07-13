let setupNeeded = false;
let submitting = false;

async function init() {
  const res = await fetch("/api/auth/status");
  const data = await res.json();
  setupNeeded = data.setup_needed;
  document.getElementById("auth-subtitle").textContent = setupNeeded
    ? "Create the admin account for this dashboard"
    : "Log in to continue";
  document.getElementById("auth-submit").textContent = setupNeeded
    ? "Create account"
    : "Log in";
}

document.getElementById("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (submitting) return;
  submitting = true;

  const submitBtn = document.getElementById("auth-submit");
  submitBtn.disabled = true;
  const username = document.getElementById("username").value;
  const password = document.getElementById("password").value;
  const errorEl = document.getElementById("auth-error");
  errorEl.textContent = "";

  const endpoint = setupNeeded ? "/api/auth/signup" : "/api/auth/login";
  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Something went wrong.");

    let confirmed = false;
    for (let i = 0; i < 10 && !confirmed; i++) {
      const who = await (await fetch("/api/auth/whoami")).json();
      if (who.username) confirmed = true;
      else await new Promise((r) => setTimeout(r, 150));
    }
    if (!confirmed) {
      throw new Error("Signed in, but the session didn't confirm. Try reloading the page.");
    }
    window.location.href = "/";
  } catch (err) {
    errorEl.textContent = err.message;
    submitting = false;
    submitBtn.disabled = false;
  }
});

init();
