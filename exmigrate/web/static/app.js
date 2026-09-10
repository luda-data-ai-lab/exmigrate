window.ExM = {
  esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, ch => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  },
  showError(el, e) {
    el.textContent = e && e.message ? e.message : String(e);
    el.classList.remove("d-none");
  },
};
